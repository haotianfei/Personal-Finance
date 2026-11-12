"""Dump OCR blocks -> rows with (y, x, text, score) for problem images.

用途：对给定截图确认几何结构根因（行分组 / x 分布 / 块文本）。
用法：
    python scripts/screenshot_dump_blocks.py <截图路径> [<截图路径>...]
    （本地截图语料不入库，路径自行指定）
输出 backend/block_dump.txt（每行簇一组，块按 (y,x) 排序）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services import screenshot_service  # noqa: E402

# 仓库根 = 脚本所在目录(backend/scripts) 退两级
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

OUT = os.path.join(os.path.dirname(__file__), "..", "block_dump.txt")


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    lines: list[str] = []
    for arg in paths:
        label = os.path.splitext(os.path.basename(arg))[0]
        path = arg if os.path.isabs(arg) else os.path.join(REPO_ROOT, arg)
        if not os.path.isfile(path):
            lines.append(f"\n\n===== {label}: 图片不存在 {path} =====")
            continue
        t0 = time.time()
        lines.append(f"\n\n===== {label} ({os.path.basename(path)}) =====")
        blocks = screenshot_service._ocr_blocks(path)
        rows = screenshot_service._rows_from_blocks(blocks)
        lines.append(f"n_blocks={len(blocks)} n_rows={len(rows)} 耗时 {time.time()-t0:.0f}s")
        for i, row in enumerate(rows):
            ys = [b[0] for b in row]
            xs = [b[1] for b in row]
            gap_prev = "" if i == 0 else f" gap_from_prev={row[0][0]-rows[i-1][-1][0]}"
            joined = " | ".join(b[2] for b in sorted(row, key=lambda b: b[1]))
            lines.append(f"  row[{i}] y={ys[0]}-{ys[-1]} x0={min(xs)}{gap_prev}")
            lines.append(f"      {joined}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"完成，写入 {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
