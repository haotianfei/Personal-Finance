#!/usr/bin/env bash
# restore_drill.sh —— 恢复演练：从宿主 tar 备份起一套隔离副本，对拍行数与审计链
#
# 用法：
#   bash scripts/restore_drill.sh                 # dry-run（默认）：只打印计划，不建容器、不解包
#   bash scripts/restore_drill.sh --apply         # 真跑一次完整演练（结束后自动清理临时栈）
#   bash scripts/restore_drill.sh --tar <path>    # 指定备份包（默认 deploy/backups 里最新一份）
#   bash scripts/restore_drill.sh --keep-tmp      # 成功后也保留解压目录（失败时无论如何都保留）
#   bash scripts/restore_drill.sh -h
#
# 环境变量：
#   DRILL_BACKEND_IMAGE  演练镜像（默认 person_fin-backend:staging-20261109）
#   DRILL_BACKEND_PORT   演练后端端口（默认 6687，仅绑 127.0.0.1）
#   DRILL_ROOT           解压基线目录（默认 ~/.cache/tmp/person_f）
#
# 安全边界（脚本级强制，见 .scratch/env-tiers/issues/06-backup-timer-and-restore-drill.md）：
#   * compose 项目名写死 person_fin_drill —— 断言不等于 person_fin（生产）与 person_fin_staging（预发），
#     网络也用独立的 person_fin_drill_network，不接 staging 的网络、不读 deploy-staging/data；
#   * 端口必须不等于生产/预发/dev 已知端口（6666/6667/6678/6679/6688/6689/6698/6699/6788/6789）；
#   * 只读演练：容器里的库是解压出来的副本，绝不写 deploy/data；
#   * 结束（无论成败）都 compose down 清容器与网络，并复查无残留；不 prune 任何镜像、不删任何备份 tar。
#
# 演练检查项：
#   1. 解压后的库 PRAGMA integrity_check = ok；
#   2. 起演练栈后 /api 读数与备份内一致：assets 总数、assets active 数（superseded_by IS NULL）；
#   3. 审计链 /api/audit-log/chain/verify = ok（含日封存自链与链头比对）；
#   4. 测试库副本（sqlite backup API 落出的单文件）与解压原库逐项相等。
#
# 报告：.scratch/env-tiers/drills/<YYYYMMDD>.md（同一天多次跑则追加一节）
# 退出码：0 全部通过 / 1 有检查不通过 / 2 环境缺失（docker/镜像/端口/备份包）
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$REPO/deploy/backups"
REPORT_DIR="$REPO/.scratch/env-tiers/drills"
DRILL_ROOT="${DRILL_ROOT:-$HOME/.cache/tmp/person_f}"

PROJECT="person_fin_drill"
NETWORK="person_fin_drill_network"
FORBIDDEN_PROJECTS=(person_fin person_fin_staging)

BE_IMAGE="${DRILL_BACKEND_IMAGE:-person_fin-backend:staging-20261109}"
BE_PORT="${DRILL_BACKEND_PORT:-6687}"
FORBIDDEN_PORTS=(6666 6667 6678 6679 6688 6689 6698 6699 6788 6789)

APPLY=0
KEEP_TMP=0
TAR_FILE=""

die()     { echo "[FAIL] $*" >&2; exit 1; }
env_die() { echo "[环境缺失] $*" >&2; exit 2; }
note()    { echo; echo "==> $*"; }
sub()     { echo "    $*"; }
usage()   { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --dry-run)    APPLY=0; shift ;;
    --tar)        TAR_FILE="${2:?--tar 需要路径}"; shift 2 ;;
    --keep-tmp)   KEEP_TMP=1; shift ;;
    --port)       BE_PORT="${2:?--port 需要端口}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

# ------------------------------------------------------------------ 项目名/端口守卫
for bad in "${FORBIDDEN_PROJECTS[@]}"; do
  [[ "$PROJECT" != "$bad" ]] || die "compose 项目名被改成 $bad，拒绝执行"
done
[[ "$PROJECT" == person_fin_drill ]] || die "项目名必须是 person_fin_drill（当前 $PROJECT）"
[[ "$NETWORK" == person_fin_drill_network ]] || die "网络名必须是 person_fin_drill_network（当前 $NETWORK）"
for bad in "${FORBIDDEN_PORTS[@]}"; do
  [[ "$BE_PORT" != "$bad" ]] || die "演练端口 $BE_PORT 与环境端口 $bad 冲突，换 DRILL_BACKEND_PORT"
done
[[ "$BE_PORT" =~ ^[0-9]+$ ]] || die "端口必须是数字（当前 $BE_PORT）"

# ------------------------------------------------------------------ 备份包选择
pick_tar() {
  if [[ -n "$TAR_FILE" ]]; then
    [[ -f "$TAR_FILE" ]] || env_die "备份包不存在：$TAR_FILE"
    printf '%s' "$TAR_FILE"
    return
  fi
  [[ -d "$BACKUP_DIR" ]] || env_die "备份目录不存在：$BACKUP_DIR"
  local newest
  newest="$(ls -1 "$BACKUP_DIR"/data-*.tar.gz 2>/dev/null | sort | tail -1 || true)"
  [[ -n "$newest" ]] || env_die "$BACKUP_DIR 下没有 data-<TS>.tar.gz（先跑 deploy/scripts/backup-data.sh）"
  printf '%s' "$newest"
}

TAR_FILE="$(pick_tar)"
TAR_SIZE="$(stat -c %s "$TAR_FILE")"
TAR_MTIME="$(stat -c %y "$TAR_FILE" | cut -d. -f1)"
TS="$(date +%Y%m%d-%H%M%S)"
DRILL_DIR="$DRILL_ROOT/drill-$TS"
REPORT="$REPORT_DIR/$(date +%Y%m%d).md"

pick_python() {
  if [[ -n "${PERSON_FIN_PYTHON:-}" ]]; then
    printf '%s' "$PERSON_FIN_PYTHON"
  elif [[ -x "$REPO/.venv/bin/python" ]]; then
    printf '%s' "$REPO/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' python3
  else
    return 1
  fi
}
PY="$(pick_python)" || env_die "找不到 python（需要 .venv/bin/python 或 python3）"

TAR_SHA256="$("$PY" -c 'import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],"rb") as fh:
    for c in iter(lambda: fh.read(1<<20), b""): h.update(c)
print(h.hexdigest())' "$TAR_FILE")"

# ------------------------------------------------------------------ 计划（dry-run 也走这里）
note "恢复演练计划"
sub "备份包     : $TAR_FILE"
sub "               $(numfmt --to=iec --suffix=B "$TAR_SIZE")  mtime=$TAR_MTIME"
sub "sha256     : $TAR_SHA256"
sub "解压目录   : $DRILL_DIR（解出 data/，含 person_fin.db 与 -wal/-shm）"
sub "演练库     : data/person_fin_drill.db（由解压库经 sqlite backup API 落成单文件，db_role=unknown）"
sub "compose    : 项目 $PROJECT｜网络 $NETWORK｜镜像 $BE_IMAGE"
sub "端口       : 127.0.0.1:$BE_PORT → 容器 8000（不对外）"
sub "报告       : $REPORT"
echo
note "演练步骤"
sub "1) tar xzf 解包 → 只读检查 integrity_check + 行数 + 链头（备份内基线）"
sub "2) sqlite backup API 落出单文件演练库，逐项对拍基线"
sub "3) docker compose -p $PROJECT up -d（只起 backend，restart=no）"
sub "4) 等 /api/health 通过，再对拍 /api 读数：assets 总数、assets active、audit_log 总数、链头"
sub "5) /api/audit-log/chain/verify 全量校验（分页拉完），要求 ok=true"
sub "6) 写报告 → compose down 清容器与网络 → 复查无残留（不 prune 镜像、不删备份）"

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "==> dry-run 结束：未解包、未建容器、未写报告。加 --apply 真跑。"
  exit 0
fi

# ------------------------------------------------------------------ 环境检查
if docker info >/dev/null 2>&1; then
  DK=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DK=(sudo -n --preserve-env=DRILL_BACKEND_IMAGE,DRILL_BACKEND_PORT docker)
else
  env_die "docker 不可用（当前用户无权限且 sudo -n 不通）"
fi
dk() { "${DK[@]}" "$@"; }

dk image inspect "$BE_IMAGE" >/dev/null 2>&1 \
  || env_die "镜像缺失：$BE_IMAGE（用 deploy/scripts/package.sh + docker build 打 staging tag）"
if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$BE_PORT/api/health"; then
  env_die "127.0.0.1:$BE_PORT 已有服务在应答，换 DRILL_BACKEND_PORT"
fi

COMPOSE_FILE="$DRILL_DIR/docker-compose.drill.yml"
compose() { dk compose -f "$COMPOSE_FILE" -p "$PROJECT" "$@"; }

mkdir -p "$DRILL_DIR" "$REPORT_DIR"
[[ -f "$REPORT" ]] || printf '# 恢复演练报告（%s）\n' "$(date +%F)" > "$REPORT"

# ------------------------------------------------------------------ 清理（幂等，trap 兜底）
CLEANED=0
VERDICT="FAIL"
FAIL_REASON=""
do_cleanup() {
  local rc=$?
  set +e
  [[ "$CLEANED" -eq 1 ]] && return 0
  CLEANED=1
  local down_state="未起栈（无需 down）" left_ids="" left_count=0 net_state="—"
  if [[ -f "$COMPOSE_FILE" ]]; then
    if compose down --remove-orphans >/dev/null 2>&1; then
      down_state="compose down 成功"
    else
      down_state="compose down 返回非 0（继续兜底清理）"
    fi
  fi
  left_ids="$(dk ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null || true)"
  if [[ -n "$left_ids" ]]; then
    # shellcheck disable=SC2086
    dk rm -f $left_ids >/dev/null 2>&1 || true
  fi
  left_count="$(dk ps -a --filter "label=com.docker.compose.project=$PROJECT" -q 2>/dev/null | wc -l)"
  if dk network inspect "$NETWORK" >/dev/null 2>&1; then
    if dk network rm "$NETWORK" >/dev/null 2>&1; then net_state="已删除"; else net_state="仍存在（可能有容器连着）"; fi
  else
    net_state="不存在"
  fi
  local tmp_state="保留 ${DRILL_DIR/#$HOME/~}"
  if [[ "$KEEP_TMP" -eq 0 && "$VERDICT" == "PASS" ]]; then
    rm -rf "$DRILL_DIR" && tmp_state="已删除"
  fi
  {
    echo
    echo "### 清理"
    echo
    echo "| 项 | 结果 |"
    echo "|---|---|"
    echo "| compose down | $down_state |"
    echo "| 残留容器（label project=$PROJECT） | $left_count 个 |"
    echo "| 网络 $NETWORK | $net_state |"
    echo "| 解压目录 | $tmp_state |"
    echo "| 镜像 | 未 prune，未删除 |"
    echo "| 备份 tar | 未删除 |"
  } >> "$REPORT"
  echo
  echo "==> 清理：$down_state｜残留容器 $left_count 个｜网络 $net_state｜解压目录 $tmp_state"
  return "$rc"
}
finish_fail() {
  FAIL_REASON="$1"
  VERDICT="FAIL"
  {
    echo
    echo "**verdict: FAIL —— $FAIL_REASON**"
  } >> "$REPORT"
  echo "[FAIL] $FAIL_REASON" >&2
  do_cleanup || true
  echo "==> 报告：$REPORT" >&2
  exit 1
}
trap 'do_cleanup' EXIT

# ------------------------------------------------------------------ 1) 解包
note "解包 $TAR_FILE → $DRILL_DIR"
tar xzf "$TAR_FILE" -C "$DRILL_DIR" || finish_fail "tar 解包失败"
DB_SRC="$DRILL_DIR/data/person_fin.db"
if [[ ! -f "$DB_SRC" ]]; then
  DB_SRC="$(ls -1 "$DRILL_DIR"/data/*.db 2>/dev/null | sort | tail -1 || true)"
  [[ -n "$DB_SRC" ]] || finish_fail "解包后找不到 data/*.db"
  echo "    [warn] 未找到 data/person_fin.db，改用 $DB_SRC"
fi
DB_COPY="$DRILL_DIR/data/person_fin_drill.db"

# ------------------------------------------------------------------ 2) 基线 + 落单文件
note "检查备份内基线（只读）并落演练库"
"$PY" - "$DB_SRC" "$DB_COPY" "$REPORT" "$TAR_FILE" "$TAR_SHA256" "$TAR_SIZE" "$DRILL_DIR" "$REPO" "$HOME" <<'PY' \
  || finish_fail "备份内基线检查失败（integrity 或行数异常）"
import hashlib
import os
import sqlite3
import sys
from datetime import datetime

db_src, db_copy, report, tar_path, tar_sha, tar_size, drill_dir, repo, home = sys.argv[1:10]


def display(path):
    """报告要入库（.scratch 受版本管理）：本机绝对路径一律短化，避免公开仓库泄露路径。"""
    p = os.path.abspath(path)
    if p.startswith(repo + os.sep):
        return os.path.relpath(p, repo)
    if p.startswith(home + os.sep):
        return "~/" + os.path.relpath(p, home)
    return p


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stats(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def one(sql):
        try:
            row = con.execute(sql).fetchone()
            return "n/a" if row is None or row[0] is None else row[0]
        except sqlite3.Error as exc:
            return f"ERR:{exc}"

    out = {
        "integrity": one("pragma integrity_check"),
        "schema_version": one("select max(version) from schema_version"),
        "assets_total": one("select count(*) from assets"),
        "assets_active": one("select count(*) from assets where superseded_by is null"),
        "audit_log": one("select count(*) from audit_log"),
        "chain_last_id": one("select last_id from audit_chain_state where id=1"),
        "chain_row_count": one("select row_count from audit_chain_state where id=1"),
        "chain_last_hash": one("select last_hash from audit_chain_state where id=1"),
        "seals": one("select count(*) from audit_chain_seals"),
    }
    con.close()
    return out


base = stats(db_src)

# 演练库：sqlite backup API 落成单文件（解压库的 -wal 一并合并），容器只碰这一份
src = sqlite3.connect(f"file:{db_src}?mode=ro", uri=True)
dst = sqlite3.connect(db_copy)
src.backup(dst)
dst.close()
src.close()
copy = stats(db_copy)

lines = []
lines.append("")
lines.append(f"## 运行 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append("")
lines.append(f"- 备份包：`{display(tar_path)}`（{int(tar_size):,} B）sha256=`{tar_sha}`")
lines.append(f"- 解压目录：`{display(drill_dir)}`")
lines.append(f"- 备份内库：`{os.path.basename(db_src)}` {os.path.getsize(db_src):,} B"
             f" sha256=`{sha256(db_src)}`")
lines.append(f"- 演练库：`{os.path.basename(db_copy)}` {os.path.getsize(db_copy):,} B"
             f" sha256=`{sha256(db_copy)}`")
lines.append("")
lines.append("### 备份内基线（解压后只读）")
lines.append("")
lines.append("| 项 | 值 |")
lines.append("|---|---|")
for key in ("integrity", "schema_version", "assets_total", "assets_active",
            "audit_log", "chain_last_id", "chain_row_count", "seals"):
    lines.append(f"| {key} | `{base[key]}` |")
lines.append(f"| chain_last_hash | `{str(base['chain_last_hash'])[:16]}…` |")

with open(report, "a", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")

print(f"    integrity={base['integrity']}  schema={base['schema_version']}"
      f"  assets={base['assets_total']}  active={base['assets_active']}"
      f"  audit_log={base['audit_log']}  chain={base['chain_last_id']}/{base['chain_row_count']}"
      f"  seals={base['seals']}")

if base["integrity"] != "ok":
    print(f"[FAIL] 备份内 integrity_check={base['integrity']}", file=sys.stderr)
    sys.exit(1)

diffs = [k for k in base if copy[k] != base[k] and k != "integrity"]
if copy["integrity"] != "ok" or diffs:
    print(f"[FAIL] 演练库与备份内不一致/不完整：integrity={copy['integrity']} diffs={diffs}",
          file=sys.stderr)
    sys.exit(1)
print(f"    演练库单文件校验通过（与备份内逐项相等）sha256={sha256(db_copy)[:16]}…")
PY

# ------------------------------------------------------------------ 3) 起演练栈
cat > "$COMPOSE_FILE" <<EOF
# 由 scripts/restore_drill.sh 生成（临时文件，不入库）
# 项目名/网络名写死：绝不与 person_fin（生产）或 person_fin_staging（预发）同项目
name: $PROJECT

services:
  backend:
    image: \${DRILL_BACKEND_IMAGE:-person_fin-backend:staging-20261109}
    init: true
    restart: "no"
    environment:
      DATABASE_PATH: /app/data/person_fin_drill.db
      TZ: Asia/Shanghai
    volumes:
      # 解压出来的副本目录；不挂 paddle 卷（演练不跑 OCR，避免额外下载/写共享缓存）
      - ./data:/app/data:z
    ports:
      - "127.0.0.1:\${DRILL_BACKEND_PORT:-$BE_PORT}:8000"

networks:
  default:
    name: $NETWORK
EOF

export DRILL_BACKEND_IMAGE="$BE_IMAGE" DRILL_BACKEND_PORT="$BE_PORT"
note "启动演练栈（项目 $PROJECT，镜像 $BE_IMAGE，端口 127.0.0.1:$BE_PORT）"
compose up -d || finish_fail "compose up 失败"

BE_CID="$(compose ps -q backend)"
[[ -n "$BE_CID" ]] || finish_fail "拿不到 backend 容器 id"

note "等待 /api/health（镜像内 start-period 90s，最长约 200s）"
health=000
for _ in $(seq 1 40); do
  health="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$BE_PORT/api/health" || true)"
  if [[ "$health" == "200" ]]; then break; fi
  if [[ "$(dk inspect --format '{{.State.Status}}' "$BE_CID" 2>/dev/null || echo unknown)" == "exited" ]]; then
    dk logs --tail=40 "$BE_CID" >&2 || true
    finish_fail "后端容器已退出，健康检查未通过"
  fi
  sleep 5
done
[[ "$health" == "200" ]] || {
  dk logs --tail=40 "$BE_CID" >&2 || true
  finish_fail "健康检查未通过（/api/health → $health）"
}
sub "后端 healthy：http://127.0.0.1:$BE_PORT/api/health → 200"

# ------------------------------------------------------------------ 4) 对拍 + 报告
note "对拍 /api 读数与备份内基线，并校验审计链"
if "$PY" - "$DB_COPY" "http://127.0.0.1:$BE_PORT" "$REPORT" "$BE_IMAGE" "$PROJECT" <<'PY'
import json
import sqlite3
import sys
import urllib.request

db_copy, base_url, report, image, project = sys.argv[1:6]
base_url = base_url.rstrip("/")


def local_stats():
    con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)

    def one(sql):
        row = con.execute(sql).fetchone()
        return "n/a" if row is None or row[0] is None else row[0]

    out = {
        "integrity": one("pragma integrity_check"),
        "assets_total": one("select count(*) from assets"),
        "assets_active": one("select count(*) from assets where superseded_by is null"),
        "audit_log": one("select count(*) from audit_log"),
        "chain_last_id": one("select last_id from audit_chain_state where id=1"),
        "chain_row_count": one("select row_count from audit_chain_state where id=1"),
        "seals": one("select count(*) from audit_chain_seals"),
    }
    con.close()
    return out


def get_json(path):
    req = urllib.request.Request(base_url + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


want = local_stats()
errors = []
live = {}

try:
    live["health"] = get_json("/api/health").get("status")
except Exception as exc:  # noqa: BLE001
    live["health"] = f"ERR:{exc}"
    errors.append(f"/api/health 失败：{exc}")

try:
    live["assets_active"] = get_json("/api/assets?page=1&page_size=1")["total"]
    live["assets_total"] = get_json(
        "/api/assets?page=1&page_size=1&include_superseded=true")["total"]
    live["audit_log"] = get_json("/api/audit-log?page=1&page_size=1")["total"]
except Exception as exc:  # noqa: BLE001
    errors.append(f"列表端点失败：{exc}")
    for key in ("assets_active", "assets_total", "audit_log"):
        live.setdefault(key, "ERR")

# 审计链：分页拉完（limit 上限 20000）
from_id = 0
pages = 0
checked = 0
chain_ok = True
first_broken = None
head = None
seals = None
try:
    while True:
        d = get_json(f"/api/audit-log/chain/verify?from_id={from_id}&limit=20000")
        pages += 1
        checked += int(d.get("checked") or 0)
        chain_ok = chain_ok and bool(d.get("ok"))
        if first_broken is None and d.get("first_broken"):
            first_broken = d["first_broken"]
        head = d.get("head") or head
        seals = d.get("seals") or seals
        if not d.get("truncated"):
            break
        nid = d.get("next_from_id")
        if not nid or pages >= 20:
            break
        from_id = nid
    live["chain_ok"] = chain_ok
    live["chain_checked"] = checked
    live["chain_pages"] = pages
    live["chain_first_broken"] = first_broken
    live["chain_head"] = head
    live["chain_seals"] = seals
except Exception as exc:  # noqa: BLE001
    live["chain_ok"] = f"ERR:{exc}"
    errors.append(f"审计链校验失败：{exc}")


def cmp_row(label, want_val, live_val, note=""):
    if want_val == live_val:
        return f"| {label} | `{want_val}` | `{live_val}` | PASS |"
    return f"| {label} | `{want_val}` | `{live_val}` | **FAIL**{(' ' + note) if note else ''} |"


rows = [
    "| 检查项 | 备份内（库） | 演练栈（/api） | 判定 |",
    "|---|---|---|---|",
    cmp_row("assets 总数", want["assets_total"], live.get("assets_total")),
    cmp_row("assets active（superseded_by IS NULL）", want["assets_active"], live.get("assets_active")),
    cmp_row("audit_log 行数", want["audit_log"], live.get("audit_log")),
    cmp_row("链头 last_id", want["chain_last_id"], (head or {}).get("last_id")),
    cmp_row("链头 row_count", want["chain_row_count"], (head or {}).get("row_count")),
    cmp_row("integrity_check", "ok", want["integrity"]),
    cmp_row("审计链 verify", True, live.get("chain_ok")),
]

verdict_pass = (
    not errors
    and want["integrity"] == "ok"
    and live.get("assets_total") == want["assets_total"]
    and live.get("assets_active") == want["assets_active"]
    and live.get("audit_log") == want["audit_log"]
    and (head or {}).get("last_id") == want["chain_last_id"]
    and live.get("chain_ok") is True
)

lines = []
lines.append("")
lines.append("### 演练栈对拍")
lines.append("")
lines.append(f"- 镜像：`{image}`｜compose 项目：`{project}`")
lines.append(f"- 审计链 verify：ok=`{live.get('chain_ok')}` 校验行数={live.get('chain_checked')}"
             f" 分页={live.get('chain_pages')} 首个断裂={live.get('chain_first_broken')}")
if head:
    lines.append(f"- 链头：last_id=`{head.get('last_id')}` row_count=`{head.get('row_count')}`"
                 f" last_hash=`{str(head.get('last_hash'))[:16]}…`")
if seals:
    lines.append(f"- 日封存：条数=`{seals.get('checked')}` 首个断裂 id={seals.get('first_broken_seal_id')}"
                 f" 链尾是否被截断={seals.get('tail_truncated')}")
if want.get("seals") != "n/a":
    lines.append(f"- 封存条数（库内）：`{want['seals']}`")
if errors:
    lines.append(f"- 错误：{'; '.join(errors)}")
lines.extend(["", *rows, ""])
if verdict_pass:
    lines.append("**verdict: PASS**")

with open(report, "a", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")

for row in rows[2:]:
    print("    " + row)
if errors:
    print("    [warn] " + "; ".join(errors), file=sys.stderr)
sys.exit(0 if verdict_pass else 1)
PY
then
  VERDICT="PASS"
  note "演练通过"
else
  finish_fail "对拍不通过（见报告）"
fi

do_cleanup || true
trap - EXIT

[[ "$VERDICT" == "PASS" ]] || exit 1
echo
echo "==> 报告：$REPORT（verdict: $VERDICT）"
echo "==> 解压目录：$DRILL_DIR$([[ "$KEEP_TMP" -eq 0 ]] && echo '（已删除）' || echo '（--keep-tmp 保留）')"