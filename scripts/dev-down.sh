#!/usr/bin/env bash
# dev-down.sh —— 停开发环境（后端 6678 + 前端 6679，源码热改）
#
# 停法（.scratch/env-tiers/issues/04-dev-up-down.md）：
#   1. 先按 pidfile（$RUN_DIR/dev-*.pid）停；pidfile 缺失/陈旧时按端口兜底找 pid；
#   2. 兜底路径只杀「命令行命中 uvicorn / next 的自家进程」——docker-proxy、别的服务一律不动；
#   3. **容器红线**：只要 docker ps 里有容器映射 6678/6679（含重排前的 6666/6667 残留容器），
#      直接拒绝执行（容器化开发是备选方案，可能是有人在用根 docker-compose.yml 起栈），
#      一个进程都不杀。
#
# 端口规则：后端尾 8 / 前端尾 9，十位数字表环境（7=dev，8=staging，9=prod）：
#   dev=6678/6679，staging=6688/6689，prod=6698/6699（见 .scratch/env-tiers/map.md）。
#
# 用法：
#   bash scripts/dev-down.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${XDG_CACHE_HOME:-${HOME}/.cache}/tmp/person_f"
BACKEND_PORT=6678
FRONTEND_PORT=6679
BE_PID="$RUN_DIR/dev-backend.pid"
FE_PID="$RUN_DIR/dev-frontend.pid"

note() { echo "==> $*"; }
warn() { echo "[warn] $*" >&2; }

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

port_pids() {
  ss -ltnpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true
}

cmd_matches() {  # cmd_matches <pid> <命令行 ERE>
  local pid="$1" pat="$2" args
  args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
  [[ -n "$args" && "$args" =~ $pat ]]
}

# 容器检测：普通用户能读就用 docker，否则 sudo -n（无免密 sudo 则视为「看不到容器」）
docker_ps_table() {
  local out=""
  if out="$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null)"; then
    printf '%s\n' "$out"
    return 0
  fi
  if out="$(sudo -n docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null)"; then
    printf '%s\n' "$out"
    return 0
  fi
  warn "既没有 docker 权限也没有免密 sudo：跳过容器检测（按端口兜底仍只杀自家进程）"
  return 0
}

# stop_one <标签> <pidfile> <端口> <命令行关键串>
stop_one() {
  local label="$1" pidfile="$2" port="$3" pat="$4"
  local -a candidates=()
  local -A seen=()
  local pid p i

  if [[ -f "$pidfile" ]]; then
    pid="$(read_pid "$pidfile")"
    if pid_alive "$pid"; then
      candidates+=("$pid")
    else
      echo "    [clean] $label：pidfile 里的进程已不在（pid ${pid:-?}），删除陈旧 pidfile"
    fi
    rm -f "$pidfile"
  else
    echo "    [note] $label：无 pidfile，按端口 $port 兜底查 pid"
  fi

  while IFS= read -r p; do
    [[ -n "$p" ]] && candidates+=("$p")
  done < <(port_pids "$port")

  for pid in "${candidates[@]}"; do
    [[ -n "${seen[$pid]:-}" ]] && continue
    seen[$pid]=1
    if ! pid_alive "$pid"; then
      continue
    fi
    if ! cmd_matches "$pid" "$pat"; then
      echo "    [skip] $label：pid $pid 已退出或命令行不匹配 '$pat' → 不动"
      continue
    fi
    echo "    [stop] $label：TERM pid $pid"
    kill -TERM "$pid" 2>/dev/null || true
  done

  for i in $(seq 1 20); do
    if ! port_listening "$port"; then
      echo "    [ok] $label：端口 $port 已释放"
      return 0
    fi
    sleep 0.5
  done

  # 兜底：端口仍在听 → 只对命令行匹配的残留 pid 补 KILL（reload 子进程等）
  for p in $(port_pids "$port"); do
    if cmd_matches "$p" "$pat"; then
      echo "    [stop] $label：KILL 残留 pid $p"
      kill -KILL "$p" 2>/dev/null || true
    fi
  done
  for i in $(seq 1 10); do
    if ! port_listening "$port"; then
      echo "    [ok] $label：端口 $port 已释放（KILL 后）"
      return 0
    fi
    sleep 0.5
  done
  warn "$label：端口 $port 仍在监听，未再动手："
  ss -ltnpH "sport = :$port" 2>/dev/null | sed 's/^/    /' >&2 || true
  return 1
}

main() {
  mkdir -p "$RUN_DIR"

  note "容器红线检查（docker ps 里 6678/6679 或旧 6666/6667 映射）"
  # 新端口之外也拦旧端口：端口重排前起的容器可能还映射 6666/6667，同样不许误杀。
  local hits
  hits="$(docker_ps_table | grep -E ':(6678|6679|6666|6667)->' || true)"
  if [[ -n "$hits" ]]; then
    echo "[REFUSE] 以下容器占用了 6678/6679（或旧 6666/6667），本脚本不碰容器，一个进程都没杀：" >&2
    printf '%s\n' "$hits" | sed 's/^/    /' >&2
    echo "         容器化开发是备选方案；要停请自行决定（如 sudo -n docker compose stop）。" >&2
    exit 2
  fi
  echo "    [ok] 无容器占用 6678/6679/6666/6667"

  note "停止开发进程"
  local rc=0
  # 模式要覆盖父进程与子进程：uvicorn --reload 的子进程命令行是 multiprocessing.spawn，
  # next dev 的子进程是 next/dist/server/lib/start-server.js —— 漏掉就杀不干净。
  stop_one "前端" "$FE_PID" "$FRONTEND_PORT" 'next/dist/bin/next|start-server\.js' || rc=1
  stop_one "后端" "$BE_PID" "$BACKEND_PORT" 'uvicorn|multiprocessing\.spawn' || rc=1

  echo
  note "端口复核"
  local port state
  for port in "$FRONTEND_PORT" "$BACKEND_PORT"; do
    if port_listening "$port"; then
      state="仍在监听"
    else
      state="无监听"
    fi
    printf '    :%s  %s\n' "$port" "$state"
  done

  [[ "$rc" -eq 0 ]] || { warn "有端口未释放，见上"; exit 1; }
  echo
  echo "    准备再起：bash scripts/dev-up.sh"
}

main "$@"