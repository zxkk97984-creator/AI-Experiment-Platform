"""练习判题 worker（批次 5）：``python -m app.worker.practice_worker``。

职责与容量边界：

* 只消费 ``practice:judge:queue`` 与 ``practice:ai:queue``，**绝不**监听
  ``judge:queue`` / ``judge:ai:queue``——正式作业与考试容量不受练习积压影响；
* 并发上限来自 ``practice_settings.max_concurrent_practice_jobs``（管理员可配），
  超额任务留在库内 ``queued``，学生看到「排队中（第 N 位）」；
* MySQL 是唯一事实源：认领、结果写入、失败都走 ``services/practice_queue`` 的
  认领栅栏；Redis 只做唤醒，故障时轮询 + 恢复扫描照常工作。
"""
from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import redis
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import JudgeGradingVersion
from app.models.practice import PracticeProblem, PracticeSubmission, PracticeSettings
from app.services import practice_queue
from app.services.data_grading import DataGradingConfigError, plan_cases, write_runner_files

logger = logging.getLogger("dai.practice_worker")


def _docker_runner():
    """间接层：真实容器执行器（测试可替换为本地桩，避免依赖 Docker）。"""
    from app.worker.judge_worker import _run_docker_data_runner

    return _run_docker_data_runner


def _work_root(settings: Settings) -> Path:
    base = (Path(settings.judge_work_dir).resolve() if settings.judge_work_dir
            else settings.studio_storage_path.parent / "judge-work")
    return base / "practice"


def worker_queue_names(settings: Settings) -> tuple[str, ...]:
    """本 worker 独占消费的队列：与正式判题队列零交集（可被测试直接断言）。"""
    return (settings.practice_judge_queue_name, settings.practice_ai_queue_name)


def _concurrency(db) -> int:
    row = db.get(PracticeSettings, 1)
    return max(1, int(row.max_concurrent_practice_jobs)) if row is not None else 1


def _case_timeout_seconds(problem: PracticeProblem) -> int:
    return max(1, int(math.ceil((problem.time_limit_ms or 10000) / 1000.0)))


def _memory_limit_mb(problem: PracticeProblem) -> int:
    return max(64, int((problem.memory_limit_kb or 262144) // 1024))


# ─────────────────────────────────────────────────────────────
# 确定性判题
# ─────────────────────────────────────────────────────────────

def grade_submission(session_factory, submission_id: int, settings: Settings,
                     redis_client) -> None:
    """单次练习判题：认领 → 容器执行 → 按 mode 落确定性分 / 入队 AI。"""
    with session_factory() as db:
        token = practice_queue.claim_submission(db, submission_id)
        if token is None:
            return
        submission = db.get(PracticeSubmission, submission_id)
        if submission is None:
            return
        try:
            _grade_claimed(db, submission, settings, redis_client)
        except Exception as exc:  # noqa: BLE001 - 未知异常也必须落库并留痕
            logger.exception("练习判题异常: id=%s", submission_id)
            db.rollback()
            practice_queue.fail_submission(
                db, redis_client, submission_id, f"判题异常: {exc}", retryable=True)


def _grade_claimed(db, submission: PracticeSubmission, settings: Settings,
                   redis_client) -> None:
    problem = db.get(PracticeProblem, submission.problem_id)
    version = (db.get(JudgeGradingVersion, submission.grading_version_id)
               if submission.grading_version_id else None)
    if problem is None or version is None or version.practice_problem_id != submission.problem_id:
        # 配置缺失是平台问题：转终态，绝不折算成学生 0 分
        practice_queue.fail_submission(
            db, redis_client, submission.id, "题目或评分版本不存在", retryable=False)
        return

    try:
        container_payload, expectations = plan_cases(version)
    except DataGradingConfigError as exc:
        practice_queue.fail_submission(
            db, redis_client, submission.id, f"评分版本配置错误: {exc}", retryable=False)
        return

    with TemporaryDirectory(prefix="practice-") as tmp:
        workdir = Path(tmp)
        write_runner_files(workdir, entry_function=version.entry_function,
                           container_payload=container_payload,
                           student_code=submission.code)
        from app.services.grading_runner import build_case_stream

        try:
            stdout, stderr, returncode, elapsed = _docker_runner()(
                workdir, settings, _case_timeout_seconds(problem),
                _memory_limit_mb(problem),
                host_workdir=None, image_ref=settings.judge_image,
                cases=build_case_stream(container_payload),
                heartbeat_callback=lambda: practice_queue.heartbeat_submission(
                    db, submission.id),
            )
        except Exception as exc:  # noqa: BLE001 - Docker/基础设施异常可重试
            practice_queue.fail_submission(
                db, redis_client, submission.id, f"Docker 判题失败: {exc}", retryable=True)
            return

    # 结果帧走 stderr（签名协议通道）；stdout 只作学生输出留档
    from app.services.data_grading import grade_output

    graded = grade_output(stderr, expectations, entry_function=version.entry_function,
                          diagnostic_text=stdout)
    if returncode != 0:
        # 超时/容器异常：可重试；达上限后转 review_required（不写 0 分）
        practice_queue.fail_submission(
            db, redis_client, submission.id,
            f"runner 退出码 {returncode}: {stderr[-500:]}", retryable=True)
        return

    base_values = {
        "functional_score": graded.get("f_score"),
        "robustness_score": graded.get("r_score"),
        "tests_passed": graded.get("tests_passed"),
        "tests_total": graded.get("tests_total"),
        "result_details": {
            "groups": graded["groups"],
            "platform_errors": graded["platform_errors"],
            "grading_version_id": version.id,
        },
        "stdout": stdout[-8000:],
        "stderr": stderr[-8000:],
        "execution_time_ms": elapsed,
    }
    if graded["needs_review"]:
        # 平台问题（结果不可验证/缺帧）：终态但不计分，等待管理员修正配置
        base_values.update({
            "grading_status": "review_required",
            "score": None,
            "deterministic_score": None,
            "last_error": "判题结果不可验证: " + "; ".join(graded["platform_errors"][:3]),
        })
        practice_queue.finish_submission(db, submission.id, base_values)
        return

    passed = int(graded["tests_passed"] or 0)
    total = int(graded["tests_total"] or 0)
    if submission.grading_mode == "deterministic":
        score = round(100.0 * passed / total, 4) if total else None
        base_values.update({
            "grading_status": "completed",
            "score": score,
            "deterministic_score": score,
            "last_error": None,
        })
        practice_queue.finish_submission(db, submission.id, base_values)
        _update_problem_counters(db, problem.id, solved=bool(score is not None
                                                            and score > 0))
        return

    _start_ai_phase(db, submission, version, problem, base_values, passed, total)


def _start_ai_phase(db, submission: PracticeSubmission, version: JudgeGradingVersion,
                    problem: PracticeProblem, base_values: dict, passed: int,
                    total: int) -> None:
    """AI 模式：先把确定性分落库（保留认领），再建 CodeGrade 并入队 AI 队列。"""
    from app.models import CodeGrade

    f_score = float(base_values.get("functional_score") or 0.0)
    r_score = float(base_values.get("robustness_score") or 0.0)
    deterministic_score = round(f_score + r_score, 4)
    values = {**base_values,
              "deterministic_score": deterministic_score,
              # 判题仍在进行（AI 未完成）：分数先留空，绝不展示未完成的 AI 成绩
              "grading_status": "running",
              "score": None}
    if not practice_queue.apply_guarded_update(db, submission.id, values,
                                              release_claim=False,
                                              stamp_finished=False):
        return
    revision = db.get(PracticeSubmission, submission.id).result_revision

    grade = CodeGrade(
        practice_submission_id=submission.id,
        rubric_id=version.rubric_id,
        mode="active",
        status="pending",
        functional_score=f_score,
        robustness_score=r_score,
        deterministic_details={
            "groups": base_values["result_details"]["groups"],
            "grading_version_id": version.id,
            # retry/regrade 的来源校验读这两个字段（与作业/考试同一契约）
            "source": {"result_revision": revision, "grading_version_id": version.id},
        },
    )
    db.add(grade)
    db.flush()
    if not practice_queue.apply_guarded_update(
            db, submission.id, {"code_grade_id": grade.id},
            release_claim=True, stamp_finished=False, bump_revision=False):
        return
    _update_problem_counters(db, problem.id, solved=False)

    from app.services.ai_grading_queue import enqueue_ai_grade

    enqueue_ai_grade(db, _get_redis(), grade.id)


def _update_problem_counters(db, problem_id: int, *, solved: bool) -> None:
    """distinct_solvers 只在首次通过时 +1（幂等：以已存在的通过记录判据）。"""
    from sqlalchemy import func, update

    if not solved:
        db.commit()
        return
    passed_before = db.scalar(
        select(func.count()).select_from(PracticeSubmission).where(
            PracticeSubmission.problem_id == problem_id,
            PracticeSubmission.grading_status == "completed",
            PracticeSubmission.score.is_not(None),
            PracticeSubmission.score >= 60.0,
        )
    ) or 0
    if passed_before <= 1:
        db.execute(update(PracticeProblem).where(PracticeProblem.id == problem_id)
                   .values(distinct_solvers=PracticeProblem.distinct_solvers + 1))
    db.commit()


def _get_redis():
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=False,
                                socket_connect_timeout=2, socket_timeout=3)


# ─────────────────────────────────────────────────────────────
# AI 评分（独立队列，正式 judge:ai:queue 不受影响）
# ─────────────────────────────────────────────────────────────

def process_ai_grade_message(session_factory, settings: Settings, code_grade_id: int) -> None:
    from app.worker.judge_worker import process_ai_grade

    totals, sink = None, None
    from app.services.practice_service import ai_token_sink

    totals, sink = ai_token_sink()
    with session_factory() as db:
        submission_id = None
        from app.models import CodeGrade

        cg = db.get(CodeGrade, code_grade_id)
        if cg is not None:
            submission_id = cg.practice_submission_id
        process_ai_grade(db, _get_redis(), settings, code_grade_id, metrics_sink=sink)
        if submission_id is not None:
            reserved = 0
            from app.services.practice_service import settings_row

            row = settings_row(db)
            reserved = 0
            if row is not None:
                reserved = int(settings.practice_ai_token_estimate)
            actual = (totals or {}).get("prompt_tokens", 0) + (totals or {}).get(
                "completion_tokens", 0)
            if actual:
                from app.services.practice_service import settle_ai_usage

                settle_ai_usage(db, reserved_tokens=reserved, actual_tokens=actual)
            from app.services.practice_service import reconcile_after_ai

            reconcile_after_ai(db, submission_id)


# ─────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────

def main(force: bool = False) -> None:
    settings = get_settings()
    if settings.worker_role != "practice" and not force:
        logger.error("worker_role=%s 不是 practice，练习 worker 退出"
                     "（请设置 DAI_WORKER_ROLE=practice）", settings.worker_role)
        return
    logging.basicConfig(level=logging.INFO)
    client = _get_redis()
    queues = list(worker_queue_names(settings))
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="practice-job")
    futures: set = set()
    next_recovery = time.monotonic()
    logger.info("practice worker 启动（队列隔离：%s）", queues)
    try:
        while True:
            futures = {future for future in futures if not future.done()}
            try:
                with SessionLocal() as db:
                    limit = _concurrency(db)
                if time.monotonic() >= next_recovery:
                    with SessionLocal() as db:
                        practice_queue.recover_stale_submissions(db, client)
                    next_recovery = time.monotonic() + 60
                claimed_any = False
                while len(futures) < limit:
                    with SessionLocal() as db:
                        row = db.scalars(
                            select(PracticeSubmission)
                            .where(PracticeSubmission.grading_status == "queued")
                            .order_by(PracticeSubmission.id).limit(1)
                        ).first()
                    if row is None:
                        break
                    futures.add(pool.submit(grade_submission, SessionLocal, row.id,
                                            settings, client))
                    claimed_any = True
                if claimed_any:
                    continue
                message = client.blpop(queues, timeout=min(5, settings.practice_queue_poll_seconds))
                if message is None:
                    continue
                queue_name, raw = message
                queues.remove(queue_name)
                queues.append(queue_name)
                payload = json.loads(raw)
                if payload.get("type") == "ai_grade" and settings.ai_ready:
                    futures.add(pool.submit(process_ai_grade_message, SessionLocal,
                                            settings, int(payload["id"])))
            except redis.exceptions.RedisError:
                time.sleep(min(5, settings.practice_queue_poll_seconds))
            except Exception:
                logger.exception("练习 worker 轮询异常；任务仍留在库内")
                time.sleep(min(5, settings.practice_queue_poll_seconds))
    except KeyboardInterrupt:
        logger.info("practice worker 正在退出（等待当前任务结束）")
    finally:
        client.close()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    from app.logging_config import setup_logging

    setup_logging(get_settings().environment, process_name="practice-worker",
                  settings=get_settings())
    main()
