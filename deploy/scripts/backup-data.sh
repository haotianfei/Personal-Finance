#!/usr/bin/env bash
# backup-data.sh —— 打包 deploy/data（宿主侧整目录 tar 备份）+ 保留策略
#
# 用法：
#   bash deploy/scripts/backup-data.sh                 # 备份一份 + 保留策略 dry-run 报告（＝旧版无参行为）
#   bash deploy/scripts/backup-data.sh --apply         # 备份一份 + 真按保留策略删超期 tar
#   bash deploy/scripts/backup-data.sh --skip-backup   # 只做保留策略 dry-run（不产生新 tar）
#   bash deploy/scripts/backup-data.sh -h
#
# 保留策略（写死，做出入都在这里改）：
#   * 日备份：保留 14 天；
#   * 每月 1 号那一份：额外留存 6 个月（不按 14 天算）；
#   * 年龄取**文件名里的时间戳** data-<YYYYMMDD>-<HHMMSS>.tar.gz，不看 mtime
#     （tar 复制/搬移后 mtime 会变，文件名才是备份时刻的事实）；
#   * 超期文件删除前先打印清单；默认 dry-run，`--apply` 才真删；
#   * 只认 data-<YYYYMMDD>-<HHMMSS>.tar.gz：orphan-*、*.db、*.db-wal 等一律不碰。
#
# 兼容性：无参数行为与旧版一致（打一份 tar 到 deploy/backups/data-<TS>.tar.gz），
#         只是末尾多打印一段保留策略报告（默认不删任何东西）。
#
# 相关：定时器见 deploy/scripts/install-backup-timer.sh（systemd user unit，每日 03:00）。
#       应用内备份（/api/backup）与保留 job 是另一套，与宿主 tar 并存、互不替代。
#
# 退出码：0 正常 / 1 备份或删除失败 / 2 环境缺失
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$DEPLOY_DIR/backups}"

RETAIN_DAYS=14          # 日备份保留天数
MONTHLY_MONTHS=6        # 每月 1 号额外留存的月数

APPLY=0
DO_BACKUP=1

die()   { echo "[FAIL] $*" >&2; exit 1; }
note()  { echo; echo "==> $*"; }
sub()   { echo "    $*"; }
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)        APPLY=1; shift ;;
    --dry-run)      APPLY=0; shift ;;
    --skip-backup)  DO_BACKUP=0; shift ;;
    --backups-dir)  BACKUP_DIR="${2:?--backups-dir 需要目录}"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

# 保留策略的日期算术走 python（不在这里手搓月减），优先仓库 .venv
pick_python() {
  if [[ -n "${PERSON_FIN_PYTHON:-}" ]]; then
    printf '%s' "$PERSON_FIN_PYTHON"
  elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    printf '%s' "$REPO_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' python3
  else
    return 1
  fi
}

if [[ ! -d "$DEPLOY_DIR/data" ]]; then
  echo "data/ 不存在" >&2
  exit 2
fi

mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------- 1. 备份
if [[ "$DO_BACKUP" -eq 1 ]]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  OUT="$BACKUP_DIR/data-$TS.tar.gz"
  note "打包 $DEPLOY_DIR/data → $OUT"
  tar czf "$OUT" -C "$DEPLOY_DIR" data
  echo "==> 已备份：$OUT"
  ls -lh "$OUT"
else
  note "跳过打包（--skip-backup）：只跑保留策略"
fi

# ------------------------------------------------------- 2. 保留策略（dry-run 默认）
PY="$(pick_python)" || die "找不到 python（需要 .venv/bin/python 或 python3）"

note "保留策略：日备份 $RETAIN_DAYS 天 / 每月 1 号额外 $MONTHLY_MONTHS 个月$( [[ "$APPLY" -eq 1 ]] && echo ' / 模式=apply' || echo ' / 模式=dry-run' )"

"$PY" - "$BACKUP_DIR" "$APPLY" "$RETAIN_DAYS" "$MONTHLY_MONTHS" <<'PY'
import os
import re
import sys
from datetime import date, datetime

backup_dir, apply_flag, retain_days, monthly_months = sys.argv[1:5]
apply = apply_flag == "1"
retain_days = int(retain_days)
monthly_months = int(monthly_months)

NAME_RE = re.compile(r"^data-(\d{8})-(\d{6})\.tar\.gz$")


def month_cutoff(today: date, months: int) -> date:
    """today 往前数 months 个月所在月的 1 号（月备份的保留下界）。"""
    year, month = today.year, today.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def human_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(n)
    for unit in units:
        if v < 1024 or unit == units[-1]:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


today = date.today()
daily_cut = today.fromordinal(today.toordinal() - retain_days)   # 早于该日＝超 14 天
monthly_cut = month_cutoff(today, monthly_months)

print(f"    今天 {today.isoformat()}｜日备份下界 {daily_cut.isoformat()}（>={daily_cut} 保留）"
      f"｜月备份下界 {monthly_cut.isoformat()}（每月 1 号 >={monthly_cut} 保留）")

entries = []          # (path, keep, reason, age_days, size)
skipped = []
for name in sorted(os.listdir(backup_dir)):
    path = os.path.join(backup_dir, name)
    m = NAME_RE.match(name)
    if not m:
        if os.path.isfile(path):
            skipped.append(name)
        continue
    stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").date()
    age = (today - stamp).days
    is_monthly = stamp.day == 1
    size = os.path.getsize(path)
    if is_monthly and stamp >= monthly_cut:
        entries.append((path, True, "每月 1 号·留 6 个月", age, size))
    elif stamp >= daily_cut:
        entries.append((path, True, "14 天内", age, size))
    elif is_monthly:
        entries.append((path, False, f"1 号备份已超 {monthly_months} 个月", age, size))
    else:
        entries.append((path, False, f"超 {retain_days} 天", age, size))

if not entries:
    print("    无 data-<TS>.tar.gz 备份，无需处理")
    sys.exit(0)

for path, keep, reason, age, size in entries:
    mark = "KEEP" if keep else "DEL "
    print(f"    {mark} {os.path.basename(path)}  {age:>5d} 天前  {human_bytes(size):>10}  ({reason})")

to_delete = [(p, s) for p, keep, _r, _a, s in entries if not keep]
kept = [e for e in entries if e[1]]
reclaim = sum(s for _p, s in to_delete)

print()
print(f"    合计 {len(entries)} 份：保留 {len(kept)}"
      f"（其中每月 1 号 {sum(1 for e in kept if '每月 1 号' in e[2])}）"
      f"｜待删 {len(to_delete)} 份，可释放 {human_bytes(reclaim)}")
if skipped:
    print(f"    非本脚本管理、不动：{', '.join(skipped)}")

if not to_delete:
    print("    [ok] 无需删除")
    sys.exit(0)

if not apply:
    print("    [dry-run] 以上为待删清单，未删除任何文件；加 --apply 才真删")
    sys.exit(0)

failed = 0
for path, _size in to_delete:
    try:
        os.remove(path)
        print(f"    [del] {os.path.basename(path)}")
    except OSError as exc:
        failed += 1
        print(f"    [FAIL] 删除 {path} 失败：{exc}", file=sys.stderr)

if failed:
    print(f"    [FAIL] {failed} 份删除失败", file=sys.stderr)
    sys.exit(1)
print(f"    [ok] 已删除 {len(to_delete)} 份，释放 {human_bytes(reclaim)}")
PY

echo
echo "==> 完成（备份目录：$BACKUP_DIR）"