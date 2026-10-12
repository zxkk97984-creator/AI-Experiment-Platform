#!/usr/bin/env python3
"""data_v1 演示数据种子（批次三 3E）。

在**可丢弃**数据库里造一套"可信数据判题"演示数据：

* 教师 / N 名学生 / 学期 / 教学班 / 课程 / 授课实例（学生已选）；
* 一份作业与一道编程题（`grading_mode=active`）；
* 锁定 Rubric（active 模式必需）；
* 一个 **已确认并启用** 的 data_v1 评分版本（功能 60 ＋ 边界 10 两组用例），
  并把题目 `current_grading_version_id` 指向它。

幂等：按用户名/标题/版本号查重，可重复执行。**必须显式传
`--allow-disposable-database`**，避免误对业务库造数据。

用法：

    DAI_DATABASE_URL=sqlite:////tmp/demo.db \\
    python scripts/seed_grading_demo.py --allow-disposable-database --students 3

输出 JSON 摘要（含演示账号、题目、版本、用例数），供演示脚本/文档引用。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DEMO_PASSWORD = "Passw0rd!"
TEACHER_USERNAME = "grading_demo_teacher"
COURSE_TITLE = "data_v1 演示课程"
ASSIGNMENT_TITLE = "data_v1 演示作业"
QUESTION_TITLE = "两倍函数（数据用例判题）"
RUBRIC_SOURCE = {
    "dimensions": [
        {"id": "algorithm", "name": "算法正确性", "max_score": 20},
        {"id": "quality", "name": "代码质量", "max_score": 10},
    ],
    "notes": "演示 Rubric：仅用于锁定门禁与 AI 分项展示。",
}


def build_payload() -> dict:
    """演示用例：F 组 2 例（60 分）、R 组 1 例（10 分）。"""
    from app.services.grading_values import encode_value

    return {
        "schema_version": 1,
        "entry_function": "double_first",
        "execution_model": "data_v1",
        "binary_cases": [],
        "groups": [
            {
                "id": "F1", "name": "功能：首元素两倍", "dimension": "F", "max_score": 60,
                "cases": [
                    {"id": "F1-1", "input": encode_value(([1, 9],)), "expected": encode_value(2),
                     "compare": {"kind": "literal"}},
                    {"id": "F1-2", "input": encode_value(([21],)), "expected": encode_value(42),
                     "compare": {"kind": "literal"}},
                ],
            },
            {
                "id": "R1", "name": "边界：空列表返回 None", "dimension": "R", "max_score": 10,
                "cases": [
                    {"id": "R1-1", "input": encode_value(([],)), "expected": encode_value(None),
                     "compare": {"kind": "identity"}},
                ],
            },
        ],
    }


def resolve_judge_image_digest(image: str) -> str | None:
    """解析本地判题镜像的真实 image ID（演示环境需要可运行的 digest）。

    拿不到（Docker 不可用/镜像未构建）时返回 None，种子仍会建数据，
    但会把 `judge_image_ready=false` 写进摘要，提示操作者先构建镜像。
    """
    import subprocess

    try:
        result = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    digest = (result.stdout or "").strip()
    return digest or None


def ensure_environment_version(db, digest: str, *, memory_mb: int = 512):
    """把 basic 环境档位的最新版本指向给定镜像 digest，并返回版本 id。"""
    from app.models import EnvironmentProfile, EnvironmentVersion

    profile = db.query(EnvironmentProfile).filter_by(slug="basic").one_or_none()
    if profile is None:
        return None
    version = (db.query(EnvironmentVersion)
               .filter_by(profile_id=profile.id)
               .order_by(EnvironmentVersion.version_number.desc()).first())
    if version is None:
        version = EnvironmentVersion(
            profile_id=profile.id, version_number=1, status="available",
            base_image_ref=digest, image_digest=digest, minimum_memory_mb=memory_mb,
            manifest_sha256="d" * 64,
            requested_spec={"schema_version": 1, "python_packages": [], "system_packages": []})
        db.add(version)
        db.flush()
        return version.id
    version.status = "available"
    version.image_digest = digest
    version.base_image_ref = digest
    db.flush()
    return version.id


def seed(students: int, *, activate: bool = True, judge_image: str | None = None) -> dict:
    from app.database import Base, create_engine_from_url, sessionmaker_for_engine
    from app.models import (
        AcademicTerm, Assignment, Course, CourseOffering, CourseTeachingClass, ExamQuestion,
        JudgeQuestion, QuestionRubric, TeachingClass, User,
    )
    from app.security import hash_password
    from app.services import grading_version_service as versions
    from app.services.offering_service import enroll_student

    database_url = os.environ.get("DAI_DATABASE_URL")
    if not database_url:
        raise SystemExit("需要 DAI_DATABASE_URL 指向可丢弃数据库")
    digest = resolve_judge_image_digest(judge_image) if judge_image else None
    engine = create_engine_from_url(database_url)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker_for_engine(engine)
    now = datetime.now(timezone.utc)

    with factory() as db:
        teacher = db.query(User).filter_by(username=TEACHER_USERNAME).one_or_none()
        if teacher is None:
            teacher = User(username=TEACHER_USERNAME, real_name="演示教师", role="teacher",
                           status="active", password_hash=hash_password(DEMO_PASSWORD))
            db.add(teacher)
            db.flush()
        term = db.query(AcademicTerm).filter_by(code="demo-term").one_or_none()
        if term is None:
            term = AcademicTerm(code="demo-term", name="演示学期", start_date=date.today(),
                                end_date=date.today() + timedelta(days=180), status="active")
            db.add(term)
            db.flush()
        klass = db.query(TeachingClass).filter_by(code="demo-class").one_or_none()
        if klass is None:
            klass = TeachingClass(academic_term_id=term.id, code="demo-class",
                                  name="演示教学班", status="active")
            db.add(klass)
            db.flush()
        course = db.query(Course).filter_by(title=COURSE_TITLE).one_or_none()
        if course is None:
            course = Course(title=COURSE_TITLE, description="data_v1 演示",
                            status="published", teacher_id=teacher.id, academic_term_id=term.id)
            db.add(course)
            db.flush()
            db.add(CourseTeachingClass(course_id=course.id, teaching_class_id=klass.id))
        offering = db.query(CourseOffering).filter_by(course_id=course.id,
                                                     teacher_id=teacher.id).one_or_none()
        if offering is None:
            offering = CourseOffering(course_id=course.id, academic_term_id=term.id,
                                      teaching_class_id=klass.id, teacher_id=teacher.id,
                                      status="active")
            db.add(offering)
            db.flush()
        env_version_id = ensure_environment_version(db, digest) if digest else None
        assignment = db.query(Assignment).filter_by(course_id=course.id,
                                                    title=ASSIGNMENT_TITLE).one_or_none()
        if assignment is None:
            assignment = Assignment(course_id=course.id, title=ASSIGNMENT_TITLE,
                                    description="演示：数据用例判题", status="published",
                                    offering_id=offering.id, published_at=now,
                                    due_at=now + timedelta(days=30),
                                    created_by_id=teacher.id,
                                    environment_version_id=env_version_id)
            db.add(assignment)
            db.flush()
        question = db.query(JudgeQuestion).filter_by(assignment_id=assignment.id,
                                                     title=QUESTION_TITLE).one_or_none()
        if question is None:
            question = JudgeQuestion(
                assignment_id=assignment.id, title=QUESTION_TITLE,
                description="返回列表首元素的两倍；空列表返回 None。",
                function_name="double_first", signature="double_first(values)",
                starter_code="def double_first(values):\n    # TODO\n    pass\n",
                public_cases=[], hidden_tests="def test_demo():\n    assert double_first([1]) == 2\n",
                grading_mode="active", test_groups=[], score_cap_rules=[],
                teacher_constraints={"notes": "演示题目"},
            )
            db.add(question)
            db.flush()
        rubric = db.query(QuestionRubric).filter_by(judge_question_id=question.id,
                                                    status="locked").one_or_none()
        if rubric is None:
            rubric = QuestionRubric(
                judge_question_id=question.id, version=1, status="locked",
                source_hash=secrets.token_hex(32), source_snapshot={"title": QUESTION_TITLE},
                rubric_json=RUBRIC_SOURCE, model_name="demo-rubric")
            db.add(rubric)
            db.flush()

        version = versions.active_version_for(db, question_kind="assignment",
                                              question_id=question.id)
        if version is None:
            version = versions.create_draft(
                db, question_kind="assignment", question_id=question.id, payload=build_payload(),
                entry_function="double_first", rubric_id=rubric.id,
                context_snapshot={"prompt": question.description or QUESTION_TITLE},
                source_kind="manual", actor_id=teacher.id)
            versions.confirm_version(db, version, actor_id=teacher.id)
            if activate:
                versions.activate_version(db, version, actor_id=teacher.id)
            versions.set_question_current_version(
                db, question_kind="assignment", question_id=question.id,
                version_id=version.id if activate else None)
            db.flush()

        accounts = []
        for index in range(students):
            username = f"demo_student_{index + 1:02d}"
            student = db.query(User).filter_by(username=username).one_or_none()
            if student is None:
                student = User(username=username, real_name=f"演示学生{index + 1}",
                               role="student", status="active",
                               password_hash=hash_password(DEMO_PASSWORD))
                db.add(student)
                db.flush()
            enroll_student(db, offering, student.id, origin="class")
            accounts.append({"username": username, "password": DEMO_PASSWORD})
        db.commit()

        if env_version_id is not None:
            assignment.environment_version_id = env_version_id
            db.flush()
        summary = {
            "database_url": database_url,
            "judge_image": judge_image,
            "judge_image_digest": digest,
            "judge_image_ready": bool(digest),
            "environment_version_id": env_version_id,
            "teacher": {"username": TEACHER_USERNAME, "password": DEMO_PASSWORD},
            "students": accounts,
            "course_id": course.id,
            "offering_id": offering.id,
            "assignment_id": assignment.id,
            "question_id": question.id,
            "function_name": question.function_name,
            "rubric_id": rubric.id,
            "grading_version_id": version.id,
            "grading_version_status": version.status,
            "case_count": sum(len(group["cases"]) for group in version.payload["groups"]),
            "functional_total": version.payload.get("functional_total"),
            "robustness_total": version.payload.get("robustness_total"),
            "reference_solution": ("def double_first(values):\n"
                                   "    if not values:\n        return None\n"
                                   "    return values[0] * 2\n"),
        }
    engine.dispose()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data_v1 演示数据种子")
    parser.add_argument("--students", type=int, default=3)
    parser.add_argument("--allow-disposable-database", action="store_true",
                        help="确认 DAI_DATABASE_URL 指向可丢弃数据库")
    parser.add_argument("--no-activate", action="store_true",
                        help="只建草稿版本，不启用（用于演示确认流程）")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--judge-image", default="dai-judge-python:latest",
                        help="本地判题镜像名；解析其 image ID 供演示环境使用")
    args = parser.parse_args(argv)
    if not args.allow_disposable_database:
        raise SystemExit("必须显式传 --allow-disposable-database（避免误对业务库造数据）")
    summary = seed(args.students, activate=not args.no_activate,
                   judge_image=args.judge_image)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
