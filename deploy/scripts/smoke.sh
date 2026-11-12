#!/usr/bin/env bash
# 冒烟检查：后端 health / 指标字典 / 前端首页 / 前端反代 API
#
# 默认打生产（deploy/.env 的 FRONTEND_PORT/BACKEND_PORT，缺省 6699/6698 —— 2026-11-09
# 已按三环境端口规则切换到 6699/6698）。
# 可用基址参数指向别的环境，例如 staging（6689/6688）：
#   ./scripts/smoke.sh --backend 127.0.0.1:6688 --frontend 127.0.0.1:6689
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$DEPLOY_DIR"
[[ -f .env ]] && set -a && . ./.env && set +a

FE="http://127.0.0.1:${FRONTEND_PORT:-6699}"
BE="http://127.0.0.1:${BACKEND_PORT:-6698}"
CUSTOM_TARGET=0

url_of() {  # 允许 "host:port" 或 "http://host:port"，去掉尾斜杠
  local v="${1%/}"
  [[ "$v" == http://* || "$v" == https://* ]] && printf '%s' "$v" || printf 'http://%s' "$v"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)  BE="$(url_of "${2:?--backend 需要 host:port}")"; CUSTOM_TARGET=1; shift 2 ;;
    --frontend) FE="$(url_of "${2:?--frontend 需要 host:port}")"; CUSTOM_TARGET=1; shift 2 ;;
    -h|--help)
      awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"
      exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

fail=0

check() {
  local name="$1" url="$2"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || true)"
  if [[ "$code" == "200" ]]; then
    echo "OK   $name 200  $url"
  else
    echo "FAIL $name $code  $url"
    fail=1
  fi
}

check "后端 health"      "$BE/api/health"
check "指标字典"          "$BE/api/metrics"
check "指标状态"          "$BE/api/metrics/status"
check "资产当前值"        "$BE/api/assets/current?as_of=2026-10-31"
check "前端首页"          "$FE/"
check "前端反代 API"      "$FE/api/health"

echo "==> 后端 health 详情"
curl -s --max-time 10 "$BE/api/health" || true
echo
if [[ "$CUSTOM_TARGET" -eq 1 ]]; then
  # 指定了别的基址时不要打印生产项目的 ps（会误导）
  echo "==> 容器（自定义基址，跳过；staging 用 bash scripts/staging.sh status）"
else
  echo "==> 容器"
  sudo docker compose --env-file .env ps || true
fi

exit "$fail"