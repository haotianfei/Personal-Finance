"""OCR 引擎层：双路线（lowlevel 逐行进度 / pipeline 兜底）+ 全局串行锁。

选择：环境变量 OCR_ENGINE = "lowlevel"（默认）| "pipeline"（旧 PaddleOCR
pipeline 路线，行为与历史 _ocr_blocks 完全一致，作 lowlevel 出问题时兜底）。

_Engine_LOCK：进程内所有 OCR 推理串行化。PaddleOCR/PaddleX 推理实例无线程
安全，旧代码经 asyncio.to_thread 并发预览存在竞态；本锁顺手根治（预览任务
worker 本身也是单线程串行，双保险）。

进度回调（progress_cb，事件 tuple，调用方翻译成 job 字段）：
    ('cache',)                    命中解析缓存，OCR 未执行（parse_screenshot 发）
    ('chunk', i, n)               第 i 片（0 基）det 完成，共 n 片
    ('line', done, total, text, score)  rec 逐批产出，本片已完成 done/total 行；
                                  附该行的识别文本与置信度（供实时行预览）

lowlevel 路线（lib-2 取证结论，paddleocr 3.7.0 / paddlex 3.7.2）：
    竖切片（同 pipeline 参数 1500/overlap100）→ TextDetection（dt_polys）
    → SortQuadBoxes 阅读顺序 → CropByPolys(det_box_type="quad") 裁剪
    → 空裁剪块过滤（paddlex ocr/pipeline.py 同款 size>0 语义）
    → TextRecognition.predict_iter(crops, batch_size=6) 逐批 yield → 每行
    发 ('line', ...)。必须显式 batch_size=6：独立实例默认 1（很慢）。
    det/rec 默认模型 PP-OCRv6_medium_det/rec，与现 pipeline 相同、共用
    ~/.paddlex 缓存；enable_mkldnn=False 必传（否则触发 Paddle PIR-onednn
    崩溃 NotImplementedError: ConvertPirAttribute2RuntimeAttribute，实测）。

拷贝算子来源与版本：SortQuadBoxes 与 CropByPolys(quad 分支) 逐行拷自
paddlex 3.7.2
  paddlex/inference/pipelines/components/common/sort_boxes.py
  paddlex/inference/pipelines/components/common/crop_image_regions.py
（Apache License 2.0，Copyright PaddlePaddle Authors；MinerU 先例=拷算子
源码进项目）。CropByPolys 仅保留 quad 路径（poly 分支依赖 shapely/
AutoRectifier，本项目不用到）。
"""
from __future__ import annotations

import copy
import os
import tempfile
import threading
from typing import Callable, Optional

import numpy as np

# 事件协议见模块 docstring
ProgressCb = Callable[[tuple], None]

REC_BATCH_SIZE = int(os.environ.get("OCR_REC_BATCH", "6"))

# det 运行时参数：paddlex 3.7.2 OCRPipeline 调 text_det_model 时显式传
# get_text_det_params()（默认 limit_side_len=64/limit_type=min/...），而独立
# TextDetection 用自己的模型配置默认值 → 不传参时检测框不同（实测 chunk0
# 26 vs 23 框）。lowlevel 必须显式对齐，实测传参后 dt_polys 与 pipeline
# 逐位一致（max|diff|=0）。来源：paddlex/inference/pipelines/ocr/pipeline.py。
DET_PARAMS = {
    "limit_side_len": 64,
    "limit_type": "min",
    "thresh": 0.3,
    "max_side_limit": 4000,
    "box_thresh": 0.6,
    "unclip_ratio": 1.5,
}

_ENGINE_LOCK = threading.Lock()

_pipeline_engine = None
_det_engine = None
_rec_engine = None


def engine_mode() -> str:
    """当前 OCR 路线（每次调用读环境变量，便于测试/脚本按次切换）。"""
    mode = (os.environ.get("OCR_ENGINE") or "lowlevel").strip().lower()
    return mode if mode in ("lowlevel", "pipeline") else "lowlevel"


def get_pipeline_engine():
    """旧路线的 PaddleOCR 全 pipeline 单例（参数与历史 _get_engine 一致）。"""
    global _pipeline_engine
    if _pipeline_engine is None:
        from paddleocr import PaddleOCR
        _pipeline_engine = PaddleOCR(lang="ch", use_doc_orientation_classify=False,
                                     use_doc_unwarping=False, use_textline_orientation=False,
                                     enable_mkldnn=False)
    return _pipeline_engine


def get_lowlevel_engines():
    """lowlevel 路线 (TextDetection, TextRecognition) 单例。

    默认模型与 pipeline 相同（PP-OCRv6_medium_det/rec）；enable_mkldnn=False
    必传（PIR-onednn 崩溃，见模块 docstring）。
    """
    global _det_engine, _rec_engine
    if _det_engine is None or _rec_engine is None:
        from paddleocr import TextDetection, TextRecognition
        if _det_engine is None:
            _det_engine = TextDetection(enable_mkldnn=False)
        if _rec_engine is None:
            _rec_engine = TextRecognition(enable_mkldnn=False)
    return _det_engine, _rec_engine


def _result_dict(obj) -> dict:
    """PaddleX 结果 → 内层 res dict（与历史 _ocr_blocks 的取法一致）。"""
    j = obj.json if hasattr(obj, "json") else obj
    if isinstance(j, dict):
        d = j.get("res", j)
        return d if isinstance(d, dict) else j
    return {}


# ---------------- 拷贝算子（paddlex 3.7.2，Apache-2.0，出处见模块头） ----------------
def _sort_quad_boxes(dt_polys):
    """SortQuadBoxes：阅读顺序（自上而下、同带内自左而右）。逐行拷贝自
    paddlex 3.7.2 sort_boxes.py（去 BaseOperator 壳，逻辑未改）。"""
    dt_boxes = np.array(dt_polys)
    if dt_boxes.size == 0:
        return []
    num_boxes = dt_boxes.shape[0]
    sorted_boxes = sorted(dt_boxes, key=lambda x: (x[0][1], x[0][0]))
    _boxes = list(sorted_boxes)

    for i in range(num_boxes - 1):
        for j in range(i, -1, -1):
            if abs(_boxes[j + 1][0][1] - _boxes[j][0][1]) < 10 and (
                _boxes[j + 1][0][0] < _boxes[j][0][0]
            ):
                tmp = _boxes[j]
                _boxes[j] = _boxes[j + 1]
                _boxes[j + 1] = tmp
            else:
                break
    return _boxes


def _get_rotate_crop_image(img, points):
    """CropByPolys.get_rotate_crop_image（paddlex 3.7.2 拷贝，逻辑未改）。"""
    assert len(points) == 4, "shape of points must be 4*2"
    import cv2
    img_crop_width = int(
        max(np.linalg.norm(points[0] - points[1]),
            np.linalg.norm(points[2] - points[3]))
    )
    img_crop_height = int(
        max(np.linalg.norm(points[0] - points[3]),
            np.linalg.norm(points[1] - points[2]))
    )
    if img_crop_width < 1 or img_crop_height < 1:  # 退化框：交由空块过滤剔除
        return None
    pts_std = np.float32(
        [[0, 0], [img_crop_width, 0],
         [img_crop_width, img_crop_height], [0, img_crop_height]]
    )
    M = cv2.getPerspectiveTransform(points, pts_std)
    dst_img = cv2.warpPerspective(
        img, M, (img_crop_width, img_crop_height),
        borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC,
    )
    dst_img_height, dst_img_width = dst_img.shape[0:2]
    if dst_img_height * 1.0 / dst_img_width >= 1.5:
        dst_img = np.rot90(dst_img)
    return dst_img


def _minarea_rect_crop(img, points):
    """CropByPolys.get_minarea_rect_crop（quad 分支，paddlex 3.7.2 拷贝）。"""
    import cv2
    bounding_box = cv2.minAreaRect(np.array(points).astype(np.int32))
    points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda x: x[0])

    index_a, index_b, index_c, index_d = 0, 1, 2, 3
    if points[1][1] > points[0][1]:
        index_a, index_d = 0, 1
    else:
        index_a, index_d = 1, 0
    if points[3][1] > points[2][1]:
        index_b, index_c = 2, 3
    else:
        index_b, index_c = 3, 2

    box = [points[index_a], points[index_b], points[index_c], points[index_d]]
    return _get_rotate_crop_image(img, np.array(box))


def _crop_quads(img, sorted_polys):
    """quad 裁剪 + paddlex 同款空块过滤，返回 (crops, kept_polys) 对齐。"""
    crops, kept = [], []
    for p in sorted_polys:
        crop = _minarea_rect_crop(img, copy.deepcopy(p))
        if crop is not None and crop.size > 0 and crop.shape[0] > 0 and crop.shape[1] > 0:
            crops.append(crop)
            kept.append(np.asarray(p))
    return crops, kept


# ---------------- 片内推理 ----------------
def _chunk_blocks_pipeline(engine, chunk_path) -> list[tuple[int, int, str, float]]:
    """旧路线单片 predict → 片内 (y, x, text, score)（消费方式与历史一致）。"""
    res = engine.predict(input=chunk_path)
    if not res or not res[0]:
        return []
    try:
        d = _result_dict(res[0])
    except Exception:
        return []  # 历史语义：结果解析失败 → 跳过该片
    texts = d.get("rec_texts", [])
    scores = d.get("rec_scores", [])
    boxes = d.get("rec_boxes", []) or d.get("dt_polys", [])
    out = []
    for i, t in enumerate(texts):
        try:
            b = boxes[i]
            if len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
                x1, y1 = int(b[0]), int(b[1])
            else:
                x1, y1 = int(b[0][0]), int(b[0][1])
        except Exception:
            x1, y1 = 0, 0
        out.append((y1, x1, str(t),
                    float(scores[i]) if i < len(scores) and isinstance(scores[i], (int, float)) else 0.0))
    return out


def _chunk_blocks_lowlevel(det, rec, chunk_path, progress_cb: Optional[ProgressCb],
                           chunk_i: int, chunk_n: int) -> list[tuple[int, int, str, float]]:
    """lowlevel 单片：det → 排序裁剪 → rec 逐批产出（每行回调进度）。"""
    import cv2
    dres = list(det.predict(chunk_path, **DET_PARAMS))
    dd = _result_dict(dres[0]) if dres else {}
    polys = _sort_quad_boxes(list(dd.get("dt_polys", [])))
    img = cv2.imread(chunk_path)
    crops, kept_polys = _crop_quads(img, polys) if (polys and img is not None) else ([], [])
    if progress_cb:
        progress_cb(("chunk", chunk_i, chunk_n))
    out: list[tuple[int, int, str, float]] = []
    total = len(crops)
    if total == 0:
        return out
    # 必须显式 batch_size（独立实例默认 1，很慢）；predict_iter 逐批 yield、
    # 产出与输入同序（paddlex 3.7.2 语义）。
    for idx, rres in enumerate(rec.predict_iter(crops, batch_size=REC_BATCH_SIZE), 1):
        d2 = _result_dict(rres)
        poly = kept_polys[idx - 1]
        x1, y1 = int(poly[:, 0].min()), int(poly[:, 1].min())
        try:
            score = float(d2.get("rec_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        text = str(d2.get("rec_text", ""))
        out.append((y1, x1, text, score))
        if progress_cb:
            progress_cb(("line", idx, total, text, score))
    return out


def run_ocr(img_path: str, chunk: int = 1500, overlap: int = 100,
            progress_cb: Optional[ProgressCb] = None) -> list[tuple[int, int, str, float]]:
    """截图 → [(abs_y, abs_x, text, score)]，已按 (y, x) 排序。

    - 竖切片参数与历史 pipeline 完全一致（chunk=1500 / overlap=100，切片仍
      经同一路径 jpg 落盘再读，保证两条路线像素输入一致）。
    - OCR_ENGINE=pipeline 走旧 engine.predict 全 pipeline（无片内 line 事件）；
      默认 lowlevel 见模块 docstring。
    - 全过程持 _ENGINE_LOCK（含切片循环）：推理串行，根治并发竞态。
    """
    import cv2
    img = cv2.imread(img_path)
    if img is None:
        raise ValueError(f"无法读取图片: {img_path}")
    H, _ = img.shape[:2]
    starts = list(range(0, H, chunk - overlap))
    n = len(starts)
    mode = engine_mode()
    blocks: list[tuple[int, int, str, float]] = []
    with _ENGINE_LOCK:
        if mode == "pipeline":
            engine = get_pipeline_engine()
        else:
            det, rec = get_lowlevel_engines()
        for ci, start in enumerate(starts):
            end = min(start + chunk, H)
            tmp = os.path.join(tempfile.gettempdir(), f"ocr_chunk_{start}.jpg")
            cv2.imwrite(tmp, img[start:end, :, :])
            # 异常语义与历史一致：engine 推理异常向上传播（只有结果解析失败
            # 才跳过该片，该兜底已在 _chunk_blocks_pipeline 内部处理）
            if mode == "pipeline":
                part = _chunk_blocks_pipeline(engine, tmp)
                if progress_cb:
                    progress_cb(("chunk", ci, n))
            else:
                part = _chunk_blocks_lowlevel(det, rec, tmp, progress_cb, ci, n)
            blocks.extend((y + start, x, t, s) for (y, x, t, s) in part)
    blocks.sort(key=lambda b: (b[0], b[1]))
    return blocks
