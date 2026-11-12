"""存量凭证 `image_meta` 回填（ADR-0022 D2 / spec §2 D5 / 票 04）。

对 `image_meta IS NULL` 且文件存在的行，读 `file_path` 指向的原文件、按票 02 的
白名单规则写回 `image_meta`。默认 **dry-run**，`--apply` 才写库。

    python scripts/backfill_evidence_image_meta.py                 # 打印将回填的行
    python scripts/backfill_evidence_image_meta.py --apply         # 真写库
    python scripts/backfill_evidence_image_meta.py --db X --evidence-root Y [--apply]

口径：

- 白名单**只有一份**：`screenshot_service.build_image_meta()`（width/height/bytes/
  format + EXIF 五键，**不含 GPS**）。本脚本不自带第二份 EXIF 规则；
- 只碰 `image_meta IS NULL` 的行：已有值一字不改（一 sha 一行一文件，元数据只写一次）；
- 坏图 / 打不开 → 跳过并计数，**仍留 NULL**（与上传链「读不出 → NULL」同语义，
  下次重跑还能再试），不伪造、不写空对象；
- 文件缺失 → 跳过并计数（先跑 `rename_evidence_files.py --apply` 也救不回丢的文件）；
- 与改名脚本**顺序无关**（spec D5）：两边都按 `file_path` 找文件，谁先跑都行。

退出码：0＝跑完（含跳过的坏图/缺失）；2＝参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                                # noqa: E402
from sqlalchemy.orm import Session                            # noqa: E402

import database                                              # noqa: E402
from models import ScreenshotEvidence                        # noqa: E402
from services import screenshot_service as ss                # noqa: E402
from services.evidence_service import default_evidence_root   # noqa: E402

# 共用管道（会话/路径/遍历）与改名脚本同源，避免第三份 `--db` 处理逻辑。
from scripts.rename_evidence_files import (                   # noqa: E402
    evidence_path, normalize_rel, open_session,
)


@dataclass
class BackfillResult:
    would_fill: list[tuple[int, str, dict]] = field(default_factory=list)  # 已回填/将回填
    bad_image: list[tuple[int, str]] = field(default_factory=list)
    missing: list[tuple[int, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.would_fill) + len(self.bad_image) + len(self.missing)


def backfill(db: Session, evidence_root: str, *, apply: bool) -> BackfillResult:
    """回填 `image_meta IS NULL` 的行（`apply=False` 只打印，不写库）。"""
    rows = db.execute(
        select(ScreenshotEvidence)
        .where(ScreenshotEvidence.image_meta.is_(None))
        .order_by(ScreenshotEvidence.id)
    ).scalars().all()

    result = BackfillResult()
    for ev in rows:
        rel = normalize_rel(ev.file_path)
        path = evidence_path(evidence_root, rel)
        if not rel or not os.path.isfile(path):
            result.missing.append((ev.id, rel or "（file_path 为空）"))
            continue
        meta = ss.build_image_meta(path)          # 白名单唯一实现；读不出 → None
        if meta is None:
            result.bad_image.append((ev.id, rel))
            continue
        if apply:
            ev.image_meta = json.dumps(meta, ensure_ascii=False)   # 与上传链同序列化口径
            db.commit()
        result.would_fill.append((ev.id, rel, meta))
    return result


def _summary(meta: dict) -> str:
    exif = meta.get("exif") or {}
    exif_txt = f"exif {len(exif)} 键" if exif else "无 exif"
    return f"{meta.get('width')}x{meta.get('height')} {meta.get('format')} {meta.get('bytes')}B {exif_txt}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="存量凭证 image_meta 回填（默认 dry-run，不写库）")
    ap.add_argument("--db", default=database.DB_PATH,
                    help=f"数据库路径（默认 {database.DB_PATH}；演练传副本库）")
    ap.add_argument("--evidence-root", default="",
                    help="证据根目录（默认项目 data/evidence）")
    ap.add_argument("--apply", action="store_true", help="确认写库（缺省只打印）")
    args = ap.parse_args(argv)

    evidence_root = args.evidence_root or default_evidence_root()
    if not os.path.isfile(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    if not os.path.isdir(evidence_root):
        print(f"证据目录不存在：{evidence_root}", file=sys.stderr)
        return 2

    db, engine = open_session(args.db)
    try:
        print(f"模式：{'--apply' if args.apply else 'dry-run（只读，不写库）'}")
        print(f"数据库：{args.db}")
        print(f"证据根目录：{evidence_root}")
        print("（提示：上方 database_path 日志是 import 期算出的默认路径，实际以本行 --db 为准）")

        result = backfill(db, evidence_root, apply=args.apply)
        verb = "已回填" if args.apply else "将回填"
        print(f"\n待处理（image_meta IS NULL）：{result.total} 条")
        print(f"\n{verb} {len(result.would_fill)} 条：")
        for ev_id, rel, meta in result.would_fill:
            print(f"  #{ev_id:<4} {rel}\n         → {_summary(meta)}")
        if not result.would_fill:
            print("  （无）")

        print(f"\n坏图跳过 {len(result.bad_image)} 条（保持 NULL，可重跑重试）：")
        for ev_id, rel in result.bad_image:
            print(f"  #{ev_id:<4} {rel}")
        if not result.bad_image:
            print("  （无）")

        print(f"\n文件缺失跳过 {len(result.missing)} 条（保持 NULL）：")
        for ev_id, rel in result.missing:
            print(f"  #{ev_id:<4} {rel}")
        if not result.missing:
            print("  （无）")
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())