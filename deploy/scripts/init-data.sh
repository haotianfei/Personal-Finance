#!/usr/bin/env bash
# 首次部署：把已有 data/（DB + WAL + evidence + backup + exports）拷入部署目录
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-}"

if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "用法：$0 /path/to/existing/data （需包含 person_fin.db）" >&2
  exit 1
fi
if [[ ! -f "$SRC/person_fin.db" ]]; then
  echo "未找到 $SRC/person_fin.db" >&2
  exit 1
fi

mkdir -p "$DEPLOY_DIR/data"
if [[ -n "$(ls -A "$DEPLOY_DIR/data" 2>/dev/null || true)" ]]; then
  echo "目标 data/ 非空，已退出（如需覆盖请先手动清空）" >&2
  exit 1
fi

echo "==> 复制数据：$SRC → $DEPLOY_DIR/data"
# -a 保留权限/时间；连 -wal/-shm 一起拷，未落盘的写入不会丢
rsync -a "$SRC/" "$DEPLOY_DIR/data/"

# 容器内以 uid 1000 运行
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R 1000:100 "$DEPLOY_DIR/data"
else
  echo "提示：容器以 uid 1000 运行，如权限不符请执行 sudo chown -R 1000:100 $DEPLOY_DIR/data"
fi

du -sh "$DEPLOY_DIR/data"
ls -la "$DEPLOY_DIR/data" | head -8