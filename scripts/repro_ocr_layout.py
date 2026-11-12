"""复现/验收：用指定 OCR 块缓存的 blocks 重跑某来源组的解析链路。

用途（`.scratch/multicolumn-signature` 验收用）：不重跑 OCR（省时间），直接把
`ocr_cache/<sha>.json` 里的 blocks 喂给 organize_blocks → apply_correction_rules
→ compute_preview，打印版式判定、各项名称/金额、合计与待录入清单。

用法：
    .venv/bin/python scripts/repro_ocr_layout.py --group <组slug> --cache <ocr_cache/xxx.json>
    .venv/bin/python scripts/repro_ocr_layout.py --group <组slug> --cache <...> --db deploy/data/person_fin.db

退出码：0 正常；2 缓存文件不存在或组不存在。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

import database  # noqa: E402
import models  # noqa: E402
from routers.screenshots import _parsing_rules  # noqa: E402
from services import parser_rules, screenshot_service as ss  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, help="来源组 slug，如 demo")
    ap.add_argument("--cache", required=True, help="ocr_cache 下的块缓存 json")
    ap.add_argument("--db", default=None, help="数据库路径（默认取 database 配置）")
    ap.add_argument("--no-rules", action="store_true",
                    help="忽略教学规则（只看 organize 层产物）")
    args = ap.parse_args()

    cache = Path(args.cache)
    if not cache.is_absolute():
        cache = (REPO / cache) if (REPO / cache).exists() else cache
    if not cache.exists():
        print(f"缓存不存在：{cache}", file=sys.stderr)
        return 2

    if args.db:
        import os
        os.environ["DATABASE_PATH"] = args.db
    payload = json.loads(cache.read_text(encoding="utf-8"))
    blocks = [tuple(b) for b in payload["blocks"]]

    db = database.SessionLocal()
    try:
        grp = db.query(models.SourceGroup).filter(
            models.SourceGroup.slug == args.group).first()
        if grp is None:
            print(f"来源组不存在：{args.group}", file=sys.stderr)
            return 2
        config = ss.get_group_config(db, args.group) or {}
        rules = {} if args.no_rules else _parsing_rules(db, args.group, config)
        vocabularies = parser_rules.load_vocabularies(db)

        vocab = ss._resolve_parser_vocab(vocabularies, rules)
        rows = ss._drop_noise_rows(ss._rows_from_blocks(blocks), vocab)
        layout = ss._detect_layout(rows, vocab.amount_headers, vocab)

        items = ss.organize_blocks(blocks, rules, vocabularies=vocabularies)
        print(f"[organize] layout={layout}（rows={len(rows)}） 项数={len(items)}")
        total = sum((i.amount for i in items), ss.Decimal("0"))
        for it in items:
            print(f"  - {it.name}\t{it.amount}")
        print(f"[organize] 合计={total}")

        if not args.no_rules:
            kept = ss.apply_correction_rules(items, rules)
            print(f"[corrected] 项数={len(kept)}")
            for it in kept:
                print(f"  - {it.name}\t{it.amount}")

            existing = [
                (a.asset_name, a.amount)
                for a in db.query(models.Asset).filter(
                    models.Asset.asset_name.like(f"{grp.asset_prefix}%")).all()
            ]
            preview = ss.compute_preview(
                kept, existing, grp.asset_prefix,
                unrecognized="keep", exclude=(rules or {}).get("exclude"))
            print(f"[preview] total_screenshot={preview.total_screenshot} "
                  f"total_current={preview.total_current_system} 行数={len(preview.rows)}")
            for r in preview.rows:
                print(f"  * {r.action:<6} {r.asset_name}\t{r.source_amount} (diff {r.diff})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())