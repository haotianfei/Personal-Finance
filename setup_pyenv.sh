#!/bin/bash

# ============================================
# pyenv 安装和配置脚本 (使用国内源)
# Python 版本：3.11.11
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
    log_info "安装 pyenv 依赖..."
    
    OS=$(detect_os)
    
    # 检查是否有 sudo 权限
    if ! command -v sudo &> /dev/null; then
        log_warn "sudo 不可用，请确保已安装以下依赖:"
        print_dependency_list "$OS"
        return 1
    fi
    
    # 尝试使用 sudo，如果需要密码则提示用户
    case "$OS" in
        *"Ubuntu"*|*"Debian"*|*"Deepin"*|*"Kali"*)
            if sudo -n apt-get update 2>/dev/null; then
                sudo apt-get update
                sudo apt-get install -y \
                    make \
                    build-essential \
                    libssl-dev \
                    zlib1g-dev \
                    libbz2-dev \
                    libreadline-dev \
                    libsqlite3-dev \
                    wget \
                    curl \
                    llvm \
                    libncursesw5-dev \
                    xz-utils \
                    tk-dev \
                    libxml2-dev \
                    libxmlsec1-dev \
                    libffi-dev \
                    liblzma-dev
            else
                log_warn "需要 sudo 权限来安装依赖"
                log_info "请手动运行以下命令安装依赖:"
                echo "  sudo apt-get update"
                echo "  sudo apt-get install -y make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev"
                read -p "按回车键继续 (如果依赖已安装可跳过)..."
            fi
            ;;
        *"CentOS"*|*"Fedora"*|*"Red Hat"*)
            if sudo -n yum --version 2>/dev/null; then
                sudo yum groupinstall -y "Development Tools"
                sudo yum install -y \
                    openssl-devel \
                    bzip2-devel \
                    readline-devel \
                    sqlite-devel \
                    wget \
                    curl \
                    llvm \
                    ncurses-devel \
                    xz \
                    tk-devel \
                    libxml2-devel \
                    libxmlsec1-devel \
                    libffi-devel \
                    zlib-devel
            else
                log_warn "需要 sudo 权限来安装依赖"
                log_info "请手动运行以下命令安装依赖:"
                echo "  sudo yum groupinstall -y 'Development Tools'"
                echo "  sudo yum install -y openssl-devel bzip2-devel readline-devel sqlite-devel wget curl llvm ncurses-devel xz tk-devel libxml2-devel libxmlsec1-devel libffi-devel zlib-devel"
                read -p "按回车键继续 (如果依赖已安装可跳过)..."
            fi
            ;;
        *"Arch"*|*"Manjaro"*)
            if sudo -n pacman --version 2>/dev/null; then
                sudo pacman -Syu --noconfirm
                sudo pacman -S --noconfirm \
                    base-devel \
                    openssl \
                    zlib \
                    bzip2 \
                    readline \
                    sqlite \
                    wget \
                    curl \
                    llvm \
                    ncurses \
                    xz \
                    tk \
                    libxml2 \
                    libxmlsec1 \
                    libffi
            else
                log_warn "需要 sudo 权限来安装依赖"
                log_info "请手动运行以下命令安装依赖:"
                echo "  sudo pacman -Syu --noconfirm"
                echo "  sudo pacman -S --noconfirm base-devel openssl zlib bzip2 readline sqlite wget curl llvm ncurses xz tk libxml2 libxmlsec1 libffi"
                read -p "按回车键继续 (如果依赖已安装可跳过)..."
            fi
            ;;
        *)
            log_warn "未识别的操作系统：$OS，请手动安装依赖"
            print_dependency_list "$OS"
            read -p "按回车键继续..."
            ;;
    esac
    
    log_info "依赖检查完成"
}

# 打印依赖列表
print_dependency_list() {
    local os=$1
    echo "需要的依赖包:"
    echo "  - make, build-essential/base-devel"
    echo "  - libssl-dev/openssl-devel"
    echo "  - zlib1g-dev/zlib-devel"
    echo "  - libbz2-dev/bzip2-devel"
    echo "  - libreadline-dev/readline-devel"
    echo "  - libsqlite3-dev/sqlite-devel"
    echo "  - wget, curl"
    echo "  - llvm"
    echo "  - libncursesw5-dev/ncurses-devel"
    echo "  - xz-utils/xz"
    echo "  - tk-dev/tk-devel"
    echo "  - libxml2-dev/libxml2-devel"
    echo "  - libxmlsec1-dev/libxmlsec1-devel"
    echo "  - libffi-dev/libffi-devel"
    echo "  - liblzma-dev"
}

# 安装 pyenv
install_pyenv() {
    log_info "安装 pyenv..."
    
    # 检查是否已安装
    if [ -d "$HOME/.pyenv" ]; then
        log_warn "pyenv 已存在，跳过安装"
        return 0
    fi
    
    # 使用国内镜像安装 pyenv (使用 Git 镜像)
    export GIT_REPO="https://gitee.com/mirrors/pyenv.git"
    
    git clone --depth 1 $GIT_REPO "$HOME/.pyenv" || {
        log_warn "Gitee 镜像失败，尝试 GitHub..."
        git clone --depth 1 https://github.com/pyenv/pyenv.git "$HOME/.pyenv"
    }
    
    log_info "pyenv 安装完成"
}

# 配置环境变量
configure_shell() {
    log_info "配置 shell 环境变量..."
    
    PYENV_INIT='
# pyenv configuration
export PYENV_ROOT="$HOME/.pyenv"
command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"
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
        fish)
            INIT_FILE="$HOME/.config/fish/config.fish"
            PYENV_INIT='
# pyenv configuration for fish
set -Ux PYENV_ROOT $HOME/.pyenv
set -Ux PATH $PYENV_ROOT/bin $PATH
pyenv init - | source
pyenv virtualenv-init - | source
'
            ;;
        *)
            log_warn "未知的 shell: $SHELL_NAME，默认使用 .bashrc"
            INIT_FILE="$HOME/.bashrc"
            ;;
    esac
    
    # 检查是否已配置
    if grep -q "PYENV_ROOT" "$INIT_FILE" 2>/dev/null; then
        log_warn "pyenv 配置已存在于 $INIT_FILE"
    else
        echo "$PYENV_INIT" >> "$INIT_FILE"
        log_info "已将 pyenv 配置添加到 $INIT_FILE"
    fi
    
    # 立即生效
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init -)"
    eval "$(pyenv virtualenv-init - 2>/dev/null)" || true
}

# 安装 Python
install_python() {
    local PYTHON_VERSION=${1:-"3.11.11"}
    
    log_info "准备安装 Python $PYTHON_VERSION..."
    
    # 检查是否已安装
    if pyenv versions | grep -q "$PYTHON_VERSION"; then
        log_warn "Python $PYTHON_VERSION 已安装"
    else
        log_info "开始安装 Python $PYTHON_VERSION (使用国内镜像)..."
        
        # 直接从国内镜像下载并放入缓存
        mkdir -p "$PYENV_ROOT/cache"
        PYTHON_TARBALL="Python-$PYTHON_VERSION.tar.xz"
        
        # 使用清华大学镜像作为主源，阿里云和淘宝作为备用
        MIRROR_URL="https://mirrors.tuna.tsinghua.edu.cn/python/$PYTHON_VERSION/$PYTHON_TARBALL"
        BACKUP_MIRROR_URL="https://mirrors.aliyun.com/python/$PYTHON_VERSION/$PYTHON_TARBALL"
        BACKUP_MIRROR_URL2="https://npm.taobao.org/mirrors/python/$PYTHON_VERSION/$PYTHON_TARBALL"
        
        if [ ! -f "$PYENV_ROOT/cache/$PYTHON_TARBALL" ]; then
            log_info "从清华大学镜像下载 Python 源码包..."
            if ! curl -L --connect-timeout 60 -o "$PYENV_ROOT/cache/$PYTHON_TARBALL" "$MIRROR_URL"; then
                log_warn "清华大学镜像失败，尝试阿里云镜像..."
                if ! curl -L --connect-timeout 60 -o "$PYENV_ROOT/cache/$PYTHON_TARBALL" "$BACKUP_MIRROR_URL"; then
                    log_warn "阿里云镜像失败，尝试淘宝镜像..."
                    if ! curl -L --connect-timeout 60 -o "$PYENV_ROOT/cache/$PYTHON_TARBALL" "$BACKUP_MIRROR_URL2"; then
                        log_error "所有国内镜像均失败"
                        return 1
                    fi
                fi
            fi
        else
            log_info "使用缓存的 Python 源码包"
        fi
        
        # 验证下载的文件大小（应该大于 10MB）
        FILE_SIZE=$(stat -c%s "$PYENV_ROOT/cache/$PYTHON_TARBALL" 2>/dev/null || echo "0")
        if [ "$FILE_SIZE" -lt 10000000 ]; then
            log_error "下载的源码包文件过小 ($FILE_SIZE bytes)，可能下载失败"
            rm -f "$PYENV_ROOT/cache/$PYTHON_TARBALL"
            return 1
        fi
        
        log_info "开始编译安装 Python $PYTHON_VERSION (可能需要几分钟)..."
        if pyenv install "$PYTHON_VERSION"; then
            log_info "Python $PYTHON_VERSION 安装成功!"
        else
            log_error "Python $PYTHON_VERSION 安装失败"
            log_error "请查看错误日志或使用 'pyenv install --verbose $PYTHON_VERSION' 获取详细信息"
            return 1
        fi
    fi
    
    # 设置为全局默认版本
    log_info "设置 Python $PYTHON_VERSION 为全局默认版本..."
    pyenv global "$PYTHON_VERSION"
    
    # 验证安装
    log_info "验证安装..."
    python --version
    pip --version
}

# 配置 pip 国内源
configure_pip() {
    log_info "配置 pip 国内源..."
    
    PIP_DIR="$HOME/.pip"
    mkdir -p "$PIP_DIR"
    
    cat > "$PIP_DIR/pip.conf" << 'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn

[install]
trusted-host = pypi.tuna.tsinghua.edu.cn
EOF
    
    log_info "pip 已配置使用清华大学镜像源"
}

# 检测 git 是否已安装
check_git() {
    log_info "检测 git 环境..."
    
    if ! command -v git &> /dev/null; then
        log_error "git 未安装，请先安装 git"
        
        OS=$(detect_os)
        case "$OS" in
            *"Ubuntu"*|*"Debian"*|*"Deepin"*|*"Kali"*)
                log_info "使用以下命令安装：sudo apt-get install git"
                ;;
            *"CentOS"*|*"Fedora"*|*"Red Hat"*)
                log_info "使用以下命令安装：sudo yum install git"
                ;;
            *"Arch"*|*"Manjaro"*)
                log_info "使用以下命令安装：sudo pacman -S git"
                ;;
            *)
                log_info "请使用包管理器安装 git"
                ;;
        esac
        exit 1
    fi
    
    log_info "git 版本：$(git --version)"
}

# 主函数
main() {
    echo "============================================"
    echo "  pyenv + Python 3.11.11 安装脚本 (国内源)"
    echo "============================================"
    echo ""
    
    # 0. 检测 git
    check_git
    
    # 1. 安装依赖
    install_dependencies
    
    # 2. 安装 pyenv
    install_pyenv
    
    # 3. 配置 shell
    configure_shell
    
    # 4. 安装 Python (使用 pyenv 内置镜像功能)
    install_python "3.11.11"
    
    # 5. 配置 pip
    configure_pip
    
    echo ""
    echo "============================================"
    log_info "安装完成!"
    echo "============================================"
    echo ""
    echo "请执行以下命令使配置生效:"
    echo "  source \$HOME/.bashrc  (或 source \$HOME/.zshrc)"
    echo ""
    echo "验证安装:"
    echo "  pyenv --version"
    echo "  python --version"
    echo "  pip --version"
    echo ""
}

# 运行主函数
main "$@"
