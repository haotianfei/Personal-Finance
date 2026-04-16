#!/bin/bash

# ============================================
# person_fin 项目自动环境检测与启动脚本
# 功能：自动检测、安装 pyenv、nvm，配置环境，启动服务
# 支持：Linux、macOS
# ============================================

set -e

# 系统检测
detect_os() {
    if [[ "$(uname)" == "Darwin" ]]; then
        echo "macOS"
    elif [[ "$(uname)" == "Linux" ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            echo "$NAME"
        else
            echo "Linux"
        fi
    else
        echo "Unknown"
    fi
}

# 检查命令是否存在
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# 检查系统兼容性
check_system_compatibility() {
    local OS=$(detect_os)
    log_step "检查系统兼容性..."
    
    case "$OS" in
        *"Ubuntu"*|*"Debian"*|*"Deepin"*|*"Kali"*|*"CentOS"*|*"Fedora"*|*"Red Hat"*|*"macOS"*)
            log_info "系统兼容: $OS"
            return 0
            ;;
        *)
            log_warn "系统可能不兼容: $OS"
            log_info "尝试继续，可能需要手动调整"
            return 0
            ;;
    esac
}

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# 检测 Linux 发行版
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$NAME
        echo $OS
    elif [ -x /usr/bin/lsb_release ]; then
        lsb_release -d | cut -f2
    else
        echo "Unknown"
    fi
}

# ============================================
# Python 环境配置
# ============================================

install_pyenv_dependencies() {
    log_info "检查 pyenv 依赖..."
    OS=$(detect_os)
    
    # 检查是否有 sudo 权限
    if ! sudo -n true 2>/dev/null; then
        log_warn "无法使用 sudo，将跳过依赖自动安装"
        log_info "请确保已安装以下依赖："
        echo "  make, build-essential, libssl-dev, zlib1g-dev, libbz2-dev"
        echo "  libreadline-dev, libsqlite3-dev, wget, curl, llvm, libncursesw5-dev"
        echo "  xz-utils, tk-dev, libxml2-dev, libxmlsec1-dev, libffi-dev, liblzma-dev, git"
        return 0
    fi
    
    case "$OS" in
        *"Ubuntu"*|*"Debian"*|*"Deepin"*|*"Kali"*)
            sudo apt-get update -qq
            sudo apt-get install -y -qq \
                make build-essential libssl-dev zlib1g-dev \
                libbz2-dev libreadline-dev libsqlite3-dev \
                wget curl llvm libncursesw5-dev xz-utils \
                tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
                git > /dev/null 2>&1
            ;;
        *"CentOS"*|*"Fedora"*|*"Red Hat"*)
            sudo yum groupinstall -y "Development Tools"
            sudo yum install -y \
                openssl-devel bzip2-devel readline-devel sqlite-devel \
                wget curl llvm ncurses-devel xz tk-devel \
                libxml2-devel libxmlsec1-devel libffi-devel zlib-devel \
                git > /dev/null 2>&1
            ;;
        *)
            log_warn "未识别的操作系统，请手动安装依赖"
            return 1
            ;;
    esac
    log_info "pyenv 依赖安装完成"
}

setup_python_env() {
    local PYTHON_VERSION="3.11.11"
    
    log_step "检查 Python 环境..."
    
    export PYENV_ROOT="$HOME/.pyenv"
    
    # 检查 pyenv（检查目录和命令）
    if [ ! -d "$PYENV_ROOT" ] || [ ! -f "$PYENV_ROOT/bin/pyenv" ]; then
        log_info "pyenv 未安装，开始安装..."
        
        # 检查 git
        if ! command -v git &> /dev/null; then
            log_error "git 未安装，请先安装 git"
            return 1
        fi
        
        # 安装依赖
        install_pyenv_dependencies || true
        
        # 清理非空目录
        if [ -d "$PYENV_ROOT" ] && [ ! -f "$PYENV_ROOT/bin/pyenv" ]; then
            rm -rf "$PYENV_ROOT"
        fi
        
        # 安装 pyenv
        git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT" 2>/dev/null || \
        git clone --depth 1 https://gitee.com/mirrors/pyenv.git "$PYENV_ROOT"
        
        # 配置环境变量
        if ! grep -q "PYENV_ROOT" "$HOME/.bashrc" 2>/dev/null; then
            cat >> "$HOME/.bashrc" << 'EOF'
export PYENV_ROOT="$HOME/.pyenv"
command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
EOF
        fi
        
        export PYENV_ROOT="$HOME/.pyenv"
        export PATH="$PYENV_ROOT/bin:$PATH"
        eval "$(pyenv init -)"
        
        log_info "pyenv 安装完成"
    else
        log_info "pyenv 已安装"
        export PYENV_ROOT="$HOME/.pyenv"
        export PATH="$PYENV_ROOT/bin:$PATH"
        eval "$(pyenv init -)" || true
    fi
    
    # 检查 Python 版本
    if ! pyenv versions 2>/dev/null | grep -q "$PYTHON_VERSION"; then
        log_info "安装 Python $PYTHON_VERSION..."
        
        # 从国内镜像下载
        mkdir -p "$PYENV_ROOT/cache"
        PYTHON_TARBALL="Python-$PYTHON_VERSION.tar.xz"
        MIRROR_URL="https://mirrors.tuna.tsinghua.edu.cn/python/$PYTHON_VERSION/$PYTHON_TARBALL"
        
        if [ ! -f "$PYENV_ROOT/cache/$PYTHON_TARBALL" ]; then
            curl -sL --connect-timeout 60 -o "$PYENV_ROOT/cache/$PYTHON_TARBALL" "$MIRROR_URL" || true
        fi
        
        pyenv install "$PYTHON_VERSION"
        log_info "Python $PYTHON_VERSION 安装完成"
    else
        log_info "Python $PYTHON_VERSION 已安装"
    fi
    
    # 设置全局版本
    pyenv global "$PYTHON_VERSION"
    
    # 创建虚拟环境
    if [ ! -d "$SCRIPT_DIR/.venv" ]; then
        log_info "创建 Python 虚拟环境..."
        cd "$SCRIPT_DIR" && python -m venv .venv
        source "$SCRIPT_DIR/.venv/bin/activate"
        pip install -q --upgrade pip
        pip install -q uvicorn fastapi
        log_info "虚拟环境创建完成"
    else
        log_info "虚拟环境已存在"
        source "$SCRIPT_DIR/.venv/bin/activate"
    fi
}

# ============================================
# Node.js 环境配置
# ============================================

setup_node_env() {
    local NODE_VERSION="20"
    
    log_step "检查 Node.js 环境..."
    
    export NVM_DIR="$HOME/.nvm"
    
    # 检查 nvm（检查目录和命令）
    if [ ! -d "$NVM_DIR" ] || [ ! -f "$NVM_DIR/nvm.sh" ]; then
        log_info "nvm 未安装，开始安装..."
        
        # 清理非空目录
        if [ -d "$NVM_DIR" ] && [ ! -f "$NVM_DIR/nvm.sh" ]; then
            rm -rf "$NVM_DIR"
        fi
        
        # 安装 nvm
        git clone --depth 1 https://github.com/nvm-sh/nvm.git "$NVM_DIR" 2>/dev/null || \
        git clone --depth 1 https://gitee.com/RockyZhang/nvm.git "$NVM_DIR"
        
        # 配置环境变量
        if ! grep -q "NVM_DIR" "$HOME/.bashrc" 2>/dev/null; then
            cat >> "$HOME/.bashrc" << 'EOF'
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
EOF
        fi
        
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
        [ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
        
        log_info "nvm 安装完成"
    else
        log_info "nvm 已安装"
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    fi
    
    # 检查 Node.js 版本
    if ! command -v node &> /dev/null; then
        log_info "安装 Node.js $NODE_VERSION..."
        
        export NVM_NODEJS_ORG_MIRROR="https://npmmirror.com/mirrors/node"
        nvm install "$NODE_VERSION"
        nvm alias default "$NODE_VERSION"
        
        # 配置 npm 镜像
        npm config set registry https://registry.npmmirror.com
        
        log_info "Node.js 安装完成"
    else
        log_info "Node.js 已安装：$(node --version)"
    fi
    
    # 安装 frontend 依赖
    if [ -d "$SCRIPT_DIR/frontend" ]; then
        cd "$SCRIPT_DIR/frontend"
        if [ ! -d "node_modules" ]; then
            log_info "安装 frontend 依赖..."
            npm install --silent
            log_info "Frontend 依赖安装完成"
        else
            log_info "Frontend 依赖已存在"
        fi
    fi
}

# ============================================
# 启动服务
# ============================================

start_backend() {
    log_step "启动 Backend 服务..."
    
    # 检查并停止占用端口的进程
    local PORT=8000
    local PID=$(lsof -ti:$PORT 2>/dev/null || ss -tlnp 2>/dev/null | grep ":$PORT " | awk '{print $NF}' | grep -oP 'pid=\K[0-9]+' | head -1)
    
    if [ -n "$PID" ]; then
        log_warn "端口 $PORT 被占用 (PID: $PID)，尝试停止..."
        kill $PID 2>/dev/null || true
        sleep 2
        
        # 如果还在运行，强制终止
        if lsof -ti:$PORT > /dev/null 2>&1; then
            log_warn "强制终止进程..."
            kill -9 $PID 2>/dev/null || true
            sleep 1
        fi
    fi
    
    # 激活虚拟环境并启动
    cd "$SCRIPT_DIR/backend"
    source "$SCRIPT_DIR/.venv/bin/activate"
    
    nohup python -m uvicorn main:app --host 0.0.0.0 --port $PORT > "$SCRIPT_DIR/backend.log" 2>&1 &
    BACKEND_PID=$!
    
    sleep 2
    
    if ps -p $BACKEND_PID > /dev/null 2>&1; then
        log_info "Backend 启动成功 (PID: $BACKEND_PID)"
        log_info "访问地址：http://localhost:$PORT"
        echo $BACKEND_PID > "$SCRIPT_DIR/.backend.pid"
    else
        log_error "Backend 启动失败，查看日志：$SCRIPT_DIR/backend.log"
        cat "$SCRIPT_DIR/backend.log" 2>/dev/null
        return 1
    fi
}

start_frontend() {
    log_step "启动 Frontend 服务..."
    
    # 加载 nvm
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    
    # 检查并停止占用端口的进程
    local PORT=3000
    local MAX_RETRIES=3
    local RETRY_COUNT=0
    
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        local PID=$(lsof -ti:$PORT 2>/dev/null || ss -tlnp 2>/dev/null | grep ":$PORT " | awk '{print $NF}' | grep -oP 'pid=\K[0-9]+' | head -1)
        
        if [ -n "$PID" ]; then
            log_warn "端口 $PORT 被占用 (PID: $PID)，尝试停止..."
            kill $PID 2>/dev/null || true
            sleep 3
            
            # 如果还在运行，强制终止
            if lsof -ti:$PORT > /dev/null 2>&1; then
                log_warn "强制终止进程..."
                kill -9 $PID 2>/dev/null || true
                sleep 2
            fi
            
            # 再次检查端口是否释放
            if lsof -ti:$PORT > /dev/null 2>&1; then
                RETRY_COUNT=$((RETRY_COUNT + 1))
                log_warn "端口仍被占用，重试 $RETRY_COUNT/$MAX_RETRIES..."
                sleep 2
                continue
            fi
        fi
        break
    done
    
    # 确保端口已释放
    if lsof -ti:$PORT > /dev/null 2>&1; then
        log_error "端口 $PORT 无法释放，请手动检查"
        return 1
    fi
    
    cd "$SCRIPT_DIR/frontend"
    
    nohup npm run dev -- -p $PORT > "$SCRIPT_DIR/frontend.log" 2>&1 &
    FRONTEND_PID=$!
    
    # 等待更长时间确保服务启动
    sleep 5
    
    # 检查进程是否还在运行
    if ! ps -p $FRONTEND_PID > /dev/null 2>&1; then
        # 检查日志是否显示端口占用错误
        if grep -q "EADDRINUSE" "$SCRIPT_DIR/frontend.log" 2>/dev/null; then
            log_warn "Frontend 启动时端口被占用，尝试重新启动..."
            sleep 2
            # 强制清理所有 next dev 进程
            pkill -f "next dev" 2>/dev/null || true
            sleep 3
            
            # 再次尝试启动
            nohup npm run dev -- -p $PORT > "$SCRIPT_DIR/frontend.log" 2>&1 &
            FRONTEND_PID=$!
            sleep 5
        fi
    fi
    
    if ps -p $FRONTEND_PID > /dev/null 2>&1; then
        log_info "Frontend 启动成功 (PID: $FRONTEND_PID)"
        log_info "访问地址：http://localhost:$PORT"
        echo $FRONTEND_PID > "$SCRIPT_DIR/.frontend.pid"
    else
        log_error "Frontend 启动失败，查看日志：$SCRIPT_DIR/frontend.log"
        cat "$SCRIPT_DIR/frontend.log" 2>/dev/null
        return 1
    fi
}

# ============================================
# 主函数
# ============================================

main() {
    echo ""
    echo "============================================"
    echo "  person_fin 项目自动环境配置与启动脚本"
    echo "============================================"
    echo ""
    
    # 0. 检查系统兼容性
    check_system_compatibility
    
    # 1. 配置 Python 环境
    setup_python_env
    
    # 2. 配置 Node.js 环境
    setup_node_env
    
    echo ""
    log_info "环境配置完成!"
    echo ""
    
    # 3. 启动服务
    start_backend
    echo ""
    start_frontend
    
    echo ""
    echo "============================================"
    log_info "所有服务已启动!"
    echo "============================================"
    echo ""
    echo "服务访问地址:"
    echo "  Backend:  http://localhost:8000"
    echo "  Frontend: http://localhost:3000"
    echo ""
    echo "日志文件:"
    echo "  Backend:  $SCRIPT_DIR/backend.log"
    echo "  Frontend: $SCRIPT_DIR/frontend.log"
    echo ""
    echo "停止服务:"
    echo "  $SCRIPT_DIR/person_fin.sh stop"
    echo ""
    
    # 保存 PID 以便后续管理
    jobs -p > "$SCRIPT_DIR/.jobs.pid"
}

# 停止服务
stop_services() {
    log_step "停止服务..."
    
    # 读取并停止 backend
    if [ -f "$SCRIPT_DIR/.backend.pid" ]; then
        kill $(cat "$SCRIPT_DIR/.backend.pid") 2>/dev/null || true
        rm -f "$SCRIPT_DIR/.backend.pid"
        log_info "Backend 已停止"
    fi
    
    # 读取并停止 frontend
    if [ -f "$SCRIPT_DIR/.frontend.pid" ]; then
        kill $(cat "$SCRIPT_DIR/.frontend.pid") 2>/dev/null || true
        rm -f "$SCRIPT_DIR/.frontend.pid"
        log_info "Frontend 已停止"
    fi
    
    # 清理残留进程
    pkill -f "uvicorn main:app" 2>/dev/null || true
    pkill -f "next dev" 2>/dev/null || true
    pkill -f "npm run dev" 2>/dev/null || true
    
    log_info "所有服务已停止"
}

# 查看状态
check_status() {
    log_step "服务状态:"
    
    echo ""
    echo "Backend (端口 8000):"
    if pgrep -f "uvicorn main:app" > /dev/null; then
        echo -e "  ${GREEN}运行中${NC} (PID: $(pgrep -f 'uvicorn main:app'))"
    else
        echo -e "  ${RED}未运行${NC}"
    fi
    
    echo ""
    echo "Frontend (端口 3000):"
    if pgrep -f "next dev" > /dev/null; then
        echo -e "  ${GREEN}运行中${NC} (PID: $(pgrep -f 'next dev'))"
    else
        echo -e "  ${RED}未运行${NC}"
    fi
    echo ""
}

# 处理命令行参数
case "${1:-start}" in
    start)
        main
        ;;
    stop)
        stop_services
        ;;
    restart)
        stop_services
        sleep 1
        main
        ;;
    status)
        check_status
        ;;
    *)
        echo "用法：$0 {start|stop|restart|status}"
        exit 1
        ;;
esac
