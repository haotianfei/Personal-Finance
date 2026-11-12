#!/usr/bin/env bash
# 把开发库源码暂存为构建上下文（不含数据/依赖/日志），供 docker compose build 使用
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$DEPLOY_DIR/.." && pwd)}"

if [[ ! -d "$REPO_DIR/backend" || ! -d "$REPO_DIR/frontend" ]]; then
  echo "REPO_DIR 无效：$REPO_DIR" >&2
  exit 1
fi

echo "==> 暂存构建上下文：$REPO_DIR → $DEPLOY_DIR/build"
rm -rf "$DEPLOY_DIR/build"
mkdir -p "$DEPLOY_DIR/build/backend" "$DEPLOY_DIR/build/frontend"

rsync -a \
  --exclude 'data/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude 'tests/' \
  --exclude 'test_*.py' \
  --exclude 'logs/' \
  --exclude '*.log' \
  --exclude 'block_dump.txt' \
  --exclude 'asset_manager.db' \
  "$REPO_DIR/backend/" "$DEPLOY_DIR/build/backend/"

rsync -a \
  --exclude 'node_modules/' \
  --exclude '.next/' \
  --exclude 'tests/' \
  --exclude 'playwright-report/' \
  --exclude 'test-results/' \
  --exclude '.env.local' \
  --exclude '*.log' \
  "$REPO_DIR/frontend/" "$DEPLOY_DIR/build/frontend/"

cp "$DEPLOY_DIR/Dockerfile.backend" "$DEPLOY_DIR/build/backend/Dockerfile"
cp "$DEPLOY_DIR/Dockerfile.frontend" "$DEPLOY_DIR/build/frontend/Dockerfile"

# 携带本机 PaddleX 模型缓存（有则避免首次识别重新下载）；无则留空目录
mkdir -p "$DEPLOY_DIR/build/backend/paddle_cache"
if [[ -d "$HOME/.paddlex" ]]; then
  rsync -a "$HOME/.paddlex/" "$DEPLOY_DIR/build/backend/paddle_cache/"
  echo "==> 已带入 PaddleX 缓存：$(du -sh "$DEPLOY_DIR/build/backend/paddle_cache" | cut -f1)"
else
  echo "==> 未发现本机 PaddleX 缓存（首次识别将在线下载模型）"
fi

echo "==> 上下文大小："
du -sh "$DEPLOY_DIR/build/backend" "$DEPLOY_DIR/build/frontend"