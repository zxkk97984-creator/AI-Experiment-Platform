"""判题 Worker——消费判题队列 + 考试队列 + AI 评分队列"""
import json as _json
import logging
import math
import os
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import (
    CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission, JudgeQuestion, QuestionRubric, Submission,
)
from app.services.environment_service import (
    installed_imports_for_version,
    resolve_run_image_ref,
)
from app.services.import_policy import ImportPolicy, classify_imports

logger = logging.getLogger("dai.worker")

EXAM_JUDGE_QUEUE = "judge:exam:queue"


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def _make_work_dir(root: Path, prefix: str):
    import shutil as _shutil
    suffix = secrets.token_hex(8)
    workdir = root / f"{prefix}{suffix}"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir, lambda: _shutil.rmtree(workdir, ignore_errors=True)


def _settings_path_or_none(value):
    """把设置项安全转成 Path：仅接受非空 str/Path，其余返回 None。

    防再犯：os.fspath(MagicMock) 在 Python 3.12+ 返回 'MagicMock/mock.xxx/<id>'
    这类伪路径，误传 mock settings 会在仓库根下递归建垃圾目录；
    返回 None 让调用方回退到 TemporaryDirectory。
    """
    if not isinstance(value, (str, Path)):
        return None
    text = os.fspath(value)
    return Path(text) if text else None


def _get_timeout(question: JudgeQuestion, settings: Settings) -> int:
    raw = max(question.time_limit_ms, 1)
    return max(min(math.ceil(raw / 1000), settings.judge_timeout_seconds), 1)


def _status_from_pytest(returncode, stdout, stderr):
    output = f"{stdout}\n{stderr}"
    if returncode == 0:
        return "accepted", 100
    if returncode == 124:
        return "time_limit_exceeded", 0
    if "AssertionError" in output or "assert " in output:
        return "wrong_answer", 0
    if returncode == 1:
        return "runtime_error", 0
    return "system_error", 0


# ═══════════════════════════════════════════════════════════════
# 传统文件写入（兼容旧判题路径）
# ═══════════════════════════════════════════════════════════════

def _write_submission_files(workdir: Path, submission: Submission, question: JudgeQuestion) -> Path:
    user_code = workdir / "user_code.py"
    test_file = workdir / "test_user_code.py"
    user_code.write_text(submission.code, encoding="utf-8")
    hidden_tests = question.hidden_tests
    if "import user_code" not in hidden_tests and "from user_code" not in hidden_tests:
        hidden_tests = f"from user_code import *\n\n{hidden_tests}"
    test_file.write_text(hidden_tests, encoding="utf-8")
    return test_file


def enqueue_exam_answer(submission_id: int, answer_id: int, question: ExamQuestion):
    from app.services.judge_queue import enqueue_job as _enq
    from app.database import SessionLocal
    with SessionLocal() as db:
        _enq(db, job_type="exam", object_id=answer_id)


# ═══════════════════════════════════════════════════════════════
# Docker 沙箱
# ═══════════════════════════════════════════════════════════════

def _run_docker_pytest(workdir: Path, settings: Settings, timeout_seconds: int,
                       memory_limit_mb: int = 256, test_filename: str = "test_user_code.py",
                       host_workdir: Path | None = None,
                       extra_args: list[str] | None = None,
                       image_ref: str | None = None) -> tuple[str, str, int, int]:
    """在隔离 Docker 沙箱中运行 pytest（Phase 5：镜像参数使用环境 digest）。

    - image_ref：环境版本不可变镜像引用（image ID 或 repository@sha256 形式）；
      None 仅用于未绑定环境版本的存量兼容路径（exam 路径显式不传，保持旧配置镜像）。
    - 安全参数全部保留：--network none / --cap-drop ALL / no-new-privileges /
      --read-only / tmpfs / pids / cpu / 内存限制 / 非 root UID 1000——只改镜像，不放松安全。
    """
    host_path = host_workdir if host_workdir is not None else workdir
    container_name = f"dai-judge-{secrets.token_hex(8)}"
    cmd = [
        "docker", "run", "--rm", "--name", container_name,
        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:exec,size=64m",
        "--cpus", str(settings.judge_cpu_limit),
        "--memory", f"{memory_limit_mb}m",
        "--pids-limit", "50", "--user", "1000:1000",
        "-v", f"{host_path}:/work:ro", "-w", "/work",
        image_ref or settings.judge_image,
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(test_filename)

    started = time.perf_counter()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result.stdout, result.stderr, result.returncode, elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        return "", "Execution timed out", 124, elapsed_ms


# ═══════════════════════════════════════════════════════════════
# pytest 结果插件 + 解析
# ═══════════════════════════════════════════════════════════════

PLUGIN_CODE = """import json
COUNTS = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
def pytest_runtest_logreport(report):
    if report.when == "call":
        if report.passed: COUNTS["passed"] += 1
        elif report.failed: COUNTS["failed"] += 1
        elif report.skipped: COUNTS["skipped"] += 1
    elif report.when in ("setup", "teardown") and report.failed:
        COUNTS["errors"] += 1
def pytest_sessionfinish(session, exitstatus):
    print("DAI_RESULT_JSON=" + json.dumps(COUNTS, separators=(",", ":")))
"""


def _parse_result_json(output: str) -> dict | None:
    import re
    match = re.search(r"DAI_RESULT_JSON=(\{.*?\})", output)
    if match:
        try:
            data = _json.loads(match.group(1))
            for k in ("passed", "failed", "errors", "skipped"):
                if not isinstance(data.get(k), int) or data[k] < 0:
                    return None
            return data
        except Exception:
            return None
    return None


# ═══════════════════════════════════════════════════════════════
# 结构化测试组评分
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# Phase 5：运行环境上下文（digest 镜像 + import 诊断）
# ═══════════════════════════════════════════════════════════════

class EnvironmentUnavailableError(Exception):
    """绑定环境版本无可用镜像——fail closed 为 system_error，不扣分。"""

    def __init__(self, message: str = "运行环境暂不可用，本次提交不会扣分，请稍后重试"):
        super().__init__(message)
        self.diagnostic_code = "ENVIRONMENT_IMAGE_MISSING"
        self.message = message


def _submission_env_context(db: Session, submission: Submission) -> tuple[str | None, ImportPolicy | None, set[str]]:
    """从提交快照解析运行环境上下文（计划 8.3）。

    返回 (image_ref, policy, installed_imports)：
    - 未绑定环境版本（存量兼容路径）→ (None, None, None)，判题使用 settings.judge_image；
    - 已绑定但版本无 digest / 不可用 → 抛 EnvironmentUnavailableError（fail closed）。
    历史提交重判：环境取自 Submission 快照字段，不受作业重新发布影响。
    """
    env_id = submission.environment_version_id
    if env_id is None:
        return None, None, None
    try:
        image_ref = resolve_run_image_ref(db, env_id)
    except Exception as exc:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None) or str(exc)
        raise EnvironmentUnavailableError(message) from exc
    policy = ImportPolicy.from_mode(
        submission.import_policy_mode_snapshot or "unrestricted",
        list(submission.allowed_imports_snapshot or []),
    )
    installed = installed_imports_for_version(db, env_id)
    return image_ref, policy, installed


def _diagnose_submission(submission: Submission, policy: ImportPolicy | None,
                         installed_imports: set[str]):
    """判题前的 import 预检——只分析学生源代码（计划 8.3）。

    返回第一个诊断 ImportDiagnosticRead 或 None（unrestricted / 无违规时不诊断）。
    语法错误返回 None——不伪装成 import 错误，交给原判题输出。
    """
    if policy is None or not policy.restricted:
        return None
    diagnostics = classify_imports(submission.code, policy, installed_imports)
    return diagnostics[0] if diagnostics else None


def _handle_import_diagnostic(db, submission, diag, fail_job) -> Submission | None:
    """按诊断类型落终态。返回终态 submission；None 表示继续正常判题。"""
    if diag is None:
        return None
    details = dict(submission.result_details or {})
    details["diagnostic"] = {
        "code": diag.code, "module": diag.module, "message": diag.message,
    }
    if diag.code == "IMPORT_NOT_ALLOWED":
        # 学生错误：不跑 Docker、不计基础设施重试、明确扣分
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"学生代码使用了不允许的导入: {diag.module}", retryable=False)
        submission.status = "runtime_error"
        submission.score = 0
        submission.result_details = details
        submission.stdout = ""
        submission.stderr = diag.message
        db.commit()
        return submission
    # IMPORT_NOT_INSTALLED / ENVIRONMENT_DRIFT：平台配置问题，不扣分，按系统错误策略可重试
    fail_job(db, job_type="assignment", object_id=submission.id,
             error=f"运行环境缺少允许导入的模块: {diag.module}", retryable=True)
    submission.status = "system_error"
    submission.score = None
    submission.result_details = details
    submission.stderr = diag.message
    db.commit()
    return submission


def run_test_groups(
    workdir: Path, host_workdir: Path, code: str,
    test_groups: list[dict], settings: Settings,
    timeout_seconds: int, memory_limit_mb: int,
    image_ref: str | None = None,
) -> dict:
    results = {}
    system_errors = []
    for group in test_groups:
        gid = group["id"]
        tests_code = group.get("tests", "")
        if not tests_code:
            system_errors.append(f"测试组 {gid} 没有测试代码")
            results[gid] = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
            continue
        (workdir / "user_code.py").write_text(code, encoding="utf-8")
        (workdir / "dai_result_plugin.py").write_text(PLUGIN_CODE, encoding="utf-8")
        test_content = tests_code
        if "import user_code" not in test_content and "from user_code" not in test_content:
            test_content = f"from user_code import *\n\n{test_content}"
        (workdir / "test_group.py").write_text(test_content, encoding="utf-8")
        try:
            stdout, stderr, returncode, elapsed = _run_docker_pytest(
                workdir, settings, timeout_seconds, memory_limit_mb,
                test_filename="test_group.py", host_workdir=host_workdir,
                extra_args=["-p", "dai_result_plugin"],
                image_ref=image_ref,
            )
        except Exception as exc:
            system_errors.append(f"测试组 {gid} Docker 执行异常: {exc}")
            results[gid] = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0}
            continue
        counts = _parse_result_json(stdout)
        if counts is None:
            system_errors.append(f"测试组 {gid} 无法解析结果")
            results[gid] = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0}
        else:
            results[gid] = counts
    return {"results": results, "system_errors": system_errors}


def _calculate_fr(group: dict, counts: dict, sys_errs: list[str]) -> tuple[float, dict]:
    from app.services.deterministic_scoring import calculate_group_score, DeterministicSystemError
    try:
        score = calculate_group_score(group.get("max_score", 0), counts)
    except DeterministicSystemError as exc:
        sys_errs.append(f"测试组 {group['id']}: {exc}")
        return 0.0, {"id": group["id"], "name": group.get("name", ""),
                      "dimension": group.get("dimension"), "max_score": group.get("max_score", 0),
                      "score": 0, "counts": counts, "error": str(exc)}
    return score, {"id": group["id"], "name": group.get("name", ""),
                   "dimension": group.get("dimension"), "max_score": group.get("max_score", 0),
                   "score": score, "counts": counts}


def _calc_fr_scores(groups, results, sys_errs):
    f_total, r_total, details = 0.0, 0.0, []
    for g in groups:
        counts = results.get(g["id"])
        if counts is None:
            sys_errs.append(f"测试组 {g['id']} 缺少结果")
            continue
        s, d = _calculate_fr(g, counts, sys_errs)
        details.append(d)
        if g.get("dimension") == "F":
            f_total += s
        elif g.get("dimension") == "R":
            r_total += s
    return round(f_total, 4), round(r_total, 4), details


# ═══════════════════════════════════════════════════════════════
# 作业判题
# ═══════════════════════════════════════════════════════════════

def _legacy_judge_submission(db, redis_client, settings, submission, question, workdir, host_workdir, timeout_s, mem_mb):
    """传统判题路径：隐藏测试全过/不过 → accepted/0"""
    from app.services.judge_queue import complete_job, fail_job
    if not question.hidden_tests or not question.hidden_tests.strip():
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="缺少隐藏测试", retryable=False)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        db.commit()
        return submission
    # Phase 5：解析提交快照环境（digest 镜像 + import 策略），fail closed
    try:
        image_ref, policy, installed = _submission_env_context(db, submission)
    except EnvironmentUnavailableError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=exc.message, retryable=True)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        submission.result_details = {
            "diagnostic": {"code": "ENVIRONMENT_IMAGE_MISSING",
                           "module": "", "message": exc.message},
        }
        db.commit()
        return submission
    # import 预检：只分析学生源代码；IMPORT_NOT_ALLOWED 学生错误，IMPORT_NOT_INSTALLED 平台配置
    terminal = _handle_import_diagnostic(db, submission,
                                         _diagnose_submission(submission, policy, installed),
                                         fail_job)
    if terminal is not None:
        return terminal
    _write_submission_files(workdir, submission, question)
    try:
        stdout, stderr, returncode, elapsed = _run_docker_pytest(
            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
            image_ref=image_ref)
    except Exception as e:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"Docker 判题失败: {e}", retryable=True)
        submission.status = "system_error"
        submission.score = None  # 基础设施异常不扣分
        submission.stderr = f"Docker 判题失败: {e}"
        db.commit()
        return submission

    final_status, score = _status_from_pytest(returncode, stdout, stderr)
    if final_status == "system_error":
        fail_job(
            db,
            job_type="assignment",
            object_id=submission.id,
            error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
            retryable=True,
        )
        submission.status = "system_error"
        submission.score = None
        submission.stdout = stdout[-8000:]
        submission.stderr = stderr[-8000:]
        submission.execution_time_ms = elapsed
        submission.result_details = {"returncode": returncode}
        db.commit()
        return submission
    submission.status = final_status
    submission.stdout = stdout[-8000:]
    submission.stderr = stderr[-8000:]
    submission.score = score
    submission.execution_time_ms = elapsed
    submission.result_details = {"returncode": returncode}
    db.commit()
    complete_job(db, job_type="assignment", object_id=submission.id, score=score)
    return submission


def _set_test_counts_from_group_results(submission, results: dict) -> None:
    """把测试组结果归一化写入 tests_passed/tests_total，供工作台最近提交表直读。"""
    passed = sum(int(counts.get("passed", 0)) for counts in results.values())
    total = sum(
        int(counts.get(key, 0))
        for counts in results.values()
        for key in ("passed", "failed", "errors", "skipped")
    )
    submission.tests_passed = passed
    submission.tests_total = total


def _v1_judge_submission(db, redis_client, settings, submission, question, workdir, host_workdir, timeout_s, mem_mb):
    """V1 评分路径：运行测试组 → 创建 CodeGrade → 入队 AI。

    配置完整性错误（缺测试组/缺锁定 Rubric/测试组缺 tests 代码）是永久错误：
    立即 system_error 终态，不消耗重试；仅 Docker/基础设施异常可重试。
    """
    from app.services.judge_queue import complete_job, fail_job

    test_groups = question.test_groups or []
    if not test_groups:
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="shadow/active 需要测试组", retryable=False)
        submission.status = "system_error"
        submission.score = None
        db.commit()
        return submission

    # 查找锁定 Rubric（缺失则系统错误，不能让学生丢分）
    locked = db.scalar(
        select(QuestionRubric).where(
            QuestionRubric.judge_question_id == question.id,
            QuestionRubric.status == "locked",
        ).order_by(QuestionRubric.version.desc()).limit(1)
    )
    if locked is None:
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="shadow/active 缺少锁定 Rubric", retryable=False)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        db.commit()
        return submission

    # 配置完整性：测试组必须有 tests 代码（永久错误，不重试、不进入 Docker）
    missing_tests = [g.get("id") for g in test_groups
                     if not (g.get("tests") or "").strip()]
    if missing_tests:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="测试组缺少测试代码: " + ",".join(str(x) for x in missing_tests),
                 retryable=False)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        db.commit()
        return submission

    # Phase 5：解析提交快照环境（digest 镜像 + import 策略），fail closed
    try:
        image_ref, policy, installed = _submission_env_context(db, submission)
    except EnvironmentUnavailableError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=exc.message, retryable=True)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        submission.result_details = {
            "diagnostic": {"code": "ENVIRONMENT_IMAGE_MISSING",
                           "module": "", "message": exc.message},
        }
        db.commit()
        return submission
    # import 预检：只分析学生源代码；IMPORT_NOT_ALLOWED 学生错误，IMPORT_NOT_INSTALLED 平台配置
    terminal = _handle_import_diagnostic(db, submission,
                                         _diagnose_submission(submission, policy, installed),
                                         fail_job)
    if terminal is not None:
        return terminal

    result = run_test_groups(workdir, host_workdir, submission.code, test_groups,
                             settings, timeout_s, mem_mb, image_ref=image_ref)
    all_errs = list(result["system_errors"])
    f_score, r_score, details = _calc_fr_scores(test_groups, result["results"], all_errs)

    # 系统错误（Docker 执行异常/结果解析失败——配置缺失已在前面拦截）→ 不创建 CodeGrade，退回重试，不扣分
    if all_errs:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="; ".join(all_errs), retryable=True)
        submission.status = "system_error"
        submission.score = None  # 系统错误不扣分
        submission.result_details = {"groups": details, "system_errors": all_errs}
        db.commit()
        return submission

    # shadow: 保留旧规则成绩（同时跑 hidden_tests）
    if question.grading_mode == "shadow":
        _write_submission_files(workdir, submission, question)
        try:
            stdout, stderr, returncode, elapsed = _run_docker_pytest(
                workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
                image_ref=image_ref)
        except Exception as e:
            fail_job(db, job_type="assignment", object_id=submission.id,
                     error=f"Docker 判题失败: {e}", retryable=True)
            submission.status = "system_error"
            submission.score = None  # 基础设施异常不扣分
            submission.stderr = f"Docker 判题失败: {e}"
            db.commit()
            return submission
        legacy_status, legacy_score = _status_from_pytest(returncode, stdout, stderr)
        if legacy_status == "system_error":
            fail_job(
                db,
                job_type="assignment",
                object_id=submission.id,
                error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                retryable=True,
            )
            submission.status = "system_error"
            submission.score = None
            submission.stdout = stdout[-8000:]
            submission.stderr = stderr[-8000:]
            submission.execution_time_ms = elapsed
            submission.result_details = {
                "groups": details,
                "system_errors": [f"Docker exit={returncode}"],
                "f_score": f_score,
                "r_score": r_score,
            }
            db.commit()
            return submission
        submission.status = legacy_status
        submission.score = legacy_score
        submission.stdout = stdout[-8000:]
        submission.stderr = stderr[-8000:]
        submission.execution_time_ms = elapsed
    else:
        # active: Docker 完成后不写正式分，等 AI 完成后再合分
        submission.status = "running"
        submission.score = None

    submission.result_details = {"groups": details, "system_errors": all_errs,
                                 "f_score": f_score, "r_score": r_score}
    _set_test_counts_from_group_results(submission, result["results"])
    db.commit()
    # 不传 score：shadow 已设 legacy 分，active 保持 None 等 AI；complete_job 只标记 grading_status=completed
    complete_job(db, job_type="assignment", object_id=submission.id,
                 result_details=submission.result_details)

    # 创建 CodeGrade（幂等：检查是否已存在）
    existing = db.scalar(select(CodeGrade).where(CodeGrade.submission_id == submission.id))
    if existing is None:
        cg = CodeGrade(
            submission_id=submission.id, rubric_id=locked.id,
            mode=question.grading_mode, status="pending",
            functional_score=f_score, robustness_score=r_score,
            deterministic_details={"groups": details, "system_errors": all_errs},
        )
        db.add(cg)
        db.commit()
        from app.services.ai_grading_queue import enqueue_ai_grade
        enqueue_ai_grade(db, redis_client, cg.id)
        db.commit()

    return submission


def process_submission(db: Session, redis_client, settings: Settings, submission_id: int) -> Submission:
    from app.services.judge_queue import claim_job, fail_job

    submission = db.get(Submission, submission_id)
    if not submission:
        return None
    if submission.grading_status == "completed":
        return submission

    try:
        if not claim_job(db, job_type="assignment", object_id=submission_id):
            return submission

        question = db.get(JudgeQuestion, submission.question_id)
        if not question:
            fail_job(db, job_type="assignment", object_id=submission_id,
                     error="题目不存在", retryable=False)
            submission.status = "system_error"
            submission.score = None  # 系统错误不扣分
            db.commit()
            return submission

        submission.status = "running"
        db.commit()

        _work_root = _settings_path_or_none(settings.judge_work_dir)
        _host_root = _settings_path_or_none(settings.judge_host_work_dir)
        _cleanup = None
        try:
            if _work_root:
                workdir, _cleanup = _make_work_dir(_work_root, "dai-judge-")
                host_workdir = _host_root / workdir.relative_to(_work_root) if _host_root else workdir
            else:
                _temp = tempfile.TemporaryDirectory(prefix="dai-judge-")
                workdir = Path(_temp.name)
                host_workdir = workdir
                _cleanup = lambda: _temp.cleanup()

            timeout_s = _get_timeout(question, settings)
            mem_mb = question.memory_limit_mb or settings.judge_memory_limit_mb

            gmode = getattr(question, 'grading_mode', 'legacy') or 'legacy'
            if gmode == "legacy":
                result = _legacy_judge_submission(
                    db, redis_client, settings, submission, question,
                    workdir, host_workdir, timeout_s, mem_mb)
            else:
                result = _v1_judge_submission(
                    db, redis_client, settings, submission, question,
                    workdir, host_workdir, timeout_s, mem_mb)

            db.refresh(submission) if result else None
            redis_client.setex(f"judge:result:{submission.id}", 3600, getattr(submission, 'status', 'unknown'))
            logger.info("Submission %s 判题完成: %s", submission_id, getattr(submission, 'status', '?'))
            return result

        finally:
            if _cleanup:
                _cleanup()

    except Exception:
        logger.exception("Submission %s 未知异常", submission_id)
        try:
            fail_job(db, job_type="assignment", object_id=submission_id,
                     error="Worker 未知异常", retryable=True)
        except Exception:
            pass
        return submission


# ═══════════════════════════════════════════════════════════════
# 考试判题
# ═══════════════════════════════════════════════════════════════

def _maybe_finalize_exam(submission_id: int, db: Session):
    from app.services.exam_grading import finalize_if_ready
    finalize_if_ready(submission_id, db)


def process_exam_answer(db: Session, redis_client, settings: Settings, answer_id: int) -> ExamAnswer:
    from app.services.judge_queue import claim_job, complete_job, fail_job

    answer = db.get(ExamAnswer, answer_id)
    if not answer:
        return None
    if answer.grading_status == "completed":
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    try:
        if not claim_job(db, job_type="exam", object_id=answer_id):
            return answer

        question = db.get(ExamQuestion, answer.question_id)
        if not question:
            fail_job(db, job_type="exam", object_id=answer_id,
                     error="题目不存在", retryable=False)
            answer.system_error = "题目不存在"
            answer.score = None  # 系统错误不扣分
            db.commit()
            _maybe_finalize_exam(answer.submission_id, db)
            return answer

        _work_root = _settings_path_or_none(settings.judge_work_dir)
        _host_root = _settings_path_or_none(settings.judge_host_work_dir)
        _cleanup = None
        try:
            if _work_root:
                workdir, _cleanup = _make_work_dir(_work_root, "dai-exam-judge-")
                host_workdir = _host_root / workdir.relative_to(_work_root) if _host_root else workdir
            else:
                _temp = tempfile.TemporaryDirectory(prefix="dai-exam-judge-")
                workdir = Path(_temp.name)
                host_workdir = workdir
                _cleanup = lambda: _temp.cleanup()

            timeout_s = max(min(math.ceil((question.time_limit_ms or 10000) / 1000), settings.judge_timeout_seconds), 1)
            mem_mb = question.memory_limit_mb or settings.judge_memory_limit_mb
            gmode = getattr(question, 'grading_mode', 'legacy') or 'legacy'

            if gmode == "legacy":
                # legacy 路径
                if not question.hidden_tests or not question.hidden_tests.strip():
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="缺少隐藏测试", retryable=False)
                    answer.score = None  # 系统错误不扣分
                    answer.system_error = "缺少隐藏测试"
                    db.commit()
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                user_code = workdir / "user_code.py"
                test_file = workdir / "test_user_code.py"
                user_code.write_text(answer.code_answer or "", encoding="utf-8")
                ht = question.hidden_tests
                if "import user_code" not in ht and "from user_code" not in ht:
                    ht = f"import user_code\n\n{ht}"
                test_file.write_text(ht, encoding="utf-8")
                try:
                    stdout, stderr, returncode, elapsed = _run_docker_pytest(
                        workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir)
                except Exception as e:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error=f"Docker 判题失败: {e}", retryable=True)
                    answer.score = None  # 系统错误不扣分
                    answer.system_error = f"Docker 判题失败: {e}"
                    db.commit()
                    return answer

                final_status, _ = _status_from_pytest(returncode, stdout, stderr)
                if final_status == "system_error":
                    fail_job(
                        db,
                        job_type="exam",
                        object_id=answer_id,
                        error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                        retryable=True,
                    )
                    answer.score = None
                    answer.system_error = f"Docker 判题系统错误（exit={returncode}）"
                    answer.result_details = {"returncode": returncode}
                    db.commit()
                    return answer
                answer.score = float(question.points) if final_status == "accepted" else 0.0
                answer.grading_status = "completed"
                answer.result_details = {"returncode": returncode}
                db.commit()
                complete_job(db, job_type="exam", object_id=answer_id, score=answer.score)
                _maybe_finalize_exam(answer.submission_id, db)
            else:
                # V1 路径：测试组 → FR 分 → 校验 → 创建 CodeGrade → 入队 AI
                test_groups = question.test_groups or []
                if not test_groups:
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="shadow/active 需要测试组", retryable=False)
                    answer.score = None
                    answer.system_error = "缺少测试组"
                    db.commit()
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                # 查找锁定 Rubric（缺失则系统错误，禁止继续）
                locked = db.scalar(
                    select(QuestionRubric).where(
                        QuestionRubric.exam_question_id == question.id,
                        QuestionRubric.status == "locked",
                    ).order_by(QuestionRubric.version.desc()).limit(1)
                )
                if locked is None:
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="shadow/active 缺少锁定 Rubric", retryable=False)
                    answer.score = None  # 系统错误不扣分
                    answer.system_error = "缺少锁定 Rubric"
                    db.commit()
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                # 配置完整性：测试组必须有 tests 代码（永久错误，不重试、不进入 Docker）
                missing_tests = [g.get("id") for g in test_groups
                                 if not (g.get("tests") or "").strip()]
                if missing_tests:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="测试组缺少测试代码: " + ",".join(str(x) for x in missing_tests),
                             retryable=False)
                    answer.score = None  # 系统错误不扣分
                    answer.system_error = "测试组缺少测试代码"
                    db.commit()
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                result = run_test_groups(workdir, host_workdir, answer.code_answer or "",
                                         test_groups, settings, timeout_s, mem_mb)
                all_errs = list(result["system_errors"])
                f_score, r_score, details = _calc_fr_scores(test_groups, result["results"], all_errs)

                # 系统错误（Docker/解析失败）→ 立即停止，不创建 CodeGrade，不 finalize
                if all_errs:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="; ".join(all_errs), retryable=True)
                    answer.score = None  # 系统错误不扣分
                    answer.system_error = "; ".join(all_errs)
                    answer.result_details = {"groups": details, "system_errors": all_errs,
                                             "f_score": f_score, "r_score": r_score}
                    db.commit()
                    return answer

                if gmode == "shadow":
                    # 保留旧二元评分：全通过才满分
                    user_code = workdir / "user_code.py"
                    test_file = workdir / "test_user_code.py"
                    user_code.write_text(answer.code_answer or "", encoding="utf-8")
                    ht = question.hidden_tests
                    if "import user_code" not in ht and "from user_code" not in ht:
                        ht = f"import user_code\n\n{ht}"
                    test_file.write_text(ht, encoding="utf-8")
                    try:
                        stdout, stderr, returncode, _ = _run_docker_pytest(
                            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir)
                    except Exception as exc:
                        # 基础设施异常→fail_job 可重试，不扣分，不 finalize
                        fail_job(db, job_type="exam", object_id=answer_id,
                                 error=f"Docker 判题失败: {exc}", retryable=True)
                        answer.score = None
                        answer.system_error = f"Docker 判题失败: {exc}"
                        db.commit()
                        return answer
                    legacy_status, _ = _status_from_pytest(returncode, stdout, stderr)
                    if legacy_status == "system_error":
                        fail_job(
                            db,
                            job_type="exam",
                            object_id=answer_id,
                            error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                            retryable=True,
                        )
                        answer.score = None
                        answer.system_error = f"Docker 判题系统错误（exit={returncode}）"
                        answer.result_details = {
                            "groups": details,
                            "system_errors": [f"Docker exit={returncode}"],
                            "f_score": f_score,
                            "r_score": r_score,
                        }
                        db.commit()
                        return answer
                    answer.score = float(question.points) if legacy_status == "accepted" else 0.0
                else:
                    # active: 等 AI 完成后才定分
                    answer.score = None

                answer.grading_status = "completed"
                answer.result_details = {"groups": details, "system_errors": all_errs,
                                         "f_score": f_score, "r_score": r_score}
                db.commit()

                complete_job(db, job_type="exam", object_id=answer_id,
                             score=answer.score, result_details=answer.result_details)

                # 创建 CodeGrade 并入队 AI（locked rubric 已确保存在）
                existing = db.scalar(
                    select(CodeGrade).where(CodeGrade.exam_answer_id == answer_id)
                )
                if existing is None:
                    cg = CodeGrade(
                        exam_answer_id=answer_id, rubric_id=locked.id,
                        mode=gmode, status="pending",
                        functional_score=f_score, robustness_score=r_score,
                        deterministic_details={"groups": details, "system_errors": all_errs},
                    )
                    db.add(cg)
                    db.commit()
                    from app.services.ai_grading_queue import enqueue_ai_grade
                    enqueue_ai_grade(db, redis_client, cg.id)
                    db.commit()

                # shadow 立即汇总; active 等 AI
                if gmode == "shadow":
                    _maybe_finalize_exam(answer.submission_id, db)

            return answer
        finally:
            if _cleanup:
                _cleanup()

    except Exception:
        logger.exception("ExamAnswer %s 未知异常", answer_id)
        try:
            fail_job(db, job_type="exam", object_id=answer_id, error="Worker 未知异常", retryable=True)
        except Exception:
            pass
        try:
            _maybe_finalize_exam(answer.submission_id, db)
        except Exception:
            pass
        return answer


# ═══════════════════════════════════════════════════════════════
# AI 评分处理
# ═══════════════════════════════════════════════════════════════

def process_ai_grade(db: Session, redis_client, settings: Settings, code_grade_id: int) -> CodeGrade:
    from app.services.ai_grading_queue import claim_ai_grade, complete_ai_grade, fail_ai_grade
    from app.services.ai_grading_service import grade_code_submission
    from app.services.ai_client import AIServiceError, DeepSeekClient
    from app.services.op_metrics import ai_metrics_sink

    if not claim_ai_grade(db, code_grade_id):
        return None

    if not settings.ai_ready:
        # TASK-020：AI 关闭（未审批/未配置）时零外呼——
        # 不构造客户端、不发任何请求；任务以不可重试终态转人工评分
        db.rollback()
        fail_ai_grade(
            db, redis_client, code_grade_id,
            "AI 服务未启用（DAI_AI_ENABLED=false 或未配置 API Key）",
            retryable=False,
        )
        return None

    try:
        client = DeepSeekClient(settings, metrics_sink=ai_metrics_sink())
        cg = grade_code_submission(db, client, code_grade_id)
        db.commit()
        db.refresh(cg)
        if cg.status != "review_required" and not cg.needs_teacher_review:
            cg.status = "completed"
            cg.finished_at = datetime.now(timezone.utc)
            db.commit()
            # 考试 active: 完成后再次触发汇总
            if cg.exam_answer_id and cg.mode == "active":
                ans = db.get(ExamAnswer, cg.exam_answer_id)
                if ans:
                    from app.services.exam_grading import finalize_if_ready, promote_review_required_if_complete
                    finalize_if_ready(ans.submission_id, db)
                    # 教师在工作台显式重试成功后的补救：失败原因已消除则收口
                    promote_review_required_if_complete(ans.submission_id, db)
        else:
            # review_required（AI 自动终态）：考试 active 父级当场转 review_required
            # finalize 自身幂等/CAS，重复触发无害
            if cg.exam_answer_id and cg.mode == "active":
                ans = db.get(ExamAnswer, cg.exam_answer_id)
                if ans:
                    from app.services.exam_grading import finalize_if_ready
                    finalize_if_ready(ans.submission_id, db)
        return cg
    except AIServiceError as exc:
        db.rollback()
        fail_ai_grade(db, redis_client, code_grade_id, str(exc), retryable=exc.retryable)
        return None
    except Exception as exc:
        db.rollback()
        fail_ai_grade(db, redis_client, code_grade_id, str(exc), retryable=True)
        return None


# ═══════════════════════════════════════════════════════════════
# Worker 主循环
# ═══════════════════════════════════════════════════════════════

def _recover_stale(db, redis_client, owner_id: str) -> bool:
    """在 grading-recovery 租约下执行 judge + AI stale recovery。

    多 Worker 实例同一时刻只有一个执行；租约被他人持有时跳过本轮。
    """
    from app.services.scheduler_lease import try_acquire_lease
    if not try_acquire_lease(db, "grading-recovery", owner_id, ttl_seconds=120):
        return False
    from app.services.judge_queue import requeue_stale_jobs
    from app.services.ai_grading_queue import recover_stale_ai_grades
    j_stats = requeue_stale_jobs(db)
    a_stats = recover_stale_ai_grades(db, redis_client)
    if any(v > 0 for v in j_stats.values()) or any(v > 0 for v in a_stats.values()):
        logger.info("Grading 恢复: judge=%s ai=%s", j_stats, a_stats)
    return True


def _worker_queue_names(settings: Settings) -> tuple[str, ...]:
    """Return only the queues owned by this worker role.

    The deterministic worker is deliberately the default.  An AI worker is
    opt-in and has no consumption queues at all while AI is disabled.
    """
    if settings.worker_role == "judge":
        return settings.judge_queue_name, EXAM_JUDGE_QUEUE
    if settings.worker_role == "ai":
        return (settings.ai_queue_name,) if settings.ai_enabled else ()
    raise ValueError(f"unsupported worker role: {settings.worker_role}")


def run_worker_loop():
    import redis as _redis

    settings = get_settings()
    worker_role = settings.worker_role
    queues = list(_worker_queue_names(settings))
    if not queues:
        logger.info("AI worker 未启用，退出消费循环")
        return

    redis_client = _redis.Redis.from_url(
        settings.redis_url, decode_responses=True, encoding_errors="replace",
        socket_connect_timeout=2, socket_timeout=7,
    )
    import socket
    owner_id = f"worker:{socket.gethostname()}:{os.getpid()}"

    logger.info("%s worker 启动，监听队列: %s", worker_role, queues)

    next_recovery = time.monotonic()
    while True:
        # Redis is only a wake-up transport. Busy or unavailable queues must
        # not prevent repairing durable jobs, including on worker startup.
        if worker_role == "judge" and time.monotonic() >= next_recovery:
            try:
                with SessionLocal() as db:
                    _recover_stale(db, redis_client, owner_id)
            except Exception:
                logger.exception("Grading 恢复异常，稍后重试")
                next_recovery = time.monotonic() + 5
            else:
                # A healthy scan and a lease owned by another worker both use
                # the normal interval. Retry failures without pausing work.
                next_recovery = time.monotonic() + 120

        try:
            result = redis_client.blpop(queues, timeout=5)
            if result is None:
                continue

            queue_name, raw_data = result
            # Rotate every consumed message before decoding or claiming it.
            # Invalid/duplicate messages must not monopolize the first queue.
            queues.remove(queue_name)
            queues.append(queue_name)
        except Exception:
            logger.exception("blpop 异常")
            time.sleep(1)
            continue

        try:
            payload = _json.loads(raw_data)
            if not isinstance(payload, dict):
                raise ValueError("queue message must be an object")
            msg_type = payload.get("type")
            job_id = payload.get("id")
            expected_type = ("ai_grade" if worker_role == "ai" else
                             "exam" if queue_name == EXAM_JUDGE_QUEUE else "assignment")
            if (msg_type != expected_type or type(job_id) is not int or job_id <= 0
                    or ("attempt" in payload and
                        (type(payload["attempt"]) is not int or payload["attempt"] < 0))):
                raise ValueError("invalid queue message fields")
        except Exception:
            # The parser can also reject deeply nested JSON with RecursionError.
            # Keep this boundary around decoding/validation, never execution.
            logger.warning("无效判题消息，已忽略: queue=%s", queue_name)
            continue

        if worker_role == "ai":
            try:
                with SessionLocal() as db:
                    process_ai_grade(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("AI 评分异常: id=%s", job_id)
        elif msg_type == "exam":
            try:
                with SessionLocal() as db:
                    process_exam_answer(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("考试判题异常: id=%s", job_id)
        else:
            try:
                with SessionLocal() as db:
                    process_submission(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("判题异常: id=%s", job_id)


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging(get_settings().environment, process_name="worker", settings=get_settings())
    run_worker_loop()
