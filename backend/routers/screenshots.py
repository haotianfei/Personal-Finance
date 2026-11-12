"""截图录入 API（T4）：preview（解析+映射清单）/ apply（确认写入+凭证）/
verify（截图 AI 一致性校验，纯展示不落库）。"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import date as _date
from decimal import Decimal
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from errors import ApiError, ErrorCode
from services.idempotency_service import idempotency_guard
from models import LLMProvider, SourceGroup
from routers.source_groups import _serialize_group
from schemas import (
    AUDIT_REASON_NOTE_MAX,
    AuditReason,
    EvidenceListResponse,
    EvidencePurgeRequest,
    EvidenceRecognitionRequest,
    ScreenshotApplyResult,
    ScreenshotPreviewResponse,
    ScreenshotPreviewRow,
    SupplementDropOut,
)
from services import evidence_service, llm_verification_service, preview_jobs, screenshot_service
from services import source_group_rules_service
from services.parser_rules import load_vocabularies
from services.source_group_rules_service import (
    CORRECTION_RULE_TYPES,
    RuleConflictError,
    RuleNotFoundError,
)

router = APIRouter()

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_GROUP = "bantuou"


_DATA_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_apply_data_date(value: Optional[str]) -> Optional[_date]:
    """apply 顶层表单参 data_date → date（纯函数，仿 parse_zero_fill_date 惯例）。

    空/缺省/全空白 → None（= 现状语义：截图识别日期 or 今天）；
    非法 → HTTPException 400（中文）。严格 YYYY-MM-DD 形态（正则前置把关，
    拒收 '2026-9-1'、'20260914' 等 fromisoformat 宽松变体）+ 真实日历校验。
    """
    if not value or not value.strip():
        return None
    v = value.strip()
    if not _DATA_DATE_RE.match(v):
        raise HTTPException(status_code=400, detail="data_date 必须为 YYYY-MM-DD")
    try:
        return _date.fromisoformat(v)
    except ValueError:
        raise HTTPException(status_code=400, detail="data_date 必须为 YYYY-MM-DD")


def _evidence_root() -> str:
    """凭证根目录（项目根/data/evidence）。薄函数便于测试注入 tmp 目录。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "evidence")


def _save_upload(file: UploadFile) -> tuple[str, str]:
    """上传的截图落到临时文件，返回 (路径, 文件sha256)；调用方负责事后删除。"""
    suffix = os.path.splitext(file.filename or "")[1] or ".png"
    fd, path = tempfile.mkstemp(prefix="shot_upload_", suffix=suffix)
    sha = hashlib.sha256()
    with os.fdopen(fd, "wb") as out:
        while chunk := file.file.read(1 << 16):
            sha.update(chunk)
            out.write(chunk)
    return path, sha.hexdigest()


def persist_upload(file: UploadFile, source_group: str,
                   db: Session) -> tuple[str, str, int]:
    """v5 步骤 6「上传即存证」：流式写 tempfile 的同时算 sha，**立即**
    get_or_create_evidence（同 sha 复用行，一 sha 一行一文件）。

    返回 (tempfile 路径, sha, evidence_id)；tempfile 仍供识别用、由调用方事后删除
    （凭证文件才是唯一持久原件）。落盘/建行失败 → 500 EVIDENCE_WRITE_FAILED
    **阻断识别**（凭证是可信底座：宁可不识别，不可无兜底），且不留 tempfile。
    """
    try:
        path, sha = _save_upload(file)
    except Exception as e:
        raise ApiError(ErrorCode.EVIDENCE_WRITE_FAILED, 500,
                       f"凭证存证失败（临时文件写入）: {e}")
    try:
        ev_id = screenshot_service.get_or_create_evidence(
            db, sha, path, source_group, file.filename or "shot.png",
            _evidence_root())
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        if isinstance(e, ApiError):
            raise          # 业务错误（如 400 SOURCE_GROUP_UNKNOWN）原样上抛，不降级 500
        raise ApiError(ErrorCode.EVIDENCE_WRITE_FAILED, 500,
                       f"凭证存证失败: {e}")
    return path, sha, ev_id


def _parsing_rules(db: Session, source_group: str, config: dict) -> dict:
    """本次解析要用的规则快照（列 + 表合并结果）。

    `config` 来自 `get_group_config`，其 `rules` 键自 spec D9 起已是**合并结果**
    （`source_group_rules_service._merged_rules` 唯一实现：列解析键 + 教学规则三键），
    故此处只需取用。保留这个薄函数是为了兼容被 monkeypatch 的 `get_group_config`
    （测试替身可能只给列口径内容），以及给上面的 docstring 留一个明确的收口点。

    **不得**在别处再长出第二条合并路径：要合并就去服务层改 `_merged_rules`。
    """
    return config.get("rules") or {}


async def _run_pipeline(path: str, sha: str, source_group: str, db: Session,
                        unrecognized: str = "keep", force_ocr: bool = False,
                        engine: str = "ocr"):
    """截图 → 解析 → 组内现有 → 映射清单（T2 全链，预览与确认共用）。

    OCR 解析（约 4 分钟）是 CPU 密集的同步块，必须挪到线程池执行，
    避免阻塞单 worker uvicorn 的事件循环（否则期间 /api/health 等都不响应）。
    解析规则取 `_parsing_rules`（列 + 表合并，坏 JSON → 422），词表取
    `parser_rules.load_vocabularies`（全局增量），两者一并喂给 `parse_screenshot`；
    compute_preview 按 unrecognized 策略生成未识别项（keep 保持原值 / all 清零），
    rules["exclude"]（「不录入」资产全名）仅从 keep 列表过滤、不碰匹配候选集。

    engine="ai"（AI 主识别，peer-entry-design §3.1）：完全不调 parse_screenshot
    /不读 blocks/不套校正规则，以空解析结果进同一 compute_preview →
    组内现有资产全为 keep/zero 行、锚点与截图日期为 None；其余语义不变。
    """
    config = screenshot_service.get_group_config(db, source_group)
    if config is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    prefix = config["asset_prefix"]
    rules = _parsing_rules(db, source_group, config)   # 列（解析键）+ 表（教学规则）
    if engine == "ai":
        parsed = screenshot_service.ParsedScreenshot()  # 空骨架：items=[] total/date=None
    else:
        # 词表每请求只读一次（AI 模式不解析，故不读）；组级 rules 在组织层再叠加覆盖。
        vocabularies = load_vocabularies(db)
        parsed = await asyncio.to_thread(
            screenshot_service.parse_screenshot, path, sha, rules,
            vocabularies=vocabularies, force_ocr=force_ocr)
        # Seam D（校正层）：把来源组 rules 的 rename[] / ignore[] 套到解析产物上，
        # 让 preview 与 apply（共用本管线）同时生效；不动 organize_blocks 核心。
        parsed.items = screenshot_service.apply_correction_rules(parsed.items, rules)
    existing = screenshot_service.get_group_existing(db, prefix)
    preview = screenshot_service.compute_preview(
        parsed.items, existing, prefix, unrecognized=unrecognized,
        exclude=(rules or {}).get("exclude"))  # 「不录入」资产不再进 keep 列表
    # 身份裁决提示（D4，只读）：新名 + 同账户疑似同源旧项 → 行打 identity_guess
    preview = screenshot_service.annotate_identity_guesses(db, preview,
                                                           config["account_ids"])
    # 汇总行提示（ADR-0029 D7，只读、纯提示）：内置汇总词表 ∪ 该组 aggregate 规则 →
    # 行打 aggregate_hint（前端预勾可取消）。**绝不据此自动跳过任何行**。
    sg = screenshot_service.get_source_group(db, source_group)
    learned_aggregates = (source_group_rules_service.aggregate_names_of(db, sg.id)
                          if sg is not None else [])
    preview = screenshot_service.annotate_aggregate_hints(preview, learned_aggregates)
    # 行级 item_id + 逐项现值（ADR-0026 补遗）：绑定行的差额基线靠它
    preview = screenshot_service.attach_item_ids(
        preview, screenshot_service.get_group_existing_items(db, prefix))
    # 归档项去向提示（spec `.scratch/archived-ingest-scope` D3，只读）：命中已归档名的
    # create 行附「历史上已并入 X」。只加提示字段，绝不改 action / 金额 / 合计。
    preview = screenshot_service.annotate_archived_merge_hints(db, preview)
    return parsed, existing, preview, prefix


def _record_recognition(db: Session, evidence_id: Optional[int], parsed, *,
                        engine: str, client_recognition: Optional[dict] = None,
                        adopted_rows=None,
                        corrected_rows: Optional[int] = None) -> None:
    """识别快照落库（ADR-0023 D7）：三个写入点的**唯一**调用形态。

    路由只回答「此刻手里有什么」，组装与写入全在 `evidence_service`
    （`recognized_of` / `recognized_of_client` / `adopted_of` / `record_recognition`）：

    - OCR 路径 → 服务端管线产物（含 OCR 原始块，`source="server"`）；
    - AI 路径 → 前端回传的行（`source="client"`；未回传则 `items: null` + `note`）；
    - `adopted_rows`（仅 `/apply`）→ 真正落账本的行 + 人改过的行数。

    服务层已把「行不存在 / 体积超限 / 写库失败」全部降级为不抛错（快照是旁证，
    绝不阻断上传与采纳），故此处无需再包 try。
    """
    recognized = (evidence_service.recognized_of_client(client_recognition)
                  if engine == "ai"
                  else evidence_service.recognized_of(parsed, engine=engine))
    adopted = (evidence_service.adopted_of(
        adopted_rows, engine=engine, corrected_rows=corrected_rows)
        if adopted_rows is not None else None)
    evidence_service.record_recognition(
        db, evidence_id, recognized=recognized, adopted=adopted)


def _preview_response(parsed, existing, preview, source_group, filename,
                      evidence_id: Optional[int] = None) -> ScreenshotPreviewResponse:
    total_screenshot = sum((i.amount for i in parsed.items), Decimal("0"))
    rows = [
        ScreenshotPreviewRow(
            index=r.index if r.index is not None else i,
            source_name=r.source_name, asset_name=r.asset_name,
            action=r.action, source_amount=r.source_amount,
            current_amount=r.current_amount, diff=r.diff,
            identity_guess=r.identity_guess,
            item_id=r.item_id,
            aggregate_hint=r.aggregate_hint,
            archived_merged_into=r.archived_merged_into,
        )
        for i, r in enumerate(preview.rows)
    ]
    return ScreenshotPreviewResponse(
        source_group=source_group,
        data_date=parsed.date,
        suspected_date=parsed.date,          # 与 data_date 同源（语义不变）
        suspected_date_text=parsed.date_text,  # 命中行原文（未命中为 None）
        total_screenshot=total_screenshot,
        total_current_system=preview.total_current_system,
        existing_count=len(existing),
        image_filename=filename,
        rows=rows,
        evidence_id=evidence_id,
        existing_items=list(preview.existing_items or []),
    )


@router.post("/preview", response_model=ScreenshotPreviewResponse)
async def screenshot_preview(
    file: UploadFile = File(...),
    source_group: str = Form(DEFAULT_SOURCE_GROUP),
    unrecognized: str = Form("keep"),   # 未识别项策略：keep 保持原值（默认） | all 全部清零
    force_ocr: bool = Form(False),      # true 时忽略 L1/L2 缓存重新 OCR 并刷新缓存
    engine: str = Form("ocr"),          # 骨架引擎：ocr（默认，旧客户端零影响）| ai（跳过 OCR 空骨架）
    recognition: str = Form(""),        # AI 路径识别行回传（JSON 串；ADR-0023 D7，可选加法字段）
    db: Session = Depends(get_db),
):
    if screenshot_service.get_group_config(db, source_group) is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    if unrecognized not in ("keep", "all"):
        raise HTTPException(status_code=400, detail=f"未知未识别项策略: {unrecognized}")
    if engine not in ("ocr", "ai"):
        raise HTTPException(status_code=400, detail=f"未知识别引擎: {engine}")
    # AI 回传先校验（非法 → 422 VALIDATION_ERROR，**不静默吞**）：绝不为一份坏 JSON
    # 白跑一轮 4 分钟 OCR，也不留一条「存不下」的凭证
    try:
        client_recognition = evidence_service.parse_client_recognition(recognition)
    except ValueError as e:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, str(e))
    # v5 步骤 6：上传即存证（失败 500 阻断识别）；tempfile 供识别、用后即删
    path, sha, ev_id = persist_upload(file, source_group, db)
    try:
        parsed, existing, preview, _prefix = await _run_pipeline(
            path, sha, source_group, db, unrecognized=unrecognized,
            force_ocr=force_ocr, engine=engine)
    except ApiError:
        raise      # 业务错误（如 SOURCE_GROUP_UNKNOWN）走统一信封，不降级成 422
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"截图解析失败: {e}")
    finally:
        os.remove(path)
    # 写入点 1/3（ADR-0023 D7）：预览即留存「机器给了什么」
    _record_recognition(db, ev_id, parsed, engine=engine,
                        client_recognition=client_recognition)
    return _preview_response(parsed, existing, preview, source_group,
                             file.filename or "", evidence_id=ev_id)


# ---------------- 异步预览任务（真实逐行进度） ----------------
def _run_pipeline_sync(path: str, sha: str, source_group: str, db: Session,
                       unrecognized: str = "keep", progress_cb=None,
                       force_ocr: bool = False, engine: str = "ocr"):
    """_run_pipeline 的同步版（preview_jobs 工作线程用）：全流程在当前线程串行。

    与 async 版逐步等价（含 engine="ai" 的跳过 OCR 分支）；多一个 progress_cb：
    OCR 事件透传给 parse_screenshot，进入映射（compute_preview）前补发
    ('match',) 阶段事件（缓存命中路径不发，其 stage 停在 cache→done；
    AI 模式无 OCR 事件，仅 match）。db 由调用方（工作线程自建 SessionLocal）提供。
    """
    config = screenshot_service.get_group_config(db, source_group)
    if config is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    prefix = config["asset_prefix"]
    rules = _parsing_rules(db, source_group, config)   # 列（解析键）+ 表（教学规则）
    if engine == "ai":
        parsed = screenshot_service.ParsedScreenshot()  # 空骨架，同 async 版
    else:
        vocabularies = load_vocabularies(db)   # 与 async 版同口径：每请求只读一次
        parsed = screenshot_service.parse_screenshot(path, sha, rules,
                                                     vocabularies=vocabularies,
                                                     progress_cb=progress_cb,
                                                     force_ocr=force_ocr)
        parsed.items = screenshot_service.apply_correction_rules(parsed.items, rules)
    existing = screenshot_service.get_group_existing(db, prefix)
    if progress_cb:
        progress_cb(("match",))
    preview = screenshot_service.compute_preview(
        parsed.items, existing, prefix, unrecognized=unrecognized,
        exclude=(rules or {}).get("exclude"))  # 「不录入」资产不再进 keep 列表
    # 身份裁决提示（D4，只读）：与 async 版逐字段等价（预览响应一致）
    preview = screenshot_service.annotate_identity_guesses(db, preview,
                                                           config["account_ids"])
    preview = screenshot_service.attach_item_ids(
        preview, screenshot_service.get_group_existing_items(db, prefix))
    # 归档项去向提示（spec `.scratch/archived-ingest-scope` D3，只读）：与 async 版一致
    preview = screenshot_service.annotate_archived_merge_hints(db, preview)
    return parsed, existing, preview, prefix


_LINES_CAP = 1000   # job.lines 实时行预览上限（超出丢最旧，行计数不受影响）


def progress_event_to_fields(ev: tuple, chunk_index: int = 0) -> dict:
    """OCR/阶段事件 → preview_jobs 增量字段（纯函数，直测）。

    ('cache',) / ('organize',) / ('match',) → 仅 stage；
    ('chunk', i, n) → ocr 计数重置（line 归 0）；
    ('line', done, total, text, score) → 行进度计数 + row（该行原始识别文本
    {t: text, s: score 三位小数, c: 所属 chunk_index}，供前端即时查看）。
    未知事件忽略（返回 {}）。
    """
    kind = ev[0]
    if kind == "cache":
        return {"stage": "cache"}
    if kind == "organize":
        return {"stage": "organize"}
    if kind == "match":
        return {"stage": "match"}
    if kind == "chunk":
        return {"stage": "ocr",
                "ocr": {"chunk_index": ev[1], "chunk_total": ev[2],
                        "line_done": 0, "line_total": 0}}
    if kind == "line":
        try:
            s = round(float(ev[4]), 3)
        except (TypeError, ValueError):
            s = 0.0
        return {"stage": "ocr",
                "line": {"line_done": ev[1], "line_total": ev[2]},
                "row": {"t": str(ev[3]), "s": s, "c": chunk_index}}
    return {}


def merge_line_into_ocr(ocr: Optional[dict], line: dict) -> dict:
    """line 事件并入现有 ocr 计数字典（chunk 字段保留）。"""
    base = {"chunk_index": 0, "chunk_total": 0, "line_done": 0, "line_total": 0}
    if ocr:
        base.update(ocr)
    base.update(line)
    return base


def run_preview_job(path: str, sha: str, source_group: str,
                    unrecognized: str, filename: str, force_ocr: bool = False,
                    engine: str = "ocr", evidence_id: Optional[int] = None,
                    cleanup: bool = True,
                    recognition: Optional[dict] = None):
    """构造 preview_jobs 的 work 闭包：工作线程自建 Session、驱动进度上报、
    完成后删除临时文件、result 为与 POST /preview 逐字段一致的响应 dict。

    evidence_id（v5 存证前置）随 result 透传，与 /preview 响应字段一致（additive）；
    续跑路径（重启恢复）从 payload 重建时同样带上，前端预览阶段即可引用凭证 #id。
    cleanup=False（重启续跑：path 即凭证持久原件）→ 完成后**绝不删文件**。

    recognition（AI 路径前端回传的识别行，**已由 `parse_client_recognition` 规范化**
    的 dict，非原始 JSON 串）：进 job 闭包后原样交给 `_record_recognition`，与
    `/preview` 完全同口径。None = 本次没回传 → 记 `items: null` + note（见 D1）。

    line 事件另把该行 {t,s,c} 累积进 job["lines"]（实时原始行预览）：上限
    _LINES_CAP 条，超出丢弃最旧、line_done 计数照常；done 后 lines 保留在
    最终快照中。engine 随 job 载荷透传给 _run_pipeline_sync；缺省 ocr 时
    不传该关键字（调用形态与既往逐字节一致，管线替身无需认识新参数）。

    完成前顺手写识别快照（ADR-0023 D7 写入点 2/3，`_record_recognition`）：
    与 `/preview` 同口径；AI 模式带 `recognition` 时落回传行，未带则记
    `items: null` + `note="client-side"`（= 那次识别内容没存下来）。
    """
    def work(report):
        state = {"ocr": None, "chunk": 0}
        lines: list[dict] = []

        def cb(ev):
            fields = progress_event_to_fields(ev, state["chunk"])
            if ev[0] == "chunk":
                state["chunk"] = ev[1]
            if "line" in fields:
                row = fields.pop("row")
                fields["ocr"] = merge_line_into_ocr(state["ocr"], fields.pop("line"))
                lines.append(row)
                if len(lines) > _LINES_CAP:
                    del lines[0]  # 丢最旧保最近；ocr.line_done 计数不受影响
                state["ocr"] = fields["ocr"]
                fields["lines"] = list(lines)
            elif "ocr" in fields:
                state["ocr"] = fields["ocr"]
            report(**fields)

        db = SessionLocal()
        try:
            report(status="running", stage="ocr")
            extra = {"engine": engine} if engine != "ocr" else {}
            parsed, existing, preview, _ = _run_pipeline_sync(
                path, sha, source_group, db, unrecognized=unrecognized,
                progress_cb=cb, force_ocr=force_ocr, **extra)
            resp = _preview_response(parsed, existing, preview, source_group,
                                     filename, evidence_id=evidence_id)
            # 写入点 2/3（ADR-0023 D7）：异步预览与 /preview 同口径留存识别快照。
            # recognition 由端点在入队时解析（payload 同份、续跑工厂一并带上）；
            # 未回传/非法 → None → 记 items=null + note="client-side"，语义是
            # 「这条没存下来」，不是「没识别」。
            _record_recognition(db, evidence_id, parsed, engine=engine,
                                client_recognition=recognition)
            return jsonable_encoder(resp)  # 与 HTTP 层同一序列化器 → 逐字段一致
        finally:
            db.close()
            if cleanup:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return work


@router.post("/preview-jobs", status_code=202)
async def create_preview_job(
    file: UploadFile = File(...),
    source_group: str = Form(DEFAULT_SOURCE_GROUP),
    unrecognized: str = Form("keep"),
    force_ocr: bool = Form(False),   # true 时本次 OCR 忽略缓存重跑并刷新缓存
    engine: str = Form("ocr"),       # 骨架引擎：ocr（默认）| ai（跳过 OCR，同 /preview）
    recognition: Optional[str] = Form(None),
    # AI 路径识别行回传（JSON 串，与 /preview /apply 同构；ADR-0023 D7 可选加法字段）。
    # 默认 None = 未传（与两个兄弟端点的 Form("") 语义等价：`parse_client_recognition`
    # 对 None/空串一视同仁返回 None）。
    db: Session = Depends(get_db),
):
    """入队异步预览任务（参数与 /preview 对齐）。202 → {job_id}，
    再轮询 GET /preview-jobs/{job_id} 取真实逐行进度与结果。"""
    if screenshot_service.get_group_config(db, source_group) is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    if unrecognized not in ("keep", "all"):
        raise HTTPException(status_code=400, detail=f"未知未识别项策略: {unrecognized}")
    if engine not in ("ocr", "ai"):
        raise HTTPException(status_code=400, detail=f"未知识别引擎: {engine}")
    # AI 回传识别行：**与 /preview 刻意不同——非法只记 warning 不报错**（spec D3）。
    # 预览是主路径，补录性质的信息不合法绝不能让整张图识别不出来；规范化后随 payload
    # 落到 job，续跑工厂一并带上（D2：否则 job 重排后快照丢结果）。
    # `recognition` 只可能是表单串或 None：直调本函数（测试/脚本）时拿到的是 FastAPI 的
    # 缺省值标记 `Form(None)`，按「未传」处理（HTTP 层由 FastAPI 注入真实值）。
    raw_recognition = recognition if isinstance(recognition, str) else None
    try:
        client_recognition = evidence_service.parse_client_recognition(raw_recognition)
    except ValueError as e:
        logger.warning("preview_jobs_recognition_ignored err=%s", e)
        client_recognition = None
    path, sha, ev_id = persist_upload(file, source_group, db)
    # 续跑载荷：tempfile 不可持久，重启后凭 evidence_id 从凭证 file_path 取图重跑
    payload = {
        "evidence_id": ev_id, "sha": sha, "source_group": source_group,
        "unrecognized": unrecognized, "filename": file.filename or "",
        "force_ocr": bool(force_ocr), "engine": engine,
        "recognition": client_recognition,
    }
    try:
        job_id = preview_jobs.submit(
            run_preview_job(path, sha, source_group, unrecognized,
                            file.filename or "", force_ocr=force_ocr,
                            engine=engine, evidence_id=ev_id,
                            recognition=client_recognition),
            payload=payload, db=db)
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"任务入队失败: {e}")
    return {"job_id": job_id, "evidence_id": ev_id}


def _abs_evidence_path(evidence_id: int) -> Optional[str]:
    """evidence_id → 凭证原图绝对路径（DB 读 file_path + 拼 evidence_root）。"""
    db = SessionLocal()
    try:
        ev = db.get(screenshot_service.ScreenshotEvidence, evidence_id)
        if ev is None:
            return None
        return os.path.join(_evidence_root(), ev.file_path)
    finally:
        db.close()


def work_from_payload(payload: dict):
    """preview_jobs 续跑工厂：直接用凭证 file_path 作图重建 work 闭包重跑管线。

    识别只读、无副作用；preview 结果幂等；OCR 缓存按 sha 多数命中即快。
    cleanup=False——path 即凭证持久原件，完成/失败都绝不删文件。

    `recognition` 必须一并从 payload 取回（spec D2）：它是入队时已规范化的 dict，
    丢了它重排后的快照就退回 items=null，等于白跑。老载荷（本字段上线前入队的行）
    缺该键 → `get` 得 None，与「没回传」同义，向后兼容。
    """
    src = _abs_evidence_path(payload["evidence_id"])
    return run_preview_job(
        src or "", payload["sha"], payload["source_group"], payload["unrecognized"],
        payload.get("filename", ""), force_ocr=payload.get("force_ocr", False),
        engine=payload.get("engine", "ocr"), evidence_id=payload["evidence_id"],
        cleanup=False, recognition=payload.get("recognition"))


def init_jobs_recovery() -> int:
    """lifespan 启动钩子：启用 preview_jobs 持久层（session/work 工厂）并恢复
    未结任务（queued/running → 重入队，从凭证 file_path 续跑）。

    返回恢复条数；本身异常上抛由调用方（main）降级 warning——不阻塞启动。
    """
    preview_jobs.configure(session_factory=SessionLocal,
                           work_factory=work_from_payload)
    return preview_jobs.resume_pending()


@router.get("/preview-jobs/{job_id}")
def get_preview_job(job_id: str):
    """任务快照 {id,status,stage,ocr,queue_position,elapsed,result,error}；
    未知/已过期 → 404（中文）。"""
    snap = preview_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="预览任务不存在或已过期")
    return snap


@router.post("/apply", response_model=ScreenshotApplyResult, dependencies=[Depends(idempotency_guard("POST /api/screenshots/apply"))])
async def screenshot_apply(
    file: UploadFile = File(...),
    options: str = Form("{}"),   # JSON: {source_group?, skipped?: [int], amounts?: {int: str},
                                #       unrecognized?: "keep"|"all", zero_names?: [str]（单独清0）,
    #       names?: {int: str}（行号 → 覆盖最终名称）,
    #       supplements?: [{name, amount, side?}]（补录，见 spec）,
    #       identity_decisions?: {"<预览行号>": {action: merge|create, ...}}（身份裁决，D4）,
    #       reconciles?: [int]（汇总行：与 skipped 同形的行号，零写入；ADR-0029 D5/D6）,
    #       batch_id?: int（批次成员登记；缺省＝旧行为逐字段一致，ADR-0029 D5）,
    #       remember_aggregate_names?: [str]（把词学成 aggregate 规则；默认不学，票 03 D3）,
    #       engine?: "ai"（AI 主识别：跳过 OCR 空骨架；缺省/其他 = ocr 现状）}
    data_date: str = Form(""),   # 可选：用户显式指定的录入日期 YYYY-MM-DD，优先于截图识别；
                                 # 缺省/空 = 现状语义（parsed.date or 今天）
    recognition: str = Form(""),  # AI 路径识别行回传（JSON 串；ADR-0023 D7，可选加法字段）
    db: Session = Depends(get_db),
):
    try:
        opts = json.loads(options) if options else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="options 不是合法 JSON")
    # 用户指定日期：进门先校验（非法 400，绝不浪费一轮 OCR/写库）
    user_date = parse_apply_data_date(data_date)
    source_group = opts.get("source_group", DEFAULT_SOURCE_GROUP)
    if screenshot_service.get_group_config(db, source_group) is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    unrecognized = opts.get("unrecognized", "keep")
    if unrecognized not in ("keep", "all"):
        raise HTTPException(status_code=400, detail=f"未知未识别项策略: {unrecognized}")
    # AI 主识别（peer-entry-design §4c）：options.engine="ai" 跳过 OCR 重放。
    # 防御性兜底：AI 静默失败的空清单 + 全部清零 = 整组资产被写 0（最坏事故），
    # 后端必须拒绝而非只靠前端禁用。
    engine = opts.get("engine") or "ocr"
    if engine not in ("ocr", "ai"):
        raise HTTPException(status_code=400, detail=f"未知识别引擎: {engine}")
    if engine == "ai" and unrecognized == "all":
        raise HTTPException(status_code=400, detail="AI 识别模式不支持全部清0")
    skipped = opts.get("skipped") or []
    amounts = opts.get("amounts") or {}
    zero_names = opts.get("zero_names") or []
    names = opts.get("names") or {}
    # 汇总行（ADR-0029 D5/D6，additive）：与 skipped 同形的行号数组；与 skipped 交集
    # → 400 RECONCILE_CONFLICT（在 apply_user_edits 内校验，先于任何写入）
    reconciles = opts.get("reconciles") or []
    if not isinstance(reconciles, list):
        raise HTTPException(status_code=400, detail="reconciles 必须为行号数组")
    # 批次（ADR-0029 D5，additive）：**放在 options JSON 里**（与 skipped/amounts/names
    # 同处，不是 multipart 顶层字段——冻结契约第 1 条）；缺省＝旧行为逐字段一致
    batch_id = opts.get("batch_id")
    if batch_id is not None and (isinstance(batch_id, bool)
                                 or not isinstance(batch_id, int)):
        raise HTTPException(status_code=400, detail="batch_id 必须为整数")
    # 汇总行词表学习（票 03 D3，additive）：默认不学，勾选后才写规则表
    remember_aggregate_names = opts.get("remember_aggregate_names") or []
    if not isinstance(remember_aggregate_names, list) or not all(
            isinstance(n, str) and n.strip() for n in remember_aggregate_names):
        raise HTTPException(status_code=400,
                            detail="remember_aggregate_names 必须为非空字符串数组")
    # 身份裁决（D4）：键＝预览行号（字符串），值＝{action: merge|create, ...}；
    # 形状校验在服务层，缺失表态 → 400 IDENTITY_DECISION_REQUIRED（零写入）
    identity_decisions = opts.get("identity_decisions") or {}
    if not isinstance(identity_decisions, dict):
        raise HTTPException(status_code=400, detail="identity_decisions 必须为对象")
    # 补录项：进门先校验（非法 400，绝不浪费一轮 OCR）；写入路径不认识「AI」
    try:
        supplements = screenshot_service.parse_supplements(opts.get("supplements"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # AI 回传识别行：与 /preview 同一校验（非法 → 422 VALIDATION_ERROR，不静默吞）
    try:
        client_recognition = evidence_service.parse_client_recognition(recognition)
    except ValueError as e:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, str(e))

    path, sha, ev_id = persist_upload(file, source_group, db)
    try:
        parsed, existing, preview, prefix = await _run_pipeline(
            path, sha, source_group, db, unrecognized=unrecognized,
            engine=engine)
        # 编辑前快照：`corrected_rows`（人改了几行）的「机器给的」基线，必须在
        # apply_user_edits **之前**留引用（那个函数返回新对象，旧行随后不可达）
        pipeline_preview = preview
        try:
            preview = screenshot_service.apply_user_edits(
                preview, skipped, amounts, zero_names=zero_names, names=names,
                reconciles=reconciles)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # 注入策略 B：行号编辑通道之后、写库之前合成补录行（纯函数）。
        outcome = screenshot_service.apply_supplements(
            preview, existing, prefix, supplements)
        preview = outcome.preview
        supplement_log = _supplement_log(outcome) if supplements else None
        # 用户显式选择 > 截图识别日期 > 今天；作用于本次 apply 全部行与
        # evidence 富化（data_date/supplement_rows/engine 随最近采纳覆盖）。
        resolved_date = user_date or (
            _date.fromisoformat(parsed.date) if parsed.date else _date.today())
        result = screenshot_service.apply_preview(
            db, preview, source_group=source_group,
            data_date=resolved_date,
            evidence_id=ev_id,
            supplement_log=supplement_log,
            engine=engine,
            identity_decisions=identity_decisions,
            reconciles=reconciles,
            batch_id=batch_id,
        )
    except HTTPException:
        raise
    except ApiError:
        raise      # 业务错误（如 IDENTITY_DECISION_REQUIRED）走统一信封，不降级成 422
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"写入失败: {e}")
    finally:
        os.remove(path)

    # 写入点 3/3（ADR-0023 D7）：写本次重跑结果 + 人改后真正落账本的行。
    # corrected_rows 口径唯一（screenshot_service.count_corrected_rows）：改数/改名/
    # 单独清0/取消勾选/补录新增各计一行。
    _record_recognition(
        db, result.evidence_id, parsed, engine=engine,
        client_recognition=client_recognition,
        adopted_rows=result.rows,
        corrected_rows=screenshot_service.count_corrected_rows(
            pipeline_preview.rows, preview.rows))

    # 汇总行词表学习（票 03 D3）：写库成功之后才学，失败不影响已落账的测量值。
    # 幂等：同组同词已存在 → 零写入（source_group_rules_service.remember_aggregate_names）
    if remember_aggregate_names:
        try:
            source_group_rules_service.remember_aggregate_names(
                db, source_group, remember_aggregate_names)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    n_sup = len(outcome.written)
    if n_sup:
        message = (f"已写入 {result.rows_written - n_sup} 条记录"
                   f" + 补录 {n_sup} 条（{result.asset_date}），凭证 #{result.evidence_id}")
    else:
        message = f"已写入 {result.rows_written} 条记录（{result.asset_date}），凭证 #{result.evidence_id}"
    # 汇总行留在清单里但不落账：必须让人看见「几行没记、为什么没记」（票 03 D6）
    if result.reconciled:
        message += (f"，{len(result.reconciled)} 行标为汇总行（只核对不记账，"
                    "请在批次定案时完成跨图核对）")
    # 账本即变更日志：值没变的行既不算写入（rows_written 已排除），也要让用户可见
    if result.skipped_unchanged:
        message += f"，{len(result.skipped_unchanged)} 条与已有记录相同已跳过"
    return ScreenshotApplyResult(
        evidence_id=result.evidence_id,
        data_date=result.asset_date,
        rows_written=result.rows_written,
        message=message,
        supplements_written=n_sup,
        conflicts=result.conflicts,
        skipped_unchanged=result.skipped_unchanged,
        bindings=result.bindings,
        zero_skipped_due_to_bind=result.zero_skipped_due_to_bind,
        reconciled=result.reconciled,
        zero_skipped_due_to_reconcile=result.zero_skipped_due_to_reconcile,
        supplements_dropped=[
            SupplementDropOut(name=d.name, reason=d.reason, asset_name=d.asset_name)
            for d in outcome.dropped],
    )


def _supplement_log(outcome) -> dict:
    """SupplementOutcome → 凭证审计结构（JSON 安全：金额字符串、None 保留）。"""
    return {
        "written": [
            {"name": r.source_name, "amount": str(r.source_amount),
             "side": r.side, "action": r.action, "asset_name": r.asset_name}
            for r in outcome.written],
        "dropped": [
            {"name": d.name, "reason": d.reason, "asset_name": d.asset_name}
            for d in outcome.dropped],
    }


@router.post("/supplements/resolve")
async def resolve_supplement_drafts(
    payload: dict,
    db: Session = Depends(get_db),
):
    """补录草稿权威预判（只读：不跑 OCR、不落库、结果不进任何写路径）。

    请求 {source_group?, supplements: [{name, amount, side?}]}；响应逐项
    [{name, final_name, action: create|update|conflict, asset_name,
    current_amount, conflict_reason, side}]。校验与匹配判定与 apply 完全同源
    （parse_supplements / _supplement_target→match_existing）；前端 apply 时
    仍只发 name/amount/side，最终以后端 apply 重算为准。
    """
    source_group = payload.get("source_group") or DEFAULT_SOURCE_GROUP
    config = screenshot_service.get_group_config(db, source_group)
    if config is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}")
    try:
        supplements = screenshot_service.parse_supplements(payload.get("supplements"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    existing = screenshot_service.get_group_existing(db, config["asset_prefix"])
    return screenshot_service.resolve_supplements(
        existing, config["asset_prefix"], supplements)


@router.get("/evidence", response_model=EvidenceListResponse)
def list_evidence(
    source_group_id: Optional[int] = Query(None, description="来源组 id（精确匹配）"),
    date_from: Optional[_date] = Query(None, description="数据日期起（含）"),
    date_to: Optional[_date] = Query(None, description="数据日期止（含）"),
    usage: str = Query(evidence_service.USAGE_ALL,
                       description="使用状态：all | used | unused"),
    q: Optional[str] = Query(None, description="对原始文件名与相对路径模糊匹配"),
    page: int = Query(1, ge=1),
    page_size: int = Query(evidence_service.DEFAULT_PAGE_SIZE, ge=1,
                           le=evidence_service.MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
):
    """凭证列表（spec D1）：分页 + 来源组/日期/使用状态/关键字筛选，按 id 倒序。

    `used_rows` 由一次 SQL 子查询算出（全行口径，含被更正的 superseded 历史行）；
    `usage` 判定与删除守卫同源（`evidence_service.used_expr()`，spec D4 单一实现）。

    **声明顺序**：必须排在 `GET /evidence/{evidence_id}`（int 路径参数）之前，
    否则静态段会被 `{evidence_id}` 先匹配走。
    """
    items, total = evidence_service.list_evidence(
        db, source_group_id=source_group_id, date_from=date_from,
        date_to=date_to, usage=usage, q=q, page=page, page_size=page_size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


def _content_disposition(original_filename: Optional[str], download: bool) -> str:
    """`inline`（默认）/ `attachment`（download=1）；文件名优先用原始上传名。

    `filename*`（RFC 5987）保证中文名不炸 header；`filename` 给 ASCII 兜底
    （非 ASCII 字符替换为 `_`）。无原始名（历史行可能为 NULL）→ 只给 type。
    """
    kind = "attachment" if download else "inline"
    name = (original_filename or "").strip()
    if not name:
        return kind
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "evidence"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/evidence/{evidence_id}/image")
def get_evidence_image(
    evidence_id: int,
    download: bool = Query(False, description="true/1 → 以附件形式下载原图"),
    db: Session = Depends(get_db),
):
    """凭证原图（spec D2）：realpath 越界校验 + 存在性校验都过后才发文件。

    响应头固定 `Cache-Control: private, max-age=86400`（单用户系统、图不变，
    浏览器可以放心缓存一天）；`Content-Disposition` 默认 inline，`?download=1`
    改 attachment 并带上原始文件名（供人工核对/导出）。

    不用 `StaticFiles` 直挂 `data/evidence`：那样没有 per-id 存在性校验、
    绕过 DB（盘上残留文件可被猜路径直接取）、且目录结构本身就是信息。
    文件用 `FileResponse` 流式返回，不整块读进内存。
    """
    image = evidence_service.resolve_image_file(
        db, evidence_id, evidence_root=_evidence_root())
    return FileResponse(
        image.path,
        media_type=image.media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": _content_disposition(
                image.original_filename, download),
        },
    )


@router.post("/evidence/purge-unused",
             dependencies=[Depends(idempotency_guard(
                 "POST /api/screenshots/evidence/purge-unused"))])
def purge_unused_evidence(payload: Optional[EvidencePurgeRequest] = None,
                          db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """批量清理未使用凭证（spec D5）：`dry_run` 默认 true，只出清单与总字节。

    每条**独立事务**：单条失败进 `failed[]`，其余照删。候选集与删除守卫同源
    （`evidence_service.used_expr()`，spec D4），绝不出现「列表说未使用、删时说在用」。

    幂等：路由级 `idempotency_guard`（带 `Idempotency-Key` 头时按首次响应回放），
    天然幂等（删过的行不会再被选中）兜底正确性。
    """
    payload = payload or EvidencePurgeRequest()
    # `dry_run=False` 是「真删」：缺 reason 时每一条都会被 422 逐个拒掉（逐条独立
    # 事务），响应只剩 `deleted=0 + failed[]`——那不是部分失败，而是**请求形态本身
    # 不成立**（spec 验收 5 的两态）。故在入口就 422，与单条/组删除同码同语义；
    # 真正的强制点仍是 `audit_service.record()`（这里只是提前拒绝零成功可能的请求）。
    if not payload.dry_run and not reason:
        raise ApiError(
            ErrorCode.AUDIT_REASON_REQUIRED, 422,
            "删除操作需要说明原因（action=delete）",
            {"action": "delete", "entity": "screenshot_evidence"})
    return evidence_service.purge_unused(
        db, source_group_id=payload.source_group_id, dry_run=payload.dry_run,
        limit=payload.limit, evidence_root=_evidence_root(),
        reason=reason, reason_note=reason_note)


@router.delete("/evidence/{evidence_id}", status_code=204)
def delete_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
) -> None:
    """删单条凭证（spec D5）：未使用 → 行 + 磁盘文件 + 审计行一起处理；已使用 → 409。

    **成功响应是 204 No Content（无 body）**：前端 `lib/api.ts` 的 `request<T>()` 对
    `res.status === 204` 直接 `return undefined`（`deleteEvidence` 的 TS 类型就是
    `request<void>`），而「200 + 空 body」会让它走 `res.json()` 分支解析失败、
    误报「删除失败」。诊断信息（`freed_bytes` / `file_deleted`）由服务层返回值承载
    （`evidence_service.delete_evidence()`），HTTP 层不吐 body——需要时改服务调用点即可。

    **声明顺序**：`/evidence/purge-unused`（静态段）必须排在返回 DELETE 路由之前，
    否则 `purge-unused` 会先被 `{evidence_id}` 的路径正则接走（方法不匹配虽仍能
    继续匹配到后面的 POST 路由，但把静态段放在前面才不依赖 Starlette 的
    partial-match 回退）；两条新路由都在 `GET /evidence/{evidence_id}` 之前。

    409 `EVIDENCE_IN_USE` 的 details 只给引用计数与首条引用类型，**不含业务金额**
    （金额不是凭证的属性，外泄也不该走这个错误通道）。

    文件删不掉（权限/IO/盘上本来就没有）不算失败：行已按未使用删除，只记日志。
    """
    evidence_service.delete_evidence(
        db, evidence_id, evidence_root=_evidence_root(),
        reason=reason, reason_note=reason_note)
    return None


def _writeback_outcome(db: Session, evidence_id: int,
                       recognized: dict) -> tuple[bool, Optional[int]]:
    """回写后重读快照 → `(saved, recognized_items)`（evidence-recognition-writeback D4）。

    `record_recognition()` 把「行不存在 / 体积超限 / 写库异常」**全部降级为不抛错**
    （快照是旁证，绝不阻断主流程），所以调用方拿不到返回值——`saved` 只能靠读回
    自己刚才那份快照来回答：`recognized.parsed_at` 与本轮一致**且条数对得上** ⇒ 本次
    确实落库；不一致（写库异常已 rollback / 行被并发删掉）⇒ `saved=false`。
    `parsed_at` 只到秒（`_now_iso`），同一秒内两次回写靠条数区分是谁最后落库；
    两份都相同的同内容回写本就幂等，「相等即已存」不产生假阴性。

    条数走 `recognition_summary_of()`（摘要口径的单一实现，与列表接口同源）：
    体积降级后 `items` 被摘掉、只留 `item_count`，它照样答得出来。
    """
    expected_items = recognized.get("items")
    expected_count = len(expected_items) if isinstance(expected_items, list) else None

    db.expire_all()
    ev = db.get(screenshot_service.ScreenshotEvidence, evidence_id)
    raw = ev.recognition_json if ev is not None else None
    try:
        snap = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        snap = None
    if not isinstance(snap, dict):
        return False, None
    # 四级兜底（`_shrink_snapshot` 理论分支）会把正文整段换成 {"note": "size_limit"}：
    # 那时 parsed_at 与条数都已不在快照里，但确实存下来了。
    if snap.get("note") == "size_limit":
        return True, None
    rec = snap.get("recognized") if isinstance(snap.get("recognized"), dict) else {}
    stored_items = rec.get("items")
    stored_count = (len(stored_items) if isinstance(stored_items, list)
                    else rec.get("item_count"))
    if rec.get("parsed_at") != recognized.get("parsed_at") or stored_count != expected_count:
        return False, None
    summary = evidence_service.recognition_summary_of(raw) or {}
    return True, summary.get("recognized_items")


@router.get("/evidence/{evidence_id}/recognition")
def get_evidence_recognition(evidence_id: int, db: Session = Depends(get_db)):
    """识别快照本体（evidence-recognition-view D2）：`{"snapshot": <obj|null>}`。

    列表接口只吐摘要（`recognition_summary`），完整快照（识别行 + 采纳行 +
    OCR 原始块，体积上限 1 MiB）只能按需单取——详情弹层点「查看识别行」才调本端点。

    三种结果（D2）：

      - 行存在且有快照 → `200 {"snapshot": {...}}`（本体原样，不再加工）；
      - 行存在但快照为空 → `200 {"snapshot": null}`（未识别是合法状态，不是错误）；
      - 行不存在 → `404 EVIDENCE_NOT_FOUND`（复用 PATCH 回写的同一个码）。

    纯读：不写库、不写审计，也不改 `recognition_json` 的结构。解析走
    `evidence_service._json_object()`——坏 JSON / 非对象一律 None（与列表摘要
    同一条「旁证读不出不 500」的取舍）。

    声明位置与 `PATCH /evidence/{evidence_id}/recognition` 相邻（同一路径的读写一对）；
    三段路径不会被 `GET /evidence/{evidence_id}`（两段）或
    `GET /evidence/orphans/list`（静态段）抢匹配。
    """
    ev = db.get(screenshot_service.ScreenshotEvidence, evidence_id)
    if ev is None:
        raise ApiError(ErrorCode.EVIDENCE_NOT_FOUND, 404,
                       f"凭证不存在: {evidence_id}")
    return {"snapshot": evidence_service._json_object(
        ev.recognition_json, "recognition_json")}


@router.patch("/evidence/{evidence_id}/recognition")
def writeback_evidence_recognition(
    evidence_id: int,
    payload: Optional[EvidenceRecognitionRequest] = None,
    db: Session = Depends(get_db),
):
    """AI 识别结果回写（evidence-recognition-writeback D1–D5）。

    **只写 `recognized` 段**：`adopted` 段只归 `/apply`（人真落账了什么），回写端
    绝不碰它；同一凭证重复回写按「按段覆盖」以最后一次为准（D3/D7，幂等由语义天然
    成立，无需 `Idempotency-Key`）。

    存在的理由：AI 识别是**前端发起**的（preview job 提交之后才跑），提交时手里没有
    识别行；没有这个端点，只预览不采纳的 AI 会话快照里 `recognized.items` 恒为 null，
    识别率无从对拍（spec §1）。

    边界（D4，与既有快照写入同一取舍）：
      - 行不存在 → 404 `EVIDENCE_NOT_FOUND`（显式调用，允许报错）；
      - `recognition` 缺失 / 显式 null / 非法形状 → 422 `VALIDATION_ERROR`；
      - 体积超限 → 服务层 `_shrink_snapshot` 降级后照存（`saved=true`）；
      - 写库异常 → 服务层 rollback，本层仍回 **200 + `saved=false`**（不回 5xx：
        调用方是旁路回写，不该让前端把识别流程判成失败）。

    **不写审计**（D5）：快照是旁证，不是业务操作。

    响应 `{"ok": true, "saved": bool, "evidence_id": int, "recognized_items": int|null}`
    —— `recognized_items` 是**落库后**的条数（体积降级后取 `item_count`），未存下时为 null。
    """
    if db.get(screenshot_service.ScreenshotEvidence, evidence_id) is None:
        raise ApiError(ErrorCode.EVIDENCE_NOT_FOUND, 404,
                       f"凭证不存在: {evidence_id}")

    recognition = None if payload is None else payload.recognition
    if recognition is None:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "recognition 不能为空",
                       {"fields": [{"field": "recognition",
                                    "message": "缺少 recognition 字段"}]})
    # 形状校验与 `/apply` 同源：对象照原样交给 parse_client_recognition 的 JSON 入口；
    # 字符串按 JSON 串解（对旧客户端/手写调用宽容），两条路最终都是同一个规范化结果。
    try:
        client_recognition = evidence_service.parse_client_recognition(
            recognition if isinstance(recognition, str)
            else json.dumps(recognition, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, str(e),
                       {"fields": [{"field": "recognition", "message": str(e)}]})

    recognized = evidence_service.recognized_of_client(client_recognition)
    evidence_service.record_recognition(db, evidence_id, recognized=recognized)
    saved, recognized_items = _writeback_outcome(db, evidence_id, recognized)
    if not saved:
        logger.warning("evidence_recognition_writeback_not_saved id=%s",
                       evidence_id)
    return {"ok": True, "saved": saved, "evidence_id": evidence_id,
            "recognized_items": recognized_items}


@router.get("/evidence/orphans/list")
def list_orphan_evidence(db: Session = Depends(get_db)):
    """孤儿凭证只读查询（v5 步骤 6）：data_date IS NULL（未采纳富化）
    且无 assets 引用 —— 「预览过又放弃」的录入过程证据，永久保留（R-11 无删除端口）。
    仅读，不提供任何删除/清理入口。"""
    ScreenshotEvidence = screenshot_service.ScreenshotEvidence
    Asset = screenshot_service.Asset
    referenced = select(Asset.source_evidence_id).where(
        Asset.source_evidence_id.isnot(None)).scalar_subquery()
    rows = db.execute(
        # 0029：组只存 source_group_id，对外字段 source_group 仍吐**当前** slug
        # （join 取现值，组改名后展示自动跟随；契约不变）
        select(ScreenshotEvidence, SourceGroup.slug)
        .join(SourceGroup, SourceGroup.id == ScreenshotEvidence.source_group_id)
        .where(ScreenshotEvidence.data_date.is_(None),
               ScreenshotEvidence.id.notin_(referenced))
        .order_by(ScreenshotEvidence.id)
    ).all()
    return {
        "total": len(rows),
        "items": [{
            "id": e.id, "source_group": slug, "file_path": e.file_path,
            "file_sha256": e.file_sha256, "original_filename": e.original_filename,
            "created_at": e.created_at.isoformat(),
        } for e, slug in rows],
    }


@router.get("/evidence/{evidence_id}")
def screenshot_evidence(evidence_id: int, db: Session = Depends(get_db)):
    """凭证详情（spec D7）：与列表项**同一构造器**的产物，另加 `file_sha256`。

    历史上这里返回的是另一套扁平形状（`source_group` 是 slug 字符串、缺
    `used_rows` / `image_meta` / `recognition_summary`），与列表项两处各写一遍；
    收敛到 `evidence_service.evidence_item()` 后，详情页与列表弹层才能共用一份
    内容（spec D8）。

    `file_sha256` 是唯一的加法字段（spec §3 第 5 条 / §6 修订 4）：列表项不含它，
    但 `EvidencePanel` 展示它做审计血缘，故单条 = 列表项 ∪ `{file_sha256}`。
    404 语义与文案（`凭证不存在`）不变。
    """
    item = evidence_service.get_evidence_detail(db, evidence_id)
    if item is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return item


def _require_rule_type(rtype):
    if rtype not in CORRECTION_RULE_TYPES:
        raise HTTPException(status_code=400,
                            detail="type 必须为 rename、ignore 或 exclude")
    return rtype


def _require_rule_match(value, label: str = "match") -> str:
    m = value.strip() if isinstance(value, str) else ""
    if not m:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空")
    return m


def _require_rule_replace(rtype, value) -> Optional[str]:
    """rename 恒需非空 replace（身份定位含 replace）；其他类型返回 None。"""
    if rtype != "rename":
        return None
    r = value.strip() if isinstance(value, str) else ""
    if not r:
        raise HTTPException(status_code=400, detail="rename 规则必须提供非空 replace")
    return r


def _mutate_correction_rules(slug: str, mutator, db: Session) -> dict:
    """规则写统一出口：调用服务层表写函数 + 异常→HTTP 状态映射。

    0031 起教学规则住在 `source_group_learned_rules` 表（不再是 rules JSON 的三键），
    写路径改为行级增删改，锁与事务仍由服务层
    （`source_group_rules_service`）承担。

    `mutator` 为 `(db, slug) → SourceGroup | None` 的服务层调用（组不存在返回 None）。
    映射：定位失败 → 404；编辑后身份重复 → 409；参数错误 → 400；成功返回更新后的
    SourceGroup 序列化对象（与 GET 单项同形，`rules` 键由服务层把表内容拼回）。

    **留痕（ADR-0027 ②口径桶）在服务层**：`add/delete/update/swap_learned_rule`
    各自在锁内、commit 之前记一行 `entity='source_group'`，本函数只做异常→HTTP
    映射，不重复记（同一写入只能有一行，两个记录点必然漂移）。校验失败/被拒的
    请求（400/404/409）在记录点之前就抛了，因此不留审计行。
    """
    try:
        sg = mutator(db, slug)
    except RuleNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "校正规则不存在")
    except RuleConflictError as e:
        raise HTTPException(status_code=409, detail=str(e) or "校正规则已存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if sg is None:  # 组不存在
        raise HTTPException(status_code=404, detail="来源组不存在")
    return _serialize_group(sg)


def save_correction_rule(slug: str, data: dict, db: Session) -> dict:
    """把用户在待录入清单里对噪声行的修正/跳过落成该来源组的可复用规则。

    0031 起落在 `source_group_learned_rules` 表（`origin='manual'`，`sort_order`
    追加到该类型末尾），不再是 `SourceGroup.rules` JSON 的三键。rename 需非空
    replace，ignore / exclude 不需要；match / replace 均 trim 校验；来源组不存在
    → 404（**先于 type 校验**，维持旧顺序）。

    可选 note（备注，仅供人看、绝不参与匹配）：存 `note` 列，输出时 ignore/exclude
    无 note 折回裸字符串、有 note 折成 {"match", "note"}，rename 恒为 dict——与拆表前
    的响应形态逐字段一致。

    **幂等**：同组同型同 (match, replace) 已存在 → 不追加、不改写已有 note
    （拆表前是内存判重 + 静默忽略；拆表后由唯一索引
    `(source_group_id, rule_type, match, COALESCE(replace,''))` 承担，外部行为不变）。

    `type=exclude`（「不录入」资产）：match 用资产全名（含组前缀），消费侧
    （compute_preview）精确相等匹配、不做子串。
    """
    if source_group_rules_service.get_group(db, slug) is None:
        raise HTTPException(status_code=404, detail="来源组不存在")   # 旧顺序：组检查先于校验

    rtype = _require_rule_type(data.get("type"))
    match = _require_rule_match(data.get("match"))
    replace_val = _require_rule_replace(rtype, data.get("replace"))
    note = (data.get("note") or "").strip() or None   # 可选备注；仅存储，不参与任何匹配

    def mutator(db_: Session, slug_: str):
        return source_group_rules_service.add_learned_rule(
            db_, slug_, rtype, match, replace_val, note)

    return _mutate_correction_rules(slug, mutator, db)


def delete_correction_rule(slug: str, data: dict, db: Session, *,
                           reason: Optional[str] = None,
                           reason_note: Optional[str] = None) -> dict:
    """删除按身份定位的单条校正规则（表行）。

    body {type, match, replace?}：rename 按 (match, replace)、ignore/exclude 按
    match 定位（与判重语义一致）；定位不到 → 404；成功返回更新后的组序列化。
    """
    rtype = _require_rule_type(data.get("type"))
    match = _require_rule_match(data.get("match"))
    replace = _require_rule_replace(rtype, data.get("replace"))

    def mutator(db_: Session, slug_: str):
        return source_group_rules_service.delete_learned_rule(
            db_, slug_, rtype, match, replace, reason=reason,
            reason_note=reason_note)

    return _mutate_correction_rules(slug, mutator, db)


def patch_correction_rule(slug: str, data: dict, db: Session) -> dict:
    """编辑（含改名/改备注）按旧身份定位的单条校正规则（原地 UPDATE 表行）。

    body {type, match, replace?, note?, new_match?, new_replace?, new_note?}：
    - 旧身份 (match[, replace]) 定位，定位不到 → 404；
    - new_* 缺省 = 维持原值；new_match 提供但为空 → 400；rename 最终 replace
      为空 → 400；
    - note：new_note 优先于 note（两者都缺省则维持原 note）；空串 = 清空 note 列；
    - 编辑后与同类型其他条目身份重复 → 409（自身与自身同值不算重复）；
    - 行原地更新（保留 id / created_at / sort_order / origin / hit_count），
      `updated_at` 刷新。
    成功返回更新后的组序列化对象。
    """
    rtype = _require_rule_type(data.get("type"))
    match = _require_rule_match(data.get("match"))
    old_replace = _require_rule_replace(rtype, data.get("replace"))

    new_match = None
    if data.get("new_match") is not None:
        new_match = _require_rule_match(data.get("new_match"), label="new_match")
    new_replace = None
    has_new_replace = data.get("new_replace") is not None
    if rtype == "rename" and has_new_replace:
        new_replace = data["new_replace"].strip() if isinstance(
            data["new_replace"], str) else ""
        if not new_replace:
            raise HTTPException(status_code=400,
                                detail="rename 规则必须提供非空 replace")
    # note 变更三态：None=不变；""（或全空白）=清空；非空=设置。new_note 优先于 note
    note_change = None
    raw_note = data.get("new_note")
    if raw_note is None:
        raw_note = data.get("note")
    if raw_note is not None:
        note_change = raw_note.strip() if isinstance(raw_note, str) else ""

    def mutator(db_: Session, slug_: str):
        return source_group_rules_service.update_learned_rule(
            db_, slug_, rtype, match, old_replace,
            new_match=new_match, new_replace=new_replace,
            has_new_replace=has_new_replace, note_change=note_change)

    return _mutate_correction_rules(slug, mutator, db)


def swap_correction_rules(slug: str, data: dict, db: Session) -> dict:
    """交换同类型两条校正规则的位置（rename 判重顺序敏感场景）。

    body {type, a: {match, replace?}, b: {match, replace?}}：任一按身份定位
    失败 → 404；a 与 b 身份相同 → 400。实现为「该类型全部行按 sort_order/id 取出
    → 交换两个下标 → 整段重编号 0..n-1」，单个事务内完成，其余规则不丢不动。
    留痕：服务层记 `source_group/update`，after 只有 `{"order": [行 id, ...]}`——
    换序改的是命中顺序，不是规则内容（内容由各自的 create/update 行负责）。
    成功返回更新后的组序列化。
    """
    rtype = _require_rule_type(data.get("type"))
    a, b = data.get("a"), data.get("b")
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise HTTPException(status_code=400, detail="a / b 必须为 {match, replace?} 对象")
    ma = _require_rule_match(a.get("match"), label="a.match")
    mb = _require_rule_match(b.get("match"), label="b.match")
    ra = _require_rule_replace(rtype, a.get("replace"))
    rb = _require_rule_replace(rtype, b.get("replace"))
    if (ma, ra) == (mb, rb):
        raise HTTPException(status_code=400, detail="a 与 b 为同一条规则，无需交换")

    def mutator(db_: Session, slug_: str):
        return source_group_rules_service.swap_learned_rules(
            db_, slug_, rtype, (ma, ra), (mb, rb))

    return _mutate_correction_rules(slug, mutator, db)


@router.post("/source-groups/{slug}/correction-rules", dependencies=[Depends(idempotency_guard("POST /api/screenshots/correction-rules"))])
def http_save_correction_rule(slug: str, data: dict, db: Session = Depends(get_db)):
    return save_correction_rule(slug, data, db)


@router.delete("/source-groups/{slug}/correction-rules", dependencies=[Depends(idempotency_guard("DELETE /api/screenshots/correction-rules"))])
def http_delete_correction_rule(
    slug: str,
    data: dict,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删一条校正规则（`action='delete'` 属纠正类：缺 reason → 422）。"""
    return delete_correction_rule(slug, data, db, reason=reason,
                                  reason_note=reason_note)


@router.patch("/source-groups/{slug}/correction-rules")
def http_patch_correction_rule(slug: str, data: dict, db: Session = Depends(get_db)):
    return patch_correction_rule(slug, data, db)


@router.post("/source-groups/{slug}/correction-rules/swap", dependencies=[Depends(idempotency_guard("POST /api/screenshots/correction-rules/swap"))])
def http_swap_correction_rule(slug: str, data: dict, db: Session = Depends(get_db)):
    return swap_correction_rules(slug, data, db)


# ---------------- AI 一致性校验（verify，纯展示不落库） ----------------
def resolve_verify_provider(db: Session, provider_id: int | None = None) -> dict | None:
    """选出可用的供应商凭证（内部明文 dict，绝不出响应体）。

    显式 provider_id → 该行（不存在或 disabled 视为不可用）；
    缺省 → is_default 且 enabled 的行。无可用返回 None（上层降级 unverified）。
    """
    row = None
    if provider_id is not None:
        got = db.get(LLMProvider, provider_id)
        if got is not None and got.enabled:
            row = got
    elif provider_id is None:
        row = db.execute(
            select(LLMProvider).where(
                LLMProvider.is_default.is_(True), LLMProvider.enabled.is_(True))
        ).scalars().first()
    if row is None:
        return None
    return {"id": row.id, "kind": row.kind, "base_url": row.base_url,
            "api_key": row.api_key, "model": row.model}


def verify_screenshot(rows: list[dict], provider: dict | None,
                      image_bytes: bytes, mime: str) -> dict:
    """校验编排入口（薄于服务层，仅为可测 seam；结果永不落库——硬约束）。

    任何异常（无供应商/无 key/超时/解析失败）→ 全行 verdict=unverified，
    正常 200 返回，不抛 5xx。
    """
    return llm_verification_service.verify_rows(rows, provider, image_bytes, mime)


@router.post("/verify")
async def screenshot_verify(
    file: UploadFile = File(...),
    rows: str = Form("[]"),                 # JSON: [{"index":0,"name":"..","amount":".."}]
    provider_id: str = Form(""),            # 可选；缺省用 is_default 且 enabled 的供应商
    db: Session = Depends(get_db),
):
    try:
        rows_data = json.loads(rows) if rows and rows.strip() else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="rows 不是合法 JSON")
    if not isinstance(rows_data, list):
        raise HTTPException(status_code=400, detail="rows 必须是 JSON 数组")
    pid: int | None = None
    if provider_id and provider_id.strip():
        try:
            pid = int(provider_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="provider_id 必须是整数")

    provider = resolve_verify_provider(db, pid)  # DB 读取留在事件循环线程
    image_bytes = await file.read()
    mime = file.content_type or "image/png"
    # LLM 调用最长 60s 同步阻塞，挪线程池，避免卡死单 worker 事件循环
    return await asyncio.to_thread(verify_screenshot, rows_data, provider, image_bytes, mime)
