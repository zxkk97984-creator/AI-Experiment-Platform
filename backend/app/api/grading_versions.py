"""评分版本编辑契约（批次三 3C，后端部分）。

教师/管理员在这里维护"可信数据判题"的评分版本：

* 结构化用例草稿（手写或 AI 生成）→ 校验 → 草稿；
* pytest 源码 → **AST 预检**（只报告，不执行、不落库）→ 应用（生成草稿）；
* 确认（摘要冻结）→ 启用（同题旧版本自动退役，题目绑定新版本）；
* 退役只禁止新绑定，历史提交与已开始考试不受影响。

安全边界：全部端点要求教师/管理员，且必须在该题所属授课范围内；AI 与转换结果都只是
**草稿**，确认与启用必须由教师显式操作（AI 无法自行激活）。
"""

from __future__ import annotations

import tempfile

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_db, get_redis_client
from app.errors import api_error
from app.models import Assignment, Course, JudgeGradingVersion, JudgeQuestion, QuestionRubric, User
from app.services import grading_conversion
from app.services import grading_version_service as versions
from app.services.offering_service import teacher_offering_ids

router = APIRouter(prefix="/judge", tags=["grading-versions"])

MAX_LIST_LIMIT = 50


# ── 请求体 ──────────────────────────────────────────────────


class GradingVersionDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict
    entry_function: str = Field(min_length=1, max_length=120)
    compare_rules: dict | None = None
    score_cap_rules: list | None = None
    context_snapshot: dict | None = None
    rubric_id: int | None = None
    execution_mode: str = "data_v1"


class GradingVersionConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_spec_digest: str | None = Field(default=None, min_length=64, max_length=64)
    confirm_isolated_case_v2: bool = False


class ConversionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=grading_conversion.MAX_SOURCE_BYTES)
    entry_function: str | None = Field(default=None, max_length=120)


class ConversionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_function: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=grading_conversion.MAX_SOURCE_BYTES)
    group_id: str = Field(default="F1", max_length=40)
    group_name: str = Field(default="功能用例", max_length=120)
    max_score: float = Field(default=60.0, gt=0, le=60)
    # A10：存在不可转换用例时必须显式确认"部分转换"，否则拒绝——
    # 不能把部分结果当作等价迁移直接启用。
    allow_partial: bool = False
    confirm_isolated_case_v2: bool = False
    context_snapshot: dict | None = None
    rubric_id: int | None = None


class LiteralEncodeRequest(BaseModel):
    """教师输入的字面量（Python 字面量语法，如 [1, 2] / 3.5 / "abc" / (1, 2)）。"""

    model_config = ConfigDict(extra="forbid")

    literal: str = Field(min_length=1, max_length=4096)


class ValueDecodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict


class AiGenerateRequest(BaseModel):
    """AI 生成数据用例草稿：只生成草稿，绝不自动启用。"""

    model_config = ConfigDict(extra="forbid")

    entry_function: str | None = Field(default=None, max_length=120)
    group_max_score: float = Field(default=60.0, gt=0, le=60)


class AiDraftRequest(BaseModel):
    """AI 生成的用例草稿：只允许产生 draft，绝不允许直接 active。"""

    model_config = ConfigDict(extra="forbid")

    payload: dict
    entry_function: str = Field(min_length=1, max_length=120)
    compare_rules: dict | None = None
    context_snapshot: dict | None = None
    rubric_id: int | None = None
    model_name: str | None = Field(default=None, max_length=120)


# ── 权限 ────────────────────────────────────────────────────


def _require_editor(db: Session, user: User, question: JudgeQuestion) -> Assignment:
    if user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可以维护评分版本")
    assignment = db.get(Assignment, question.assignment_id)
    if assignment is None:
        # 教学范围过滤器会让范围外教师看不到作业；此处按权限拒绝（403），
        # 而不是伪装成"作业不存在"，便于前端给出正确提示。
        raise api_error(403, "FORBIDDEN", "没有该作业的授课范围")
    if user.role == "teacher" and assignment.offering_id not in teacher_offering_ids(db, user.id):
        raise api_error(403, "FORBIDDEN", "没有该作业的授课范围")
    return assignment


def _require_question(db: Session, question_id: int) -> JudgeQuestion:
    question = db.get(JudgeQuestion, question_id)
    if question is None:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    return question


def require_version(db: Session, version_id: int) -> JudgeGradingVersion:
    version = db.get(JudgeGradingVersion, version_id)
    if version is None:
        raise api_error(404, "GRADING_VERSION_NOT_FOUND", "评分版本不存在")
    return version


def require_assignment_version(db: Session, version_id: int) -> JudgeGradingVersion:
    """作业路由专用：按**真实目标**授权。

    作业路由拿到考试版本 ID 时必须拒绝（404），否则会绕过角色与教学范围检查——
    学生可以用作业端点读取考试版本的 payload（含隐藏期望值），甚至确认/启用/退役。
    """
    version = require_version(db, version_id)
    if version.exam_question_id is not None or version.judge_question_id is None:
        raise api_error(404, "GRADING_VERSION_NOT_FOUND", "评分版本不存在")
    return version


def require_assignment_version_editor(db: Session, user: User,
                                     version: JudgeGradingVersion) -> JudgeQuestion:
    """作业版本编辑权限：先确认目标类型，再校验教学范围。"""
    if version.exam_question_id is not None or version.judge_question_id is None:
        raise api_error(404, "GRADING_VERSION_NOT_FOUND", "评分版本不存在")
    question = _require_question(db, version.judge_question_id)
    _require_editor(db, user, question)
    return question


def _assert_conversion_complete(report: dict, *, allow_partial: bool) -> None:
    """A10：不可转换用例未显式确认时，拒绝把部分转换当作等价迁移。"""
    if report.get("unsupported_count") and not allow_partial:
        summary = grading_conversion.conversion_summary(report)
        raise api_error(
            422, "CONVERSION_INCOMPLETE",
            f"仍有 {report['unsupported_count']} 条用例无法静态转换，未迁移的用例不会生效；"
            "请人工补齐后重试，或显式确认部分转换（该版本会标记 partial_conversion）",
            fields={"convertible_count": report.get("convertible_count", 0),
                    "unsupported_count": report["unsupported_count"],
                    "reasons": summary["reasons"]},
        )


def version_view(db: Session, version: JudgeGradingVersion, *, detail: bool = False) -> dict:
    payload = version.payload or {}
    groups = payload.get("groups") or []
    binary_cases = payload.get("binary_cases") or []
    view = {
        "id": version.id,
        "judge_question_id": version.judge_question_id,
        "exam_question_id": version.exam_question_id,
        "version": version.version,
        "status": version.status,
        "execution_mode": version.execution_mode,
        "grading_mode_snapshot": version.grading_mode_snapshot,
        "entry_function": version.entry_function,
        "source_kind": version.source_kind,
        "source_digest": version.source_digest,
        "spec_digest": version.spec_digest,
        "rubric_id": version.rubric_id,
        "case_count": len(binary_cases) + sum(len(group.get("cases") or []) for group in groups),
        "group_count": len(groups) + (1 if binary_cases else 0),
        "binary_case_count": len(binary_cases),
        "functional_total": payload.get("functional_total"),
        "robustness_total": payload.get("robustness_total"),
        "created_at": version.created_at,
        "confirmed_at": version.confirmed_at,
        "activated_at": version.activated_at,
        "confirmed_by_id": version.confirmed_by_id,
    }
    if detail:
        view["payload"] = version.payload
        view["compare_rules"] = version.compare_rules
        view["score_cap_rules"] = version.score_cap_rules
        view["context_snapshot"] = version.context_snapshot
        view["rubric_binding_ok"] = versions.rubric_binding_ok(db, version)
    return view


# ── 端点 ────────────────────────────────────────────────────


@router.get("/questions/{question_id}/grading-versions")
def list_grading_versions(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    rows = db.scalars(
        select(JudgeGradingVersion)
        .where(JudgeGradingVersion.judge_question_id == question_id)
        .order_by(JudgeGradingVersion.version.desc())
        .limit(MAX_LIST_LIMIT)
    ).all()
    return {
        "items": [version_view(db, row) for row in rows],
        "current_grading_version_id": question.current_grading_version_id,
        "grading_mode": question.grading_mode,
    }


@router.post("/grading-versions/literal")
def encode_literal(
    payload: LiteralEncodeRequest,
    current_user: User = Depends(get_current_user),
):
    """把教师输入的字面量转成值协议（**只解析字面量，不执行任何代码**）。

    编辑器用它把"输入/期望"文本框转成协议值；服务端仍是协议的唯一权威实现，
    避免前端各自复制一份编码规则。
    """
    if current_user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可以使用该工具")
    import ast

    from app.services.grading_values import ValueEncodingError, encode_value

    try:
        value = ast.literal_eval(payload.literal)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise api_error(422, "INVALID_LITERAL",
                        f"必须是合法的 Python 字面量（不能引用变量或调用函数）: {exc}") from exc
    try:
        encoded = encode_value(value)
    except ValueEncodingError as exc:
        raise api_error(422, "VALUE_NOT_SUPPORTED", str(exc)) from exc
    return {"value": encoded}


@router.post("/grading-versions/literal/decode")
def decode_literal(
    payload: ValueDecodeRequest,
    current_user: User = Depends(get_current_user),
):
    """把协议值转回可编辑的字面量文本（编辑器回显用）。"""
    if current_user.role not in ("teacher", "admin"):
        raise api_error(403, "FORBIDDEN", "仅教师和管理员可以使用该工具")
    from app.services.grading_values import ValueEncodingError, decode_value, is_array

    try:
        value = decode_value(payload.value)
    except ValueEncodingError as exc:
        raise api_error(422, "INVALID_VALUE", str(exc)) from exc
    if is_array(value):
        return {"literal": f"<array {value['dtype']} {value['shape']}>", "array": True}
    return {"literal": repr(value), "array": False}


@router.get("/questions/{question_id}/grading-versions/preflight")
def grading_version_preflight(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """启用前影响面核查：在途任务、无基线历史记录、进行中的考试场次。"""
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    return versions.enablement_preflight(db, question_kind="assignment",
                                         question_id=question_id)


@router.post("/questions/{question_id}/grading-versions",
             status_code=status.HTTP_201_CREATED)
def create_grading_version_draft(
    question_id: int,
    payload: GradingVersionDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    try:
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload=payload.payload, entry_function=payload.entry_function,
            compare_rules=payload.compare_rules, score_cap_rules=payload.score_cap_rules,
            context_snapshot=payload.context_snapshot, rubric_id=payload.rubric_id,
            source_kind="manual", execution_mode=payload.execution_mode,
            actor_id=current_user.id,
        )
    except versions.GradingVersionError as exc:
        raise api_error(422, "INVALID_GRADING_VERSION", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.get("/grading-versions/{version_id}")
def get_grading_version(
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = require_assignment_version(db, version_id)
    require_assignment_version_editor(db, current_user, version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/confirm")
def confirm_grading_version(
    version_id: int,
    payload: GradingVersionConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = require_assignment_version(db, version_id)
    require_assignment_version_editor(db, current_user, version)
    try:
        versions.confirm_version(db, version, actor_id=current_user.id,
                                 expected_spec_digest=payload.expected_spec_digest,
                                 confirm_isolated_case_v2=payload.confirm_isolated_case_v2)
    except versions.GradingVersionError as exc:
        raise api_error(409, "GRADING_VERSION_CONFLICT", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/activate")
def activate_grading_version(
    version_id: int,
    acknowledge_in_flight: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = require_assignment_version(db, version_id)
    require_assignment_version_editor(db, current_user, version)
    if not versions.rubric_binding_ok(db, version):
        raise api_error(422, "RUBRIC_REQUIRED", "该评分模式必须绑定有效的 Rubric 后才能启用")
    # 启用前核查在途任务：不允许"判到一半"切换评分协议（教师可显式确认后继续）
    if version.judge_question_id is not None:
        try:
            versions.assert_enablement_allowed(
                db, question_kind="assignment", question_id=version.judge_question_id,
                acknowledge_in_flight=acknowledge_in_flight)
        except versions.GradingVersionError as exc:
            raise api_error(409, "IN_FLIGHT_GRADING", str(exc)) from exc
    try:
        versions.activate_version(db, version, actor_id=current_user.id)
    except versions.GradingVersionError as exc:
        raise api_error(409, "GRADING_VERSION_CONFLICT", str(exc)) from exc
    if version.judge_question_id is not None:
        versions.set_question_current_version(
            db, question_kind="assignment", question_id=version.judge_question_id,
            version_id=version.id)
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/grading-versions/{version_id}/retire")
def retire_grading_version(
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = require_assignment_version(db, version_id)
    require_assignment_version_editor(db, current_user, version)
    try:
        versions.retire_version(db, version)
    except versions.GradingVersionError as exc:
        raise api_error(409, "GRADING_VERSION_CONFLICT", str(exc)) from exc
    if version.judge_question_id is not None:
        versions.set_question_current_version(
            db, question_kind="assignment", question_id=version.judge_question_id, version_id=None)
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)


@router.post("/questions/{question_id}/grading-versions/convert-preview")
def convert_preview(
    question_id: int,
    payload: ConversionPreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AST 预检：只报告可转换/不可转换及原因，**不写库、不执行源码**。"""
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    source = payload.source if payload.source is not None else (question.hidden_tests or "")
    entry = payload.entry_function or question.function_name
    try:
        report = grading_conversion.analyze_pytest_source(source, entry_function=entry)
    except ValueError as exc:
        raise api_error(422, "CONVERSION_PREVIEW_FAILED", str(exc)) from exc
    return {
        "summary": grading_conversion.conversion_summary(report),
        "cases": report["cases"],
        "functions": report["functions"],
        "entry_function": report["entry_function"],
    }


@router.post("/questions/{question_id}/grading-versions/convert-apply",
             status_code=status.HTTP_201_CREATED)
def convert_apply(
    question_id: int,
    payload: ConversionApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """把可转换用例落成**草稿**版本（仍需确认与启用）。"""
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    source = payload.source if payload.source is not None else (question.hidden_tests or "")
    entry = payload.entry_function or question.function_name
    try:
        report = grading_conversion.analyze_pytest_source(source, entry_function=entry)
        report["notes"].append(
            "新版本按 isolated_case_v2 执行：每个用例重新启动容器；依赖跨用例共享状态的旧测试不会保留该行为。"
        )
        _assert_conversion_complete(report, allow_partial=payload.allow_partial)
        converted = grading_conversion.build_conversion_payload(
            report, entry_function=entry, group_id=payload.group_id,
            group_name=payload.group_name, max_score=payload.max_score)
        if not payload.confirm_isolated_case_v2:
            raise versions.GradingVersionError(
                "创建草稿前必须确认逐例独立执行；共享状态旧用例需人工处理或明确作为新规则"
            )
        version_payload = {
            "schema_version": 2, "entry_function": entry,
            "execution_model": "isolated_case_v2",
            "binary_cases": [
                {key: case[key] for key in ("id", "input", "expected", "compare")}
                for group in converted["groups"] for case in group["cases"]
            ],
            "groups": [],
        }
        context = dict(payload.context_snapshot or {"prompt": question.title})
        context["isolated_case_v2_confirmed"] = True
        if report["unsupported_count"]:
            # 部分转换必须留痕：确认与启用时都能看到"仍有 N 条未迁移"
            context.update({"partial_conversion": True,
                            "unsupported_count": report["unsupported_count"],
                            "unsupported_reasons": grading_conversion.conversion_summary(
                                report)["reasons"]})
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload=version_payload, entry_function=entry,
            context_snapshot=context,
            rubric_id=payload.rubric_id, source_kind="converted_from_pytest",
            grading_mode_snapshot="legacy",
            actor_id=current_user.id,
        )
    except (ValueError, versions.GradingVersionError) as exc:
        raise api_error(422, "CONVERSION_APPLY_FAILED", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return {"version": version_view(db, version, detail=True),
            "summary": grading_conversion.conversion_summary(report)}


@router.post("/questions/{question_id}/grading-versions/ai-generate",
             status_code=status.HTTP_201_CREATED)
def ai_generate_data_draft(
    question_id: int,
    payload: AiGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    redis_client=Depends(get_redis_client),
):
    """调用 AI 生成测试组，再经 **AST 预检**转成数据用例草稿（只落草稿）。

    关键约束：AI 输出必须先通过静态转换（可证明等价）才能进入草稿；
    无法转换时返回 422 并附逐条原因，绝不静默落一个跑不了的版本。
    """
    from app.api.ai_grading import _check_generation_rate_limit
    from app.services.ai_client import DeepSeekClient
    from app.services.ai_prompts import build_test_group_snapshot
    from app.services.op_metrics import ai_metrics_sink
    from app.services.test_group_generator import (
        AIServiceError,
        PreflightUnavailableError,
        TestGroupValidationError,
        generate_test_groups,
    )

    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    if not settings.ai_ready:
        raise api_error(503, "AI_NOT_READY", "AI 服务未配置 API Key")
    _check_generation_rate_limit(redis_client, current_user.id,
                                 f"data-gen:assignment:{question_id}")

    entry = payload.entry_function or question.function_name
    if not isinstance(entry, str) or not entry.isidentifier():
        raise api_error(422, "ENTRY_FUNCTION_REQUIRED", "请先填写合法的入口函数名")

    rubric = db.scalar(
        select(QuestionRubric).where(
            QuestionRubric.judge_question_id == question_id,
            QuestionRubric.status == "locked",
        ).order_by(QuestionRubric.version.desc()).limit(1)
    )
    if rubric is None:
        raise api_error(422, "AI_RUBRIC_REQUIRED",
                        "启用 AI 评分前，必须先创建并锁定本题 Rubric")

    snapshot = build_test_group_snapshot(
        title=question.title, description=question.description,
        function_name=question.function_name, signature=question.signature,
        starter_code=question.starter_code, hidden_tests=question.hidden_tests,
        reference_solution=question.reference_solution,
        teacher_constraints=question.teacher_constraints,
    )
    client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
    try:
        with tempfile.TemporaryDirectory(prefix="dai-datagen-") as tmp:
            result = generate_test_groups(client, snapshot, settings, workdir=tmp,
                                          host_workdir=settings.judge_host_work_dir or None)
    except TestGroupValidationError as exc:
        raise api_error(502, "AI_GENERATION_INVALID", "AI 生成测试组不合规，请重新生成或手工编写",
                        fields={"issues": exc.issues, "retryable": True}) from exc
    except PreflightUnavailableError as exc:
        raise api_error(503, "JUDGE_UNAVAILABLE", str(exc)) from exc
    except AIServiceError as exc:
        if exc.code == "timeout":
            raise api_error(504, "AI_GENERATION_TIMEOUT", "AI 生成超时，请稍后重试") from exc
        if exc.code == "http_429":
            raise api_error(429, "AI_RATE_LIMITED", "AI 服务限流，请稍后重试") from exc
        raise api_error(502, "AI_GENERATION_INVALID", f"AI 生成失败: {exc}",
                        fields={"retryable": bool(exc.retryable)}) from exc

    try:
        # 真实响应字段是 test_groups（TestGroupsGenerateResponse）；读 result.groups 会 500
        version_payload, report = versions.build_data_payload_from_generated_groups(
            result.test_groups, entry_function=entry)
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload=version_payload, entry_function=entry,
            score_cap_rules=list(question.score_cap_rules or []),
            context_snapshot={"prompt": question.title, "draft_model": settings.ai_model,
                              "generation_id": getattr(result, "generation_id", None)},
            rubric_id=rubric.id, grading_mode_snapshot="active",
            source_kind="ai_draft", actor_id=current_user.id)
    except (ValueError, versions.GradingVersionError) as exc:
        raise api_error(422, "AI_GENERATION_NOT_CONVERTIBLE", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return {"version": version_view(db, version, detail=True),
            "summary": grading_conversion.conversion_summary(report),
            "warnings": list(getattr(result, "warnings", []) or [])}


@router.post("/questions/{question_id}/grading-versions/ai-draft",
             status_code=status.HTTP_201_CREATED)
def create_ai_draft(
    question_id: int,
    payload: AiDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI 生成的用例只落成草稿；激活必须由教师显式确认后另行调用 activate。"""
    question = _require_question(db, question_id)
    _require_editor(db, current_user, question)
    context = dict(payload.context_snapshot or {})
    context.setdefault("prompt", question.title)
    if payload.model_name:
        context["draft_model"] = payload.model_name
    try:
        version = versions.create_draft(
            db, question_kind="assignment", question_id=question_id,
            payload=payload.payload, entry_function=payload.entry_function,
            compare_rules=payload.compare_rules, context_snapshot=context,
            rubric_id=payload.rubric_id, source_kind="ai_draft",
            actor_id=current_user.id,
        )
    except versions.GradingVersionError as exc:
        raise api_error(422, "INVALID_GRADING_VERSION", str(exc)) from exc
    db.commit()
    db.refresh(version)
    return version_view(db, version, detail=True)
