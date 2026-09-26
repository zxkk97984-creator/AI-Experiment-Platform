#!/usr/bin/env bash
# 人工智能基础实验平台 —— 本地开发一键启动（稳健版）
#
# 用法: ./scripts/dev-up.sh
#
# 可用环境变量:
#   DAI_PYTHON          后端 Python（默认 backend/.venv/bin/python）
#   DAI_DEV_API_PORT    API 端口（默认 8000）
#   DAI_DEV_RUN_DIR     PID/日志目录（默认 /tmp/dai-dev）
#   DAI_DEV_NO_BROWSER  =1 时不自动打开浏览器

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# This is a disposable development entrypoint.  Pin the mode for every child
# process so a production-oriented backend/.env cannot turn the local bootstrap
# into a production migration or require production-only digest evidence.
export DAI_ENVIRONMENT=development
export DAI_MIGRATION_MODE=disposable
RUN_DIR="${DAI_DEV_RUN_DIR:-/tmp/dai-dev}"
PY="${DAI_PYTHON:-$PROJECT_DIR/backend/.venv/bin/python}"
API_PORT="${DAI_DEV_API_PORT:-8000}"
FRONTEND_PORT=5173
CURRENT_USER="${USER:-$(id -un)}"
mkdir -p "$RUN_DIR"

log()  { printf '\033[36m[dev-up]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dev-up]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[31m[dev-up]\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

# 端口是否正在监听。
port_busy() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

proc_cwd() {
    readlink -f "/proc/$1/cwd" 2>/dev/null || true
}

# PID 必须仍存活，而且工作目录必须位于当前项目中。
# 比仅检查 npm/node 或命令行中是否带项目路径更可靠，可避免旧 PID 被复用后误判。
is_our_process() {
    local pid="$1" cwd
    kill -0 "$pid" 2>/dev/null || return 1
    cwd="$(proc_cwd "$pid")"
    [[ "$cwd" == "$PROJECT_DIR" || "$cwd" == "$PROJECT_DIR/"* ]]
}

pid_alive() {
    local name="$1" pidfile="$RUN_DIR/$1.pid" pid
    [ -f "$pidfile" ] || return 1
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    is_our_process "$pid"
}

show_log_tail() {
    local name="$1" logfile="$RUN_DIR/$1.log"
    if [ -f "$logfile" ]; then
        printf '\n\033[33m---- %s 最后 40 行日志 ----\033[0m\n' "$name" >&2
        tail -n 40 "$logfile" >&2 || true
        printf '\033[33m---- 日志结束 ----\033[0m\n\n' >&2
    fi
}

# 每个长期服务都放到一个新的 session/process group 中。
# dev-down 可以直接对整个 PGID 发 TERM/KILL，避免 npm -> sh -> vite 或 worker 子进程残留。
launch() {
    local name="$1" workdir="$2"
    shift 2
    local pidfile="$RUN_DIR/$name.pid" logfile="$RUN_DIR/$name.log" pid pgid

    if pid_alive "$name"; then
        log "[跳过] $name 已在运行 (PID $(cat "$pidfile"))"
        return 0
    fi

    rm -f "$pidfile"
    : >"$logfile"

    (
        cd "$workdir"
        exec setsid "$@"
    ) >"$logfile" 2>&1 < /dev/null &
    pid=$!
    echo "$pid" >"$pidfile"

    # 立刻退出通常意味着模块缺失、配置错误、端口冲突等；不要再假装启动成功。
    sleep 0.8
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pidfile"
        show_log_tail "$name"
        fail "$name 启动后立即退出"
    fi

    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ "$pgid" != "$pid" ]; then
        warn "$name 未成为独立进程组（PID=$pid, PGID=${pgid:-未知}），关闭脚本将使用兼容清理逻辑"
    fi

    log "[启动] $name (PID $pid, PGID ${pgid:-未知})"
}

wait_http() {
    local name="$1" url="$2" attempts="${3:-40}" i
    for ((i=1; i<=attempts; i++)); do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    show_log_tail "$name"
    return 1
}

check_alive() {
    local name="$1"
    if ! pid_alive "$name"; then
        show_log_tail "$name"
        fail "$name 已退出"
    fi
}

# ── 1. 环境检查 ───────────────────────────────────────────────
[ -d "$PROJECT_DIR/backend" ] || fail "未找到 backend 目录。请把脚本放在 <项目根目录>/scripts/dev-up.sh"
[ -d "$PROJECT_DIR/frontend" ] || fail "未找到 frontend 目录。请把脚本放在 <项目根目录>/scripts/dev-up.sh"
[ -x "$PY" ] || fail "未找到可执行 Python: $PY"
[ -f "$PROJECT_DIR/backend/.env" ] || fail "缺少 backend/.env（可从 .env.example 复制）"

require_cmd curl
require_cmd docker
require_cmd npm
require_cmd setsid
require_cmd ps

"$PY" -c 'import fastapi, sqlalchemy' >/dev/null 2>&1 \
    || fail "后端虚拟环境缺少依赖，请在 backend 中安装 requirements-dev.txt"

# Worker 需要直接访问 Docker socket。原脚本在 docker 无权限时用 sudo -u $USER
# 启动 Python，但这不会给当前用户增加 docker 组权限，容易出现“API 能开、判题 Worker 不能用”。
if ! docker info >/dev/null 2>&1; then
    if id -nG "$CURRENT_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        fail "当前 shell 尚未获得 docker 组权限。请执行: newgrp docker（或注销后重新登录），再重试。"
    else
        fail "当前用户无法直接访问 Docker。建议执行: sudo usermod -aG docker \"$CURRENT_USER\"，然后注销重登或执行 newgrp docker。"
    fi
fi
DOCKER=(docker)

# ── 2. MySQL + Redis ───────────────────────────────────────────
log "启动 MySQL 与 Redis..."
cd "$PROJECT_DIR"
"${DOCKER[@]}" compose up -d --wait --no-recreate mysql redis
log "MySQL/Redis 健康检查通过"

# ── 3. 数据库 bootstrap ───────────────────────────────────────
log "执行统一数据库 bootstrap（disposable 两阶段迁移）..."
cd "$PROJECT_DIR/backend"
if ! "$PY" "$PROJECT_DIR/scripts/bootstrap_database.py"; then
    fail "数据库 bootstrap 失败"
fi

# ── 4. 判题镜像 ───────────────────────────────────────────────
cd "$PROJECT_DIR"
for img in dai-judge-python:latest dai-kernel-python:latest; do
    if ! "${DOCKER[@]}" image inspect "$img" >/dev/null 2>&1; then
        log "构建镜像 $img ..."
        case "$img" in
            dai-judge*)  "${DOCKER[@]}" build -t "$img" backend/docker/judge ;;
            dai-kernel*) "${DOCKER[@]}" build -t "$img" backend/docker/kernel ;;
        esac
    fi
done

# ── 5. API + Workers ───────────────────────────────────────────
# 若 API 已运行，则必须确认本次请求的端口与上次一致。
if pid_alive api; then
    old_port="$(cat "$RUN_DIR/api.port" 2>/dev/null || true)"
    if [ -n "$old_port" ] && [ "$old_port" != "$API_PORT" ]; then
        fail "API 已在端口 $old_port 运行，但本次请求端口是 $API_PORT。请先 ./scripts/dev-down.sh"
    fi
elif port_busy "$API_PORT"; then
    fail "端口 $API_PORT 已被其他进程占用。排查: ss -ltnp | grep ':$API_PORT '"
else
    launch api "$PROJECT_DIR/backend" "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT"
fi
echo "$API_PORT" >"$RUN_DIR/api.port"

launch judge "$PROJECT_DIR/backend" "$PY" -m app.worker.judge_worker
launch envbuilder "$PROJECT_DIR/backend" "$PY" -m app.worker.environment_builder_worker
launch accounts "$PROJECT_DIR/backend" "$PY" -m app.worker.account_worker

# Worker 没有 HTTP 健康接口，至少确认启动后仍持续存活。
sleep 1
check_alive judge
check_alive envbuilder
check_alive accounts

# ── 6. 前端 ───────────────────────────────────────────────────
cd "$PROJECT_DIR/frontend"
if [ ! -d node_modules ]; then
    log "安装前端依赖..."
    npm ci
fi

if pid_alive frontend; then
    log "[跳过] frontend 已在运行 (PID $(cat "$RUN_DIR/frontend.pid"))"
elif port_busy "$FRONTEND_PORT"; then
    fail "端口 $FRONTEND_PORT 已被其他/残留进程占用。请先运行 ./scripts/dev-down.sh；仍占用时执行: ss -ltnp | grep ':$FRONTEND_PORT '"
else
    launch frontend "$PROJECT_DIR/frontend" env VITE_API_PROXY_TARGET="http://localhost:${API_PORT}" npm run dev
fi
echo "$FRONTEND_PORT" >"$RUN_DIR/frontend.port"

# ── 7. 真正的就绪检查 ─────────────────────────────────────────
log "等待后端就绪..."
wait_http api "http://127.0.0.1:${API_PORT}/api/v1/health/ready" 60 \
    || fail "后端未就绪。完整日志: $RUN_DIR/api.log"

log "等待前端就绪..."
wait_http frontend "http://127.0.0.1:${FRONTEND_PORT}/" 60 \
    || fail "前端未就绪。完整日志: $RUN_DIR/frontend.log"

# 最终再检查所有长期进程，避免健康检查期间某个 Worker 已退出。
check_alive api
check_alive judge
check_alive envbuilder
check_alive accounts
check_alive frontend

printf '\n\033[32m✔ 全部启动完成，并已通过存活/HTTP 检查\033[0m\n'
printf '  前端页面:   http://localhost:%s\n' "$FRONTEND_PORT"
printf '  Swagger:    http://localhost:%s/docs\n' "$API_PORT"
printf '  健康检查:   http://localhost:%s/api/v1/health/ready\n' "$API_PORT"
printf '  日志目录:   %s\n' "$RUN_DIR"
printf '  关闭:       ./scripts/dev-down.sh\n\n'

# ── 8. 自动打开浏览器 ─────────────────────────────────────────
if [ "${DAI_DEV_NO_BROWSER:-0}" != "1" ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        nohup xdg-open "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1 < /dev/null &
        log "已请求默认浏览器打开前端"
    elif command -v open >/dev/null 2>&1; then
        nohup open "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1 < /dev/null &
        log "已请求默认浏览器打开前端"
    else
        log "未找到 xdg-open/open，请手动打开前端地址"
    fi
fi
