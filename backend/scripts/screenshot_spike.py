"""截图解析 spike：隔离验证通用解析器 organize_blocks 对真实截图的准确率。

不经过 compute_preview 映射 / apply_preview 写库——只测「_ocr_blocks → organize_blocks」
这条解析链路，输出每张图的原始 [{name, amount}] 清单，供与真实截图人工对照判定准确率。

用法：
    python scripts/screenshot_spike.py [截图...|目录] [-o spike_result.json]

输出：每张图跑完后即实时写入结果文件（含 date/total/items/raw row 摘要），
OCR 4~5 分钟/张，长跑中断不丢已完成的图。
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import screenshot_service  # noqa: E402

DIRECTORY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def collect_images(paths: list[str], recursive: bool = False) -> list[str]:
    images: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in DIRECTORY_EXTS:
                        images.append(os.path.join(root, f))
                if not recursive:
                    break
        else:
            images.append(p)
    return images


def human_decimal(v) -> str:
    if v is None:
        return "-"
    return f"{Decimal(v):,.2f}"


def process_one(path: str) -> dict:
    print(f"\n===== {os.path.basename(path)} =====")
    t0 = time.time()
    blocks = screenshot_service._ocr_blocks(path)
    items = screenshot_service.organize_blocks(blocks)
    rows = screenshot_service._rows_from_blocks(blocks)
    date = None
    for r in rows:
        d = screenshot_service._extract_header_date(screenshot_service._row_join(r))
        if d:
            date = d
            break
    # 对账锚点取汇总行金额的旧逻辑已随字段删除（0030/ADR-0021）
    elapsed = time.time() - t0
    print(f"解析 {len(items)} 项 | 数据日期 {date or '(未识别)'}"
          f" | 耗时 {elapsed:.0f}s")
    for i, it in enumerate(items):
        print(f"  [{i}] {it.name:<28} {human_decimal(it.amount)}")
    return {
        "file": os.path.basename(path),
        "path": path,
        "date": date,
        "n_blocks": len(blocks),
        "items": [{"name": it.name, "amount": str(it.amount)} for it in items],
        "elapsed_seconds": round(elapsed, 1),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="截图解析 spike（隔离验证 organize_blocks）")
    ap.add_argument("paths", nargs="+", help="截图文件或目录")
    ap.add_argument("--recursive", action="store_true", help="目录递归收集截图")
    ap.add_argument("-o", "--out", default="spike_result.json", help="结果输出文件")
    args = ap.parse_args()

    images = collect_images(args.paths, args.recursive)
    if not images:
        print("未找到任何图片。")
        sys.exit(1)

    results: list[dict] = []
    for img in images:
        try:
            result = process_one(img)
        except Exception as e:  # 单张失败不终止后续
            print(f"  !! 解析失败: {e}")
            result = {"file": os.path.basename(img), "path": img, "error": str(e)}
        results.append(result)
        # 实时落盘：长跑中断已完成的图不丢
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n完成，结果写入 {args.out}（共 {len(results)} 张）")


if __name__ == "__main__":
    main()
