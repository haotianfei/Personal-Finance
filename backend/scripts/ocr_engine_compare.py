"""OCR 双路线行为等价取证脚本（一次性对比，非单测）。

用法：
    cd backend && ../.venv/bin/python -u scripts/ocr_engine_compare.py [图片...]
缺省从 data/evidence/ 递归取最新的 2 张真实截图。

对同一组图先跑 OCR_ENGINE=pipeline（旧路线基线），再跑 lowlevel（新路线），输出：
  - 块条数差 + 贪心最近邻配对（|Δy|+|Δx|≤10 视为同一块）：
    共同块数、仅单侧出现的块（列子样例）、配对中文本不一致数、坐标差>5px 数
  - 两条路线耗时 + lowlevel 内 det/rec 实测占比（计时包装器包住引擎实例）
并校验 lowlevel 进度事件协议（chunk 递增、line done≤total）。
原始 blocks 会 dump 到 /tmp/opencode/ocr_compare_<模式>_<idx>.json 供事后取证。
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ocr_engine  # noqa: E402


class TimingDet:
    """包一层累计 det.predict 耗时。"""

    def __init__(self, det):
        self._det = det
        self.t = 0.0

    def predict(self, img, **kw):
        t0 = time.time()
        r = self._det.predict(img, **kw)
        self.t += time.time() - t0
        return r


class TimingRec:
    """包一层累计 rec.predict_iter 全程消费耗时。"""

    def __init__(self, rec):
        self._rec = rec
        self.t = 0.0

    def predict_iter(self, crops, batch_size=None):
        t0 = time.time()
        try:
            for r in self._rec.predict_iter(crops, batch_size=batch_size):
                yield r
        finally:
            self.t += time.time() - t0


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_images(n=2):
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        files += glob.glob(os.path.join(_ROOT, "data", "evidence", "**", ext),
                           recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    return files[:n]


def run_route(img, mode, events=None):
    os.environ["OCR_ENGINE"] = mode
    t0 = time.time()
    cb = events.append if events is not None else None
    blocks = ocr_engine.run_ocr(img, progress_cb=cb)
    return blocks, time.time() - t0


def match_blocks(pb, lb, tol=10):
    """贪心最近邻配对（曼哈顿距离 ≤tol）→ (pairs, pipeline独有, lowlevel独有)。"""
    lb = sorted(lb)
    used = [False] * len(lb)
    pairs, p_only = [], []
    for b in sorted(pb):
        best, bd = -1, 10 ** 9
        for j, c in enumerate(lb):
            if used[j]:
                continue
            d = abs(b[0] - c[0]) + abs(b[1] - c[1])
            if d < bd:
                bd, best = d, j
        if best >= 0 and bd <= tol:
            used[best] = True
            pairs.append((b, lb[best], bd))
        else:
            p_only.append(b)
    l_only = [c for j, c in enumerate(lb) if not used[j]]
    return pairs, p_only, l_only


def compare(pb, lb):
    pairs, p_only, l_only = match_blocks(pb, lb)
    text_diff = [(a, b) for a, b, _ in pairs if a[2] != b[2]]
    coord_far = [(a, b, d) for a, b, d in pairs if d > 5]
    print(f"  配对: 共同={len(pairs)}  仅pipeline={len(p_only)}  仅lowlevel={len(l_only)}")
    print(f"  配对中文本不一致: {len(text_diff)}  坐标差>5px: {len(coord_far)}")
    for a, b in text_diff[:5]:
        print(f"    文本差: p={a[2]!r}@({a[0]},{a[1]})  l={b[2]!r}@({b[0]},{b[1]})")
    for b in p_only[:5]:
        print(f"    仅pipeline:   {b[2]!r}@({b[0]},{b[1]})")
    for b in l_only[:5]:
        print(f"    仅lowlevel:   {b[2]!r}@({b[0]},{b[1]})")


def check_events(events):
    chunks = [e for e in events if e[0] == "chunk"]
    lines = [e for e in events if e[0] == "line"]
    ok_chunk = [c[1] for c in chunks] == list(range(len(chunks))) \
        and all(c[1] < c[2] for c in chunks)
    ok_line = all(e[1] <= e[2] for e in lines)
    return ok_chunk, ok_line, len(chunks), len(lines)


def main():
    imgs = sys.argv[1:] or default_images()
    if not imgs:
        print("data/evidence 下未找到图片")
        return 1
    print(f"对比 {len(imgs)} 张图（pipeline=旧路线基线 vs lowlevel=新路线）\n", flush=True)
    rc = 0
    for idx, img in enumerate(imgs):
        print(f"=== {img} ({os.path.getsize(img)//1024} KB) ===")
        pb, tp = run_route(img, "pipeline")
        os.environ["OCR_ENGINE"] = "lowlevel"
        det, rec = ocr_engine.get_lowlevel_engines()
        tdet, trec = TimingDet(det), TimingRec(rec)
        ocr_engine._det_engine, ocr_engine._rec_engine = tdet, trec
        events = []
        lb, tl = run_route(img, "lowlevel", events)
        for mode, blocks in (("pipeline", pb), ("lowlevel", lb)):
            with open(f"/tmp/opencode/ocr_compare_{mode}_{idx}.json", "w",
                      encoding="utf-8") as f:
                json.dump([list(b) for b in blocks], f, ensure_ascii=False)
        det_t, rec_t = tdet.t, trec.t
        ok_chunk, ok_line, nc, nl = check_events(events)
        share = (det_t / tl * 100) if tl else 0
        print(f"  条数:      pipeline={len(pb)}  lowlevel={len(lb)}  (差 {len(lb)-len(pb)})")
        compare(pb, lb)
        print(f"  耗时:      pipeline={tp:.1f}s  lowlevel={tl:.1f}s"
              f"   [lowlevel 内 det={det_t:.1f}s ({share:.0f}%) rec={rec_t:.1f}s"
              f" ({100-share:.0f}%)]")
        print(f"  进度事件:  chunk×{nc} 递增={ok_chunk}  line×{nl} done≤total={ok_line}")
        if not (ok_chunk and ok_line) or nc == 0:
            rc = 1
        print(flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
