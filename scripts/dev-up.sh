#!/usr/bin/env bash
# dev-up.sh —— 开发环境一键起（后端 6678 + 前端 6679，源码热改）
#
# 范围（.scratch/env-tiers/issues/04-dev-up-down.md）：
#   * 后端：cwd backend，../.venv/bin/python -m uvicorn main:app --reload --port 6678
#   * 前端：cwd frontend，bun node_modules/next/dist/bin/next dev -p 6679
#   * 库固定 <repo>/data/person_fin_dev.db + DB_ROLE=dev（绝不指向 deploy/data 的生产库）
#   * 日志与 pid：$RUN_DIR/dev-{backend,frontend}.{log,pid}
#   * 幂等：进程已在跑则跳过并打印；不启停任何容器（容器化开发是备选方案，另有入口）
#   * 不在这里做迁移/造数：迁移由后端启动时自动执行（runner 自带前置备份）
#
# 端口服从三环境统一规则「后端尾 8 / 前端尾 9」：dev=6678/6679，staging=6688/6689，
# prod=6698/6699（见 .scratch/env-tiers/map.md，2026-11-09 业主裁决）。
#
# 用法：
#   bash scripts/dev-up.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${XDG_CACHE_HOME:-${HOME}/.cache}/tmp/person_f"
# 三环境端口规则：后端尾 8、前端尾 9，十位环境表（7=dev，8=staging，9=prod）
BACKEND_PORT=6678
FRONTEND_PORT=6679
DB="$REPO/data/person_fin_dev.db"
BE_LOG="$RUN_DIR/dev-backend.log"
FE_LOG="$RUN_DIR/dev-frontend.log"
BE_PID="$RUN_DIR/dev-backend.pid"
FE_PID="$RUN_DIR/dev-frontend.pid"
PY="$REPO/.venv/bin/python"

note() { echo "==> $*"; }
warn() { echo "[warn] $*" >&2; }
die()  { echo "[FAIL] $*" >&2; exit 1; }

pid_alive() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local f="$1" pid=""
  if [[ -f "$f" ]]; then
    pid="$(tr -dc '0-9' < "$f" 2>/dev/null || true)"
  fi
  printf '%s' "$pid"
}

port_listening() {
  ss -ltnH 2>/dev/null | awk -v p=":$1\$" '$4 ~ p {f=1} END{exit(f?0:1)}'
}

port_owner() {
  ss -ltnpH "sport = :$1" 2>/dev/null | sed 's/^/    /' || true
}

# wait_http <标签> <url> <最多秒数>
wait_http() {
  local label="$1" url="$2" tries="$3" i code
  for ((i = 1; i <= tries; i++)); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
    if [[ "$code" =~ ^(2|3) ]]; then
      echo "    [ok] $label 就绪（HTTP $code，用时 ${i}s）"
      return 0
    fi
    sleep 1
  done
  return 1
}

banner() {  # banner <日志文件>
  { echo; echo "=== dev-up $(date -Is) ==="; } >>"$1"
}

start_backend() {
  local pid
  pid="$(read_pid "$BE_PID")"
  if pid_alive "$pid"; then
    if port_listening "$BACKEND_PORT"; then
      echo "    [skip] 后端已在跑（pid $pid，:$BACKEND_PORT）"
      return 0
    fi
    die "后端进程在跑（pid $pid）但不在 :$BACKEND_PORT —— 端口漂移，先 bash scripts/dev-down.sh"
  fi
  if port_listening "$BACKEND_PORT"; then
    warn "端口 $BACKEND_PORT 已被其他进程占用（非本脚本 pidfile），不重复启动："
    port_owner "$BACKEND_PORT" >&2
    return 0
  fi
  rm -f "$BE_PID"

  note "启动后端（uvicorn --reload，:$BACKEND_PORT，库 $(basename "$DB")）"
  banner "$BE_LOG"
  # exec 让后台子 shell 直接变成 uvicorn 本体 → pidfile 里的就是真 pid
  (
    cd "$REPO/backend" || exit 1
    exec env DATABASE_PATH="$DB" DB_ROLE=dev \
      nohup "$PY" -m uvicorn main:app --reload --port "$BACKEND_PORT"
  ) >>"$BE_LOG" 2>&1 </dev/null &
  echo $! >"$BE_PID"
  sleep 2
  pid="$(read_pid "$BE_PID")"
  if ! pid_alive "$pid"; then
    tail -n 20 "$BE_LOG" >&2 || true
    die "后端启动即退出（pid ${pid:-?}），日志：$BE_LOG"
  fi
  echo "    pid $pid"
}

start_frontend() {
  local pid bun_bin
  pid="$(read_pid "$FE_PID")"
  if pid_alive "$pid"; then
    if port_listening "$FRONTEND_PORT"; then
      echo "    [skip] 前端已在跑（pid $pid，:$FRONTEND_PORT）"
      return 0
    fi
    die "前端进程在跑（pid $pid）但不在 :$FRONTEND_PORT —— 端口漂移，先 bash scripts/dev-down.sh"
  fi
  if port_listening "$FRONTEND_PORT"; then
    warn "端口 $FRONTEND_PORT 已被其他进程占用（非本脚本 pidfile），不重复启动："
    port_owner "$FRONTEND_PORT" >&2
    return 0
  fi
  rm -f "$FE_PID"

  bun_bin="$(command -v bun || true)"
  [[ -n "$bun_bin" ]] || die "找不到 bun（本机只用 bun 跑前端，无 node/npx）"
  [[ -f "$REPO/frontend/node_modules/next/dist/bin/next" ]] \
    || die "缺少 frontend/node_modules/next（先按 README 装前端依赖）"

  note "启动前端（next dev，:$FRONTEND_PORT，API 反代 → 127.0.0.1:$BACKEND_PORT）"
  banner "$FE_LOG"
  (
    cd "$REPO/frontend" || exit 1
    exec env NEXT_PUBLIC_API_URL="http://127.0.0.1:$BACKEND_PORT" \
      nohup "$bun_bin" node_modules/next/dist/bin/next dev -p "$FRONTEND_PORT"
  ) >>"$FE_LOG" 2>&1 </dev/null &
  echo $! >"$FE_PID"
  sleep 2
  pid="$(read_pid "$FE_PID")"
  if ! pid_alive "$pid"; then
    tail -n 20 "$FE_LOG" >&2 || true
    die "前端启动即退出（pid ${pid:-?}），日志：$FE_LOG"
  fi
  echo "    pid $pid"
}

main() {
  [[ -x "$PY" ]] || die "缺少 venv 解释器：$PY"
  command -v curl >/dev/null 2>&1 || die "缺少 curl（就绪轮询要用）"
  command -v ss >/dev/null 2>&1 || die "缺少 ss（端口检测要用）"
  mkdir -p "$RUN_DIR" "$(dirname "$DB")"

  note "仓库 $REPO"
  echo "    库 DATABASE_PATH=$DB DB_ROLE=dev"
  echo "    端口 后端 :$BACKEND_PORT  前端 :$FRONTEND_PORT（dev=667x，staging=668x，prod=669x）"
  echo "    日志目录 $RUN_DIR"

  start_backend
  start_frontend

  note "等待后端 /api/health"
  wait_http "后端 health" "http://127.0.0.1:${BACKEND_PORT}/api/health" 90 || {
    tail -n 20 "$BE_LOG" >&2 || true
    die "后端 90s 内未就绪（看 $BE_LOG；停止：bash scripts/dev-down.sh）"
  }
  note "等待前端就绪"
  wait_http "前端首页" "http://127.0.0.1:${FRONTEND_PORT}/" 180 || {
    tail -n 20 "$FE_LOG" >&2 || true
    die "前端 180s 内未就绪（首次 dev 编译较慢，可看 $FE_LOG；停止：bash scripts/dev-down.sh）"
  }

  echo
  note "已就绪"
  echo "    前端      http://127.0.0.1:${FRONTEND_PORT}"
  echo "    后端      http://127.0.0.1:${BACKEND_PORT}  （健康检查 /api/health，文档 /docs）"
  echo "    后端日志  $BE_LOG"
  echo "    前端日志  $FE_LOG"
  echo "    停止      bash scripts/dev-down.sh"
  echo
  echo "    库角色取证："
  grep -a 'database_role' "$BE_LOG" | tail -n 1 | sed 's/^/      /' || true
  grep -a 'startup_migration_completed' "$BE_LOG" | tail -n 1 | sed 's/^/      /' || true
}

main "$@"