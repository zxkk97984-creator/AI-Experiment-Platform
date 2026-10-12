#!/usr/bin/env python3
"""考试答案保存容量验证：30/100/200 名独立测试学生，遵守批次五的判定基线。

判据（暂定本地基线，不是生产 SLA）：

* 读取（GET /exams/{id}/session）P95 ≤ 1 秒
* 保存（PUT /exams/{id}/answers）P95 ≤ 2 秒、P99 ≤ 5 秒
* 零答案丢失（结束时逐人核对服务端答案）
* 零版本回退（同一题的 expected_version 只增不减）
* 429/409/5xx/超时全部进入报告，**不从分母里剔除**

两种用法：

    # 1) 在可丢弃数据库里造 N 名独立学生 + 一场考试（显式确认标志）
    python scripts/verify_exam_capacity.py seed --students 30 --out /tmp/exam-capacity.json

    # 2) 对指定服务跑负载（ramp 60s，稳定期默认 300s；200 人建议 1800s）
    python scripts/verify_exam_capacity.py run --manifest /tmp/exam-capacity.json \
        --ramp-seconds 60 --steady-seconds 300 --report .runtime/review-fixes-20261012/capacity-30.json

脚本自身：每人最多一个保存在途；按服务器返回的版本推进；不写业务库（seed 只在显式要求时
对给定 DAI_DATABASE_URL 生效，且必须带 --allow-disposable-database）。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import math
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1] / "backend"
READ_P95_LIMIT_MS = 1000
SAVE_P95_LIMIT_MS = 2000
SAVE_P99_LIMIT_MS = 5000


def build_targets(*, read_p95: float, save_p95: float, save_p99: float,
                  missing: int, mismatched: int, unverified: int, regressions: int,
                  error_count: int, expected_students: int, saved_students: int,
                  verified_students: int, save_samples: int) -> tuple[dict, dict, bool]:
    """容量门禁判据（A13）：延迟分位数 **加** 错误数、人数覆盖与每人成功样本。

    只比较分位数会把"全员 500、零成功保存"误判为通过——这里要求：

    * 全部 HTTP/协议/任务异常计入 ``error_count``，必须为 0；
    * 完整人数覆盖（每人都有成功保存样本、每位学生都完成最终答案核对）；
    * 存在实际样本量（``save_samples >= expected_students``）。
    """
    targets = {
        f"read_p95_le_{READ_P95_LIMIT_MS}ms": read_p95 <= READ_P95_LIMIT_MS,
        f"save_p95_le_{SAVE_P95_LIMIT_MS}ms": save_p95 <= SAVE_P95_LIMIT_MS,
        f"save_p99_le_{SAVE_P99_LIMIT_MS}ms": save_p99 <= SAVE_P99_LIMIT_MS,
        "zero_answer_loss": missing == 0 and mismatched == 0 and unverified == 0,
        "zero_version_regression": regressions == 0,
        "zero_errors": error_count == 0,
        "full_coverage": (unverified == 0 and expected_students > 0
                          and save_samples >= expected_students),
        "every_student_saved": (expected_students > 0
                                and saved_students >= expected_students),
        "final_answers_verified": (expected_students > 0
                                   and verified_students >= expected_students),
    }
    coverage = {
        "expected_students": expected_students,
        "saved_students": saved_students,
        "verified_students": verified_students,
        "unverified_students": unverified,
        "save_samples": save_samples,
    }
    return targets, coverage, all(targets.values())


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


@dataclass
class Latency:
    read_ms: list[float] = field(default_factory=list)
    save_ms: list[float] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def note(self, bucket: str, status: int) -> None:
        self.statuses[f"{bucket}:{status}"] = self.statuses.get(f"{bucket}:{status}", 0) + 1


@dataclass
class Student:
    username: str
    password: str
    token: str = ""
    submission_id: int | None = None
    versions: dict[int, int] = field(default_factory=dict)
    answered: dict[int, list[str]] = field(default_factory=dict)
    saving: int = 0
    version_regressions: int = 0
    write_generation: int = 1  # 换机/接管会推进写入代次；每次保存都必须回传当前值
    saved_ok: int = 0          # 成功保存次数（A13：每人必须有成功样本）
    verified: bool = False     # 核对阶段是否成功读取到会话
    access_expires_at: float = 0.0


def _client(base_url: str, timeout: float = 20.0,
            transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    # 本机回环验证：显式关闭环境代理，避免宿主代理影响结果
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False,
                             transport=transport)


async def _login(client: httpx.AsyncClient, student: Student) -> bool:
    response = await client.post("/api/v1/auth/login",
                                 json={"username": student.username, "password": student.password})
    if response.status_code != 200:
        return False
    payload = response.json()
    student.token = payload["access_token"]
    student.access_expires_at = time.monotonic() + int(payload.get("expires_in", 1800))
    return True


async def _refresh_if_needed(client: httpx.AsyncClient, student: Student) -> bool:
    """Use this student's own refresh-cookie jar before its access token expires."""
    if student.access_expires_at and time.monotonic() + 60 < student.access_expires_at:
        return True
    response = await client.post("/api/v1/auth/refresh", json={})
    if response.status_code != 200:
        return False
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    student.token = token
    student.access_expires_at = time.monotonic() + int(payload.get("expires_in", 1800))
    return True


def _auth(student: Student) -> dict[str, str]:
    return {"Authorization": f"Bearer {student.token}"}


async def _ensure_started(client: httpx.AsyncClient, exam_id: int, student: Student) -> bool:
    session = await client.get(f"/api/v1/exams/{exam_id}/session", headers=_auth(student))
    if session.status_code != 200:
        return False
    payload = session.json()
    submission = payload.get("submission")
    if submission and submission.get("status") == "started":
        student.submission_id = submission["id"]
        student.write_generation = int(submission.get("write_generation") or 1)
        for saved in payload.get("saved_answers") or []:
            student.versions[saved["question_id"]] = saved.get("version", 0)
        return True
    started = await client.post(f"/api/v1/exams/{exam_id}/start", headers=_auth(student))
    if started.status_code >= 400:
        return False
    started_submission = started.json().get("submission") or {}
    student.submission_id = started_submission.get("id")
    student.write_generation = int(started_submission.get("write_generation") or 1)
    return student.submission_id is not None


async def _one_student_loop(client: httpx.AsyncClient, exam_id: int, student: Student,
                            questions: list[int], latency: Latency, stop: asyncio.Event,
                            interval: float) -> None:
    choice = ["A", "B"]
    round_index = 0
    while not stop.is_set():
        if student.saving:
            await asyncio.sleep(0.05)
            continue
        question_id = questions[round_index % len(questions)]
        selection = [choice[round_index % 2]]
        expected = student.versions.get(question_id, 0)
        payload = {"expected_write_generation": student.write_generation, "answers": [{
            "question_id": question_id,
            "expected_version": expected,
            "selected_options": selection,
        }]}
        student.saving = 1
        started = time.perf_counter()
        try:
            if not await _refresh_if_needed(client, student):
                latency.errors.append("refresh-before-save")
                break
            response = await client.put(f"/api/v1/exams/{exam_id}/answers", headers=_auth(student),
                                        json=payload)
            elapsed = (time.perf_counter() - started) * 1000
            latency.save_ms.append(elapsed)
            latency.note("save", response.status_code)
            if response.status_code == 200:
                try:
                    response_payload = response.json()
                    results = response_payload.get("results")
                    if not isinstance(results, list) or len(results) != 1:
                        raise ValueError("save-results-cardinality")
                    result = results[0]
                    if not isinstance(result, dict) or result.get("question_id") != question_id:
                        raise ValueError("save-result-question-id")
                    if result.get("ok") is not True:
                        raise ValueError(f"save-item:{result.get('code') or 'not-ok'}")
                    version = result.get("version")
                    if not isinstance(version, int) or isinstance(version, bool) \
                            or version <= expected:
                        raise ValueError("save-result-version")
                    version = int(result.get("version", 0))
                    if version < student.versions.get(question_id, 0):
                        student.version_regressions += 1
                    student.versions[question_id] = max(student.versions.get(question_id, 0), version)
                    student.answered[question_id] = selection
                    student.saved_ok += 1
                except (ValueError, TypeError, KeyError) as exc:
                    latency.errors.append(f"save-protocol:{str(exc)[:80]}")
            elif response.status_code == 409:
                latency.errors.append("save-conflict")
            else:
                latency.errors.append(f"save-http:{response.status_code}")
        except Exception as exc:  # 超时/连接错误都计入
            latency.save_ms.append((time.perf_counter() - started) * 1000)
            latency.errors.append(f"save-exc:{type(exc).__name__}")
        finally:
            student.saving = 0

        read_started = time.perf_counter()
        try:
            if not await _refresh_if_needed(client, student):
                latency.errors.append("refresh-before-read")
                round_index += 1
                await asyncio.sleep(interval)
                continue
            session = await client.get(f"/api/v1/exams/{exam_id}/session", headers=_auth(student))
            latency.read_ms.append((time.perf_counter() - read_started) * 1000)
            latency.note("read", session.status_code)
            if session.status_code == 200:
                try:
                    payload_session = session.json()
                    saved_answers = payload_session.get("saved_answers")
                    if not isinstance(saved_answers, list):
                        raise ValueError("read-saved-answers")
                    seen: set[int] = set()
                    for saved in saved_answers:
                        if not isinstance(saved, dict):
                            raise ValueError("read-answer-shape")
                        saved_question_id = saved.get("question_id")
                        version = saved.get("version")
                        if saved_question_id not in questions or saved_question_id in seen:
                            raise ValueError("read-answer-question-id")
                        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                            raise ValueError("read-answer-version")
                        seen.add(saved_question_id)
                        current = student.versions.get(saved_question_id, 0)
                        if version < current:
                            student.version_regressions += 1
                        student.versions[saved_question_id] = max(current, version)
                except (ValueError, TypeError, KeyError) as exc:
                    latency.errors.append(f"read-protocol:{str(exc)[:80]}")
            else:
                latency.errors.append(f"read-http:{session.status_code}")
        except Exception as exc:
            latency.read_ms.append((time.perf_counter() - read_started) * 1000)
            latency.errors.append(f"read-exc:{type(exc).__name__}")

        round_index += 1
        await asyncio.sleep(interval)


async def run_load(manifest: dict, ramp_seconds: int, steady_seconds: int,
                   interval: float, timeout: float = 20.0,
                   transport: httpx.AsyncBaseTransport | None = None) -> dict:
    base_url = manifest["base_url"].rstrip("/")
    exam_id = manifest["exam_id"]
    questions = manifest["question_ids"]
    students = [Student(item["username"], item["password"]) for item in manifest["students"]]
    latency = Latency()
    started_at = datetime.now(timezone.utc)
    stop = asyncio.Event()

    async with AsyncExitStack() as stack:
        authenticated = []
        student_clients: dict[str, httpx.AsyncClient] = {}
        for index, student in enumerate(students):
            try:
                client = await stack.enter_async_context(
                    _client(base_url, timeout, transport=transport))
                if await _login(client, student) and await _ensure_started(client, exam_id, student):
                    authenticated.append(student)
                    student_clients[student.username] = client
                else:
                    latency.errors.append(f"setup:{student.username}")
            except Exception as exc:  # noqa: BLE001 - 登录/开场超时不得中断整场验证
                latency.errors.append(f"setup-exc:{type(exc).__name__}")
            # 登录是 bcrypt 密集操作：错开一点，避免 100 个并发登录把 CPU 打满
            await asyncio.sleep(0.05 if index else 0)
        ramp_step = max(ramp_seconds / len(authenticated), 0.05) if authenticated else 0
        tasks = []
        for index, student in enumerate(authenticated):
            tasks.append(asyncio.create_task(
                _one_student_loop(student_clients[student.username], exam_id, student,
                                  questions, latency, stop, interval)))
            await asyncio.sleep(ramp_step)

        await asyncio.sleep(steady_seconds)
        stop.set()
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in task_results:
            if isinstance(result, BaseException):
                latency.errors.append(f"task-exc:{type(result).__name__}")

        # 逐人核对：服务端答案必须与本地最后一次成功保存一致（零答案丢失）。
        # 核对阶段同样计入失败：超时/连接错误 = 该学生未验证（绝不当成"没丢"）。
        missing = 0
        mismatched = 0
        unverified = 0
        for student in authenticated:
            client = student_clients[student.username]
            try:
                if not await _refresh_if_needed(client, student):
                    unverified += 1
                    latency.errors.append("verify-refresh")
                    continue
                session = await client.get(f"/api/v1/exams/{exam_id}/session",
                                           headers=_auth(student))
            except Exception as exc:  # noqa: BLE001 - 核对超时/断连都算未验证
                unverified += 1
                latency.errors.append(f"verify-exc:{type(exc).__name__}")
                continue
            if session.status_code != 200:
                missing += 1
                latency.errors.append(f"verify-http:{session.status_code}")
                continue
            try:
                payload_session = session.json()
                raw_answers = payload_session.get("saved_answers")
                if not isinstance(raw_answers, list):
                    raise ValueError("verify-saved-answers")
                server_answers = {}
                for item in raw_answers:
                    if not isinstance(item, dict):
                        raise ValueError("verify-answer-shape")
                    question_id = item.get("question_id")
                    version = item.get("version")
                    if question_id not in questions or question_id in server_answers:
                        raise ValueError("verify-answer-question-id")
                    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                        raise ValueError("verify-answer-version")
                    server_answers[question_id] = item
                student.verified = True
            except (ValueError, TypeError, KeyError) as exc:
                unverified += 1
                latency.errors.append(f"verify-protocol:{str(exc)[:80]}")
                continue
            for question_id, selection in student.answered.items():
                saved = server_answers.get(question_id)
                if saved is None:
                    missing += 1
                elif sorted(saved.get("selected_options") or []) != sorted(selection):
                    mismatched += 1

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    read_p95 = _percentile(latency.read_ms, 0.95)
    save_p95 = _percentile(latency.save_ms, 0.95)
    save_p99 = _percentile(latency.save_ms, 0.99)
    regressions = sum(student.version_regressions for student in students)
    saved_students = sum(1 for student in students if student.saved_ok > 0)
    successful_save_samples = sum(student.saved_ok for student in students)
    verified_students = sum(1 for student in students if student.verified)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "students": len(students),
        "authenticated_students": len(authenticated),
        "ramp_seconds": ramp_seconds,
        "steady_seconds": steady_seconds,
        "elapsed_seconds": round(elapsed, 1),
        "read": {"count": len(latency.read_ms), "p95_ms": round(read_p95, 1),
                 "max_ms": round(max(latency.read_ms, default=0.0), 1)},
        "save": {"count": len(latency.save_ms), "p95_ms": round(save_p95, 1),
                 "p99_ms": round(save_p99, 1),
                 "max_ms": round(max(latency.save_ms, default=0.0), 1)},
        "successful_save_samples": successful_save_samples,
        "statuses": latency.statuses,
        "errors": latency.errors[:50],
        "error_count": len(latency.errors),
        "answer_loss": {"missing": missing, "mismatched": mismatched,
                        "unverified_students": unverified},
        "version_regressions": regressions,
        "targets": {
            f"read_p95_le_{READ_P95_LIMIT_MS}ms": read_p95 <= READ_P95_LIMIT_MS,
            f"save_p95_le_{SAVE_P95_LIMIT_MS}ms": save_p95 <= SAVE_P95_LIMIT_MS,
            f"save_p99_le_{SAVE_P99_LIMIT_MS}ms": save_p99 <= SAVE_P99_LIMIT_MS,
            "zero_answer_loss": missing == 0 and mismatched == 0 and unverified == 0,
            "zero_version_regression": regressions == 0,
        },
    }
    # A13：passed 必须覆盖"协议/任务异常 + 人数覆盖 + 每人成功保存 + 最终答案校验"，
    # 只比较延迟分位数会把"全员 500"误判为通过。
    targets, coverage, passed = build_targets(
        read_p95=read_p95, save_p95=save_p95, save_p99=save_p99,
        missing=missing, mismatched=mismatched, unverified=unverified,
        regressions=regressions, error_count=len(latency.errors),
        expected_students=len(students), saved_students=saved_students,
        verified_students=verified_students, save_samples=successful_save_samples)
    report["targets"].update(targets)
    report["coverage"] = coverage
    report["passed"] = passed
    return report


def seed_students(students: int, exam_questions: int, output: Path, base_url: str,
                  password: str) -> dict:
    """在给定 DAI_DATABASE_URL（必须可丢弃）里造独立学生与一场考试。"""
    database_url = os.environ.get("DAI_DATABASE_URL")
    if not database_url:
        raise SystemExit("seed 需要 DAI_DATABASE_URL 指向可丢弃数据库")
    sys.path.insert(0, str(BACKEND))
    from app.security import hash_password

    from app.database import Base, create_engine_from_url, sessionmaker_for_engine
    from app.models import (AcademicTerm, Assignment, Course, CourseOffering, CourseTeachingClass,
                            Exam, ExamQuestion, ExamSubmission, TeachingClass, User)
    from app.services.offering_service import enroll_student

    engine = create_engine_from_url(database_url)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker_for_engine(engine)
    now = datetime.now(timezone.utc)
    created = []
    with factory() as db:
        teacher = db.query(User).filter(User.username == "capacity_teacher").one_or_none()
        if teacher is None:
            teacher = User(username="capacity_teacher", real_name="容量教师", role="teacher",
                           status="active", password_hash=hash_password(password))
            db.add(teacher)
            db.flush()
        term = AcademicTerm(code=f"cap-{secrets.token_hex(3)}", name="容量学期",
                            start_date=now.date(), end_date=(now + timedelta(days=120)).date(),
                            status="active")
        db.add(term)
        db.flush()
        klass = TeachingClass(academic_term_id=term.id, code=f"cap-{secrets.token_hex(3)}",
                              name="容量教学班", status="active")
        db.add(klass)
        db.flush()
        course = Course(title=f"容量课-{secrets.token_hex(3)}", status="published",
                        teacher_id=teacher.id, academic_term_id=term.id)
        db.add(course)
        db.flush()
        db.add(CourseTeachingClass(course_id=course.id, teaching_class_id=klass.id))
        offering = CourseOffering(course_id=course.id, academic_term_id=term.id,
                                  teaching_class_id=klass.id, teacher_id=teacher.id, status="active")
        db.add(offering)
        db.flush()
        exam = Exam(course_id=course.id, title=f"容量考试-{secrets.token_hex(3)}",
                    status="published", duration_minutes=120, offering_id=offering.id,
                    start_at=now - timedelta(minutes=10), end_at=now + timedelta(hours=2))
        db.add(exam)
        db.flush()
        question_ids = []
        for index in range(exam_questions):
            question = ExamQuestion(exam_id=exam.id, question_type="single_choice",
                                    prompt=f"容量题 {index + 1}", points=2,
                                    correct_answer={"correct": ["A"]},
                                    options={"A": "选项 A", "B": "选项 B"},
                                    order_index=index, grading_mode="legacy")
            db.add(question)
            db.flush()
            question_ids.append(question.id)
        for index in range(students):
            username = f"cap_student_{index + 1:04d}"
            student = db.query(User).filter_by(username=username).one_or_none()
            if student is None:
                student = User(username=username, real_name=f"容量学生{index + 1}",
                               role="student", status="active",
                               password_hash=hash_password(password))
                db.add(student)
                db.flush()
            enroll_student(db, offering, student.id, origin="class")
            created.append({"username": username, "password": password})
        db.commit()
        manifest = {
            "base_url": base_url,
            "exam_id": exam.id,
            "course_id": course.id,
            "question_ids": question_ids,
            "students": created,
        }
    engine.dispose()
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"exam_id": manifest["exam_id"], "students": len(created),
            "questions": len(manifest["question_ids"]), "manifest": str(output)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="考试答案保存容量验证")
    sub = parser.add_subparsers(dest="action", required=True)

    seed = sub.add_parser("seed", help="在可丢弃数据库里造学生与考试")
    seed.add_argument("--students", type=int, default=30)
    seed.add_argument("--questions", type=int, default=10)
    seed.add_argument("--base-url", default="http://127.0.0.1:8000")
    seed.add_argument("--password", default="Passw0rd!")
    seed.add_argument("--out", type=Path, required=True)
    seed.add_argument("--allow-disposable-database", action="store_true",
                      help="确认 DAI_DATABASE_URL 指向可丢弃数据库")

    run = sub.add_parser("run", help="按 manifest 跑负载")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--ramp-seconds", type=int, default=60)
    run.add_argument("--steady-seconds", type=int, default=300)
    run.add_argument("--interval", type=float, default=1.0,
                     help="每个学生两轮之间的间隔（秒）")
    run.add_argument("--timeout", type=float, default=20.0,
                     help="单请求超时（秒）；超时计入错误，不从分母剔除")
    run.add_argument("--report", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.action == "seed":
        if not args.allow_disposable_database:
            raise SystemExit("必须显式传 --allow-disposable-database（避免误对业务库造数据）")
        result = seed_students(args.students, args.questions, args.out, args.base_url, args.password)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = asyncio.run(run_load(manifest, args.ramp_seconds, args.steady_seconds,
                                  args.interval, args.timeout))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
