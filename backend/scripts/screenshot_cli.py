"""截图录入 CLI（T5）：批量处理截图，复用 T2/T3 后端逻辑，不重复实现。

用法：
    python scripts/screenshot_cli.py 截图.png [更多截图/目录...] [选项]

选项：
    --apply          确认写入（默认只出待录入清单，不写库）
    --group NAME     来源组（默认 bantuou）
    --skip 0,2       跳过清单中这些行号（与 --apply 搭配）
    --amount 1=100   把第 2 行金额改为 100（与 --apply 搭配）
    --evidence-root  凭证根目录（默认 data/evidence）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date as _date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal                 # noqa: E402
from services import screenshot_service           # noqa: E402


def _shorter(name: str, width: int = 26) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def _human_decimal(v, width=12) -> str:
    return f"{Decimal(v):>{width},.2f}" if v is not None else f"{'-':>{width}}"


def process_one(path: str, args, db) -> None:
    print(f"\n===== {os.path.basename(path)} =====")
    t0 = time.time()
    parsed = screenshot_service.parse_screenshot(path, cache_key=None)  # CLI 无跨进程缓存
    print(f"解析 {len(parsed.items)} 项 | 数据日期 {parsed.date or f'{_date.today().isoformat()}(今天)'}"
          f" | 投顾总金额(锚点) {_human_decimal(parsed.total)} | 耗时 {time.time() - t0:.0f}s")
    fmt = "  {:>3} {:<10} {:>26}  {:>26}"
    suffix = "  " + "-" * 64
    print("  " + "-" * 64)
    print(fmt.format("#", "动作", "来源名 / 系统名", "截图金额 / 现值 / 差异"))
    print(suffix if isinstance(suffix, str) else "")
    prefix = screenshot_service.SOURCE_GROUPS[args.group]["asset_prefix"]
    existing = screenshot_service.get_group_existing(db, prefix)
    preview = screenshot_service.compute_preview(parsed.items, existing, prefix)
    for i, r in enumerate(preview.rows):
        lhs = (r.source_name or r.asset_name or "") if r.action != "create" else f"(新增) {r.source_name}"
        rhs = f"{_human_decimal(r.source_amount)} / {_human_decimal(r.current_amount)} / {_human_decimal(r.diff)}"
        print(fmt.format(i, r.action, _shorter(lhs), rhs))
    print("  " + "-" * 64)
    sys_total = preview.total_current_system
    shot_total = sum((it.amount for it in parsed.items), Decimal("0"))
    print(f"  截图合计 {_human_decimal(shot_total)} | 系统合计 {_human_decimal(sys_total)}"
          f" | 差异 {_human_decimal(shot_total - (sys_total or Decimal('0')))}")
    actions = {}
    for r in preview.rows:
        actions[r.action] = actions.get(r.action, 0) + 1
    print(f"  动作分布：{ ', '.join(f'{k}={v}' for k, v in actions.items()) or '空清单'}")

    if args.apply:
        if not preview.rows:
            print("  空清单，没有可写入的行。")
            return
        try:
            edited = screenshot_service.apply_user_edits(preview, args.skip, args.amount)
        except ValueError as e:
            print(f"  编辑参数错误：{e}")
            return
        dropped = len(preview.rows) - len(edited.rows)
        if dropped:
            print(f"  已跳过 {dropped} 行")
        evidence_root = args.evidence_root or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "evidence")
        data_date = _date.fromisoformat(parsed.date) if parsed.date else _date.today()
        result = screenshot_service.apply_preview(
            db, edited, source_group=args.group,
            data_date=data_date,
            image_path=path,
            evidence_root=evidence_root,
        )
        print(f"  已写入 {result.rows_written} 条记录（{result.asset_date}），凭证 #{result.evidence_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="截图录入 CLI（帮你投等来源，默认只出清单）")
    ap.add_argument("paths", nargs="+", help="截图文件或目录")
    ap.add_argument("--apply", action="store_true", help="确认写入（默认不写库）")
    ap.add_argument("--group", default="bantuou", help="来源组标识（默认 bantuou）")
    ap.add_argument("--skip", default="", help="跳过行号，逗号分隔，如 0,2")
    ap.add_argument("--amount", default="", help="行号=金额，逗号分隔，如 1=100")
    ap.add_argument("--evidence-root", default="", help="凭证根目录（默认项目 data/evidence）")
    ap.add_argument("--recursive", action="store_true", help="目录递归收集截图")
    args = ap.parse_args()

    if args.group not in screenshot_service.SOURCE_GROUPS:
        print(f"未知来源组：{args.group}（可选：{list(screenshot_service.SOURCE_GROUPS)}）")
        sys.exit(2)

    args.skip = [int(x) for x in args.skip.split(",") if x.strip() != ""]
    raw_amount = args.amount
    args.amount = {}
    if raw_amount.strip():
        for pair in raw_amount.split(","):
            k, v = pair.split("=", 1)
            args.amount[int(k)] = v

    images: list[str] = []
    DIRECTORY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    for p in args.paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in DIRECTORY_EXTS:
                        images.append(os.path.join(root, f))
                if not args.recursive:
                    break
        else:
            images.append(p)
    if not images:
        print("未找到任何图片。")
        sys.exit(1)

    db = SessionLocal()
    try:
        for img in images:
            process_one(img, args, db)
    finally:
        db.close()
    print("\n完成。")


if __name__ == "__main__":
    main()