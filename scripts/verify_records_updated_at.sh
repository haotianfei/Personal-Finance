#!/usr/bin/env bash
# 记录明细「默认按更新时间排序 + 更新时间列」验收：造合成 fixture → 起后端副本 → 构建前端 → 浏览器抓包 + 截图。
#
# 由来：`.scratch/records-updated-at/spec.md` §3 第 5–7 条（首屏请求带 sort_field=updated_at&desc /
# 最右列「更新时间」+ title 原串 / 点表头翻转 asc / 移动端 meta / ?view=current 不受影响）。
# 产物全部落在临时目录，合成数据由 scripts/make_analysis_long_fixture.py 生成，**不碰真实库**。
#
# 用法：
#   bash scripts/verify_records_updated_at.sh                     # 全流程（含构建）
#   bash scripts/verify_records_updated_at.sh --no-build          # 复用已有 .next（须同 API 地址构建）
#   bash scripts/verify_records_updated_at.sh --shots DIR         # 截图落到 DIR（默认随临时目录删除）
#   bash scripts/verify_records_updated_at.sh --clean             # 构建前删 .next（参照 verify_analysis_ui.sh）
#   bash scripts/verify_records_updated_at.sh --keep              # 保留临时目录便于排查
#
# 端口：BE_PORT=8011 / FE_PORT=3110（可用环境变量覆盖，占用则直接报错退出）。
# 退出码：0 全通过；1 断言失败；2 环境/启动/构建失败。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DO_BUILD=1
DO_CLEAN=0
KEEP=0
SHOTS=""
BE_PORT="${BE_PORT:-8011}"
FE_PORT="${FE_PORT:-3110}"
# fixture 账期数/项数：5 日 × 8 项 = 40 行，保证 page_size=10 至少 4 页，30 行平局组全部落在头部。
DAYS=5
ITEMS=8
FIXTURE_END="2026-02-20"
# 平局组：25 行同一较旧时刻 + 5 行同一较新时刻（均带微秒，见下方注释）。
TIE_COUNT=30
TIE_OLD_RAW="2026-03-01 10:00:00.654321"
TIE_NEW_RAW="2026-03-02 10:00:00.123456"
# 探针侧比对用的后端原串（DATETIME → isoformat()：T 分隔、6 位小数秒）。
TIE_OLD_ISO="2026-03-01T10:00:00.654321"
TIE_NEW_ISO="2026-03-02T10:00:00.123456"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) DO_BUILD=0; shift ;;
    --clean) DO_CLEAN=1; shift ;;
    --keep) KEEP=1; shift ;;
    --shots) SHOTS="$2"; shift 2 ;;
    -h|--help) awk 'NR > 1 && /^set -uo pipefail/{exit} NR > 1' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3<&- 2>/dev/null; return 0; }; return 1; }
for p in "$BE_PORT" "$FE_PORT"; do
  if port_busy "$p"; then
    echo "失败：端口 $p 已被占用（用 BE_PORT=… / FE_PORT=… 换端口）" >&2
    exit 2
  fi
done

TMP="$(mktemp -d "${TMPDIR:-/tmp}/pf_verify_records.XXXXXX")"
BE_PID=""
FE_PID=""

# 进程树收尾：先收子进程再收主进程，避免 next 的 fork 残留占端口。
stop_pid() {
  local pid="$1"
  [ -z "$pid" ] && return 0
  pkill -TERM -P "$pid" 2>/dev/null
  kill "$pid" 2>/dev/null
  sleep 1
  pkill -KILL -P "$pid" 2>/dev/null
  kill -9 "$pid" 2>/dev/null
  return 0
}

cleanup() {
  stop_pid "$FE_PID"
  stop_pid "$BE_PID"
  if [ "$KEEP" = "1" ]; then
    echo "临时目录已保留：$TMP"
  else
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  echo "失败：找不到仓库根 .venv 里的 python（$ROOT/.venv/bin/python）" >&2
  exit 2
fi
RUNNER="$(command -v bun || true)"
[ -z "$RUNNER" ] && RUNNER="$(command -v node || true)"
if [ -z "$RUNNER" ]; then
  echo "失败：找不到 bun 或 node，无法构建/跑探针" >&2
  exit 2
fi

echo "== 1/5 生成合成数据（$DAYS 期 × $ITEMS 项，止于 $FIXTURE_END）+ 制造 $TIE_COUNT 行平局"
"$PY" "$ROOT/scripts/make_analysis_long_fixture.py" \
  --src "$ROOT/data/person_fin_dev.db" --dst "$TMP/fixture.db" \
  --days "$DAYS" --items "$ITEMS" --end "$FIXTURE_END" > "$TMP/fixture.log" 2>&1 || {
  echo "失败：合成数据生成失败，见 $TMP/fixture.log" >&2
  cat "$TMP/fixture.log" >&2
  KEEP=1
  exit 2
}

# 平局场景：该表 created_at/updated_at 无 DDL 默认值，必须一起写。
# 时间串带 6 位小数秒是刻意的：后端 format_date() 走 datetime.isoformat()，
# microsecond=0 时会省略小数部分（`…T10:00:00`），spec §3 的「title 含 6 位小数秒」就无从比对。
"$PY" - "$TMP/fixture.db" "$TIE_COUNT" "$TIE_OLD_RAW" "$TIE_NEW_RAW" <<'PYEOF' || { KEEP=1; exit 2; }
import sqlite3, sys

db, tie_count, old_raw, new_raw = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
new_count = 5
old_count = tie_count - new_count

conn = sqlite3.connect(db)
with conn:
    # 末尾 tie_count 行里，最后 new_count 行取较新时刻，其余取较旧时刻 → 平局组落在 DESC 头部。
    conn.execute(
        """
        update assets
           set updated_at = ?, created_at = ?, asset_name = '示例-平局-新-' || id
         where id in (select id from assets order by id desc limit ?)
        """,
        (new_raw, new_raw, new_count),
    )
    conn.execute(
        """
        update assets
           set updated_at = ?, created_at = ?, asset_name = '示例-平局-老-' || id
         where id in (select id from assets order by id desc limit ? offset ?)
        """,
        (old_raw, old_raw, old_count, new_count),
    )

rows = conn.execute("select count(*) from assets").fetchone()[0]
grp = conn.execute(
    "select updated_at, count(*) from assets where asset_name like '示例-平局-%' group by updated_at order by updated_at desc"
).fetchall()
print(f"fixture 行数: {rows}")
for ts, n in grp:
    print(f"  同刻组 {ts} × {n}")
conn.close()
if len(grp) != 2 or sum(n for _, n in grp) != tie_count:
    print("错误：平局组分组不符（期望 2 组 / 合计 tie_count 行）", file=sys.stderr)
    raise SystemExit(2)
PYEOF

echo "== 2/5 启动后端副本（:$BE_PORT）"
(
  cd "$ROOT/backend" || exit 2
  DATABASE_PATH="$TMP/fixture.db" exec "$PY" -m uvicorn main:app \
    --host 127.0.0.1 --port "$BE_PORT" > "$TMP/backend.log" 2>&1
) &
BE_PID=$!

for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:$BE_PORT/api/assets?page=1&page_size=1"; then
    break
  fi
  sleep 2
  if [ "$i" = "60" ]; then
    echo "失败：后端未就绪，见 $TMP/backend.log" >&2
    KEEP=1
    exit 2
  fi
done

FE="$ROOT/frontend"
API="http://127.0.0.1:$BE_PORT"

if [ "$DO_BUILD" = "1" ]; then
  echo "== 3/5 构建前端（NEXT_PUBLIC_API_URL=$API）"
  # 注意：构建会覆盖 frontend/.next（与 scripts/verify_analysis_ui.sh 同一口径）。
  [ "$DO_CLEAN" = "1" ] && rm -rf "$FE/.next"
  ( cd "$FE" && NEXT_TELEMETRY_DISABLED=1 NEXT_PUBLIC_API_URL="$API" \
      "$RUNNER" node_modules/next/dist/bin/next build > "$TMP/build.log" 2>&1 ) || {
    echo "失败：前端构建失败，见 $TMP/build.log" >&2
    tail -30 "$TMP/build.log" >&2
    KEEP=1
    exit 2
  }
else
  echo "== 3/5 跳过构建（--no-build，复用 $FE/.next）"
fi

echo "== 4/5 启动前端（:$FE_PORT）"
(
  cd "$FE" || exit 2
  NEXT_TELEMETRY_DISABLED=1 exec "$RUNNER" node_modules/next/dist/bin/next start -p "$FE_PORT" \
    > "$TMP/frontend.log" 2>&1
) &
FE_PID=$!
for i in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$FE_PORT/records?view=ledger")"
  [ "$code" = "200" ] && break
  sleep 2
  if [ "$i" = "45" ]; then
    echo "失败：前端未就绪（HTTP $code），见 $TMP/frontend.log" >&2
    KEEP=1
    exit 2
  fi
done

if [ -n "$SHOTS" ]; then
  mkdir -p "$SHOTS"
  SHOT_DIR="$SHOTS"
else
  SHOT_DIR="$TMP/shots"
  mkdir -p "$SHOT_DIR"
fi

echo "== 5/5 浏览器断言（A1–A8；截图目录 $SHOT_DIR）"
( cd "$FE" && BASE="http://127.0.0.1:$FE_PORT" SHOT_DIR="$SHOT_DIR" \
    TIE_COUNT="$TIE_COUNT" TIE_NEW="$TIE_NEW_ISO" TIE_OLD="$TIE_OLD_ISO" \
    "$RUNNER" "$ROOT/scripts/records_sort_probe.js" > "$TMP/probe.log" 2>&1 )
RC=$?
grep -o 'PROBE_RESULT .*' "$TMP/probe.log" | head -1 | cut -c14- > "$TMP/result.json"

if [ -s "$TMP/result.json" ]; then
  "$PY" - "$TMP/result.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
fx = d.get("fixture", {})
print(f"\n通过 {d['passed']} / 失败 {d['failed']}"
      f"（合成数据 平局 {fx.get('tieCount')} 行：{fx.get('tieNew')} × 5 / {fx.get('tieOld')} × 25）")
for c in d["checks"]:
    mark = "✔" if c["pass"] else "✘"
    line = f"  {mark} [{c.get('id', '')}] {c['name']}"
    if not c["pass"]:
        line += f"\n       期望: {c.get('expected')}\n       实际: {c.get('actual')}"
    print(line)
m = d.get("metrics", {})
a5 = m.get("a5")
if a5:
    sb = a5.get("scrollBox") or {}
    print(f"  · A5 1536×900：「更新时间」表头右缘 {a5.get('thRight')}px / 首行单元格右缘 {a5.get('tdRight')}px"
          f"（视口宽 1536）｜表格滚动容器 scrollWidth={sb.get('scrollWidth')} clientWidth={sb.get('clientWidth')}"
          f" → 横向滚动 {sb.get('scrollWidth', 0) - sb.get('clientWidth', 0)}px（{'有' if sb.get('hasHScroll') else '无'}，业主已裁决接受）")
if m.get("sortAsc"):
    s = m["sortAsc"]
    print(f"  · A4 翻转后请求：sort_field={s.get('sortField')} sort_order={s.get('sortOrder')}｜aria-sort={s.get('ariaSort')}")
a8 = (m.get("a8") or {}).get("pages")
if a8:
    for p in a8:
        ats = p.get("updatedAts") or []
        print(f"  · A8 第 {p.get('page')} 页（page_size={p.get('pageSize')}，{p.get('count')} 条）："
              f"id {p.get('ids', [])[0]}→{p.get('ids', [])[-1]}｜updated_at {ats[0] if ats else None} → {ats[-1] if ats else None}")
a6 = m.get("a6")
if a6:
    card = a6.get("card") or {}
    print(f"  · A6 390×844：卡片 {a6.get('cardCount')} 张｜meta 标签 {card.get('labels')}"
          f"｜更新时间值 {card.get('value')}｜卡片 {card.get('scrollWidth')}/{card.get('clientWidth')}")
a7 = m.get("a7") or {}
if a7:
    mdl = a7.get("currentModelRequests") or []
    led = a7.get("ledgerRequests") or []
    print(f"  · A7 current 视图读模型 /api/assets/current {len(mdl)} 条"
          f"{'：' + json.dumps([r.get('query') for r in mdl], ensure_ascii=False) if mdl else '（无）'}")
    print(f"  · A7 current 视图页面级 /api/assets 请求 {len(led)} 条"
          f"{'：' + json.dumps(led, ensure_ascii=False) if led else '（无）'}")
for s in d.get("notes", []):
    print(f"  · {s}")
for s in d.get("screenshots", []):
    print(f"  · 截图 {s}")
PYEOF
else
  echo "失败：探针无输出，见 $TMP/probe.log" >&2
  tail -20 "$TMP/probe.log" 2>/dev/null
  KEEP=1
  exit 2
fi

if [ -n "$SHOTS" ]; then
  echo "截图目录：$SHOTS"
else
  echo "截图目录：$SHOT_DIR（随临时目录删除；用 --shots DIR 保留）"
fi

[ "$RC" = "0" ] && exit 0
echo "探针日志：$TMP/probe.log"
KEEP=1
exit 1