#!/usr/bin/env bash
# 人工智能基础实验平台 —— 本地开发一键关闭（稳健版）
#
# 用法: ./scripts/dev-down.sh
#
# 策略:
#   1) 优先根据 pidfile 停止每个服务的整个 process group；
#   2) 再扫描 /proc，清理 pidfile 丢失/失效后的本项目服务残留；
#   3) 复查前端/API 端口；
#   4) 最后停止 MySQL/Redis（Docker 出错不会阻止应用进程清理）。

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${DAI_DEV_RUN_DIR:-/tmp/dai-dev}"
FRONTEND_PORT="$(cat "$RUN_DIR/frontend.port" 2>/dev/null || printf '5173')"
API_PORT="$(cat "$RUN_DIR/api.port" 2>/dev/null || printf '%s' "${DAI_DEV_API_PORT:-8000}")"

log()  { printf '\033[36m[dev-down]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dev-down]\033[0m %s\n' "$*" >&2; }

proc_cwd() {
    readlink -f "/proc/$1/cwd" 2>/dev/null || true
}

proc_cmdline() {
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true
}

is_our_process() {
    local pid="$1" cwd
    kill -0 "$pid" 2>/dev/null || return 1
    cwd="$(proc_cwd "$pid")"
    [[ "$cwd" == "$PROJECT_DIR" || "$cwd" == "$PROJECT_DIR/"* ]]
}

# 兼容旧脚本产生的 PID（旧进程不一定是独立 PGID）。
killtree_fallback() {
    local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null || true); do
        killtree_fallback "$c"
    done
    kill -TERM "$p" 2>/dev/null || true
}

wait_pid_gone() {
    local pid="$1" i
    for ((i=0; i<30; i++)); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    return 1
}

wait_group_gone() {
    local pgid="$1" i
    for ((i=0; i<30; i++)); do
        kill -0 -- "-$pgid" 2>/dev/null || return 0
        sleep 0.2
    done
    return 1
}

stop_pid() {
    local name="$1" pid="$2" pgid self_pgid

    if ! is_our_process "$pid"; then
        return 1
    fi

    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)"

    # 新版 dev-up：PID 同时是新 session 的 PGID，可安全杀整个组。
    if [[ "$pgid" =~ ^[0-9]+$ ]] && [ "$pgid" = "$pid" ] && [ "$pgid" != "$self_pgid" ]; then
        kill -TERM -- "-$pgid" 2>/dev/null || true
        if ! wait_group_gone "$pgid"; then
            kill -KILL -- "-$pgid" 2>/dev/null || true
            sleep 0.2
            log "[停止] $name (PID $pid / PGID $pgid，已强制结束进程组)"
        else
            log "[停止] $name (PID $pid / PGID $pgid)"
        fi
    else
        # 兼容旧版脚本：递归 TERM，超时后对仍存活的主 PID KILL。
        killtree_fallback "$pid"
        if ! wait_pid_gone "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
            log "[停止] $name (PID $pid，兼容模式强制结束)"
        else
            log "[停止] $name (PID $pid，兼容模式)"
        fi
    fi
    return 0
}

# ── 1. 根据 pidfile 停止 ──────────────────────────────────────
for name in frontend api judge envbuilder accounts lab kernel practice ai; do
    pidfile="$RUN_DIR/$name.pid"
    if [ ! -f "$pidfile" ]; then
        log "[跳过] $name 无 PID 文件"
        continue
    fi

    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        if ! stop_pid "$name" "$pid"; then
            warn "[清理] $name 的 PID $pid 已不属于当前项目，仅删除失效 PID 文件"
        fi
    else
        log "[清理] $name 的 PID 文件已失效"
    fi
    rm -f "$pidfile"
done

# ── 2. /proc 二次清扫 ─────────────────────────────────────────
# 不只匹配某一个 python/vite 路径；同时要求 cwd 位于本项目，并且命令行是我们这四类服务。
# 这样能处理 npm 包装进程、pidfile 丢失、旧版脚本遗留等情况，又不会杀同项目里的普通 shell/editor。
declare -A residual_pgids=()
declare -A residual_pids=()

for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" = "$$" ] && continue
    kill -0 "$pid" 2>/dev/null || continue

    cwd="$(proc_cwd "$pid")"
    [[ "$cwd" == "$PROJECT_DIR/backend"* || "$cwd" == "$PROJECT_DIR/frontend"* ]] || continue

    cmd="$(proc_cmdline "$pid")"
    case "$cmd" in
        *"uvicorn app.main:app"*|*"app.worker.judge_worker"*|*"app.worker.kernel_worker"*|*"app.worker.practice_worker"*|*"app.worker.lab_worker"*|*"app.worker.environment_builder_worker"*|*"app.worker.account_worker"*|*"npm run dev"*|*"node_modules/.bin/vite"*|*" vite "*)
            pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
            if [[ "$pgid" =~ ^[0-9]+$ ]]; then
                residual_pgids["$pgid"]=1
            else
                residual_pids["$pid"]=1
            fi
            ;;
    esac
done

self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)"
for pgid in "${!residual_pgids[@]}"; do
    [ "$pgid" = "$self_pgid" ] && continue
    kill -TERM -- "-$pgid" 2>/dev/null || true
done
for pid in "${!residual_pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
done

if [ "${#residual_pgids[@]}" -gt 0 ] || [ "${#residual_pids[@]}" -gt 0 ]; then
    sleep 1
    # 第二遍强杀仍存在的残留进程组/PID。
    for pgid in "${!residual_pgids[@]}"; do
        [ "$pgid" = "$self_pgid" ] && continue
        kill -0 -- "-$pgid" 2>/dev/null && kill -KILL -- "-$pgid" 2>/dev/null || true
    done
    for pid in "${!residual_pids[@]}"; do
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    done
    log "[清扫] 已处理未登记/旧版残留进程"
fi

# ── 3. 端口复查 ───────────────────────────────────────────────
port_busy() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

for port in "$FRONTEND_PORT" "$API_PORT"; do
    if port_busy "$port"; then
        warn "端口 $port 仍被占用。当前占用者可能不是本项目；排查: ss -ltnp | grep ':$port '"
    else
        log "端口 $port 已释放"
    fi
done

rm -f "$RUN_DIR/frontend.port" "$RUN_DIR/api.port"

# ── 4. 停止 MySQL 与 Redis ────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    elif command -v sudo >/dev/null 2>&1; then
        DOCKER=(sudo docker)
    else
        DOCKER=()
    fi

    if [ "${#DOCKER[@]}" -gt 0 ]; then
        cd "$PROJECT_DIR"
        log "停止 MySQL 与 Redis 容器（数据卷保留）..."
        if "${DOCKER[@]}" compose stop mysql redis; then
            log "MySQL/Redis 容器已停止"
        else
            warn "Docker 容器停止失败，但应用进程清理已经完成"
        fi
    else
        warn "无法访问 Docker，跳过 MySQL/Redis 停止"
    fi
else
    warn "未找到 docker，跳过 MySQL/Redis 停止"
fi

printf '\n\033[32m✔ 关闭流程完成\033[0m\n'
printf '  再次启动: ./scripts/dev-up.sh\n'
printf '  若仍有端口占用: ss -ltnp | grep -E ":(%s|%s) "\n' "$FRONTEND_PORT" "$API_PORT"
printf '  注意: docker compose down 默认不会删除 named volumes；只有加 -v 才会删除卷。\n'
