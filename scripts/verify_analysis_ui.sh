#!/usr/bin/env bash
# 分析页 UI 验收：造「日粒度 N 期」合成数据 → 起后端副本 → 构建前端 → 浏览器断言。
#
# 由来：`analysis-detail-prune`（数据明细长表退场）+ `.scratch/chart-table-window-perf/spec.md`
# §3 A1–A8（数据表跟随窗口、折叠表惰性挂载、caption 随窗口、390px 无溢出）。
# 产物全部落在临时目录，不碰真实库；合成数据由 scripts/make_analysis_long_fixture.py 生成。
#
# 用法：
#   bash scripts/verify_analysis_ui.sh              # 默认 730 期 × 8 项
#   bash scripts/verify_analysis_ui.sh --days 90    # 短账期冒烟
#   bash scripts/verify_analysis_ui.sh --no-build   # 复用已有 .next（须同端口构建）
#   bash scripts/verify_analysis_ui.sh --keep       # 保留临时目录便于排查
#
# 退出码：0 全通过；1 断言失败；2 环境/启动失败。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAYS=730
ITEMS=8
DO_BUILD=1
KEEP=0
BE_PORT="${BE_PORT:-18010}"
FE_PORT="${FE_PORT:-13012}"

while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --items) ITEMS="$2"; shift 2 ;;
    --no-build) DO_BUILD=0; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

TMP="$(mktemp -d "${TMPDIR:-/tmp}/pf_verify_ui.XXXXXX")"
BE_PID=""
FE_PID=""
cleanup() {
  [ -n "$BE_PID" ] && kill "$BE_PID" 2>/dev/null
  [ -n "$FE_PID" ] && kill "$FE_PID" 2>/dev/null
  sleep 1
  if [ "$KEEP" = "1" ]; then
    echo "临时目录已保留：$TMP"
  else
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

PY="$ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
BUN="$(command -v bun || true)"
RUNNER="$BUN"
if [ -z "$RUNNER" ]; then
  RUNNER="$(command -v node || true)"
fi
if [ -z "$RUNNER" ]; then
  echo "失败：找不到 bun 或 node，无法构建/跑探针" >&2
  exit 2
fi

echo "== 1/5 生成合成数据（$DAYS 期 × $ITEMS 项）"
"$PY" "$ROOT/scripts/make_analysis_long_fixture.py" \
  --src "$ROOT/data/person_fin_dev.db" --dst "$TMP/fixture.db" \
  --days "$DAYS" --items "$ITEMS" || exit 2

echo "== 2/5 启动后端副本（:$BE_PORT）"
(
  cd "$ROOT/backend" || exit 2
  DATABASE_PATH="$TMP/fixture.db" exec "$PY" -m uvicorn main:app \
    --host 127.0.0.1 --port "$BE_PORT" > "$TMP/backend.log" 2>&1
) &
BE_PID=$!

for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:$BE_PORT/api/analysis/mixed-chart?period_type=day"; then
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
  rm -rf "$FE/.next"
  ( cd "$FE" && NEXT_TELEMETRY_DISABLED=1 NEXT_PUBLIC_API_URL="$API" \
      "$RUNNER" node_modules/next/dist/bin/next build > "$TMP/build.log" 2>&1 ) || {
    echo "失败：前端构建失败，见 $TMP/build.log" >&2
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
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$FE_PORT/analysis")"
  [ "$code" = "200" ] && break
  sleep 2
  if [ "$i" = "45" ]; then
    echo "失败：前端未就绪，见 $TMP/frontend.log" >&2
    KEEP=1
    exit 2
  fi
done

echo "== 5/5 浏览器断言（$DAYS 期 × $ITEMS 项；A1–A8）"
( cd "$FE" && BASE="http://127.0.0.1:$FE_PORT" DAYS="$DAYS" ITEMS="$ITEMS" EXPECT_ROWS="$DAYS" \
    "$RUNNER" "$ROOT/scripts/analysis_ui_probe.js" > "$TMP/probe.log" 2>&1 )
RC=$?
grep -o 'PROBE_RESULT .*' "$TMP/probe.log" | head -1 | cut -c14- > "$TMP/result.json"

if [ -s "$TMP/result.json" ]; then
  "$PY" - "$TMP/result.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"\n通过 {d['passed']} / 失败 {d['failed']}"
      f"（合成数据 {d.get('fixture', {}).get('days')} 期 × {d.get('fixture', {}).get('items')} 项；日粒度实测 {d.get('dayCount')} 期）")
for c in d["checks"]:
    mark = "✔" if c["pass"] else "✘"
    line = f"  {mark} [{c.get('id', '')}] {c['name']}"
    if not c["pass"]:
        line += f"  ← 期望: {c.get('expected')} / 实际: {c.get('actual')}"
    print(line)
for s in d.get("skipped", []):
    print(f"  – [{s['id']}] 未覆盖：{s['name']}（{s['reason']}）")
if d.get("dayReadyMs") is not None:
    print(f"  · 切「日」到数据就绪: {d['dayReadyMs']} ms")
if d.get("expandMs") is not None:
    print(f"  · 点击 summary 到 {d.get('expanded', {}).get('rowCount')} 行渲染完成: {d['expandMs']} ms")
if d.get("collapsed"):
    c730, c90 = d["collapsed"]["window730"], d["collapsed"]["window90"]
    print(f"  · 折叠态 DOM 节点 730期={c730['domNodes']} / 90期={c90['domNodes']}"
          f"｜页面高 730期={c730['bodyScrollHeight']}px / 90期={c90['bodyScrollHeight']}px")
if d.get("expanded"):
    print(f"  · 展开态 DOM 节点={d['expanded']['domNodes']}｜页面高={d['expanded']['bodyScrollHeight']}px")
if d.get("scrollHeightVsBaseline"):
    b = d["scrollHeightVsBaseline"]
    print(f"  · 折叠态高度对改前基线 {b['baseline']}px：实际 {b['actual']}px（Δ={b['delta']}）")
for pt, g in (d.get("granularity") or {}).items():
    print(f"  · {pt}: 账期 {g['count']}，默认窗口近 {g['window']} 期 → 表 {g['rows']} 行"
          f"（{g['firstPeriod']} → {g['lastPeriod']}）")
PYEOF
else
  echo "失败：探针无输出，见 $TMP/probe.log" >&2
  tail -5 "$TMP/probe.log" 2>/dev/null
  KEEP=1
  exit 2
fi

[ "$RC" = "0" ] && exit 0
KEEP=1
exit 1