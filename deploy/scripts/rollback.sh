#!/usr/bin/env bash
# rollback.sh —— 回滚到指定版本 tag（前后端成对；把 tag 重新指回 :local → up -d → smoke → 留痕）
#
# 用法：
#   bash deploy/scripts/rollback.sh                    # 无参数：列出现役 :local 与可用回滚点/版本 tag
#   bash deploy/scripts/rollback.sh <tag>              # 执行回滚（<tag> 例：rollback-20261109-143000 / 20261109-882c91b）
#   bash deploy/scripts/rollback.sh <tag> --dry-run    # 只打印将执行的动作，不改状态
#   bash deploy/scripts/rollback.sh -h
#
# <tag> 可带仓库前缀（person_fin-backend:rollback-…）；脚本会去掉前缀，前后端成对使用同一后缀。
# 回滚点由 release.sh 第 ② 步生成：:rollback-<YYYYMMDD-HHMMSS>。
#
# 退出码（与 scripts/ci.sh 的约定一致）：0 通过 / 1 断言失败（healthy 超时、smoke 红）/ 2 环境缺失
#
# 安全边界：只操作生产项目 person_fin（deploy/docker-compose.yml）；不碰 staging 环境与 staging-* 镜像。
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEPLOY_DIR="${DEPLOY_DIR_OVERRIDE:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO_DIR="${REPO_DIR:-$(cd "$DEPLOY_DIR/.." && pwd)}"
export REPO_DIR
cd "$DEPLOY_DIR"

BE_REPO="person_fin-backend"
FE_REPO="person_fin-frontend"
REL_FILE="$DEPLOY_DIR/RELEASES.md"
ENV_FILE="$DEPLOY_DIR/.env"
OPERATOR="${RELEASE_OPERATOR:-$(id -un)}"

die()     { echo "[FAIL] $*" >&2; exit 1; }
env_die() { echo "[环境缺失] $*" >&2; exit 2; }
note()    { echo; echo "==> $*"; }
sub()     { echo "    $*"; }
usage()   { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

APPLY=1        # 带 <tag> 调用的默认语义就是「执行」
TAG_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)  usage; exit 0 ;;
    --list)     TAG_ARG=""; shift ;;
    --dry-run)  APPLY=0; shift ;;
    -*)         die "未知参数：$1（-h 看用法）" ;;
    *)
      [[ -z "$TAG_ARG" ]] || die "只接受一个 tag（已收到 $TAG_ARG，又收到 $1）"
      TAG_ARG="$1"; shift ;;
  esac
done

# ---------------------------------------------------------------- 基础

if docker info >/dev/null 2>&1; then
  DK=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DK=(sudo -n docker)        # sudo 清环境无影响：镜像名写死，.env 走 --env-file
else
  env_die "docker 不可用（当前用户无权限且 sudo -n 不通）"
fi

releases_ensure() {
  [[ -f "$REL_FILE" ]] && return 0
  cat > "$REL_FILE" <<'HDR'
# RELEASES · 生产上线与回滚台账（append-only）

由 `deploy/scripts/release.sh` 与 `deploy/scripts/rollback.sh` 自动追加。
**不要改历史行**：记录与实际不符时补一行说明（带「修正」字样），而不是覆盖。

| 时间 | commit | 版本 tag | smoke | 备注 |
| --- | --- | --- | --- | --- |
HDR
}

releases_append() {
  releases_ensure
  printf '%s\n' "$1" >> "$REL_FILE"
}

# 从 RELEASES 里查某个 tag 当初是哪个 commit 发的（查不到返回空）
commit_of_tag() {
  [[ -f "$REL_FILE" ]] || return 0
  awk -F'|' -v t="$1" '
    NF>=7 {
      tag=$4; gsub(/^[ \t]+|[ \t]+$/,"",tag)
      if (tag==t) { c=$3; gsub(/^[ \t]+|[ \t]+$/,"",c); print c; exit }
    }' "$REL_FILE"
}

image_id_of() { "${DK[@]}" image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

short_id() { local i="${1#sha256:}"; [[ -n "$i" ]] && printf '%s' "${i:0:12}" || printf '?'; }

list_images() {  # $1=正则（对 <repo>:<tag> 匹配），按 tag 逆序（时间新的在前）
  "${DK[@]}" images --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.CreatedAt}}' \
    | awk -F'|' -v p="$1" '$1 ~ p' | sort -t'|' -k1,1r
}

cmd_list() {
  note "现役 :local（容器正在用的 tag）"
  local t
  for t in "$BE_REPO:local" "$FE_REPO:local"; do
    if "${DK[@]}" image inspect "$t" >/dev/null 2>&1; then
      "${DK[@]}" images --format '    {{.Repository}}:{{.Tag}}  {{.ID}}  {{.CreatedAt}}' "$t" || true
    else
      sub "$t 不存在"
    fi
  done

  note "回滚点 rollback-*（release.sh 第 ② 步生成；prune-images.sh 保留最近 3 个）"
  local rows
  rows="$(list_images "^(${BE_REPO}|${FE_REPO}):rollback-[0-9]{8}-[0-9]{6}$" || true)"
  if [[ -z "$rows" ]]; then
    sub "（还没有回滚点：下次 release.sh --apply 会自动生成）"
  else
    printf '%s\n' "$rows" | awk -F'|' '{printf "    %-46s %-14s %s\n", $1, $2, $3}'
  fi

  note "版本化 tag <YYYYMMDD>-<shortsha>（prune-images.sh 保留最近 5 个）"
  rows="$(list_images "^(${BE_REPO}|${FE_REPO}):[0-9]{8}-[0-9a-f]{6,40}$" || true)"
  if [[ -z "$rows" ]]; then
    sub "（还没有版本化 tag）"
  else
    printf '%s\n' "$rows" | awk -F'|' '{printf "    %-46s %-14s %s\n", $1, $2, $3}'
  fi

  note "其它 tag（本脚本不动）"
  rows="$(list_images "^(${BE_REPO}|${FE_REPO}):(staging-|pre-|latest)" || true)"
  if [[ -z "$rows" ]]; then sub "（无）"; else
    printf '%s\n' "$rows" | awk -F'|' '{printf "    %-46s %-14s %s\n", $1, $2, $3}'
  fi

  echo
  echo "==> 回滚用法：bash deploy/scripts/rollback.sh <上面任一的 tag 后缀，不含仓库名前缀也行>"
  echo "    例：bash deploy/scripts/rollback.sh rollback-20261109-143000"
}

cmd_rollback() {
  local tag="$1"
  tag="${tag##*:}"                        # 允许传 person_fin-backend:rollback-…
  [[ -n "$tag" ]] || die "空 tag"
  [[ "$tag" != "local" ]] || die "回滚到 :local 是空操作（就是要改 :local）；请给 rollback-* 或 <YYYYMMDD>-<sha>"

  [[ -f "$ENV_FILE" ]] || env_die "缺少 $ENV_FILE（端口/数据目录都在里面）先 cp deploy/.env.example deploy/.env"
  grep -qE "^[[:space:]]*image:[[:space:]]*${BE_REPO}:local[[:space:]]*$" "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null \
    || env_die "deploy/docker-compose.yml 的 backend image 不是 ${BE_REPO}:local（tag 契约失效，拒绝继续）"

  local be="$BE_REPO:$tag" fe="$FE_REPO:$tag" prev_id
  "${DK[@]}" image inspect "$be" >/dev/null 2>&1 || env_die "镜像不存在：$be（用 --list 看可用 tag）"
  "${DK[@]}" image inspect "$fe" >/dev/null 2>&1 || env_die "镜像不存在：$fe（回滚需要前后端成对，缺一不可）"

  prev_id="$(short_id "$(image_id_of "$BE_REPO:local")")"
  local commit; commit="$(commit_of_tag "$tag")"
  [[ -n "$commit" ]] || commit="${commit:-未知（RELEASES 无该 tag 记录）}"

  echo
  echo "rollback.sh · $([[ "$APPLY" -eq 1 ]] && echo 'APPLY（真回滚）' || echo 'dry-run（不改状态）')"
  sub "目标 tag : $be / $fe"
  sub "现役     : :local（后端镜像 ${prev_id}，将被目标 tag 覆盖）"
  sub "commit   : $commit"
  sub "操作者   : $OPERATOR"

  ROW_TS="$(date '+%Y-%m-%d %H:%M:%S')"

  note "① 把 $tag 重新指回 :local"
  if [[ "$APPLY" -eq 1 ]]; then
    sub "\$ ${DK[*]} tag $be $BE_REPO:local"
    "${DK[@]}" tag "$be" "$BE_REPO:local"
    sub "\$ ${DK[*]} tag $fe $FE_REPO:local"
    "${DK[@]}" tag "$fe" "$FE_REPO:local"
  else
    sub "[dry-run] ${DK[*]} tag $be $BE_REPO:local"
    sub "[dry-run] ${DK[*]} tag $fe $FE_REPO:local"
  fi

  note "② up -d（compose 按新镜像重建容器）"
  if [[ "$APPLY" -eq 1 ]]; then
    sub "\$ ${DK[*]} compose --env-file .env up -d"
    "${DK[@]}" compose --env-file .env up -d
  else
    sub "[dry-run] ${DK[*]} compose --env-file .env up -d"
  fi

  note "③ 等 healthy + smoke"
  if [[ "$APPLY" -ne 1 ]]; then
    sub "[dry-run] 轮询 healthy（最多 90×5s）"
    sub "[dry-run] bash $DEPLOY_DIR/scripts/smoke.sh"
    echo
    echo "==> dry-run 结束：未改 tag、未动容器"
    return 0
  fi

  local smoke="6/6 ok" rc=0
  if ! wait_healthy backend 90; then smoke="fail（后端未 healthy）"; rc=1
  elif ! wait_healthy frontend 30; then smoke="fail（前端未 healthy）"; rc=1
  elif ! assert_running_image backend "$BE_REPO:local"; then smoke="fail（后端镜像未切换）"; rc=1
  elif ! assert_running_image frontend "$FE_REPO:local"; then smoke="fail（前端镜像未切换）"; rc=1
  else
    bash "$DEPLOY_DIR/scripts/smoke.sh" || { rc=$?; smoke="fail（smoke rc=$rc）"; }
  fi

  note "④ 留痕：$REL_FILE"
  local row="| $ROW_TS | $commit | $tag（回滚） | $smoke | 回滚事件：:local ← $tag（原 :local 后端镜像 ${prev_id}） · $OPERATOR |"
  releases_append "$row"
  sub "$row"

  if [[ "$rc" -ne 0 ]]; then
    echo "[FAIL] 回滚后验收未通过（$smoke）：容器已切到 $tag，但服务不可用，需人工介入" >&2
    return 1
  fi
  note "回滚完成"
  sub "现役 :local = $tag"
  echo
  return 0
}

wait_healthy() {                 # $1=service $2=最多轮数（每轮 5s）
  local svc="$1" rounds="$2" cid state="unknown"
  cid="$("${DK[@]}" compose --env-file .env ps -q "$svc")"
  [[ -n "$cid" ]] || { echo "    找不到 $svc 容器" >&2; return 1; }
  for _ in $(seq 1 "$rounds"); do
    state="$("${DK[@]}" inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    case "$state" in
      healthy)   sub "$svc healthy"; return 0 ;;
      unhealthy) echo "    $svc unhealthy（日志：sudo docker compose --env-file .env logs --tail=100 $svc）" >&2; return 1 ;;
      *)         sleep 5 ;;
    esac
  done
  echo "    等 $svc healthy 超时（最后状态：$state）" >&2
  return 1
}

assert_running_image() {         # $1=service $2=期望 tag
  local svc="$1" tag="$2" cid want got
  cid="$("${DK[@]}" compose --env-file .env ps -q "$svc")"
  want="$(image_id_of "$tag")"
  got="$("${DK[@]}" inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
  if [[ -n "$want" && "$want" == "$got" ]]; then
    sub "$svc 运行镜像 = $tag（$(short_id "$got")）"
    return 0
  fi
  echo "    $svc 运行镜像与 $tag 不一致（期望 $(short_id "$want") 实际 $(short_id "$got")）" >&2
  return 1
}

# ---------------------------------------------------------------- 分发

if [[ -z "$TAG_ARG" ]]; then
  cmd_list
else
  # 失败即退出：healthy/smoke 断言失败统一 rc=1
  cmd_rollback "$TAG_ARG" || exit 1
fi