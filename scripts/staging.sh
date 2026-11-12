#!/usr/bin/env bash
# staging.sh —— staging（预发）栈的唯一入口：up | down | status | logs
#
# 安全边界（脚本级强制）：
#   * 每次 docker compose 调用都固定带 -f deploy/docker-compose.staging.yml -p person_fin_staging，
#     不接受外部覆盖，因此**永远碰不到生产项目 person_fin**；
#   * 启动前断言编排文件里 name: 仍是 person_fin_staging、项目名不等于 person_fin；
#   * up 要求 deploy-staging/data/person_fin_staging.db 已存在 —— 宁可报错也不让后端
#     自己 CREATE 一个空库（空库会跑迁移 + seed，长得像成功，实则不是生产副本）。
#
# 用法：
#   bash scripts/staging.sh up         # 起栈并等后端 healthy
#   bash scripts/staging.sh status     # 只读：容器 + 库文件 + /api/health
#   bash scripts/staging.sh logs [backend|frontend] [-f]   # 默认最近 200 行
#   bash scripts/staging.sh down       # 停栈；数据目录与镜像保留
#
# 首次/刷新数据：bash scripts/staging-refresh.sh --apply
#
# 端口规则（2026-11-09 业主裁决）：后端尾 8 / 前端尾 9，十位数字表环境
# （7=dev，8=staging，9=prod）：dev=6678/6679，staging=6688/6689，prod=6698/6699。
# 见 .scratch/env-tiers/map.md；本脚本默认 6688/6689。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="$REPO/deploy/docker-compose.staging.yml"
PROJECT="person_fin_staging"
DATA_DIR="$REPO/deploy-staging/data"
DB="$DATA_DIR/person_fin_staging.db"
# 镜像可覆盖：staging 专用 tag 用 STAGING_BACKEND_IMAGE / STAGING_FRONTEND_IMAGE 传入
BE_IMAGE="${STAGING_BACKEND_IMAGE:-person_fin-backend:local}"
FE_IMAGE="${STAGING_FRONTEND_IMAGE:-person_fin-frontend:local}"
BACKEND_PORT="${STAGING_BACKEND_PORT:-6688}"
FRONTEND_PORT="${STAGING_FRONTEND_PORT:-6689}"

die() { echo "[FAIL] $*" >&2; exit 1; }
note() { echo "==> $*"; }

dk() {
  # compose 的镜像 tag 与端口都走环境变量插值，而 sudo 默认清空环境 → 显式 preserve 这四个键
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo -n --preserve-env=STAGING_BACKEND_IMAGE,STAGING_FRONTEND_IMAGE,STAGING_BACKEND_PORT,STAGING_FRONTEND_PORT docker "$@"
  fi
}

# 唯一允许的 compose 调用形式：文件与项目名写死在这里
compose() { dk compose -f "$COMPOSE" -p "$PROJECT" "$@"; }

assert_project() {
  [[ "$PROJECT" != "person_fin" ]] || die "项目名被改成 person_fin，拒绝执行"
  [[ -f "$COMPOSE" ]] || die "缺少编排文件：$COMPOSE"
  grep -qE "^name: *${PROJECT} *$" "$COMPOSE" \
    || die "$COMPOSE 里的 name: 与项目名 ${PROJECT} 不一致（拒绝在错项目上动手）"
}

cmd_up() {
  assert_project
  [[ -f "$DB" ]] || die "staging 库不存在：$DB
      先取生产库一致快照：bash scripts/staging-refresh.sh --apply
      （不让后端自建空库：空库会跑迁移+seed，看起来正常但不是生产副本）"
  dk image inspect "$BE_IMAGE" >/dev/null 2>&1 || die "镜像缺失：$BE_IMAGE（用 deploy/scripts/package.sh + docker build 打 staging tag）"
  dk image inspect "$FE_IMAGE" >/dev/null 2>&1 || die "镜像缺失：$FE_IMAGE（用 deploy/scripts/package.sh + docker build 打 staging tag）"
  if [[ "$BE_IMAGE" == *:local || "$FE_IMAGE" == *:local ]]; then
    echo "[warn] 仍在用生产 tag（$BE_IMAGE / $FE_IMAGE）：预演的是生产镜像，不是当前源码" >&2
    echo "       换 staging 专用 tag：STAGING_BACKEND_IMAGE=person_fin-backend:staging-YYYYMMDD \\" >&2
    echo "                            STAGING_FRONTEND_IMAGE=person_fin-frontend:staging-YYYYMMDD bash scripts/staging.sh up" >&2
  fi

  # 显式导出，保证 compose 用的镜像与上面校验的是同一对 tag
  export STAGING_BACKEND_IMAGE="$BE_IMAGE" STAGING_FRONTEND_IMAGE="$FE_IMAGE"

  note "启动项目 $PROJECT（$BE_IMAGE / $FE_IMAGE）"
  compose up -d

  # 镜像插值走环境变量（sudo 会清环境），落定后按容器实际 Config.Image 复核一次
  local be_cid fe_cid be_actual fe_actual
  be_cid="$(compose ps -q backend)"
  fe_cid="$(compose ps -q frontend)"
  be_actual="$(dk inspect --format '{{.Config.Image}}' "$be_cid")"
  fe_actual="$(dk inspect --format '{{.Config.Image}}' "$fe_cid")"
  [[ "$be_actual" == "$BE_IMAGE" ]] || die "后端实际镜像 $be_actual ≠ 期望 $BE_IMAGE（compose 插值没生效）"
  [[ "$fe_actual" == "$FE_IMAGE" ]] || die "前端实际镜像 $fe_actual ≠ 期望 $FE_IMAGE（compose 插值没生效）"

  note "等待后端健康检查通过（镜像内 HEALTHCHECK，start-period 90s）…"
  local cid state
  cid="$(compose ps -q backend)"
  for _ in $(seq 1 40); do
    state="$(dk inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    case "$state" in
      healthy) echo "    后端 healthy"; break ;;
      unhealthy) echo "    后端 unhealthy，看日志：bash scripts/staging.sh logs backend" >&2; break ;;
      *) sleep 5 ;;
    esac
  done
  compose ps
  echo
  echo "     前端 http://127.0.0.1:${FRONTEND_PORT}  后端 http://127.0.0.1:${BACKEND_PORT}"
  echo "     冒烟：bash deploy/scripts/smoke.sh --backend 127.0.0.1:${BACKEND_PORT} --frontend 127.0.0.1:${FRONTEND_PORT}"
  [[ "$state" == "healthy" ]]
}

cmd_down() {
  assert_project
  note "停止项目 $PROJECT（数据目录与镜像保留：$DATA_DIR）"
  compose down
  echo "     注：paddle 模型卷 person_fin_staging_paddle_cache 与库文件都不会被删除"
}

cmd_status() {
  assert_project
  note "项目 $PROJECT 容器"
  compose ps || true
  echo
  note "库文件"
  if [[ -f "$DB" ]]; then
    echo "     $DB"
    ls -l "$DB" | sed 's/^/     /'
    echo "     db_role=staging（文件名 _staging 推断，见 backend/database.py derive_db_role）"
  else
    echo "     不存在（先跑 bash scripts/staging-refresh.sh --apply）"
  fi
  echo
  note "后端 /api/health"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${BACKEND_PORT}/api/health" || true)"
  echo "     http://127.0.0.1:${BACKEND_PORT}/api/health → ${code:-000}"
}

cmd_logs() {
  local service="" follow=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--follow) follow=1; shift ;;
      backend|frontend) service="$1"; shift ;;
      *) die "logs 只接受 backend|frontend|-f（收到 $1）" ;;
    esac
  done
  assert_project
  local args=(logs --tail=200)
  [[ "$follow" -eq 1 ]] && args+=(-f)
  [[ -n "$service" ]] && args+=("$service")
  compose "${args[@]}"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  down) shift; cmd_down "$@" ;;
  status) shift; cmd_status "$@" ;;
  logs) shift; cmd_logs "$@" ;;
  -h|--help|"") awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; [[ -n "${1:-}" ]] || exit 2 ;;
  *) die "未知子命令：$1（up|down|status|logs）" ;;
esac