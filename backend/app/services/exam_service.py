"""考试系统业务逻辑"""
import hashlib
import hmac
import logging
import random
import re
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.config import get_settings
from app.errors import api_error
from app.models import (AuditEvent, Exam, ExamAnswer, ExamEvent, ExamGrade, ExamQuestion,
                        ExamSubmission, JudgeGradingVersion, User)
from app.services.student_ai_results import build_student_grading_breakdown
from app.services.offering_service import require_student_task_write

logger = logging.getLogger("dai.exam_service")

_BLANK_PATTERN = re.compile(r"\[\[blank:([A-Za-z0-9_-]+)\]\]")
_COMPLETED_SUBMISSION_STATUSES = ("submitted", "grading", "graded", "review_required")
_LIVE_POINTS = object()
# 学生端可上报的监考事件；device_switch / autosave_rejected / warning 只由服务端生成
_CLIENT_EVENT_KINDS = ("blur", "focus", "reconnect")
_MAX_EVENT_DURATION_MS = 24 * 60 * 60 * 1000

def require_exam_editable(exam, user):
    if user.role != "admin" and exam.created_by_id != user.id:
        raise api_error(403, "FORBIDDEN", "只能管理自己的考试")
    if exam.status != "draft":
        raise api_error(403, "EXAM_LOCKED", "考试已发布，不能修改题目")

def round_score(value: float) -> float:
    """正式考试题目得分统一按十进制四舍五入保留一位。"""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def score_choice_answer(question, selected_options, *, points: float | None = None) -> float:
    """选择题评分；历史数据默认完全匹配，多选可启用无错选部分得分。"""
    answer_config = question.correct_answer or {}
    correct = set(answer_config.get("correct", []))
    selected = set(selected_options or [])
    if question.question_type == "multi_choice" \
            and answer_config.get("scoring_mode", "all_or_nothing") == "partial_no_wrong":
        if selected - correct or not correct:
            return 0.0
        return round_score(float(question.points if points is None else points)
                           * len(selected & correct) / len(correct))
    return round_score(float(question.points if points is None else points)) if correct == selected else 0.0


def score_true_false_answer(question, selected_options, *, points: float | None = None) -> float:
    """判断题评分：T/F 单值完全匹配，满分或 0 分（沿用 score_choice_answer 骨架）。

    答案载体沿用 ExamAnswer.selected_options（["T"] / ["F"]），因此评分、冻结与
    _frozen_exam_max_score 都不需要额外的数据类型分支。
    """
    correct = set((question.correct_answer or {}).get("correct", []))
    selected = set(selected_options or [])
    return round_score(float(question.points if points is None else points)) if correct == selected else 0.0


def score_non_code_answer(question, answer, *, points: float | None = None) -> float:
    """非代码题统一评分入口：选择题、判断题、填空题各走既有评分骨架。"""
    if question.question_type == "fill_blank":
        return score_fill_blank_answer(question, answer.text_answers, points=points)
    if question.question_type == "true_false":
        return score_true_false_answer(question, answer.selected_options, points=points)
    return score_choice_answer(question, answer.selected_options, points=points)


def _normalized_fill_value(value, *, case_sensitive: bool) -> str:
    normalized = str(value or "").strip()
    return normalized if case_sensitive else normalized.casefold()

def score_fill_blank_answer(question, text_answers, *, points: float | None = None) -> float:
    """多空填空题按空等分；每空支持多个可接受答案与大小写设置。"""
    blanks = (question.correct_answer or {}).get("blanks", [])
    if not blanks:
        return 0.0
    per_blank = float(question.points if points is None else points) / len(blanks)
    earned = 0.0
    provided = text_answers or {}
    for blank in blanks:
        blank_id = str(blank.get("id", ""))
        case_sensitive = bool(blank.get("case_sensitive", False))
        actual = _normalized_fill_value(provided.get(blank_id, ""), case_sensitive=case_sensitive)
        accepted = {
            _normalized_fill_value(candidate, case_sensitive=case_sensitive)
            for candidate in blank.get("accepted_answers", [])
        }
        if actual and actual in accepted:
            earned += per_blank
    return round_score(earned)


def validate_question(question, *, publish: bool = False, number: int | None = None) -> list[str]:
    """逐题校验，返回错误列表（空=通过）

    number：展示给教师的题号（1 起始，与题目列表序号一致）。
    缺省时退回 order_index + 1（order_index 为 0 起始存储值）。
    """
    label = number if number is not None else (question.order_index or 0) + 1
    errors = []

    # 所有题型通用校验
    if not question.points or question.points <= 0:
        errors.append(f"题目 {label}: 分值必须大于 0，当前为 {question.points}")

    if question.question_type in ("single_choice", "multi_choice"):
        options = question.options or {}
        if len(options) < 2:
            errors.append(f"题目 {label}: 选项至少需要 2 个，当前 {len(options)} 个")
        correct = (question.correct_answer or {}).get("correct", [])
        if question.question_type == "single_choice":
            if len(correct) != 1:
                errors.append(f"题目 {label}: 单选题必须有恰好 1 个正确答案，当前 {len(correct)} 个")
        if question.question_type == "multi_choice":
            if len(correct) < 1:
                errors.append(f"题目 {label}: 多选题至少需要 1 个正确答案")
        for key in correct:
            if key not in options:
                errors.append(f"题目 {label}: 正确答案 '{key}' 不在选项中")
        if any(not str(key).strip() or not str(value).strip() for key, value in options.items()):
            errors.append(f"题目 {label}: 选项标识和内容不能为空")
        scoring_mode = (question.correct_answer or {}).get("scoring_mode", "all_or_nothing")
        if scoring_mode not in ("all_or_nothing", "partial_no_wrong"):
            errors.append(f"题目 {label}: 不支持的多选计分方式 '{scoring_mode}'")
        if question.question_type == "single_choice" and scoring_mode != "all_or_nothing":
            errors.append(f"题目 {label}: 单选题只支持完全匹配计分")

    elif question.question_type == "true_false":
        # 判断题不配置选项：答案仍由 ExamAnswer.selected_options 承载 ["T"] / ["F"]。
        if question.options:
            errors.append(f"题目 {label}: 判断题不能配置选项")
        correct = (question.correct_answer or {}).get("correct", [])
        if len(correct) != 1 or correct[0] not in ("T", "F"):
            errors.append(f"题目 {label}: 判断题正确答案必须是 T 或 F 中的恰好一个")

    elif question.question_type == "fill_blank":
        markers = _BLANK_PATTERN.findall(question.prompt or "")
        configured = (question.correct_answer or {}).get("blanks", [])
        configured_ids = [str(blank.get("id", "")) for blank in configured]
        if not markers:
            errors.append(f"题目 {label}: 填空题至少需要插入一个空格")
        if len(markers) != len(set(markers)):
            errors.append(f"题目 {label}: 填空题空格标识不能重复")
        if markers != configured_ids:
            errors.append(f"题目 {label}: 填空题占位符与答案配置不一致")
        for blank in configured:
            accepted = [str(value).strip() for value in blank.get("accepted_answers", [])]
            if not accepted or any(not value for value in accepted):
                errors.append(f"题目 {label}: 每个空格至少需要一个非空可接受答案")

    elif question.question_type == "code":
        mode = question.grading_mode or "active"
        if publish and mode in ("legacy", "shadow") and not (question.hidden_tests or "").strip():
            errors.append(f"题目 {label}: {mode} 模式必须配置隐藏测试")
        if publish and mode in ("shadow", "active") and not (question.test_groups or []):
            errors.append(f"题目 {label}: {mode} 模式必须配置 AI 测试组")
        if question.time_limit_ms is not None and question.time_limit_ms <= 0:
            errors.append(f"题目 {label}: 时间限制必须为正数")
        if question.time_limit_ms is None:
            # 默认 10000ms
            pass
        if question.memory_limit_mb is not None and question.memory_limit_mb <= 0:
            errors.append(f"题目 {label}: 内存限制必须为正数")

    else:
        errors.append(f"题目 {label}: 不支持的题型 '{question.question_type}'")

    return errors


def validate_publish(exam, db):
    if not exam.start_at or not exam.end_at:
        raise api_error(422, "PUBLISH_INVALID", "发布考试前必须设置开始时间和最晚进入时间")
    if exam.start_at and exam.end_at and exam.start_at >= exam.end_at:
        raise api_error(422, "PUBLISH_INVALID", "开始时间必须早于结束时间")
    if (exam.timing_mode or "unified") == "unified" and not exam.end_at:
        # 统一时间交卷必须能收敛到考试结束时间；没有 end_at 就无法确定统一收卷时刻。
        raise api_error(422, "PUBLISH_INVALID", "统一时间交卷模式必须设置考试结束时间")
    if exam.show_answers_after_review and not exam.show_questions_after_review:
        raise api_error(422, "PUBLISH_INVALID", "公开标准答案时必须同时公开题目")
    if not exam.duration_minutes or exam.duration_minutes <= 0:
        raise api_error(422, "PUBLISH_INVALID", "考试时长必须大于0")
    questions = db.scalars(
        select(ExamQuestion).where(ExamQuestion.exam_id == exam.id)
        .order_by(ExamQuestion.order_index, ExamQuestion.id)
    ).all()
    if not questions:
        raise api_error(422, "PUBLISH_INVALID", "至少需要一道题目")
    total = sum(q.points for q in questions)
    if total <= 0:
        raise api_error(422, "PUBLISH_INVALID", "总分必须大于0")
    # 逐题校验（题号按排序后的展示位置 1 起始，与题目列表一致）
    all_errors = []
    for position, q in enumerate(questions, start=1):
        all_errors.extend(validate_question(q, publish=True, number=position))
    if all_errors:
        raise api_error(422, "QUESTION_INVALID", "题目校验失败：" + "; ".join(all_errors))
def _submission_expiry(exam, now):
    """按时间模式计算个人交卷时刻。

    unified：与考试结束时间取 min（G-2 时长收敛：晚入场者不再拿到越过 end_at 的时长）；
    per_student：进入后获得完整时长，此时 end_at 只表示「最晚可开考时间」。
    """
    from app.services.time_utils import as_utc
    full_window = now + timedelta(minutes=exam.duration_minutes)
    if (exam.timing_mode or "unified") != "unified" or exam.end_at is None:
        return full_window
    return min(full_window, as_utc(exam.end_at))


def _exam_shuffle_seed(exam_id: int, student_id: int, settings) -> int:
    digest = hmac.new(
        settings.secret_key.encode("utf-8"), f"{exam_id}:{student_id}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return int(digest, 16)


def freeze_exam_shuffle(exam, submission, db, *, settings=None) -> None:
    """开考时把乱序结果写入提交并冻结；之后一切以库中持久化顺序为准，不再按种子重算。"""
    if not (exam.shuffle_questions or exam.shuffle_options):
        return
    settings = settings or get_settings()
    randomizer = random.Random(_exam_shuffle_seed(exam.id, submission.student_id, settings))
    questions = db.scalars(
        select(ExamQuestion).where(ExamQuestion.exam_id == exam.id)
        .order_by(ExamQuestion.order_index, ExamQuestion.id)
    ).all()
    if exam.shuffle_questions:
        order = [question.id for question in questions]
        randomizer.shuffle(order)
        submission.question_order = order
    if exam.shuffle_options:
        option_order = {}
        for question in questions:
            keys = list((question.options or {}).keys())
            randomizer.shuffle(keys)
            option_order[str(question.id)] = keys
        submission.option_order = option_order
    db.flush()


def hash_client_identity(settings, value: str | None) -> str | None:
    """IP / User-Agent 只保存 HMAC 摘要，永不落明文（考试页有可见的隐私告知）。"""
    if not value:
        return None
    return hmac.new(settings.secret_key.encode("utf-8"), value.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _append_exam_events(db, submission, rows, *, ip_hash=None, ua_hash=None) -> int:
    """写入监考事件；同 (submission, kind, client_ts) 幂等，随后按事件表重算计数。"""
    from app.services.time_utils import utc_now
    now = utc_now()
    keys = {row["dedupe_key"] for row in rows if row.get("dedupe_key")}
    existing = set()
    if keys:
        existing = set(db.scalars(select(ExamEvent.dedupe_key).where(
            ExamEvent.submission_id == submission.id,
            ExamEvent.dedupe_key.in_(keys),
        )).all())
    recorded = 0
    seen = set()
    for row in rows:
        key = row.get("dedupe_key")
        if key and (key in existing or key in seen):
            continue
        if key:
            seen.add(key)
        db.add(ExamEvent(
            exam_id=submission.exam_id, submission_id=submission.id, kind=row["kind"],
            payload=row.get("payload") or {}, client_ts=row.get("client_ts"), server_ts=now,
            duration_ms=row.get("duration_ms"), ip_hash=ip_hash, ua_hash=ua_hash, dedupe_key=key,
        ))
        recorded += 1
    if recorded:
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # 并发补报同一 client_ts：唯一约束兜底；计数仍按事件表重算，不会多算。
            recorded = 0
    _recompute_integrity_counters(db, submission)
    return recorded


def _recompute_integrity_counters(db, submission) -> None:
    """监考计数一律由事件表幂等重算：重复补报、并发补报都不会累加两次。"""
    rows = db.execute(
        select(ExamEvent.kind, func.count(ExamEvent.id), func.coalesce(func.sum(ExamEvent.duration_ms), 0))
        .where(ExamEvent.submission_id == submission.id)
        .group_by(ExamEvent.kind)
    ).all()
    stats = {kind: (int(count), int(total or 0)) for kind, count, total in rows}
    submission.blur_count = stats.get("blur", (0, 0))[0]
    # 离开时长由回到页面时的 focus 事件带回（离开那一刻还不知道本次离开多久）。
    submission.away_total_ms = stats.get("focus", (0, 0))[1]
    submission.device_switch_count = stats.get("device_switch", (0, 0))[0]


def record_exam_events(db, submission, events, *, ip_hash=None, ua_hash=None) -> int:
    """学生端上报的监考事件（blur / focus / reconnect），按 client_ts 幂等去重。"""
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    rows = []
    for item in events or []:
        kind = str(item.get("kind") or "")
        if kind not in _CLIENT_EVENT_KINDS:
            raise api_error(422, "EVENT_KIND_INVALID", f"不支持的监考事件类型 '{kind}'")
        client_ts = as_utc(item.get("client_ts")) or now
        duration_ms = item.get("duration_ms")
        if duration_ms is not None:
            duration_ms = int(duration_ms)
            if duration_ms < 0 or duration_ms > _MAX_EVENT_DURATION_MS:
                raise api_error(422, "EVENT_DURATION_INVALID", "监考事件时长超出允许范围")
        rows.append({
            "kind": kind, "client_ts": client_ts, "duration_ms": duration_ms,
            "dedupe_key": f"{kind}:{int(client_ts.timestamp() * 1000)}",
        })
    return _append_exam_events(db, submission, rows, ip_hash=ip_hash, ua_hash=ua_hash)


def reject_superseded_write(db, submission, *, source: str) -> None:
    """写入代次不匹配：只落 autosave_rejected 事件，绝不写任何答案数据。"""
    from app.services.time_utils import utc_now
    key = f"autosave_rejected:{source}"
    exists = db.scalar(select(ExamEvent.id).where(
        ExamEvent.submission_id == submission.id, ExamEvent.dedupe_key == key).limit(1))
    if exists is None:
        db.add(ExamEvent(
            exam_id=submission.exam_id, submission_id=submission.id, kind="autosave_rejected",
            payload={"source": source}, client_ts=None, server_ts=utc_now(), dedupe_key=key,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    raise api_error(409, "DEVICE_SUPERSEDED", "本设备已失去答题写入权限，请在新设备继续作答")


def start_exam(exam, student, db, *, settings=None):
    from app.services.time_utils import as_utc, utc_now
    exam = require_student_task_write(db, exam, student)
    now = utc_now()
    existing = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id, ExamSubmission.student_id == student.id)
        .with_for_update().execution_options(populate_existing=True))
    if existing:
        if existing.status == "started":
            if existing.expires_at and as_utc(existing.expires_at) <= now:
                _auto_submit(existing, db, now)
                db.refresh(existing)
            return existing
        raise api_error(403, "EXAM_ALREADY_SUBMITTED", "考试已提交")
    if exam.start_at is not None and as_utc(exam.start_at) > now:
        raise api_error(403, 'EXAM_NOT_STARTED', '考试尚未开始')
    if exam.end_at is not None and as_utc(exam.end_at) <= now:
        raise api_error(403, 'EXAM_EXPIRED', '考试已结束')
    expires_at = _submission_expiry(exam, now)
    sub = ExamSubmission(exam_id=exam.id, student_id=student.id, status="started",
                         started_at=now, expires_at=expires_at, write_generation=1)
    db.add(sub)
    db.flush()
    # 批次三 3B：开始考试即冻结逐代码题的评分版本；
    # 之后创建/补建答案只从该映射复制，考试中途改题目配置不影响本场考试。
    from app.services.grading_version_service import freeze_exam_versions

    freeze_exam_versions(db, sub)
    # 乱序快照与评分版本/分值在同一事务写入并冻结：刷新与换机都不改变顺序。
    freeze_exam_shuffle(exam, sub, db, settings=settings)
    db.commit()
    db.refresh(sub)
    return sub


def save_answer(db, exam_id, question_id, student, payload, *, expected_write_generation,
                ip_hash=None, ua_hash=None):
    from app.services.time_utils import as_utc, utc_now
    exam = db.get(Exam, exam_id)
    if not exam:
        raise api_error(404, 'EXAM_NOT_FOUND', '考试不存在')
    require_student_task_write(db, exam, student)
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id, ExamSubmission.student_id == student.id)
        .with_for_update().execution_options(populate_existing=True))
    if not sub or sub.status != "started":
        raise api_error(403, "EXAM_NOT_STARTED", "考试未开始或已结束")
    if sub.write_generation != expected_write_generation:
        # 换机后旧设备的一切写入在此断绝：不写答案，只落一条 autosave_rejected 事件。
        reject_superseded_write(db, sub, source="save_answer")
    now = utc_now()
    if sub.expires_at and as_utc(sub.expires_at) <= now:
        _auto_submit(sub, db, now)
        raise api_error(403, "EXAM_EXPIRED", "考试已过期")
    q = db.get(ExamQuestion, question_id)
    if not q or q.exam_id != exam_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    ans = db.scalar(select(ExamAnswer).where(
        ExamAnswer.submission_id == sub.id, ExamAnswer.question_id == question_id)
        .with_for_update().execution_options(populate_existing=True))
    if not ans:
        expected_version = payload.get("expected_version")
        if expected_version not in (None, 0):
            raise api_error(409, "ANSWER_VERSION_CONFLICT", "答案版本已变化，请刷新后重试")
        ans = ExamAnswer(submission_id=sub.id, question_id=question_id)
        db.add(ans)
    else:
        expected_version = payload.get("expected_version")
        incoming_matches = (
            (q.question_type == "code" and ans.code_answer == payload.get("code_answer", ""))
            or (q.question_type == "fill_blank" and (ans.text_answers or {}) == (payload.get("text_answers") or {}))
            or (q.question_type in ("single_choice", "multi_choice", "true_false") and (ans.selected_options or []) == (payload.get("selected_options") or []))
        )
        if expected_version is not None and expected_version != ans.version:
            if incoming_matches:
                return ans
            raise api_error(409, "ANSWER_VERSION_CONFLICT", "答案已在其他页面更新，请刷新后重试")
    if q.question_type == "code":
        ans.code_answer = payload.get("code_answer", "")
    elif q.question_type == "fill_blank":
        raw_answers = payload.get("text_answers", {})
        configured_ids = {str(blank.get("id")) for blank in (q.correct_answer or {}).get("blanks", [])}
        ans.text_answers = {str(key): str(value) for key, value in raw_answers.items() if str(key) in configured_ids}
    else:
        ans.selected_options = payload.get("selected_options", [])
    if ans.id is not None:
        ans.version += 1
    else:
        # 批次三 3B：新答案落库后再绑定版本。
        # 必须放在版本号判定之后：既有语义用 ans.id is None 判断"首次保存"，
        # 提前 flush 会让首次保存被当作更新而多记一个版本号。
        db.flush()
        from app.services.grading_version_service import bind_exam_answer_version

        bind_exam_answer_version(db, ans)
    sub.last_saved_at = now
    db.commit()
    db.refresh(ans)
    return ans


def submit_exam(exam, student, db, *, expected_write_generation, ip_hash=None, ua_hash=None):
    """交卷：CAS 认领 → 评分准备 → 入队 Redis → 汇总成绩。

    幂等：已 submitted/grading/graded/review_required 的提交直接返回
    （review_required 不自动重试，只能走显式受控重试）。
    写入代次不匹配（换机后的旧设备）→ 409 DEVICE_SUPERSEDED，不改任何状态。
    """
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id, ExamSubmission.student_id == student.id))
    if sub and sub.status in _COMPLETED_SUBMISSION_STATUSES:
        return sub
    exam = require_student_task_write(db, exam, student)
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id, ExamSubmission.student_id == student.id)
        .with_for_update().execution_options(populate_existing=True))
    if not sub:
        raise api_error(403, 'EXAM_NOT_STARTED', '请先开始考试')
    if sub.status in _COMPLETED_SUBMISSION_STATUSES:
        return sub
    if sub.write_generation != expected_write_generation:
        reject_superseded_write(db, sub, source="submit")
    now = utc_now()

    # 截止时的交卷请求也必须幂等成功：服务端按超时原因收卷后直接返回
    # 当前提交，避免客户端在零秒不断重试并把成功状态误报为失败。
    if sub.status == "started" and sub.expires_at:
        if as_utc(sub.expires_at) <= now:
            _auto_submit(sub, db, now)
            db.refresh(sub)
            return sub

    sub, code_answers = _submit_and_prepare(sub, db, now, reason="manual")
    _enqueue_and_finalize(sub.id, code_answers, db)
    db.refresh(sub)
    return sub


def _prepare_answers(sub, db):
    """评分选择题、初始化代码题（有作答→pending 待入队；未作答→0 分 completed）。

    幂等：重复执行结果一致。配置错误（如缺 hidden_tests）交由 worker 永久错误路径
    处理（system_error + 父 review_required），绝不按 0 分结算。
    """
    questions = db.scalars(select(ExamQuestion).where(ExamQuestion.exam_id == sub.exam_id)).all()
    # A collector may have waited behind the student's parent-row lock after
    # caching an older answer. Grade the committed answer, not that RR snapshot.
    answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id)
                         .order_by(ExamAnswer.id).with_for_update()
                         .execution_options(populate_existing=True)).all()
    by_qid = {a.question_id: a for a in answers}
    code_answers = []

    from app.services.grading_version_service import frozen_points_for_question

    for q in questions:
        ans = by_qid.get(q.id)
        if not ans:
            ans = ExamAnswer(
                submission_id=sub.id, question_id=q.id, grading_status="completed",
                score=0.0,
            )
            db.add(ans)
        frozen_points = frozen_points_for_question(sub, q.id)
        if frozen_points is None:
            ans.score = None
            ans.grading_status = "system_error"
            ans.system_error = "考试开始时缺少可信分值快照，需教师人工复核"
            ans.finished_at = None
            continue
        if q.question_type in ("single_choice", "multi_choice", "true_false", "fill_blank"):
            # 客观题统一走 score_non_code_answer：判断题与填空题不再落到选择题分支。
            ans.score = score_non_code_answer(q, ans, points=frozen_points)
            ans.grading_status = "completed"
            ans.result_revision = (ans.result_revision or 0) + 1
        elif q.question_type == "code":
            if ans.code_answer and ans.code_answer.strip():
                ans.grading_status = "pending"
                code_answers.append((ans, q))
            else:
                ans.score = 0  # 未作答：正常 0 分
                ans.grading_status = "completed"
                ans.result_revision = (ans.result_revision or 0) + 1
    return code_answers


def _submit_and_prepare(sub, db, now, *, reason: str | None = None):
    """手动/自动交卷统一路径：CAS started→submitted → 评分准备 → CAS submitted→grading。

    短事务：认领、评分、父状态转换一次提交；返回 (sub, code_answers)。
    并发安全：条件 UPDATE 守卫，rowcount=0 表示已被并发实例处理。
    """
    if sub.status == "started":
        result = db.execute(
            update(ExamSubmission).execution_options(synchronize_session=False)
            .where(ExamSubmission.id == sub.id, ExamSubmission.status == "started")
            .values(status="submitted", submitted_at=now, submission_reason=reason or sub.submission_reason)
        )
        if result.rowcount == 0:
            db.rollback()
            return db.get(ExamSubmission, sub.id), []
        db.refresh(sub)  # 重新加载（db.get 会命中 identity map 返回旧状态对象）
    if sub.status != "submitted":
        return sub, []

    code_answers = _prepare_answers(sub, db)
    result = db.execute(
        update(ExamSubmission).execution_options(synchronize_session=False)
        .where(ExamSubmission.id == sub.id, ExamSubmission.status == "submitted")
        .values(status="grading")
    )
    if result.rowcount == 0:
        db.rollback()
        return db.get(ExamSubmission, sub.id), []
    db.commit()
    db.refresh(sub)
    return sub, code_answers


def _enqueue_and_finalize(submission_id, code_answers, db, metrics=None):
    """提交后统一入队 + 结构化汇总；metrics 为 None 时静默。"""
    from app.services.judge_queue import enqueue_job as _enq
    from app.services.exam_grading import finalize_if_ready
    for ans, _q in code_answers:
        _enq(db, job_type="exam", object_id=ans.id)
    r = finalize_if_ready(submission_id, db)
    if metrics is not None:
        metrics[r.outcome.value] += 1


def _auto_submit(sub, db, now, *, reason: str = "time_expired"):
    """自动交卷（调用方检测到已过期）：CAS 认领 → 准备 → grading → 入队 → 汇总"""
    sub, code_answers = _submit_and_prepare(sub, db, now, reason=reason)
    _enqueue_and_finalize(sub.id, code_answers, db)

def _finalize_grade(sub, score, db):
    """[已废弃] 直接写入成绩——请改用 exam_grading.finalize_if_ready。

    保留以兼容旧测试，新代码不应直接调用此函数。
    """
    sub.score = float(score)
    sub.status = "graded"
    from app.services.time_utils import utc_now
    sub.graded_at = utc_now()
    grade = db.scalar(select(ExamGrade).where(
        ExamGrade.exam_id == sub.exam_id, ExamGrade.student_id == sub.student_id))
    if grade:
        grade.score = float(score)
    else:
        db.add(ExamGrade(exam_id=sub.exam_id, student_id=sub.student_id, score=float(score)))


def retry_exam_submission(submission_id: int, answer_ids: list[int], actor, db):
    """显式重试 review_required 的考试提交（管理员/教师受控入口）。

    前置条件（全部满足才重置；配置缺失直接拒绝，避免第二轮无限失败）：
    - 父状态必须是 review_required
    - 每个选中答案必须是 system_error 且属于该提交
    - legacy 必须有 hidden_tests；shadow/active 必须有 test_groups 与锁定 Rubric

    只重置被选中的 system_error 答案：pending、attempt_count=0、清空队列/错误字段，
    score 保持 NULL。清空父 review 字段并转 grading；事务提交后再统一入队。
    """
    from app.services.time_utils import utc_now

    sub = db.scalar(
        select(ExamSubmission).where(ExamSubmission.id == submission_id).with_for_update()
    )
    if not sub:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "考试提交不存在")
    if sub.status != "review_required":
        raise api_error(409, "NOT_REVIEW_REQUIRED", "仅 review_required 状态可显式重试")

    if not answer_ids:
        raise api_error(422, "NO_ANSWERS", "至少选择一道需要重试的答案")

    # 逐题校验配置完整（代码题还需有效版本；全部题型都必须有开始考试时的分值快照）。
    from app.services.grading_version_service import frozen_points_for_question

    for aid in answer_ids:
        ans = db.get(ExamAnswer, aid)
        if not ans or ans.submission_id != submission_id:
            raise api_error(404, "ANSWER_NOT_FOUND", "答案不存在")
        if ans.grading_status != "system_error":
            raise api_error(409, "NOT_SYSTEM_ERROR", "仅 system_error 答案可重试")
        q = db.get(ExamQuestion, ans.question_id)
        if not q:
            raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
        if frozen_points_for_question(sub, q.id) is None:
            raise api_error(409, "FROZEN_POINTS_MISSING", "该考试提交缺少可信分值快照，需管理员修复基线后再重试")
        if q.question_type == "code":
            entry = (sub.grading_version_map or {}).get(str(q.id))
            version_id = ans.grading_version_id or (
                entry.get("version_id") if isinstance(entry, dict) else entry
            )
            version = db.get(JudgeGradingVersion, version_id) if version_id else None
            if (version is None
                    or (version.payload or {}).get("schema_version") != 2
                    or (version.payload or {}).get("execution_model") != "isolated_case_v2"):
                raise api_error(409, "GRADING_VERSION_REQUIRED", "缺少可信的逐例隔离评分版本，不能自动重试，请人工处理")
            if version.grading_mode_snapshot in ("active", "shadow"):
                rubric_snapshot = (version.context_snapshot or {}).get("rubric_snapshot")
                if not version.rubric_id or not isinstance(rubric_snapshot, dict) or not rubric_snapshot:
                    raise api_error(409, "RUBRIC_BASELINE_MISSING", "冻结 Rubric 基线不可用，不能自动重试，请人工处理")
            try:
                from app.services.data_grading import DataGradingConfigError, plan_cases

                plan_cases(version)
            except DataGradingConfigError as exc:
                raise api_error(409, "GRADING_BASELINE_INVALID", "冻结评分版本不可执行，请教师人工处理") from exc

    # 原子重置/重评选中的答案：
    # - code 题：重置为 pending（score 保持 NULL），等待重新判题
    # - 选择题：直接评分（与 _prepare_answers 一致），不入判题队列
    code_answer_ids = []
    for aid in answer_ids:
        ans = db.get(ExamAnswer, aid)
        q = db.get(ExamQuestion, ans.question_id)
        if q.question_type == "code":
            ans.grading_status = "pending"
            ans.attempt_count = 0
            ans.queued_at = None
            ans.started_at = None
            ans.finished_at = None
            ans.last_error = None
            ans.system_error = None
            ans.result_details = None
            ans.score = None  # 原始方案：选中的 system_error 答案清理 score=NULL，等判题后定分
            # 受控重试必须让旧认领失效：清空代次后，旧 Worker 的迟到写入一律被拒。
            ans.claim_token = None
            ans.heartbeat_at = None
            ans.result_revision = (ans.result_revision or 0) + 1
            code_answer_ids.append(aid)
        else:
            frozen_points = frozen_points_for_question(sub, q.id)
            ans.score = score_non_code_answer(q, ans, points=frozen_points)
            ans.grading_status = "completed"
            ans.attempt_count = 0
            ans.queued_at = None
            ans.started_at = None
            ans.finished_at = None
            ans.last_error = None
            ans.system_error = None
            ans.result_details = None
            ans.claim_token = None
            ans.heartbeat_at = None
            ans.result_revision = (ans.result_revision or 0) + 1

    # 清父 review 字段并转 grading
    sub.status = "grading"
    sub.review_reason = None
    sub.review_required_at = None

    db.commit()

    # 提交后再统一入队（仅 code 题；Redis 推送失败由 stale recovery 补偿）
    from app.services.judge_queue import enqueue_job
    for aid in code_answer_ids:
        enqueue_job(db, job_type="exam", object_id=aid)

    # 结构化收尾：立即触发 finalize（无 code 题或全部已定分时父级当场 graded/review_required，
    # 不等 scanner 的 5 分钟阈值）。finalize 自身幂等：仍有 pending/queued 答案时返回
    # WAITING 不提前汇总，无副作用。
    from app.services.exam_grading import finalize_if_ready
    finalize_if_ready(submission_id, db)
    db.refresh(sub)  # finalize 用 core UPDATE（expire_on_commit=False 时不同步会话），
                     # 重读父状态，保证返回对象与数据库一致（API 不会误报 status=grading）

    # 审计日志：只记 id/操作者，禁止泄露隐藏测试或学生代码
    logger.info(
        "exam_retry submission=%s answer_ids=%s actor=%s",
        submission_id, answer_ids, getattr(actor, "username", None),
    )
    return sub


def get_my_grade(exam_id, student, db):
    from app.models import CodeGrade
    exam = db.get(Exam, exam_id)
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam_id, ExamSubmission.student_id == student.id))
    if not sub:
        raise api_error(404, "SUBMISSION_NOT_FOUND", "未找到考试记录")
    answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id)).all()

    answer_list = []
    answers_visible = bool(exam and exam.review_released_at and exam.show_answers_after_review)
    for a in answers:
        # 安全返回：system_error 只暴露通用状态，绝不返回内部错误原文
        # （禁止泄露 hidden tests、学生代码、密钥、堆栈）
        item = {"question_id": a.question_id, "grading_status": a.grading_status,
                "score": a.score if answers_visible else None,
                "system_error": "评分遇到系统问题，请联系教师" if a.system_error else None}
        if answers_visible:
            item["manual_score_reason"] = a.manual_score_reason
            item["manual_score_at"] = a.manual_score_at
        # active 模式：返回安全的学生反馈（F/A/R/Q、扣分依据、测试结果、代码建议）
        # shadow 模式：不泄露 AI 数据，仅返回确定性分数
        cg = db.scalar(
            select(CodeGrade).where(
                CodeGrade.exam_answer_id == a.id,
                CodeGrade.mode == "active",
                CodeGrade.status == "completed",
            )
        )
        if answers_visible and cg and cg.ai_result:
            item["grading_breakdown"] = build_student_grading_breakdown(cg)
        answer_list.append(item)

    score_visible = bool(exam and exam.show_score_after_grading and sub.status == "graded")
    max_score = _frozen_exam_max_score(sub)
    return {"submission_id": sub.id, "status": sub.status, "score": sub.score if score_visible else None,
            "score_visible": score_visible, "max_score": max_score,
            "started_at": sub.started_at.isoformat() if sub.started_at else None,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
            "review_reason": sub.review_reason,
            "review_required_at": sub.review_required_at.isoformat() if sub.review_required_at else None,
            "answers": answer_list}


def exam_max_score(exam_id: int, db: Session) -> float:
    return float(db.scalar(select(func.sum(ExamQuestion.points)).where(ExamQuestion.exam_id == exam_id)) or 0)


def _frozen_exam_max_score(submission: ExamSubmission | None) -> float | None:
    """Return a complete frozen maximum; never substitute today's question points."""
    mapping = submission.grading_version_map if submission else None
    if not isinstance(mapping, dict) or not mapping:
        return None
    points = []
    for entry in mapping.values():
        if not isinstance(entry, dict) or entry.get("points") is None:
            return None
        try:
            points.append(float(entry["points"]))
        except (TypeError, ValueError):
            return None
    return sum(points)


def student_exam_status(exam: Exam, submission: ExamSubmission | None, now=None) -> tuple[str, bool, bool]:
    """返回 (student_status, is_completed, can_start)，只依赖服务器时间。"""
    from app.services.time_utils import as_utc, utc_now
    now = now or utc_now()
    if submission:
        # 防御异常数据（如 Demo 种子曾把未来考试写成 started）：作答尚未真正开始
        # 前，不得暴露为 in_progress，否则倒计时会按 expires_at 显示 817:xx:xx。
        if submission.status == "started":
            started_at = as_utc(submission.started_at)
            # MySQL 的 DATETIME(0) 会把微秒四舍五入到下一秒（.7 → 下一秒），刚开考的
            # started_at 回读后可能比 now 晚不到 1 秒。留出存储精度容差，否则开考瞬间
            # 会被误判成「尚未开始」而不下发题目；真正的未来考试（Demo 种子）仍被拦下。
            if started_at is not None and now < started_at - timedelta(seconds=1):
                return "scheduled", False, False
        mapping = {
            "started": "in_progress",
            "submitted": "submitted",
            "grading": "grading",
            "graded": "graded",
            "review_required": "review_required",
        }
        status = mapping.get(submission.status, submission.status)
        return status, submission.status in _COMPLETED_SUBMISSION_STATUSES, False
    start_at = as_utc(exam.start_at)
    latest_start_at = as_utc(exam.end_at)
    if start_at and now < start_at:
        return "scheduled", False, False
    if latest_start_at and now >= latest_start_at:
        return "missed", False, False
    return "ready", False, exam.status == "published"


def build_student_exam_summary(
    exam: Exam,
    submission: ExamSubmission | None,
    db: Session,
    now=None,
    max_scores: dict[int, float] | None = None,
) -> dict:
    """max_scores 提供批量预聚合的最高分（TASK-022），避免逐考试查询；未提供时回退单查。"""
    from app.services.time_utils import utc_now
    now = now or utc_now()
    status, is_completed, can_start = student_exam_status(exam, submission, now)
    if max_scores is not None:
        max_score = float(max_scores.get(exam.id, 0.0))
    else:
        max_score = exam_max_score(exam.id, db)
    if submission is not None:
        max_score = _frozen_exam_max_score(submission)
    return {
        "id": exam.id,
        "course_id": exam.course_id,
        "title": exam.title,
        "status": exam.status,
        "duration_minutes": exam.duration_minutes,
        "exam_kind": exam.exam_kind or "quiz",
        "timing_mode": exam.timing_mode or "unified",
        "shuffle_questions": bool(exam.shuffle_questions),
        "shuffle_options": bool(exam.shuffle_options),
        "start_at": exam.start_at,
        "end_at": exam.end_at,
        "student_status": status,
        "is_completed": is_completed,
        "is_submitted": is_completed,
        "can_start": can_start,
        "max_score": max_score,
        "server_now": now,
        "attempt_expires_at": submission.expires_at if submission and submission.status == "started" else None,
        "review_released_at": exam.review_released_at,
    }


def _student_question_payload(question: ExamQuestion, *, include_correct_answers: bool = False,
                              points: float | None | object = _LIVE_POINTS) -> dict:
    payload = {
        "id": question.id,
        "exam_id": question.exam_id,
        "question_type": question.question_type,
        "prompt": question.prompt,
        "options": question.options,
        "points": float(question.points) if points is _LIVE_POINTS else points,
        "order_index": question.order_index,
        "starter_code": question.starter_code,
        "public_cases": question.public_cases,
    }
    if include_correct_answers:
        if question.question_type == "code":
            # 参考实现是讲评页唯一需要的编程题答案；旧版
            # correct_answer 可能包含评测代码，不能原样暴露给学生端。
            payload["reference_solution"] = question.reference_solution
        else:
            payload["correct_answer"] = question.correct_answer or {}
    return payload


def _saved_answer_payload(answer: ExamAnswer, *, include_score: bool = False) -> dict:
    payload = {
        "id": answer.id,
        "question_id": answer.question_id,
        "selected_options": answer.selected_options,
        "code_answer": answer.code_answer,
        "text_answers": answer.text_answers,
        "version": answer.version,
        "saved_at": answer.updated_at,
    }
    if include_score:
        payload["score"] = answer.score
        payload["grading_status"] = answer.grading_status
        payload["manual_score_reason"] = answer.manual_score_reason
        payload["manual_score_at"] = answer.manual_score_at
    return payload


def build_student_exam_session(exam: Exam, student: User, db: Session) -> dict:
    """统一构建候考、作答恢复与结果页会话；任何正确答案公开都在此处集中控制。"""
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    submission = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id,
        ExamSubmission.student_id == student.id,
    ))
    if submission and submission.status == "started" and submission.expires_at and as_utc(submission.expires_at) <= now:
        try:
            require_student_task_write(db, exam, student)
        except HTTPException as exc:
            if not isinstance(exc.detail, dict) or exc.detail.get('code') != 'OFFERING_READ_ONLY':
                raise
            # Legacy inconsistent attempts in a closed context are recovered
            # by the system scanner; a student's GET remains strictly read-only.
            db.rollback()
        else:
            submission = db.scalar(select(ExamSubmission).where(ExamSubmission.id == submission.id)
                                   .with_for_update().execution_options(populate_existing=True))
            if (submission.status == 'started' and submission.expires_at
                    and as_utc(submission.expires_at) <= utc_now()):
                _auto_submit(submission, db, utc_now())
                db.refresh(submission)

    summary = build_student_exam_summary(exam, submission, db, now)
    score_visible = bool(submission and submission.status == "graded" and exam.show_score_after_grading)
    questions_visible = bool(exam.review_released_at and exam.show_questions_after_review)
    answers_visible = bool(questions_visible and exam.show_answers_after_review)
    student_status, _is_completed, _can_start = student_exam_status(exam, submission, now)
    active = bool(submission and student_status == "in_progress")
    include_questions = active or questions_visible
    questions = list_questions(db, exam.id) if include_questions else []
    saved_answers = []
    if submission and include_questions:
        answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == submission.id)).all()
        saved_answers = [_saved_answer_payload(answer, include_score=score_visible) for answer in answers]

    submission_payload = None
    if submission:
        submission_payload = {
            "id": submission.id,
            "exam_id": submission.exam_id,
            "student_id": submission.student_id,
            "status": submission.status,
            "score": submission.score if score_visible else None,
            "score_visible": score_visible,
            # MySQL 的 DATETIME 会丢失 tzinfo；API 边界必须恢复 UTC 标识，
            # 否则浏览器会按本地时区解析并导致倒计时提前归零。
            "started_at": as_utc(submission.started_at),
            "expires_at": as_utc(submission.expires_at),
            "last_saved_at": as_utc(submission.last_saved_at),
            "submitted_at": as_utc(submission.submitted_at),
            "submission_reason": submission.submission_reason,
            "review_reason": submission.review_reason,
            # 客户端后续每次写入都必须回传该代次；换机接管后旧设备将被 409 拒绝。
            "write_generation": int(submission.write_generation or 1),
        }
    from app.services.grading_version_service import frozen_points_for_question

    student_questions = [
        _student_question_payload(
            q, include_correct_answers=answers_visible,
            points=(frozen_points_for_question(submission, q.id)
                    if submission is not None else _LIVE_POINTS),
        )
        for q in questions
    ]
    student_questions = _apply_frozen_shuffle(submission, student_questions)
    return {
        "exam": summary,
        "submission": submission_payload,
        "questions": student_questions,
        "saved_answers": saved_answers,
        "visibility": {
            "score": score_visible,
            "questions": questions_visible,
            "answers": answers_visible,
            "review_released": bool(exam.review_released_at),
        },
        "server_now": now,
    }


def _apply_frozen_shuffle(submission: ExamSubmission | None, payloads: list[dict]) -> list[dict]:
    """展示顺序一律以提交里冻结的快照为准：刷新、换设备、重算都不会改变顺序。

    答案始终按原题目 id 与原选项键存储，所以只调整展示顺序，不影响评分与讲评。
    """
    if submission is None or not payloads:
        return payloads
    by_key = {str(item["id"]): item for item in payloads}
    ordered: list[dict] = []
    for question_id in (submission.question_order or []):
        item = by_key.pop(str(question_id), None)
        if item is not None:
            ordered.append(item)
    # 教师在本场考试开始后新增的题目没有冻结位置：按原顺序补在末尾，绝不丢题。
    ordered.extend(item for item in payloads if str(item["id"]) in by_key)
    option_order = submission.option_order or {}
    for item in ordered:
        keys = option_order.get(str(item["id"]))
        options = item.get("options")
        if not keys or not isinstance(options, dict):
            continue
        shuffled = {key: options[key] for key in keys if key in options}
        for key, value in options.items():
            shuffled.setdefault(key, value)
        item["options"] = shuffled
    return ordered


def release_exam_review(exam: Exam, actor: User, db: Session) -> Exam:
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    if not exam.end_at or now < as_utc(exam.end_at):
        raise api_error(409, "REVIEW_TOO_EARLY", "最晚进入时间尚未结束，不能发布讲评")
    active_rows = db.scalars(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id,
        ExamSubmission.status == "started",
    )).all()
    for submission in active_rows:
        if submission.expires_at and as_utc(submission.expires_at) <= now:
            _auto_submit(submission, db, now)
    active = db.scalar(select(ExamSubmission.id).where(
        ExamSubmission.exam_id == exam.id,
        ExamSubmission.status == "started",
    ).limit(1))
    if active is not None:
        raise api_error(409, "ACTIVE_EXAMS_EXIST", "仍有学生正在考试，不能发布讲评")
    exam.review_released_at = now
    exam.review_released_by_id = actor.id
    db.commit()
    db.refresh(exam)
    logger.info("exam_review_released exam=%s actor=%s", exam.id, actor.id)
    return exam


def extend_exam_submission(submission: ExamSubmission, minutes: int, actor: User, db: Session,
                           *, settings=None) -> ExamSubmission:
    """教师延时——明确、留痕的例外：写 AuditEvent + exam_events，且累计不得超过配置上限。"""
    from app.services.time_utils import as_utc, utc_now
    settings = settings or get_settings()
    cap = int(getattr(settings, "exam_extension_max_minutes", 60))
    now = utc_now()
    if submission.status != "started":
        raise api_error(409, "SUBMISSION_NOT_ACTIVE", "仅进行中的考试可以延长时间")
    if not submission.expires_at or as_utc(submission.expires_at) <= now:
        _auto_submit(submission, db, now)
        raise api_error(409, "EXAM_EXPIRED", "考试已经到期，无法延长")
    # 累计值以事件表为准（可审计、可回溯），反复调用同一入口无法绕过上限。
    prior = db.scalars(select(ExamEvent).where(
        ExamEvent.submission_id == submission.id, ExamEvent.kind == "warning")).all()
    used = sum(
        int((event.payload or {}).get("minutes") or 0) for event in prior
        if (event.payload or {}).get("action") == "extend"
    )
    if used + minutes > cap:
        raise api_error(422, "EXTENSION_LIMIT_EXCEEDED",
                        f"累计延长不得超过 {cap} 分钟（已使用 {used} 分钟）")
    submission.expires_at = as_utc(submission.expires_at) + timedelta(minutes=minutes)
    db.add(ExamEvent(
        exam_id=submission.exam_id, submission_id=submission.id, kind="warning",
        payload={"action": "extend", "minutes": minutes, "total_extended_minutes": used + minutes,
                 "expires_at": submission.expires_at.isoformat()},
        client_ts=None, server_ts=now, dedupe_key=None,
    ))
    db.add(AuditEvent(
        actor_id=actor.id, action="exam.extend_submission", target_type="exam_submission",
        target_id=str(submission.id),
        reason=f"教师延时 +{minutes} 分钟（累计 {used + minutes}/{cap}）",
        changes={"minutes": minutes, "total_extended_minutes": used + minutes, "exam_id": submission.exam_id},
    ))
    db.commit()
    db.refresh(submission)
    logger.info("exam_time_extended submission=%s minutes=%s actor=%s", submission.id, minutes, actor.id)
    return submission


def takeover_exam_session(db, exam: Exam, student: User, *, expected_write_generation: int,
                          device_id: str | None = None, ip_hash=None, ua_hash=None) -> dict:
    """换机接管：二次验密后提升写入代次，旧设备立即失去写入权限，剩余时间不重置。

    代次 CAS 保证两设备并发接管只有一个成功：行锁串行化后，第二个请求的
    expected_write_generation 已过期，直接 409 DEVICE_SUPERSEDED。
    """
    from app.services.time_utils import as_utc, utc_now
    now = utc_now()
    sub = db.scalar(select(ExamSubmission).where(
        ExamSubmission.exam_id == exam.id, ExamSubmission.student_id == student.id)
        .with_for_update().execution_options(populate_existing=True))
    if not sub:
        raise api_error(403, "EXAM_NOT_STARTED", "请先开始考试")
    if sub.status != "started":
        raise api_error(409, "SUBMISSION_NOT_ACTIVE", "考试已交卷，不能换机接管")
    if sub.expires_at and as_utc(sub.expires_at) <= now:
        _auto_submit(sub, db, now)
        raise api_error(403, "EXAM_EXPIRED", "考试已过期，不能换机接管")

    device_token = secrets.token_urlsafe(32)
    result = db.execute(
        update(ExamSubmission)
        .execution_options(synchronize_session=False)
        .where(
            ExamSubmission.id == sub.id,
            ExamSubmission.status == "started",
            ExamSubmission.write_generation == expected_write_generation,
        )
        .values(
            write_generation=ExamSubmission.write_generation + 1,
            active_device_id=device_id,
            active_device_token_hash=hashlib.sha256(device_token.encode("utf-8")).hexdigest(),
        )
    )
    if result.rowcount != 1:
        db.rollback()
        current = db.get(ExamSubmission, sub.id)
        reject_superseded_write(db, current, source="takeover")
    db.commit()
    db.refresh(sub)
    generation = int(sub.write_generation)
    _append_exam_events(db, sub, [{
        "kind": "device_switch", "client_ts": None, "duration_ms": None,
        "payload": {"generation": generation, "device_id": device_id},
        "dedupe_key": f"device_switch:{generation}",
    }], ip_hash=ip_hash, ua_hash=ua_hash)
    db.commit()
    db.refresh(sub)
    answers = db.scalars(select(ExamAnswer).where(ExamAnswer.submission_id == sub.id)).all()
    return {
        "submission": {
            "id": sub.id,
            "exam_id": sub.exam_id,
            "status": sub.status,
            "started_at": as_utc(sub.started_at),
            "expires_at": as_utc(sub.expires_at),
            "last_saved_at": as_utc(sub.last_saved_at),
            "write_generation": generation,
            "device_token": device_token,
            "device_id": device_id,
        },
        "saved_answers": [_saved_answer_payload(answer) for answer in answers],
        "server_now": now,
    }


def force_submit_exam_submission(submission: ExamSubmission, actor: User, db: Session) -> ExamSubmission:
    from app.services.time_utils import utc_now
    if submission.status != "started":
        return submission
    _auto_submit(submission, db, utc_now(), reason="teacher_forced")
    db.refresh(submission)
    logger.info("exam_force_submitted submission=%s actor=%s", submission.id, actor.id)
    return submission


def build_proctor_overview(exam: Exam, db: Session, *, limit: int = 200,
                           student_id: int | None = None) -> dict:
    """教师只读监考视图：切屏次数、累计离开时长、换机次数、最后保存时间与事件时间线。

    只读：不做任何写操作，既不自动交卷也不扣分——异常记录只供教师了解考试过程。
    """
    from app.services.time_utils import utc_now
    limit = max(1, min(int(limit), 500))
    rows = db.execute(
        select(ExamSubmission, User)
        .join(User, User.id == ExamSubmission.student_id)
        .where(ExamSubmission.exam_id == exam.id)
        .order_by(ExamSubmission.id)
    ).all()
    students = []
    for submission, user in rows:
        students.append({
            "submission_id": submission.id,
            "student_id": submission.student_id,
            "username": user.username,
            "display_name": user.real_name or user.username,
            "status": submission.status,
            "started_at": submission.started_at,
            "expires_at": submission.expires_at,
            "submitted_at": submission.submitted_at,
            "last_saved_at": submission.last_saved_at,
            "blur_count": int(submission.blur_count or 0),
            "away_total_ms": int(submission.away_total_ms or 0),
            "device_switch_count": int(submission.device_switch_count or 0),
            "write_generation": int(submission.write_generation or 1),
            "score": submission.score,
        })
    event_query = select(ExamEvent).where(ExamEvent.exam_id == exam.id)
    if student_id is not None:
        event_query = event_query.join(
            ExamSubmission, ExamSubmission.id == ExamEvent.submission_id,
        ).where(ExamSubmission.student_id == student_id)
    events = db.scalars(
        event_query.order_by(ExamEvent.server_ts.desc(), ExamEvent.id.desc()).limit(limit)
    ).all()
    student_by_submission = {row["submission_id"]: row["student_id"] for row in students}
    return {
        "exam": {
            "id": exam.id, "title": exam.title, "status": exam.status,
            "exam_kind": exam.exam_kind or "quiz", "timing_mode": exam.timing_mode or "unified",
            "duration_minutes": exam.duration_minutes, "start_at": exam.start_at, "end_at": exam.end_at,
            "shuffle_questions": bool(exam.shuffle_questions),
            "shuffle_options": bool(exam.shuffle_options),
        },
        "students": students,
        "events": [{
            "id": event.id, "submission_id": event.submission_id,
            "student_id": student_by_submission.get(event.submission_id),
            "kind": event.kind, "payload": event.payload or {}, "client_ts": event.client_ts,
            "server_ts": event.server_ts, "duration_ms": event.duration_ms,
        } for event in events],
        "totals": {
            "students": len(students),
            "in_progress": sum(1 for row in students if row["status"] == "started"),
            "blur_count": sum(row["blur_count"] for row in students),
            "away_total_ms": sum(row["away_total_ms"] for row in students),
            "device_switch_count": sum(row["device_switch_count"] for row in students),
        },
        "server_now": utc_now(),
    }


def scan_expired_exams(db, now) -> dict:
    """扫描：过期自动交卷 + submitted 崩溃恢复 + grading finalize（多实例 CAS 安全）。

    职责边界：只做 exam expiry/finalization 扫描，不做 judge recovery
    （judge/AI stale recovery 由 Judge Worker 在 grading-recovery 租约下执行）。

    返回真实转换计数（不含候选数）：
    {"expired_claimed": N, "auto_submitted": N, "submitted_resumed": N,
     "graded": N, "review_required": N, "waiting": N, "noop": N}
    """
    from datetime import timedelta as _td
    from app.services.time_utils import as_utc
    from app.services.exam_grading import finalize_if_ready

    metrics = {"expired_claimed": 0, "auto_submitted": 0, "submitted_resumed": 0,
               "graded": 0, "review_required": 0, "waiting": 0, "noop": 0}

    # 1. 过期认领：started + 已过期 → 条件 UPDATE 转 submitted（带过期阈值，双实例只一个成功）
    expired = db.scalars(select(ExamSubmission).where(
        ExamSubmission.status == "started",
        ExamSubmission.expires_at <= now)).all()
    claimed_ids = set()
    for sub in expired:
        result = db.execute(
            update(ExamSubmission).execution_options(synchronize_session=False)
            .where(
                ExamSubmission.id == sub.id,
                ExamSubmission.status == "started",
                ExamSubmission.expires_at <= now,
            )
            .values(status="submitted", submitted_at=now, submission_reason="time_expired")
        )
        if result.rowcount == 0:
            db.rollback()
            continue  # 已被并发实例认领
        db.commit()
        metrics["expired_claimed"] += 1
        claimed_ids.add(sub.id)

    # 2. submitted 处理：本轮认领的 + 崩溃恢复（submitted_at 超时）→ 评分准备 + CAS grading
    resumed_deadline = now - _td(minutes=5)
    submitted_rows = db.scalars(select(ExamSubmission).where(
        ExamSubmission.status == "submitted")).all()
    for sub in submitted_rows:
        is_new = sub.id in claimed_ids
        if not is_new and (sub.submitted_at is None or as_utc(sub.submitted_at) >= resumed_deadline):
            continue  # 非本轮认领且未超时：仍在处理中，不打扰
        code_answers = _prepare_answers(sub, db)
        result = db.execute(
            update(ExamSubmission).execution_options(synchronize_session=False)
            .where(ExamSubmission.id == sub.id, ExamSubmission.status == "submitted")
            .values(status="grading")
        )
        if result.rowcount == 0:
            db.rollback()
            continue  # 已被并发实例处理
        db.commit()
        metrics["auto_submitted" if is_new else "submitted_resumed"] += 1
        _enqueue_and_finalize(sub.id, code_answers, db, metrics)

    # 3. grading 超时 → 结构化汇总（真实转换计数，waiting 只 debug/限频）
    stuck_deadline = now - _td(minutes=5)
    stuck = db.scalars(select(ExamSubmission).where(
        ExamSubmission.status == "grading",
        ExamSubmission.submitted_at < stuck_deadline,
    )).all()
    for sub in stuck:
        r = finalize_if_ready(sub.id, db)
        metrics[r.outcome.value] += 1

    return metrics
def create_question(db, exam_id, payload, user):
    exam = db.get(Exam, exam_id)
    if not exam:
        raise api_error(404, "EXAM_NOT_FOUND", "考试不存在")
    require_exam_editable(exam, user)
    q = ExamQuestion(exam_id=exam_id, **payload)
    requested_mode = payload.get("grading_mode")
    if q.question_type == "code":
        # JSON null 与未提供字段语义一致：新建编程题默认进入 active。
        if requested_mode is None:
            q.grading_mode = "active"
    else:
        # 选择题只能使用确定性的 legacy 评分。
        if requested_mode not in (None, "legacy"):
            raise api_error(422, "CHOICE_LEGACY_ONLY", "选择题只支持 legacy 模式")
        q.grading_mode = "legacy"

    # 创建时立即校验（新题追加到末尾，展示题号 = 现有题数 + 1）
    position = db.scalar(select(func.count()).where(ExamQuestion.exam_id == exam_id)) or 0
    errors = validate_question(q, number=position + 1)
    if errors:
        raise api_error(422, "QUESTION_INVALID", "题目校验失败：" + "; ".join(errors))

    db.add(q)
    db.commit()
    db.refresh(q)
    return q

def list_questions(db, exam_id):
    return list(db.scalars(select(ExamQuestion).where(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.order_index)))

def get_question(db, exam_id, question_id):
    q = db.get(ExamQuestion, question_id)
    if not q or q.exam_id != exam_id:
        raise api_error(404, "QUESTION_NOT_FOUND", "题目不存在")
    return q

def update_question(db, exam_id, question_id, payload, user):
    exam = db.get(Exam, exam_id)
    require_exam_editable(exam, user)
    q = get_question(db, exam_id, question_id)
    for key, value in payload.items():
        setattr(q, key, value)

    if q.question_type != "code":
        q.grading_mode = "legacy"

    # 更新后校验（按排序位置计算展示题号，删除造成的 order_index 空洞不影响编号）
    position = db.scalar(
        select(func.count()).where(
            ExamQuestion.exam_id == exam_id,
            ExamQuestion.id != question_id,
            ExamQuestion.order_index < q.order_index,
        )
    ) or 0
    errors = validate_question(q, number=position + 1)
    if errors:
        raise api_error(422, "QUESTION_INVALID", "题目校验失败：" + "; ".join(errors))

    db.commit()
    db.refresh(q)
    return q

def delete_question(db, exam_id, question_id, user):
    exam = db.get(Exam, exam_id)
    require_exam_editable(exam, user)
    q = get_question(db, exam_id, question_id)
    db.delete(q)
    db.flush()
    # 重新收拢 order_index（0 起始连续），避免删除后题号与展示序号错位
    remaining = db.scalars(
        select(ExamQuestion).where(ExamQuestion.exam_id == exam_id)
        .order_by(ExamQuestion.order_index, ExamQuestion.id)
    ).all()
    for index, row in enumerate(remaining):
        if row.order_index != index:
            row.order_index = index
    db.commit()
