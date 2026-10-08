"""TASK-031（F-19）：Docker Socket 持有者清单与学生容器隔离基线（静态断言）。

- Socket 只允许确需服务持有（api/worker/environment-builder），多挂少挂都失败
- 学生代码执行容器：--network none / --cap-drop ALL / no-new-privileges /
  --read-only / pids / cpu / 内存 / 非 root，且全仓无 --privileged 与 host 网络
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.prod.yml"

SOCKET_HOLDERS = {"api", "worker", "environment-builder", "lab-worker"}
NON_HOLDERS = {"frontend", "mysql", "redis", "migrate", "ai-worker", "account-worker"}


def _service_blocks() -> dict[str, str]:
    text = COMPOSE.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = ""
        elif current is not None:
            blocks[current] += line + "\n"
    return blocks


def test_socket_holder_list_matches_compose():
    """Socket 持有者清单与 compose 一致：确需服务持有，其余一律不持有。"""
    blocks = _service_blocks()
    holders = {name for name, body in blocks.items() if "/var/run/docker.sock" in body}
    assert holders == SOCKET_HOLDERS, (
        f"Socket 持有者漂移：{holders} ≠ 预期 {SOCKET_HOLDERS}；"
        "修改持有者必须同步 docs/security/docker-socket-isolation.md §2"
    )


def test_socket_holders_subset_of_services():
    """持有者清单内的服务必须真实存在于 compose。"""
    blocks = _service_blocks()
    assert SOCKET_HOLDERS <= set(blocks)


def test_non_holders_have_no_socket():
    blocks = _service_blocks()
    for name in NON_HOLDERS:
        assert name in blocks, f"{name} 服务缺失"
        assert "/var/run/docker.sock" not in blocks[name], f"{name} 不应持有 Socket"


def test_ai_worker_has_no_judge_runtime_resources():
    """AI worker 只消费 AI 队列，不继承 Docker 判题运行时或 Registry Secret。"""
    blocks = _service_blocks()
    ai_worker = blocks["ai-worker"]
    assert "extends:" not in ai_worker
    for forbidden in (
        "/var/run/docker.sock",
        "/judge-work",
        "DAI_JUDGE_HOST_WORK_DIR",
        "DAI_JUDGE_WORK_DIR",
        "DOCKER_CONFIG",
        "config.json",
    ):
        assert forbidden not in ai_worker, f"ai-worker 不应包含 {forbidden}"


def _read_app_files() -> list[str]:
    app_dir = REPO_ROOT / "backend" / "app"
    return [
        p.read_text(encoding="utf-8")
        for p in app_dir.rglob("*.py")
        if "__pycache__" not in str(p)
    ]


def test_no_privileged_or_host_network_in_repo():
    """全仓代码不得使用 --privileged 或 host 网络模式启动学生容器。"""
    for text in _read_app_files():
        assert "--privileged" not in text
        assert re.search(r'"--network"\s*,\s*"host"', text) is None


def test_judge_container_isolation_flags():
    """判题容器隔离基线（judge_worker）。"""
    text = (REPO_ROOT / "backend/app/worker/judge_worker.py").read_text(encoding="utf-8")
    for flag in (
        '"--network", "none"',
        '"--cap-drop", "ALL"',
        '"--security-opt", "no-new-privileges"',
        '"--read-only"',
        '"--pids-limit"',
        '"--user", "1000:1000"',
        '"--cpus"',
        '"--memory"',
    ):
        assert flag in text, f"判题容器缺失隔离参数: {flag}"


def test_kernel_container_isolation_flags():
    """内核（Notebook）容器隔离基线（kernel_manager）。"""
    text = (REPO_ROOT / "backend/app/services/kernel_manager.py").read_text(encoding="utf-8")
    for flag in (
        '"--network", "none"',
        '"--cap-drop", "ALL"',
        '"--pids-limit"',
    ):
        assert flag in text, f"内核容器缺失隔离参数: {flag}"
