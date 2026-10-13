"""Rubric 版本服务——生成、校验、锁定与发布门禁"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExamQuestion, JudgeQuestion, QuestionRubric
from app.schemas.ai_grading import RubricDocument
from app.services.ai_client import AIServiceError, DeepSeekClient
from app.services.ai_prompts import build_rubric_messages
from pydantic import ValidationError


class RubricGenerationError(Exception):
    """AI 生成的 Rubric 结构不合规（可读错误，不产生 500）"""

logger = logging.getLogger("dai.rubric")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_question_snapshot(
    title: str,
    description: str | None = None,
    function_name: str | None = None,
    teacher_constraints: dict | None = None,
    test_groups: list | None = None,
    reference_solution: str | None = None,
    is_exam: bool = False,
) -> dict:
    groups_for_hash = [dict(g) for g in (test_groups or [])]
    return {
        "title": title,
        "description": description or "",
        "function_name": function_name or "",
        "is_exam": is_exam,
        "teacher_constraints": teacher_constraints or {},
        "test_groups": groups_for_hash,
        "reference_solution": reference_solution,
    }


def get_latest_locked_rubric(
    db: Session, *, kind: str, question_id: int
) -> QuestionRubric | None:
    col = _target_column(kind)
    return db.scalars(
        select(QuestionRubric)
        .where(col == question_id, QuestionRubric.status == "locked")
        .order_by(QuestionRubric.version.desc())
        .limit(1)
    ).first()


def generate_rubric(
    db: Session,
    client: DeepSeekClient,
    *,
    kind: str,
    question_id: int,
    snapshot: dict,
) -> QuestionRubric:
    """调用 AI 生成新 Rubric 并保存为 draft——唯一入口"""
    col = _target_column(kind)
    col_name = _kind_to_column(kind)

    max_ver = db.scalar(
        select(QuestionRubric.version)
        .where(col == question_id)
        .order_by(QuestionRubric.version.desc())
        .limit(1)
    )
    next_version = (max_ver or 0) + 1

    hash_snapshot = {k: v for k, v in snapshot.items() if k != "reference_solution"}
    source_hash = _sha256(_canonical_json(hash_snapshot))

    messages = build_rubric_messages(snapshot)
    # TASK-028：rubric_generation 预算 2000 completion tokens
    raw_response = client.chat_json(messages, operation="rubric_generation")
    raw_json_str = json.dumps(raw_response, ensure_ascii=False)

    try:
        rubric_doc = RubricDocument.model_validate(raw_response)
    except ValidationError as exc:
        # 2026-08-09：AI 输出结构漂移（如 criteria 多带字段/缺字段、分值合计不符）
        # 必须转为可读错误而非 500——生成端点据此返回 502 并附具体字段问题。
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        raise RubricGenerationError(
            f"AI 生成的 Rubric 不符合格式规范：{'；'.join(errors[:5])}"
        ) from exc

    rubric = QuestionRubric(
        **{col_name: question_id},
        version=next_version,
        status="draft",
        source_hash=source_hash,
        source_snapshot=snapshot,
        rubric_json=rubric_doc.model_dump(),
        model_name=client._settings.ai_model,
        raw_response=raw_json_str,
        locked_at=None,
    )
    db.add(rubric)
    db.flush()

    logger.info(
        "rubric_generated",
        extra={"kind": kind, "question_id": question_id, "version": next_version, "source_hash": source_hash},
    )
    return rubric


def update_draft_rubric(
    db: Session, rubric_id: int, document: RubricDocument, *, commit: bool = True
) -> QuestionRubric:
    """修改草稿正文；``commit=False`` 供「保存并启用」单事务收口（只 flush）。"""
    rubric = db.get(QuestionRubric, rubric_id)
    if rubric is None:
        raise ValueError(f"Rubric {rubric_id} 不存在")
    if rubric.status != "draft":
        raise ValueError(f"只有 draft 状态可修改，当前状态为 {rubric.status}")
    rubric.rubric_json = document.model_dump()
    if commit:
        db.commit()
    else:
        db.flush()
    return rubric


def lock_rubric(db: Session, rubric_id: int, *, commit: bool = True) -> QuestionRubric:
    rubric = db.get(QuestionRubric, rubric_id)
    if rubric is None:
        raise ValueError(f"Rubric {rubric_id} 不存在")
    if rubric.status == "locked":
        raise ValueError("Rubric 已锁定")

    target_kind = _detect_kind(rubric)
    col = _target_column(target_kind)
    target_id = getattr(rubric, _kind_to_column(target_kind))

    old_locked = db.scalars(
        select(QuestionRubric).where(
            col == target_id, QuestionRubric.status == "locked", QuestionRubric.id != rubric_id
        )
    ).all()
    for old in old_locked:
        old.status = "superseded"

    rubric.status = "locked"
    rubric.locked_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    else:
        db.flush()

    logger.info("rubric_locked", extra={"rubric_id": rubric_id, "kind": target_kind, "question_id": target_id, "version": rubric.version})
    return rubric


def ensure_locked_rubrics_for_publish(
    db: Session,
    client: DeepSeekClient,
    questions: Sequence,
) -> None:
    """发布前确保所有影子/正式题目有锁定 Rubric"""
    for q in questions:
        if hasattr(q, 'grading_mode'):
            grading_mode = q.grading_mode
        else:
            grading_mode = q.get("grading_mode", "legacy") if isinstance(q, dict) else "legacy"

        if grading_mode == "legacy":
            continue

        if isinstance(q, dict):
            question_id = q["id"]
            kind = "exam" if "exam_id" in q else "assignment"
            title = q.get("title", "")
            description = q.get("description")
            function_name = q.get("function_name")
            teacher_constraints = q.get("teacher_constraints", {})
            test_groups = q.get("test_groups", [])
            reference_solution = q.get("reference_solution")
        else:
            question_id = q.id
            kind = "exam" if isinstance(q, ExamQuestion) else "assignment"
            title = getattr(q, "title", None) or getattr(q, "prompt", "")
            description = getattr(q, "description", None) if hasattr(q, "description") else getattr(q, "prompt", None)
            function_name = getattr(q, "function_name", None) if hasattr(q, "function_name") else getattr(q, "prompt", None)
            teacher_constraints = getattr(q, "teacher_constraints", {})
            test_groups = getattr(q, "test_groups", [])
            reference_solution = getattr(q, "reference_solution", None)

        existing = get_latest_locked_rubric(db, kind=kind, question_id=question_id)
        if existing is not None:
            snapshot = build_question_snapshot(
                title=title, description=description, function_name=function_name,
                teacher_constraints=teacher_constraints, test_groups=test_groups,
                reference_solution=reference_solution, is_exam=(kind == "exam"),
            )
            hash_snapshot = {k: v for k, v in snapshot.items() if k != "reference_solution"}
            if _sha256(_canonical_json(hash_snapshot)) == existing.source_hash:
                continue

        snapshot = build_question_snapshot(
            title=title, description=description, function_name=function_name,
            teacher_constraints=teacher_constraints, test_groups=test_groups,
            reference_solution=reference_solution, is_exam=(kind == "exam"),
        )

        try:
            rubric = generate_rubric(db, client, kind=kind, question_id=question_id, snapshot=snapshot)
            lock_rubric(db, rubric.id)
        except AIServiceError:
            raise
        except Exception:
            raise AIServiceError("rubric_generation_failed", f"题目 {question_id} Rubric 生成失败", retryable=True)


# ── 内部辅助 ──

def _target_column(kind: str):
    if kind == "assignment":
        return QuestionRubric.judge_question_id
    if kind == "exam":
        return QuestionRubric.exam_question_id
    if kind == "practice_problem":
        return QuestionRubric.practice_problem_id
    raise ValueError(f"未知 kind: {kind}")


def _kind_to_column(kind: str) -> str:
    if kind == "assignment":
        return "judge_question_id"
    if kind == "exam":
        return "exam_question_id"
    if kind == "practice_problem":
        return "practice_problem_id"
    raise ValueError(f"未知 kind: {kind}")


def _detect_kind(rubric: QuestionRubric) -> str:
    if rubric.judge_question_id is not None:
        return "assignment"
    if rubric.exam_question_id is not None:
        return "exam"
    if rubric.practice_problem_id is not None:
        return "practice_problem"
    raise ValueError("Rubric 未关联任何题目")
