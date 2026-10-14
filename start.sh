#!/usr/bin/env bash
# 人工智能基础实验平台：本地开发统一入口
#
#   ./start.sh                 启动完整开发环境并打开浏览器
#   ./start.sh --no-browser    启动但不打开浏览器
#   ./start.sh stop            停止项目
#   ./start.sh restart         重启项目
#   ./start.sh status          查看各服务状态
#   ./start.sh logs [服务名]   跟踪日志（默认全部）

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${DAI_DEV_RUN_DIR:-/tmp/dai-dev}"
UP_SCRIPT="$PROJECT_DIR/scripts/dev-up.sh"
DOWN_SCRIPT="$PROJECT_DIR/scripts/dev-down.sh"

usage() {
    cat <<'EOF'
用法：
  ./start.sh [start]        启动 MySQL、Redis、API、Workers 和前端
  ./start.sh --no-browser  启动项目，但不自动打开浏览器
  ./start.sh stop           停止项目（保留数据库数据卷）
  ./start.sh restart        重新启动项目
  ./start.sh status         查看进程、端口和容器状态
  ./start.sh logs [name]    跟踪日志；name 可为 api/frontend/judge/envbuilder/accounts/lab/kernel/practice/ai
  ./start.sh help           显示帮助

可选环境变量：
  DAI_DEV_API_PORT          API 端口，默认 8000
  DAI_DEV_RUN_DIR           PID 和日志目录，默认 /tmp/dai-dev
  DAI_PYTHON                后端 Python 路径，默认 backend/.venv/bin/python
EOF
}

require_scripts() {
    [ -x "$UP_SCRIPT" ] || { printf '启动脚本不可执行或不存在：%s\n' "$UP_SCRIPT" >&2; exit 1; }
    [ -x "$DOWN_SCRIPT" ] || { printf '关闭脚本不可执行或不存在：%s\n' "$DOWN_SCRIPT" >&2; exit 1; }
}

service_status() {
    local name="$1" pidfile="$RUN_DIR/$1.pid" pid
    if [ ! -f "$pidfile" ]; then
        printf '  %-12s %s\n' "$name" '未运行'
        return
    fi
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        printf '  %-12s 运行中（PID %s）\n' "$name" "$pid"
    else
        printf '  %-12s PID 文件失效\n' "$name"
    fi
}

show_status() {
    local api_port frontend_port
    api_port="$(cat "$RUN_DIR/api.port" 2>/dev/null || printf '%s' "${DAI_DEV_API_PORT:-8000}")"
    frontend_port="$(cat "$RUN_DIR/frontend.port" 2>/dev/null || printf '5173')"

    printf '应用进程：\n'
    for name in api frontend judge envbuilder accounts lab kernel practice ai; do
        service_status "$name"
    done

    printf '\n访问地址：\n'
    printf '  前端      http://localhost:%s\n' "$frontend_port"
    printf '  API 文档  http://localhost:%s/docs\n' "$api_port"
    printf '  健康检查  http://localhost:%s/api/v1/health/ready\n' "$api_port"

    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        printf '\n基础设施：\n'
        (cd "$PROJECT_DIR" && docker compose ps mysql redis) || true
    fi
}

follow_logs() {
    local name="${1:-all}"
    local files=()
    case "$name" in
        all)
            for candidate in api frontend judge envbuilder accounts lab kernel practice ai; do
                [ -f "$RUN_DIR/$candidate.log" ] && files+=("$RUN_DIR/$candidate.log")
            done
            ;;
        api|frontend|judge|envbuilder|accounts|lab|kernel|practice|ai)
            files+=("$RUN_DIR/$name.log")
            ;;
        *)
            printf '未知服务名：%s\n' "$name" >&2
            usage >&2
            exit 2
            ;;
    esac

    if [ "${#files[@]}" -eq 0 ]; then
        printf '尚无可用日志，请先启动项目。日志目录：%s\n' "$RUN_DIR" >&2
        exit 1
    fi
    printf '按 Ctrl+C 停止查看日志。\n'
    exec tail -n 80 -F "${files[@]}"
}

require_scripts
command="${1:-start}"

case "$command" in
    start)
        exec "$UP_SCRIPT"
        ;;
    --no-browser)
        export DAI_DEV_NO_BROWSER=1
        exec "$UP_SCRIPT"
        ;;
    stop)
        exec "$DOWN_SCRIPT"
        ;;
    restart)
        "$DOWN_SCRIPT"
        exec "$UP_SCRIPT"
        ;;
    status)
        show_status
        ;;
    logs)
        follow_logs "${2:-all}"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        printf '未知命令：%s\n\n' "$command" >&2
        usage >&2
        exit 2
        ;;
esac
