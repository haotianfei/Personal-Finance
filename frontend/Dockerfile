# 前端 Dockerfile
# 基于 Node.js 20 镜像
FROM node:20-alpine

# 设置工作目录
WORKDIR /app

# 复制 package.json 和 package-lock.json
COPY package*.json ./

# 安装依赖
RUN npm install --silent

# 复制应用代码
COPY . .

# 暴露端口
EXPOSE 3000

# 启动命令（在运行时构建，以便使用正确的环境变量）
CMD sh -c "npm run build && npm start"
