"""Phase 4 worker-role and queue-isolation regressions."""

from pathlib import Path

import yaml

from app.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_worker_roles_consume_disjoint_queues():
    from app.worker.judge_worker import EXAM_JUDGE_QUEUE, _worker_queue_names

    judge = Settings(_env_file=None, worker_role="judge")
    ai = Settings(_env_file=None, worker_role="ai", ai_enabled=True)

    assert _worker_queue_names(judge) == (judge.judge_queue_name, EXAM_JUDGE_QUEUE)
    # 批次 2：AI worker 同时消费一键 AI 配置队列（草稿生成/试判），仍与判题队列完全隔离
    assert _worker_queue_names(ai) == (ai.ai_queue_name, ai.ai_setup_queue_name)
    assert ai.ai_queue_name not in _worker_queue_names(judge)
    assert ai.ai_setup_queue_name not in _worker_queue_names(judge)


def test_disabled_ai_worker_has_no_consumption_queues():
    from app.worker.judge_worker import _worker_queue_names

    settings = Settings(_env_file=None, worker_role="ai", ai_enabled=False)

    assert _worker_queue_names(settings) == ()


def test_production_compose_declares_optional_ai_worker():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    judge = services["worker"]
    assert "python -m app.worker.judge_worker" in judge["command"]
    assert judge["environment"]["DAI_WORKER_ROLE"] == "judge"

    ai = services["ai-worker"]
    assert "ai" in ai["profiles"]
    assert "python -m app.worker.judge_worker" in ai["command"]
    assert ai["environment"]["DAI_WORKER_ROLE"] == "ai"
    assert ai["environment"]["DAI_AI_ENABLED"] == "${DAI_AI_ENABLED:-false}"
    assert "extends" not in ai
    assert "secrets" not in ai
    ai_text = str(ai)
    assert "/var/run/docker.sock" not in ai_text
    assert "/judge-work" not in ai_text
    assert "DAI_JUDGE_HOST_WORK_DIR" not in ai_text
