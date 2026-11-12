#!/bin/bash

# ============================================
# nvm + Node.js 安装脚本 (使用国内源)
# Node.js 版本：LTS (20.x)
# ============================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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

# 安装依赖
install_dependencies() {
    log_info "检查 nvm 依赖..."
    
    # 检查 curl 或 wget
    if ! command -v curl &> /dev/null && ! command -v wget &> /dev/null; then
        log_error "需要安装 curl 或 wget"
        OS=$(detect_os)
        case "$OS" in
            *"Ubuntu"*|*"Debian"*)
                echo "  sudo apt-get install curl"
                ;;
            *"CentOS"*|*"Fedora"*)
                echo "  sudo yum install curl"
                ;;
        esac
        exit 1
    fi
    
    log_info "依赖检查通过"
}

# 安装 nvm
install_nvm() {
    log_info "安装 nvm..."
    
    # 检查是否已安装
    if [ -d "$HOME/.nvm" ]; then
        log_warn "nvm 已存在，跳过安装"
        return 0
    fi
    
    # 使用国内镜像安装 nvm (使用 Gitee 镜像)
    export NVM_INSTALL_REPO="https://gitee.com/RockyZhang/nvm.git"
    export NVM_DIR="$HOME/.nvm"
    
    log_info "从 Gitee 镜像克隆 nvm..."
    git clone --depth 1 $NVM_INSTALL_REPO "$NVM_DIR" || {
        log_warn "Gitee 镜像失败，尝试 GitHub..."
        git clone --depth 1 https://github.com/nvm-sh/nvm.git "$NVM_DIR"
    }
    
    log_info "nvm 安装完成"
}

# 配置环境变量
configure_shell() {
    log_info "配置 shell 环境变量..."
    
    NVM_INIT='
# nvm configuration
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
'
    
    # 检测 shell 类型
    SHELL_NAME=$(basename "$SHELL")
    
    case "$SHELL_NAME" in
        bash)
            INIT_FILE="$HOME/.bashrc"
            ;;
        zsh)
            INIT_FILE="$HOME/.zshrc"
            ;;
        *)
            log_warn "未知的 shell: $SHELL_NAME，默认使用 .bashrc"
            INIT_FILE="$HOME/.bashrc"
            ;;
    esac
    
    # 检查是否已配置
    if grep -q "NVM_DIR" "$INIT_FILE" 2>/dev/null; then
        log_warn "nvm 配置已存在于 $INIT_FILE"
    else
        echo "$NVM_INIT" >> "$INIT_FILE"
        log_info "已将 nvm 配置添加到 $INIT_FILE"
    fi
    
    # 立即生效
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    [ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
}

# 安装 Node.js
install_node() {
    local NODE_VERSION=${1:-"20"}
    
    log_info "准备安装 Node.js $NODE_VERSION..."
    
    # 设置 npm 国内镜像
    export NVM_NODEJS_ORG_MIRROR="https://npmmirror.com/mirrors/node"
    
    # 检查是否已安装
    if command -v node &> /dev/null; then
        log_warn "Node.js 已安装：$(node --version)"
        read -p "是否重新安装？(y/N): " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            return 0
        fi
    fi
    
    log_info "开始安装 Node.js $NODE_VERSION (使用 npmmirror 镜像)..."
    
    if nvm install "$NODE_VERSION"; then
        log_info "Node.js 安装成功!"
        
        # 设置为默认版本
        nvm alias default "$NODE_VERSION"
        
        # 验证安装
        log_info "验证安装..."
        node --version
        npm --version
    else
        log_error "Node.js 安装失败"
        return 1
    fi
}

# 配置 npm 国内源
configure_npm() {
    log_info "配置 npm 国内源..."
    
    # 设置淘宝镜像
    npm config set registry https://registry.npmmirror.com
    
    # 验证配置
    NPM_REGISTRY=$(npm config get registry)
    log_info "npm registry 已配置为：$NPM_REGISTRY"
}

# 主函数
main() {
    echo "============================================"
    echo "  nvm + Node.js 安装脚本 (国内源)"
    echo "============================================"
    echo ""
    
    # 1. 安装依赖
    install_dependencies
    
    # 2. 安装 nvm
    install_nvm
    
    # 3. 配置 shell
    configure_shell
    
    # 4. 安装 Node.js
    install_node "lts"
    
    # 5. 配置 npm
    configure_npm
    
    echo ""
    echo "============================================"
    log_info "安装完成!"
    echo "============================================"
    echo ""
    echo "请执行以下命令使配置生效:"
    echo "  source \$HOME/.bashrc  (或 source \$HOME/.zshrc)"
    echo ""
    echo "验证安装:"
    echo "  nvm --version"
    echo "  node --version"
    echo "  npm --version"
    echo ""
}

# 运行主函数
main "$@"
