#!/usr/bin/env bash
# prune-images.sh —— 清理旧镜像 tag：每个镜像仓库各保留最近 5 个版本化 tag + 最近 3 个 rollback-* tag
#
# 用法：
#   bash deploy/scripts/prune-images.sh            # dry-run（默认）：只列出待删清单
#   bash deploy/scripts/prune-images.sh --apply    # 真删（只删 tag，不删 :local）
#   bash deploy/scripts/prune-images.sh -h
#
# 规则（写死，做出入都在这里改）：
#   * 版本化 tag：<YYYYMMDD>-<shortsha>          保留最新 5 个（按 tag 名逆序＝时间新的在前）
#   * 回滚点    ：rollback-<YYYYMMDD-HHMMSS>     保留最新 3 个
#   * 永不动   ：:local / staging-* / pre-* / latest，以及被任何容器（含已停止）引用的镜像
#   * 计数是「每个镜像仓库各自」：后端 5+3、前端 5+3
#
# 退出码（与 scripts/ci.sh 的约定一致）：0 通过 / 1 删除失败 / 2 环境缺失
#
# 安全边界：只操作 person_fin-backend / person_fin-frontend 两个本地仓库，不碰 staging 项目与其它镜像。
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEPLOY_DIR="${DEPLOY_DIR_OVERRIDE:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$DEPLOY_DIR"

BE_REPO="person_fin-backend"
FE_REPO="person_fin-frontend"

KEEP_VER=5
KEEP_RB=3

VER_RE='^[0-9]{8}-[0-9a-f]{6,40}$'
RB_RE='^rollback-[0-9]{8}-[0-9]{6}$'

APPLY=0

die()     { echo "[FAIL] $*" >&2; exit 1; }
env_die() { echo "[环境缺失] $*" >&2; exit 2; }
note()    { echo; echo "==> $*"; }
sub()     { echo "    $*"; }
usage()   { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

if docker info >/dev/null 2>&1; then
  DK=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DK=(sudo -n docker)
else
  env_die "docker 不可用（当前用户无权限且 sudo -n 不通）"
fi

image_id_of() { "${DK[@]}" image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

in_use() {               # 被任何容器（含已停止）引用的镜像一律不删
  local id cids
  id="$(image_id_of "$1")"
  [[ -n "$id" ]] || return 0        # 取不到 ID 时不冒险
  cids="$("${DK[@]}" ps -aq --filter "ancestor=$id" 2>/dev/null || true)"
  [[ -n "$cids" ]]
}

# 单仓库清理：$1=repo
PRUNE_COUNT=0
SKIP_COUNT=0
DELETE_FAILED=0

prune_repo() {
  local repo="$1"
  local tags ver rb keep_ver drop_ver keep_rb drop_rb t

  tags="$("${DK[@]}" images --format '{{.Repository}}|{{.Tag}}' | awk -F'|' -v r="$repo" '$1==r{print $2}')"
  if [[ -z "$tags" ]]; then
    note "$repo"
    sub "本地无该仓库镜像，跳过"
    return 0
  fi

  ver="$(printf '%s\n' "$tags" | grep -E "$VER_RE" | sort -r || true)"
  rb="$(printf '%s\n' "$tags" | grep -E "$RB_RE"  | sort -r || true)"

  keep_ver="$(printf '%s\n' "$ver" | sed -n "1,${KEEP_VER}p" | sed '/^$/d')"
  drop_ver="$(printf '%s\n' "$ver" | sed -n "$((KEEP_VER + 1)),\$p" | sed '/^$/d')"
  keep_rb="$(printf '%s\n' "$rb"  | sed -n "1,${KEEP_RB}p" | sed '/^$/d')"
  drop_rb="$(printf '%s\n' "$rb"  | sed -n "$((KEEP_RB + 1)),\$p" | sed '/^$/d')"

  note "$repo"
  sub "版本化 tag：共 $(printf '%s\n' "$ver" | sed '/^$/d' | wc -l) 个 → 保留 $KEEP_VER"
  if [[ -n "$keep_ver" ]]; then printf '%s\n' "$keep_ver" | sed 's/^/      [keep] /'; fi
  sub "回滚点     ：共 $(printf '%s\n' "$rb" | sed '/^$/d' | wc -l) 个 → 保留 $KEEP_RB"
  if [[ -n "$keep_rb" ]];  then printf '%s\n' "$keep_rb"  | sed 's/^/      [keep] /'; fi

  for t in $drop_ver $drop_rb; do
    local full="$repo:$t"
    if in_use "$full"; then
      sub "[skip] $full（被容器引用）"
      SKIP_COUNT=$((SKIP_COUNT + 1))
      continue
    fi
    if [[ "$APPLY" -eq 1 ]]; then
      if "${DK[@]}" rmi "$full" >/dev/null 2>&1; then
        sub "[del]  $full"
      else
        echo "    [FAIL] 删除失败：$full" >&2
        DELETE_FAILED=1
      fi
    else
      sub "[dry-run] 将删除 $full"
    fi
    PRUNE_COUNT=$((PRUNE_COUNT + 1))
  done

  if [[ -z "$drop_ver$drop_rb" ]]; then sub "（无需清理）"; fi
  sub "未纳入清理：$repo:local / staging-* / pre-* / latest"
}

echo
echo "prune-images.sh · $([[ "$APPLY" -eq 1 ]] && echo 'APPLY（真删）' || echo 'dry-run（只列清单）')"
sub "保留策略：每仓库 版本化 tag $KEEP_VER 个 + rollback-* $KEEP_RB 个"
sub "永不动  ：:local / staging-* / pre-* / latest / 被容器引用的镜像"

prune_repo "$BE_REPO"
prune_repo "$FE_REPO"

echo
if [[ "$APPLY" -eq 1 ]]; then
  echo "==> 已删除 $PRUNE_COUNT 个 tag（跳过 $SKIP_COUNT 个在用镜像）"
else
  echo "==> dry-run：待删 $PRUNE_COUNT 个 tag（跳过 $SKIP_COUNT 个在用镜像）；确认后加 --apply 真删"
fi

exit "$DELETE_FAILED"