#!/usr/bin/env python3
"""本轮验证的隔离运行环境启动器（review-fixes-20261012）。

约定（见 docs/review-fixes-20261012.md 与实施计划 §1）：

* 每次运行分配唯一 ``RUN_ID``；临时容器、卷与端口全部独立，只绑定 ``127.0.0.1``；
* 端口由操作系统分配（不固定端口），启动器把实际端口写入环境文件供子进程继承；
* 不复用共享 Redis、不执行 ``flush``；只清理带本 RUN_ID 标签的容器；
* 每项检查记录命令、环境、退出码与实际输出，证据写入
  ``.runtime/review-fixes-20261012/evidence/<RUN_ID>/``。

用法::

    python scripts/verify_env.py up                 # 起隔离 Redis，打印环境文件路径
    python scripts/verify_env.py up --mysql         # 再加一个空 schema 的 MySQL
    python scripts/verify_env.py run <RUN_ID> --slug judge-claim -- <命令...>
    python scripts/verify_env.py down <RUN_ID>      # 只删本 RUN_ID 的容器
    python scripts/verify_env.py status <RUN_ID>

``run`` 会把隔离环境变量注入子进程，并把 stdout/stderr 同时写到终端与证据日志。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / ".runtime" / "review-fixes-20261012" / "evidence"
ENV_ROOT = ROOT / ".runtime" / "review-fixes-20261012" / "env"
LABEL_KEY = "dai.verify.run_id"

REDIS_IMAGE = os.environ.get("DAI_VERIFY_REDIS_IMAGE", "redis:7-alpine")
MYSQL_IMAGE = os.environ.get("DAI_VERIFY_MYSQL_IMAGE", "mysql:8.0")

DEFAULT_PYTHON_CANDIDATES = (
    ROOT / ".runtime" / "school-security-venv" / "bin" / "python",
    ROOT / "backend" / ".venv" / "bin" / "python",
)


def default_python() -> str:
    for candidate in DEFAULT_PYTHON_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def free_port() -> int:
    """让内核分配一个当前空闲的 127.0.0.1 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_tcp(port: int, timeout: float = 30.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def docker_available() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if not docker:
        return False, "未找到 docker 可执行文件"
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, f"docker info 异常: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, "docker daemon 不可访问: " + (detail[0] if detail else "unknown")
    return True, result.stdout.strip()


def run_container(name: str, run_id: str, args: list[str], port: int, container_port: int,
                  timeout: float) -> None:
    docker = shutil.which("docker") or "docker"
    cmd = [docker, "run", "-d", "--rm", "--name", name, "--label", f"{LABEL_KEY}={run_id}",
           "-p", f"127.0.0.1:{port}:{container_port}"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise SystemExit(f"启动容器失败: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    if not wait_tcp(port, timeout=timeout):
        subprocess.run([docker, "rm", "-f", name], capture_output=True, text=True)
        raise SystemExit(f"容器 {name} 在 {timeout}s 内未就绪（127.0.0.1:{port}）")


def env_file(run_id: str) -> Path:
    return ENV_ROOT / f"{run_id}.json"


def load_env(run_id: str) -> dict:
    path = env_file(run_id)
    if not path.exists():
        raise SystemExit(f"RUN_ID {run_id} 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def child_environment(ctx: dict) -> dict:
    """构造子进程环境：**绝不**继承外部业务配置或测试覆盖变量（A14）。

    * 删除所有 ``DAI_TEST_*`` 覆盖变量（否则可能把测试指向真实库/共享 Redis）；
    * 删除指向业务库/共享 Redis 的配置后显式注入本轮隔离资源；
    * 关闭 dotenv：``DAI_ENV_FILE`` 指向空的临时文件，避免 backend/.env 回填真实配置。
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("DAI_TEST_"):
            env.pop(key, None)
    for key in ("DAI_DATABASE_URL", "DAI_REDIS_URL", "DAI_SECRET_KEY", "DAI_AI_API_KEY",
                "DAI_ENV_FILE"):
        env.pop(key, None)
    empty_dotenv = ctx.get("empty_dotenv")
    if empty_dotenv:
        env["DAI_ENV_FILE"] = empty_dotenv
    env["DAI_VERIFY_RUN_ID"] = ctx["run_id"]
    env["DAI_ENVIRONMENT"] = "development"
    env["DAI_MIGRATION_MODE"] = "disposable"
    env["DAI_SECRET_KEY"] = ctx["secret_key"]
    env["DAI_REDIS_URL"] = ctx["redis_url"]
    env["DAI_AI_ENABLED"] = "false"
    env["DAI_AI_API_KEY"] = ""
    if ctx.get("database_url"):
        env["DAI_DATABASE_URL"] = ctx["database_url"]
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def cmd_up(args: argparse.Namespace) -> int:
    ok, detail = docker_available()
    if not ok:
        print(json.dumps({"ok": False, "reason": detail}, ensure_ascii=False), file=sys.stderr)
        return 3
    run_id = args.run_id or new_run_id()
    ENV_ROOT.mkdir(parents=True, exist_ok=True)
    ctx = {
        "run_id": run_id,
        "created_at": utc_now(),
        "docker_server": detail,
        "secret_key": f"verify-{secrets.token_urlsafe(24)}",
    }
    # 空的 dotenv：子进程即使回退读取 env 文件也拿不到任何业务配置（A14）
    empty_dotenv = ENV_ROOT / f"{run_id}.empty.env"
    empty_dotenv.write_text("", encoding="utf-8")
    ctx["empty_dotenv"] = str(empty_dotenv)

    redis_port = free_port()
    run_container(f"dai-verify-redis-{run_id}", run_id, [REDIS_IMAGE], redis_port, 6379,
                  timeout=30)
    ctx["redis_port"] = redis_port
    ctx["redis_url"] = f"redis://127.0.0.1:{redis_port}/15"
    ctx["redis_image"] = REDIS_IMAGE

    if args.mysql:
        mysql_port = free_port()
        password = f"verify-{secrets.token_hex(8)}"
        run_container(
            f"dai-verify-mysql-{run_id}", run_id,
            ["-e", f"MYSQL_ROOT_PASSWORD={password}", "-e", "MYSQL_DATABASE=dai_verify",
             MYSQL_IMAGE],
            mysql_port, 3306, timeout=180,
        )
        ctx["mysql_port"] = mysql_port
        ctx["mysql_password"] = password
        ctx["database_url"] = (
            f"mysql+pymysql://root:{password}@127.0.0.1:{mysql_port}/dai_verify"
        )
        ctx["mysql_image"] = MYSQL_IMAGE

    path = env_file(run_id)
    path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    (EVIDENCE_ROOT / run_id).mkdir(parents=True, exist_ok=True)
    print(json.dumps({"ok": True, "env_file": str(path), **ctx}, ensure_ascii=False, indent=2))
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    docker = shutil.which("docker")
    if not docker:
        print("未找到 docker", file=sys.stderr)
        return 3
    filters = ["--filter", f"label={LABEL_KEY}={args.run_id}"]
    listed = subprocess.run([docker, "ps", "-aq", *filters], capture_output=True, text=True, timeout=60)
    ids = [line for line in listed.stdout.split() if line]
    for container_id in ids:
        subprocess.run([docker, "rm", "-f", container_id], capture_output=True, text=True, timeout=120)
    print(json.dumps({"ok": True, "run_id": args.run_id, "removed": len(ids)}, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = load_env(args.run_id)
    print(json.dumps(ctx, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _slugify(text: str) -> str:
    keep = [ch if ch.isalnum() or ch in "-_." else "-" for ch in text]
    slug = "".join(keep).strip("-")
    return (slug or "check")[:80]


def cmd_run(args: argparse.Namespace) -> int:
    ctx = load_env(args.run_id)
    if not args.command:
        print("缺少要执行的命令（-- 之后）", file=sys.stderr)
        return 2
    slug = _slugify(args.slug or Path(args.command[0]).name)
    evidence_dir = EVIDENCE_ROOT / args.run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_path = evidence_dir / f"{slug}.log"
    summary_path = evidence_dir / f"{slug}.json"

    env = child_environment(ctx)
    # A14：默认使用显式临时工作目录——不从 backend/ 继承 dotenv、相对路径与本地产物；
    # 需要仓库内相对路径（例如 pytest 配置）时必须显式 --cwd repo|backend|绝对路径。
    invoke_cwd = os.getcwd()
    requested_cwd = getattr(args, "cwd", "temp")
    if requested_cwd == "temp":
        workdir = Path(tempfile.mkdtemp(prefix=f"dai-verify-{args.run_id}-"))
    elif requested_cwd == "repo":
        workdir = REPO_ROOT
    elif requested_cwd == "backend":
        workdir = REPO_ROOT / "backend"
    else:
        workdir = Path(requested_cwd).expanduser().resolve()
    if not workdir.exists():
        raise SystemExit(f"--cwd 不存在: {workdir}")
    # 命令里的相对可执行路径按"调用者目录"解析（cwd 已隔离到临时目录，相对路径会失效）
    command = list(args.command)
    if (os.sep in command[0] or command[0].startswith(".")) and not Path(command[0]).is_absolute():
        # 不要 .resolve()：venv 里的 python 是符号链接，解析后会绕开虚拟环境
        candidate = (Path(invoke_cwd) / command[0]).absolute()
        if candidate.exists():
            command[0] = str(candidate)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# cmd: {' '.join(args.command)}\n")
        log.write(f"# cwd: {workdir}\n")
        log.write(f"# run_id: {args.run_id}\n")
        log.write(f"# started_at: {utc_now()}\n\n")
        log.flush()
        proc = subprocess.Popen(command, env=env, cwd=str(workdir),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        returncode = proc.wait()
        duration = round(time.monotonic() - started, 3)
        log.write(f"\n# exit_code: {returncode}\n# duration_s: {duration}\n")

    summary = {
        "run_id": args.run_id,
        "slug": slug,
        "command": command,
        "cwd": str(workdir),
        "environment": {
            "DAI_REDIS_URL": env.get("DAI_REDIS_URL"),
            "DAI_DATABASE_URL": env.get("DAI_DATABASE_URL"),
            "DAI_ENVIRONMENT": env.get("DAI_ENVIRONMENT"),
            "DAI_MIGRATION_MODE": env.get("DAI_MIGRATION_MODE"),
        },
        "exit_code": returncode,
        "duration_s": duration,
        "log": str(log_path),
        "finished_at": utc_now(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return returncode


def cmd_env(args: argparse.Namespace) -> int:
    ctx = load_env(args.run_id)
    for key, value in sorted(child_environment(ctx).items()):
        if key.startswith("DAI_"):
            print(f"export {key}={value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="隔离验证运行环境启动器")
    sub = parser.add_subparsers(dest="action", required=True)

    up = sub.add_parser("up", help="创建隔离 Redis（可选 MySQL）并写环境文件")
    up.add_argument("--mysql", action="store_true", help="同时创建空 schema 的 MySQL")
    up.add_argument("--run-id", default=None, help="指定 RUN_ID（默认自动生成）")
    up.set_defaults(func=cmd_up)

    run = sub.add_parser("run", help="在隔离环境中执行命令并记录证据")
    run.add_argument("run_id")
    run.add_argument("--slug", default=None, help="证据文件名（默认取命令名）")
    run.add_argument("--cwd", default="temp",
                     help="子进程工作目录：temp（默认隔离）/ repo / backend / 绝对路径")
    run.set_defaults(func=cmd_run)

    down = sub.add_parser("down", help="删除本 RUN_ID 的容器")
    down.add_argument("run_id")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="显示环境文件")
    status.add_argument("run_id")
    status.set_defaults(func=cmd_status)

    env = sub.add_parser("env", help="打印 export 形式的环境变量")
    env.add_argument("run_id")
    env.set_defaults(func=cmd_env)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 命令部分统一放在 ``--`` 之后，避免 argparse 把子命令选项吞进剩余参数。
    if "--" in raw:
        split = raw.index("--")
        head, command = raw[:split], raw[split + 1:]
    else:
        head, command = raw, []
    parser = build_parser()
    args = parser.parse_args(head)
    args.command = command
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
