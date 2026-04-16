# 项目部署指南

本指南提供了三种部署方式的详细步骤，确保不同环境的用户都能轻松运行项目。

## 目录

- [直接运行部署](#直接运行部署)
- [Docker 容器化部署](#docker-容器化部署)
- [Windows 系统部署](#windows-系统部署)
- [常见问题与故障排除](#常见问题与故障排除)

## 直接运行部署

### 环境要求

- **操作系统**: Linux 或 macOS
- **依赖**: 
  - Git
  - Python 3.11+
  - Node.js 20+

### 部署步骤

1. **克隆项目代码**
   ```bash
   git clone <项目仓库地址>
   cd person_fin
   ```

2. **运行启动脚本**
   ```bash
   ./person_fin.sh start
   ```

3. **等待环境配置完成**
   脚本会自动：
   - 检查并安装 pyenv（如果需要）
   - 安装 Python 3.11.11
   - 创建并配置虚拟环境
   - 安装后端依赖
   - 检查并安装 nvm（如果需要）
   - 安装 Node.js 20
   - 安装前端依赖
   - 启动后端和前端服务

4. **访问服务**
   - 后端服务: http://localhost:8000
   - 前端服务: http://localhost:3000

### 管理命令

- **停止服务**
  ```bash
  ./person_fin.sh stop
  ```

- **重启服务**
  ```bash
  ./person_fin.sh restart
  ```

- **查看服务状态**
  ```bash
  ./person_fin.sh status
  ```

## Docker 容器化部署

### 环境要求

- **Docker**: 20.10+
- **Docker Compose**: 1.29+

### 部署步骤

1. **克隆项目代码**
   ```bash
   git clone <项目仓库地址>
   cd person_fin
   ```

2. **构建并启动容器**
   ```bash
   docker-compose up -d
   ```

3. **等待容器启动**
   命令会自动：
   - 构建后端 Docker 镜像
   - 构建前端 Docker 镜像
   - 启动后端服务容器
   - 启动前端服务容器
   - 配置网络连接

4. **访问服务**
   - 后端服务: http://localhost:8000
   - 前端服务: http://localhost:3000

### 管理命令

- **查看容器状态**
  ```bash
  docker-compose ps
  ```

- **查看容器日志**
  ```bash
  docker-compose logs
  ```

- **停止容器**
  ```bash
  docker-compose down
  ```

- **重启容器**
  ```bash
  docker-compose restart
  ```

## Windows 系统部署

### 环境要求

- **操作系统**: Windows 10 或 Windows 11
- **PowerShell**: 5.0+
- **管理员权限**（用于安装依赖）

### 部署步骤

1. **克隆项目代码**
   使用 Git Bash 或 PowerShell 克隆代码：
   ```powershell
   git clone <项目仓库地址>
   cd person_fin
   ```

2. **运行 PowerShell 启动脚本**
   以管理员身份运行 PowerShell，执行：
   ```powershell
   .\person_fin.ps1 start
   ```

3. **等待环境配置完成**
   脚本会自动：
   - 检查 PowerShell 版本
   - 检查 Windows 版本
   - 安装 Chocolatey 包管理器（如果需要）
   - 安装 Python 3.11.11
   - 创建并配置虚拟环境
   - 安装后端依赖
   - 安装 Node.js 20
   - 安装前端依赖
   - 启动后端和前端服务

4. **访问服务**
   - 后端服务: http://localhost:8000
   - 前端服务: http://localhost:3000

### 管理命令

- **停止服务**
  ```powershell
  .\person_fin.ps1 stop
  ```

- **重启服务**
  ```powershell
  .\person_fin.ps1 restart
  ```

- **查看服务状态**
  ```powershell
  .\person_fin.ps1 status
  ```

## 常见问题与故障排除

### 端口占用问题

**症状**: 服务启动失败，提示端口被占用

**解决方案**:
- 检查是否有其他服务占用了 8000 或 3000 端口
- 使用 `./person_fin.sh stop` 或 `.erson_fin.ps1 stop` 停止现有服务
- 手动停止占用端口的进程

### 依赖安装失败

**症状**: 脚本执行过程中依赖安装失败

**解决方案**:
- 检查网络连接是否正常
- 对于 Linux/macOS，确保有足够的权限
- 对于 Windows，确保以管理员身份运行 PowerShell
- 手动安装依赖后再运行脚本

### Docker 构建失败

**症状**: `docker-compose up` 失败，构建过程出错

**解决方案**:
- 检查网络连接
- 确保 Docker 服务正常运行
- 清理 Docker 缓存后重试
  ```bash
  docker system prune -f
  docker-compose build --no-cache
  ```

### 服务启动后无法访问

**症状**: 服务显示启动成功，但浏览器无法访问

**解决方案**:
- 检查防火墙设置，确保 8000 和 3000 端口已开放
- 检查服务日志，查看是否有错误信息
  ```bash
  # Linux/macOS
  cat backend.log
  cat frontend.log
  
  # Docker
  docker-compose logs
  ```

### Windows PowerShell 执行权限问题

**症状**: PowerShell 提示无法执行脚本

**解决方案**:
- 以管理员身份运行 PowerShell
- 执行以下命令开启执行权限：
  ```powershell
  Set-ExecutionPolicy RemoteSigned -Scope Process
  ```

### 数据库问题

**症状**: 服务启动后数据无法正常访问

**解决方案**:
- 检查 SQLite 数据库文件权限
- 确保数据库文件路径正确
- 对于 Docker 部署，检查卷挂载是否正确

## 技术支持

如果遇到其他问题，请检查以下文件：
- 后端日志: `backend.log`
- 前端日志: `frontend.log`
- Docker 日志: `docker-compose logs`

或联系项目维护人员获取支持。
