#!/usr/bin/env bash
# staging-refresh.sh —— 用生产在线库的**一致快照**刷新 staging 预发库（默认 dry-run）
#
# 为什么不用 cp：生产库常带未合并的 -wal（可达数 MB），裸拷主文件会得到陈旧快照
#   （.scratch/db-dev-suffix + MEMORY.md §八 2026-11-06 实测）。快照一律走 sqlite
#   backup API，实现复用 scripts/sync_deploy_db.py（不重复造轮子、不改它）。
#
# 行为：
#   dry-run（默认）：打印源/目标路径与各自 db_role、源库行数、归档计划，不写任何文件；
#   --apply        ：backup API 快照 → 校验行数/完整性 → 旧目标移入
#                    deploy-staging/data/archived-db-<YYYYMMDD>/（移动不删除，含 -wal/-shm）
#                    → 落位 → 修好容器 uid 1000 的读写权限 → 两侧 assets/active 对拍。
#
# 红线：源库 deploy/data/** 全程只读（URI mode=ro）；目标一律在 deploy-staging/data/ 下。
#
# 用法：
#   bash scripts/staging-refresh.sh              # dry-run
#   bash scripts/staging-refresh.sh --apply      # 真正落盘（需业主授权）
#   bash scripts/staging-refresh.sh --apply --force   # 栈仍在跑也强行替换（不推荐）
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$REPO/scripts/sync_deploy_db.py"
SRC_DEFAULT="$REPO/deploy/data/person_fin.db"
DST_DEFAULT="$REPO/deploy-staging/data/person_fin_staging.db"
ARCHIVE_DEFAULT="$REPO/deploy-staging/data/archived-db-$(date +%Y%m%d)"

PYTHON="${PYTHON:-python3}"
STAGING_PROJECT="person_fin_staging"

APPLY=0
FORCE=0
SRC="$SRC_DEFAULT"
DST="$DST_DEFAULT"
ARCHIVE="$ARCHIVE_DEFAULT"

usage() {
  awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --force) FORCE=1; shift ;;
    --src) SRC="${2:?--src 需要路径}"; shift 2 ;;
    --dst) DST="${2:?--dst 需要路径}"; shift 2 ;;
    --archive-root) ARCHIVE="${2:?--archive-root 需要路径}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

die() { echo "[FAIL] $*" >&2; exit 2; }
note() { echo "==> $*"; }

# docker 可用性：有权限直连就用 docker，否则用免密 sudo（不交互、不阻塞）
docker_query_ok() { docker info >/dev/null 2>&1 || sudo -n docker info >/dev/null 2>&1; }
dk() { if docker info >/dev/null 2>&1; then docker "$@"; else sudo -n docker "$@"; fi; }

[[ -f "$SYNC" ]] || die "缺少 $SYNC"
[[ -f "$SRC" ]] || die "源库不存在：$SRC（生产部署未初始化？）"

SRC="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"
DST_DIR="$(dirname "$DST")"
DST_NAME="$(basename "$DST")"

# —— 目标白名单：只允许写进 deploy-staging/data/，避免任何路径写错碰生产 ——
ALLOWED_ROOT="$REPO/deploy-staging/data"
mkdir_resolved="$(realpath -m "$DST_DIR")"
[[ "$mkdir_resolved" == "$(realpath -m "$ALLOWED_ROOT")" ]] \
  || die "目标目录必须是 $ALLOWED_ROOT（实际 $mkdir_resolved）"
[[ "$DST_NAME" == "person_fin_staging.db" ]] \
  || die "目标文件名必须是 person_fin_staging.db（实际 $DST_NAME，否则 db_role 推断不出 staging）"
[[ "$(realpath -m "$SRC")" != "$(realpath -m "$DST")" ]] || die "src 与 dst 相同，拒绝执行"

# —— 生产库只读断言：dst 绝不能落在 deploy/data/ 内 ——
case "$(realpath -m "$DST")" in
  "$(realpath -m "$REPO/deploy/data")"/*) die "拒绝把 staging 库写进 deploy/data（生产数据目录）" ;;
esac

echo "================ staging 库刷新 ================"
echo "源库（只读） $SRC"
echo "目标库       $DST"
echo "归档目录     $ARCHIVE"
[[ "$APPLY" -eq 1 ]] && echo "模式         apply（会落盘）" || echo "模式         dry-run（不落盘）"
echo

# —— 目标库被在跑的 staging 后端占用时替换是自找麻烦：先拦 ——
RUNNING=""
if docker_query_ok; then
  RUNNING="$(dk ps --filter "label=com.docker.compose.project=$STAGING_PROJECT" \
                --format '{{.Names}} ({{.State}})' 2>/dev/null || true)"
else
  echo "[warn] 无法查询 docker，跳过「staging 栈是否在跑」检查" >&2
fi
if [[ -n "$RUNNING" ]]; then
  echo "[warn] 项目 $STAGING_PROJECT 仍有容器在："
  printf '       %s\n' "$RUNNING"
  if [[ "$APPLY" -eq 1 && "$FORCE" -ne 1 ]]; then
    die "请先 bash scripts/staging.sh down 再刷新（确实要硬替换则加 --force）"
  fi
fi

if [[ "$APPLY" -eq 1 ]]; then
  # 目标目录由本脚本创建（umask 归调用者，随后由 fix_permissions 交给容器 uid）
  mkdir -p "$DST_DIR" "$ARCHIVE"
  # 容器内后端固定 uid/gid 1000（deploy/build/backend/Dockerfile: useradd -u 1000 app）
  # 副本由宿主用户创建，不授权则该用户写不了 WAL/evidence → 起栈即 unhealthy。
  note "修容器 uid 1000 的读写权限（$DST_DIR）"
  if command -v setfacl >/dev/null 2>&1 \
     && setfacl -R -m u:1000:rwX -m d:u:1000:rwX "$DST_DIR" 2>/dev/null; then
    echo "       ACL: u:1000:rwX + 默认 ACL"
  else
    echo "[warn] setfacl 不可用，退回 a+rwX（本机单用户数据目录）"
    chmod -R a+rwX "$DST_DIR"
  fi
fi

# —— 快照本体：全部委托 sync_deploy_db.py（backup API + 行数校验 + 归档 + 原子落位）——
SYNC_ARGS=(--src "$SRC" --dst "$DST" --archive-root "$ARCHIVE")
[[ "$APPLY" -eq 1 ]] && SYNC_ARGS+=(--apply)
echo "---- sync_deploy_db.py ${SYNC_ARGS[*]} ----"
rc=0
"$PYTHON" "$SYNC" "${SYNC_ARGS[@]}" || rc=$?
echo "---- end sync_deploy_db.py (rc=$rc) ----"
[[ $rc -eq 0 ]] || die "sync_deploy_db.py 失败（目标库未改动）"

# —— assets / active 对拍（sync 脚本只核 5 张表总行数，不含 active 维度）——
# active 口径＝`assets.superseded_by IS NULL`（models.py §assets 更正指针注释：
# 「写一次金额本体；净值/占比取 IS NULL 的 active 行」）。不是 asset_items.status，
# 也不是 assets 整表行数 —— 票 02 验收里的「active」按此口径。
echo
note "assets / active 行数对拍"
rc=0
"$PYTHON" - "$SRC" "$DST" <<'PY' || rc=$?
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]

def counts(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        total = conn.execute("select count(*) from assets").fetchone()[0]
        active = conn.execute(
            "select count(*) from assets where superseded_by is null").fetchone()[0]
        return total, active
    finally:
        conn.close()

import os
s_total, s_active = counts(src)
print(f"  src assets={s_total} active={s_active}")
if not os.path.exists(dst):
    print("  dst 不存在（dry-run：加 --apply 后才有对拍）")
    sys.exit(0)
d_total, d_active = counts(dst)
print(f"  dst assets={d_total} active={d_active}")
if (s_total, s_active) != (d_total, d_active):
    print(f"  [FAIL] 不一致：src={(s_total, s_active)} dst={(d_total, d_active)}")
    sys.exit(3)
print("  [ok] 一致")
PY
[[ $rc -eq 0 ]] || die "对拍失败（rc=$rc）"

echo
if [[ "$APPLY" -eq 1 ]]; then
  note "完成：$DST"
  echo "     下一步：bash scripts/staging.sh up"
else
  note "dry-run 结束（未做任何改动）；落盘请加 --apply"
fi