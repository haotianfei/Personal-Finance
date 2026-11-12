#!/bin/bash
# ============================================
# person_fin — 一键启动脚本
# 用法: ./start.sh [start|stop|restart|status] [prod|dev]
#   prod (默认): 前端生产模式 next build + next start，页面秒开，源码有改动自动重新构建
#   dev:         前端开发模式 next dev，按需编译(首次打开页面较慢)但支持热更新
# 也可用环境变量: FRONTEND_MODE=dev ./start.sh
# ============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
VENV_DIR="$SCRIPT_DIR/.venv"
BACKEND_LOG="$SCRIPT_DIR/backend_dev.log"
FRONTEND_LOG="$SCRIPT_DIR/frontend_dev.log"
BACKEND_PID_FILE="$SCRIPT_DIR/.backend.pid"
FRONTEND_PID_FILE="$SCRIPT_DIR/.frontend.pid"
BACKEND_PORT=8000
FRONTEND_PORT=3000
FRONTEND_MODE="${FRONTEND_MODE:-prod}"   # prod=生产构建 | dev=开发热更新
FRONTEND_BUILD_LOG="$SCRIPT_DIR/frontend_build.log"

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${BLUE}[STEP]${NC}  $1"; }

# ---- 工具 ----
cleanup_port() {
    local port=$1 pid
    pid=$(ss -tlnp 2>/dev/null | grep ":$port " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' || true)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null && sleep 1 && info "释放端口 $port"
    fi
    return 0
}

# ============================================
# 环境准备
# ============================================
prepare_env() {
    step "检查 Python 虚拟环境..."
    local PYTHON="$VENV_DIR/bin/python"
    local PIP="$VENV_DIR/bin/pip"
    if [ ! -f "$PYTHON" ]; then
        error "未找到虚拟环境: $VENV_DIR"
        exit 1
    fi

    # 自动安装/更新后端依赖
    if [ -f "$BACKEND_DIR/requirements.txt" ]; then
        "$PIP" install -q -r "$BACKEND_DIR/requirements.txt" --upgrade
        info "后端依赖已就绪"
    fi

    step "检查前端依赖..."
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        warn "node_modules 不存在，执行 npm install..."
        (cd "$FRONTEND_DIR" && npm install --silent)
    fi
    info "前端依赖已就绪"
}

# ============================================
# 启动服务
# ============================================
wait_for_port() {
    local port=$1 timeout=${2:-30}
    for i in $(seq 1 $timeout); do
        if ss -tlnp 2>/dev/null | grep -q ":$port " 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

start_backend() {
    step "启动后端服务 (port $BACKEND_PORT)..."
    cleanup_port $BACKEND_PORT
    local PYTHON="$VENV_DIR/bin/python"
    cd "$BACKEND_DIR"
    mkdir -p logs
    # 后端日志：结构化日志输出到 backend/logs/backend.log（JSON 按天轮转）
    # stdout/stderr 兜底仍写 backend_dev.log；prod 模式用 JSON，dev 模式用彩色 console
    if [ "$FRONTEND_MODE" = "prod" ]; then
        export LOG_FORMAT=json
    else
        export LOG_FORMAT=console
    fi
    export LOG_LEVEL=INFO
    nohup "$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port $BACKEND_PORT >> "$BACKEND_LOG" 2>&1 &
    local pid=$!
    echo $pid > "$BACKEND_PID_FILE"
    if wait_for_port $BACKEND_PORT 15; then
        info "后端服务已启动 (PID: $pid) → http://localhost:$BACKEND_PORT"
    else
        error "后端启动超时，日志: $BACKEND_LOG"; tail -5 "$BACKEND_LOG"; return 1
    fi
}

# 判断前端生产构建是否过期（源码/配置比上次构建产物新）
frontend_needs_build() {
    local build_id="$FRONTEND_DIR/.next/BUILD_ID"
    [ -f "$build_id" ] || return 0
    local newer
    newer=$(find "$FRONTEND_DIR/src" "$FRONTEND_DIR/next.config.js" "$FRONTEND_DIR/package.json" \
        "$FRONTEND_DIR/tsconfig.json" "$FRONTEND_DIR/tailwind.config.js" "$FRONTEND_DIR/postcss.config.js" \
        -newer "$build_id" -print -quit 2>/dev/null || true)
    [ -n "$newer" ]
}

start_frontend() {
    cd "$FRONTEND_DIR"
    local timeout=60
    if [ "$FRONTEND_MODE" = "prod" ]; then
        if frontend_needs_build; then
            step "前端生产构建中 (npm run build，首次约需几分钟)..."
            if ! npm run build 2>&1 | tee "$FRONTEND_BUILD_LOG"; then
                error "前端构建失败，日志: $FRONTEND_BUILD_LOG"; tail -20 "$FRONTEND_BUILD_LOG"; return 1
            fi
            info "前端构建完成"
        else
            info "前端构建产物已是最新，跳过构建"
        fi
        step "启动前端服务 (port $FRONTEND_PORT, 生产模式)..."
        cleanup_port $FRONTEND_PORT
        nohup npm run start -- -p $FRONTEND_PORT > "$FRONTEND_LOG" 2>&1 &
        timeout=20
    else
        step "启动前端服务 (port $FRONTEND_PORT, 开发模式 — 注意: 每个页面首次打开需按需编译)..."
        cleanup_port $FRONTEND_PORT
        nohup npm run dev -- -p $FRONTEND_PORT > "$FRONTEND_LOG" 2>&1 &
    fi
    local pid=$!
    echo $pid > "$FRONTEND_PID_FILE"
    if wait_for_port $FRONTEND_PORT $timeout; then
        info "前端服务已启动 (PID: $pid) → http://localhost:$FRONTEND_PORT"
    else
        error "前端启动超时，日志: $FRONTEND_LOG"; tail -10 "$FRONTEND_LOG"; return 1
    fi
}

# ============================================
# 管理命令
# ============================================
stop_services() {
    step "停止服务..."
    for pid_file in "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"; do
        if [ -f "$pid_file" ]; then
            kill "$(cat "$pid_file")" 2>/dev/null && info "已停止 $(basename "$pid_file" .pid)"
            rm -f "$pid_file"
        fi
    done
    # 清理残留
    pkill -f "uvicorn main:app" 2>/dev/null || true
    pkill -f "next dev" 2>/dev/null || true
    pkill -f "next start" 2>/dev/null || true
    pkill -f "next-server" 2>/dev/null || true
    info "所有服务已停止"
}

check_status() {
    echo ""
    echo "  服务              端口      状态"
    echo "  ─────────────────────────────────"
    local backend_running="否"
    local frontend_running="否"
    if ss -tlnp 2>/dev/null | grep -q ":$BACKEND_PORT "; then backend_running="是"; fi
    if ss -tlnp 2>/dev/null | grep -q ":$FRONTEND_PORT "; then frontend_running="是"; fi

    if [ "$backend_running" = "是" ]; then
        printf "  %-18s %-9s ${GREEN}运行中${NC}\n" "Backend (FastAPI)" ":$BACKEND_PORT"
    else
        printf "  %-18s %-9s ${RED}未运行${NC}\n" "Backend (FastAPI)" ":$BACKEND_PORT"
    fi
    if [ "$frontend_running" = "是" ]; then
        printf "  %-18s %-9s ${GREEN}运行中${NC}\n" "Frontend (Next.js)" ":$FRONTEND_PORT"
    else
        printf "  %-18s %-9s ${RED}未运行${NC}\n" "Frontend (Next.js)" ":$FRONTEND_PORT"
    fi
    echo ""
}

# ============================================
# 主入口
# ============================================
case "${2:-}" in
    prod|dev) FRONTEND_MODE="$2" ;;
    "") ;;
    *) echo "用法: $0 {start|stop|restart|status} [prod|dev]"; exit 1 ;;
esac

case "${1:-start}" in
    start)
        echo "============================================"
        echo "  person_fin — 一键启动 (前端: $FRONTEND_MODE)"
        echo "============================================"
        echo ""
        prepare_env
        start_backend
        start_frontend
        echo ""
        echo "============================================"
        info "服务已全部启动！"
        echo "  Frontend: http://localhost:$FRONTEND_PORT  ($FRONTEND_MODE)"
        echo "  Backend:  http://localhost:$BACKEND_PORT"
        echo "  API 文档: http://localhost:$BACKEND_PORT/docs"
        echo "  停止:     ./start.sh stop"
        [ "$FRONTEND_MODE" = "dev" ] && echo "  提示:     去掉 dev 即生产模式，页面免编译秒开"
        echo "============================================"
        ;;
    stop)
        stop_services
        ;;
    restart)
        stop_services
        sleep 1
        exec "$0" start "$FRONTEND_MODE"
        ;;
    status)
        check_status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status} [prod|dev]"
        exit 1
        ;;
esac