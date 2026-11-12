#!/usr/bin/env bash
# dev-status.sh —— 开发环境只读体检（不起不停）
#
# 打印（.scratch/env-tiers/issues/04-dev-up-down.md）：
#   * 6678/6679 监听状态、端口上的 pid 与命令行
#   * pidfile 记录与进程实际存活
#   * 后端 /api/health 状态码
#   * 实际库路径与 db_role（从后端日志 grep database_role；日志缺则按文件名推断并标注）
#   * 附加只读事实：schema_version / assets 行数 / 启动迁移结果 / 容器是否占用端口
#
# 端口规则：后端尾 8 / 前端尾 9，十位数字表环境（7=dev，8=staging，9=prod）：
#   dev=6678/6679，staging=6688/6689，prod=6698/6699（见 .scratch/env-tiers/map.md）。
#
# 用法：
#   bash scripts/dev-status.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${XDG_CACHE_HOME:-${HOME}/.cache}/tmp/person_f"
BACKEND_PORT=6678
FRONTEND_PORT=6679
DB="$REPO/data/person_fin_dev.db"
BE_LOG="$RUN_DIR/dev-backend.log"
FE_LOG="$RUN_DIR/dev-frontend.log"
BE_PID="$RUN_DIR/dev-backend.pid"
FE_PID="$RUN_DIR/dev-frontend.pid"
PY="$REPO/.venv/bin/python"

note() { echo "==> $*"; }

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

print_port() {  # print_port <标签> <端口>
  local label="$1" port="$2" line pids
  line="$(ss -ltnpH "sport = :$port" 2>/dev/null || true)"
  if [[ -z "$line" ]]; then
    printf '  %-6s :%s  未监听\n' "$label" "$port"
    return 0
  fi
  pids="$(printf '%s\n' "$line" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u | tr '\n' ' ' || true)"
  printf '  %-6s :%s  监听（pid=%s）\n' "$label" "$port" "${pids:-未暴露}"
  printf '%s\n' "$line" | sed 's/^/         /'
}

print_pidfile() {  # print_pidfile <标签> <pidfile>
  local label="$1" f="$2" pid
  if [[ ! -f "$f" ]]; then
    printf '  %-6s 无 pidfile（%s）\n' "$label" "$f"
    return 0
  fi
  pid="$(read_pid "$f")"
  if pid_alive "$pid"; then
    printf '  %-6s pid %s 存活\n' "$label" "$pid"
    ps -o pid=,etime=,args= -p "$pid" 2>/dev/null | sed 's/^/         /' || true
  else
    printf '  %-6s pidfile 陈旧（pid %s 已不在）\n' "$label" "${pid:-?}"
  fi
}

main() {
  echo "仓库 $REPO"
  echo

  note "端口"
  print_port "前端" "$FRONTEND_PORT"
  print_port "后端" "$BACKEND_PORT"
  echo

  note "pidfile（$RUN_DIR）"
  print_pidfile "前端" "$FE_PID"
  print_pidfile "后端" "$BE_PID"
  echo

  note "后端 /api/health"
  local code body
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${BACKEND_PORT}/api/health" 2>/dev/null || true)"
  body="$(curl -s --max-time 5 "http://127.0.0.1:${BACKEND_PORT}/api/health" 2>/dev/null || true)"
  echo "  http://127.0.0.1:${BACKEND_PORT}/api/health → HTTP ${code:-000} ${body}"
  printf '  前端 http://127.0.0.1:%s/ → HTTP %s\n' "$FRONTEND_PORT" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${FRONTEND_PORT}/" 2>/dev/null || true)"
  echo

  note "数据库"
  echo "  期望 DATABASE_PATH=$DB（DB_ROLE=dev）"
  if [[ -f "$DB" ]]; then
    printf '  文件 %s\n' "$(ls -l --time-style=long-iso "$DB" | awk '{print $5" bytes  mtime "$6" "$7}')"
  else
    echo "  [warn] 文件不存在（后端首次启动会自建）"
  fi
  if [[ -x "$PY" ]]; then
    "$PY" - "$DB" <<'PYEOF'
import sqlite3
import sys

path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ver = conn.execute("select max(version) from schema_version").fetchone()[0]
    assets = conn.execute("select count(*) from assets").fetchone()[0]
    conn.close()
    print(f"  schema_version={ver}  assets={assets}（只读 mode=ro 查得）")
except Exception as exc:  # noqa: BLE001 —— 体检脚本不因读取失败中断
    print(f"  [warn] 只读查询失败：{exc}")
PYEOF
  fi

  note "日志里的库角色（grep database_role）"
  local line
  if [[ -f "$BE_LOG" ]]; then
    line="$(grep -a 'database_role' "$BE_LOG" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      echo "  $line"
    else
      echo "  [warn] $BE_LOG 里没有 database_role 行（后端未启动过？）"
    fi
    line="$(grep -a 'startup_migration_completed' "$BE_LOG" | tail -n 1 || true)"
    [[ -n "$line" ]] && echo "  $line" || true
    echo "  后端日志：$BE_LOG"
  else
    echo "  无后端日志：$BE_LOG（bash scripts/dev-up.sh 后生成）"
  fi
  echo "  前端日志：$FE_LOG"
  echo

  note "容器（备选方案）是否占用 6678/6679（含旧 6666/6667）"
  local table hits
  table="$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null || sudo -n docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null || true)"
  hits="$(printf '%s\n' "$table" | grep -E ':(6678|6679|6666|6667)->' || true)"
  if [[ -n "$hits" ]]; then
    printf '%s\n' "$hits" | sed 's/^/  /'
    echo "  → 端口被容器占用；dev-down.sh 会拒绝执行（不误杀容器）"
  else
    echo "  无"
  fi
}

main "$@"