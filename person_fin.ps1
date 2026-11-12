<#
.SYNOPSIS
person_fin 项目自动环境配置与启动脚本

.DESCRIPTION
自动检测、安装 Python 和 Node.js，配置环境，启动服务
支持：Windows 10/11

.EXAMPLE
./person_fin.ps1 start
启动所有服务

./person_fin.ps1 stop
停止所有服务

./person_fin.ps1 status
查看服务状态

./person_fin.ps1 restart
重启所有服务
#>

param (
    [string]$Action = "start"
)

# 颜色定义
$GREEN = "[32m"
$YELLOW = "[33m"
$RED = "[31m"
$BLUE = "[34m"
$NC = "[0m"

# 日志函数
function Write-Info {
    param ([string]$Message)
    Write-Host "${GREEN}[INFO]${NC} $Message"
}

function Write-Warn {
    param ([string]$Message)
    Write-Host "${YELLOW}[WARN]${NC} $Message"
}

function Write-Error {
    param ([string]$Message)
    Write-Host "${RED}[ERROR]${NC} $Message"
}

function Write-Step {
    param ([string]$Message)
    Write-Host "${BLUE}[STEP]${NC} $Message"
}

# 获取脚本所在目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# 检查 PowerShell 版本
function Check-PowerShellVersion {
    $version = $PSVersionTable.PSVersion
    if ($version.Major -lt 5) {
        Write-Error "PowerShell 版本过低，需要 PowerShell 5.0 或更高版本"
        return $false
    }
    Write-Info "PowerShell 版本: $($version.ToString())"
    return $true
}

# 检查 Windows 版本
function Check-WindowsVersion {
    $osVersion = [System.Environment]::OSVersion
    $osBuild = [System.Environment]::OSVersion.Version.Build
    
    if ($osVersion.Platform -ne "Win32NT") {
        Write-Error "此脚本仅支持 Windows 系统"
        return $false
    }
    
    if ($osBuild -lt 10240) { # Windows 10 最低版本
        Write-Error "Windows 版本过低，需要 Windows 10 或更高版本"
        return $false
    }
    
    Write-Info "Windows 版本: $($osVersion.Version.ToString())"
    return $true
}

# 检查管理员权限
function Test-Admin {
    $currentUser = New-Object Security.Principal.WindowsPrincipal $([Security.Principal.WindowsIdentity]::GetCurrent())
    $currentUser.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# 安装 Chocolatey
function Install-Chocolatey {
    Write-Step "检查 Chocolatey 包管理器..."
    
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Info "Chocolatey 已安装"
        return
    }
    
    Write-Info "安装 Chocolatey 包管理器..."
    
    if (-not (Test-Admin)) {
        Write-Warn "需要管理员权限安装 Chocolatey"
        Write-Info "请以管理员身份运行 PowerShell"
        return $false
    }
    
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))
    
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Info "Chocolatey 安装完成"
        return $true
    } else {
        Write-Error "Chocolatey 安装失败"
        return $false
    }
}

# 配置 Python 环境
function Setup-PythonEnvironment {
    $PythonVersion = "3.11.11"
    Write-Step "检查 Python 环境..."
    
    # 检查 Python 是否安装
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $currentVersion = python --version 2>&1
        Write-Info "Python 已安装: $currentVersion"
    } else {
        Write-Info "安装 Python $PythonVersion..."
        
        if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
            if (-not (Install-Chocolatey)) {
                Write-Error "无法安装 Chocolatey，无法继续"
                return $false
            }
        }
        
        choco install python --version $PythonVersion --yes --no-progress
        
        if (Get-Command python -ErrorAction SilentlyContinue) {
            Write-Info "Python 安装完成"
        } else {
            Write-Error "Python 安装失败"
            return $false
        }
    }
    
    # 创建虚拟环境
    $VenvPath = Join-Path $ScriptDir ".venv"
    if (-not (Test-Path $VenvPath)) {
        Write-Info "创建 Python 虚拟环境..."
        python -m venv $VenvPath
        if (Test-Path $VenvPath) {
            Write-Info "虚拟环境创建完成"
        } else {
            Write-Error "虚拟环境创建失败"
            return $false
        }
    } else {
        Write-Info "虚拟环境已存在"
    }
    
    # 激活虚拟环境并安装依赖
    $ActivateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (Test-Path $ActivateScript) {
        & $ActivateScript
        Write-Info "激活虚拟环境"
        
        # 升级 pip
        python -m pip install --upgrade pip -q
        
        # 安装依赖
        $BackendPath = Join-Path $ScriptDir "backend"
        $RequirementsPath = Join-Path $BackendPath "requirements.txt"
        if (Test-Path $RequirementsPath) {
            Write-Info "安装后端依赖..."
            python -m pip install -r $RequirementsPath -q
            Write-Info "后端依赖安装完成"
        }
    }
    
    return $true
}

# 配置 Node.js 环境
function Setup-NodeEnvironment {
    $NodeVersion = "20"
    Write-Step "检查 Node.js 环境..."
    
    # 检查 Node.js 是否安装
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $currentVersion = node --version
        Write-Info "Node.js 已安装: $currentVersion"
    } else {
        Write-Info "安装 Node.js $NodeVersion..."
        
        if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
            if (-not (Install-Chocolatey)) {
                Write-Error "无法安装 Chocolatey，无法继续"
                return $false
            }
        }
        
        choco install nodejs --version $NodeVersion --yes --no-progress
        
        if (Get-Command node -ErrorAction SilentlyContinue) {
            Write-Info "Node.js 安装完成"
        } else {
            Write-Error "Node.js 安装失败"
            return $false
        }
    }
    
    # 安装前端依赖
    $FrontendPath = Join-Path $ScriptDir "frontend"
    if (Test-Path $FrontendPath) {
        Set-Location $FrontendPath
        
        $NodeModulesPath = Join-Path $FrontendPath "node_modules"
        if (-not (Test-Path $NodeModulesPath)) {
            Write-Info "安装前端依赖..."
            npm install --silent
            Write-Info "前端依赖安装完成"
        } else {
            Write-Info "前端依赖已存在"
        }
    }
    
    return $true
}

# 启动后端服务
function Start-Backend {
    Write-Step "启动 Backend 服务..."
    
    $Port = 8000
    $BackendPath = Join-Path $ScriptDir "backend"
    $VenvPath = Join-Path $ScriptDir ".venv"
    $ActivateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    
    # 检查端口是否被占用
    $Process = Get-Process -Id (Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue).OwningProcess 2>$null
    if ($Process) {
        Write-Warn "端口 $Port 被占用 (PID: $($Process.Id))，尝试停止..."
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    
    # 启动后端
    $BackendLog = Join-Path $ScriptDir "backend.log"
    $BackendPidFile = Join-Path $ScriptDir ".backend.pid"
    
    $BackendCommand = {
        param($ScriptDir, $BackendPath, $VenvPath, $Port, $BackendLog)
        Set-Location $BackendPath
        & "$VenvPath\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port $Port > $BackendLog 2>&1
    }
    
    $job = Start-Job -ScriptBlock $BackendCommand -ArgumentList $ScriptDir, $BackendPath, $VenvPath, $Port, $BackendLog
    Start-Sleep -Seconds 3
    
    if (Get-Job -Id $job.Id) {
        $jobId = $job.Id
        Write-Info "Backend 启动成功 (Job ID: $jobId)"
        Write-Info "访问地址：http://localhost:$Port"
        $jobId | Out-File -FilePath $BackendPidFile
    } else {
        Write-Error "Backend 启动失败，查看日志：$BackendLog"
        Get-Content $BackendLog -ErrorAction SilentlyContinue
        return $false
    }
    
    return $true
}

# 启动前端服务
function Start-Frontend {
    Write-Step "启动 Frontend 服务..."
    
    $Port = 3000
    $FrontendPath = Join-Path $ScriptDir "frontend"
    
    # 检查端口是否被占用
    $Process = Get-Process -Id (Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue).OwningProcess 2>$null
    if ($Process) {
        Write-Warn "端口 $Port 被占用 (PID: $($Process.Id))，尝试停止..."
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
    
    # 启动前端
    $FrontendLog = Join-Path $ScriptDir "frontend.log"
    $FrontendPidFile = Join-Path $ScriptDir ".frontend.pid"
    
    $FrontendCommand = {
        param($FrontendPath, $Port, $FrontendLog)
        Set-Location $FrontendPath
        npm run dev -- -p $Port > $FrontendLog 2>&1
    }
    
    $job = Start-Job -ScriptBlock $FrontendCommand -ArgumentList $FrontendPath, $Port, $FrontendLog
    Start-Sleep -Seconds 5
    
    if (Get-Job -Id $job.Id) {
        $jobId = $job.Id
        Write-Info "Frontend 启动成功 (Job ID: $jobId)"
        Write-Info "访问地址：http://localhost:$Port"
        $jobId | Out-File -FilePath $FrontendPidFile
    } else {
        Write-Error "Frontend 启动失败，查看日志：$FrontendLog"
        Get-Content $FrontendLog -ErrorAction SilentlyContinue
        return $false
    }
    
    return $true
}

# 停止服务
function Stop-Services {
    Write-Step "停止服务..."
    
    # 停止后端
    $BackendPidFile = Join-Path $ScriptDir ".backend.pid"
    if (Test-Path $BackendPidFile) {
        $jobId = Get-Content $BackendPidFile -ErrorAction SilentlyContinue
        if ($jobId) {
            Stop-Job -Id $jobId -ErrorAction SilentlyContinue
            Remove-Job -Id $jobId -ErrorAction SilentlyContinue
        }
        Remove-Item $BackendPidFile -ErrorAction SilentlyContinue
        Write-Info "Backend 已停止"
    }
    
    # 停止前端
    $FrontendPidFile = Join-Path $ScriptDir ".frontend.pid"
    if (Test-Path $FrontendPidFile) {
        $jobId = Get-Content $FrontendPidFile -ErrorAction SilentlyContinue
        if ($jobId) {
            Stop-Job -Id $jobId -ErrorAction SilentlyContinue
            Remove-Job -Id $jobId -ErrorAction SilentlyContinue
        }
        Remove-Item $FrontendPidFile -ErrorAction SilentlyContinue
        Write-Info "Frontend 已停止"
    }
    
    # 清理端口占用
    $Ports = @(8000, 3000)
    foreach ($Port in $Ports) {
        $Process = Get-Process -Id (Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue).OwningProcess 2>$null
        if ($Process) {
            Write-Warn "清理端口 $Port 占用的进程..."
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    
    Write-Info "所有服务已停止"
}

# 查看服务状态
function Check-Status {
    Write-Step "服务状态:"
    
    Write-Host ""
    Write-Host "Backend (端口 8000):"
    $BackendProcess = Get-Process -Id (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess 2>$null
    if ($BackendProcess) {
        Write-Host "  ${GREEN}运行中${NC} (PID: $($BackendProcess.Id))"
    } else {
        Write-Host "  ${RED}未运行${NC}"
    }
    
    Write-Host ""
    Write-Host "Frontend (端口 3000):"
    $FrontendProcess = Get-Process -Id (Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue).OwningProcess 2>$null
    if ($FrontendProcess) {
        Write-Host "  ${GREEN}运行中${NC} (PID: $($FrontendProcess.Id))"
    } else {
        Write-Host "  ${RED}未运行${NC}"
    }
    Write-Host ""
}

# 主函数
function Main {
    Write-Host ""
    Write-Host "============================================"
    Write-Host "  person_fin 项目自动环境配置与启动脚本"
    Write-Host "============================================"
    Write-Host ""
    
    # 检查系统
    if (-not (Check-PowerShellVersion)) {
        return
    }
    
    if (-not (Check-WindowsVersion)) {
        return
    }
    
    # 配置环境
    if (-not (Setup-PythonEnvironment)) {
        return
    }
    
    if (-not (Setup-NodeEnvironment)) {
        return
    }
    
    Write-Host ""
    Write-Info "环境配置完成!"
    Write-Host ""
    
    # 启动服务
    if (-not (Start-Backend)) {
        return
    }
    
    Write-Host ""
    if (-not (Start-Frontend)) {
        return
    }
    
    Write-Host ""
    Write-Host "============================================"
    Write-Info "所有服务已启动!"
    Write-Host "============================================"
    Write-Host ""
    Write-Host "服务访问地址:"
    Write-Host "  Backend:  http://localhost:8000"
    Write-Host "  Frontend: http://localhost:3000"
    Write-Host ""
    Write-Host "日志文件:"
    Write-Host "  Backend:  $ScriptDir\backend.log"
    Write-Host "  Frontend: $ScriptDir\frontend.log"
    Write-Host ""
    Write-Host "停止服务:"
    Write-Host "  $PSCommandPath stop"
    Write-Host ""
}

# 处理命令行参数
switch ($Action) {
    "start" {
        Main
    }
    "stop" {
        Stop-Services
    }
    "restart" {
        Stop-Services
        Start-Sleep -Seconds 1
        Main
    }
    "status" {
        Check-Status
    }
    default {
        Write-Host "用法：$PSCommandPath {start|stop|restart|status}"
        exit 1
    }
}
