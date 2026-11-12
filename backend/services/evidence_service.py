"""截图凭证的查询、删除与「已使用」单一定义（evidence-manager D1/D2/D4/D5/D6）。

本模块是 spec D4「已使用」判定的**唯一实现点**：列表筛选（D1）、删除守卫（D5）、
删组守卫（D6）三处必须调用 `used_expr()`，不允许任何一处另写一套 SQL——两套口径
一旦分裂，会出现「列表说未使用、删除时又说被引用」的自相矛盾。

删除相关（D5/D6，票 03）的调用面：
  - `delete_evidence()`：单条删除（未使用才可删；审计 + 删行 + 删文件）
  - `purge_unused()`：批量清理（`dry_run` 默认 True；每条独立事务）
  - `purge_group_evidence()` + `remove_evidence_files()`：删组的显式级联（D6）
删除顺序永远是「先删行（DB）再删文件」：行没了文件还在只是浪费磁盘，反过来就是
「行还在、文件没了」的假证据（spec D5）。

三个口径要分清（易错，改前必读）：

| 名字 | 含义 | 是否含被更正的历史行 |
|---|---|---|
| `used_expr()` | 四处反查任一非空 → 已使用（布尔） | 不适用（只看有无引用） |
| `used_rows_expr()` | `count(assets) where source_evidence_id = e.id` | **含**（全行，不加 superseded 过滤） |
| `usage` 筛选 | `all` / `used`（`used_expr`）/ `unused`（`~used_expr`） | — |

`used_rows` 取全行口径是 spec D1 的明确要求：凭证详情下钻链接固定带
`view=ledger`（`include_superseded=true`），两侧条数必须对齐；否则会出现
「说 N 条、只看到 N-1 条」。故本模块内**绝不**给 `used_rows` 加 `superseded_by`
条件。列表本身的默认 active 口径属 `list_assets` 的事，与本模块无关。

另一块职责（evidence-naming D7 / ADR-0023）：**识别快照**（`recognition_json`）的
组装与写入。`record_recognition()` 是全项目唯一的写入点，`recognized_of()` /
`recognized_of_client()` / `adopted_of()` 是唯一的组装入口；路由侧三个写入点
（`/preview`、预览 job 完成、`/apply`）只许调用它们，不得各自拼 JSON。
"""

from __future__ import annotations

import json
import os
import structlog
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (
    Asset,
    AuditLog,
    PrincipalChange,
    ScreenshotEvidence,
    SourceGroup,
    TransferEvent,
)
from services import audit_service

logger = structlog.get_logger(__name__)

# `usage` 三个取值（spec D1：all 为默认）
USAGE_ALL = "all"
USAGE_USED = "used"
USAGE_UNUSED = "unused"
USAGES = (USAGE_ALL, USAGE_USED, USAGE_UNUSED)

DEFAULT_PAGE_SIZE = 100     # 仓库惯例（对齐 routers/assets.py 的列表分页）
MAX_PAGE_SIZE = 1000

# 批量清理：一次最多删多少条（spec D5：防一次删穿）
DEFAULT_PURGE_LIMIT = 200

# 反查引用源（spec D4 的四处；顺序即 409 details 里「首条引用类型」的取序）
_REF_COLUMNS = {
    "assets": Asset.source_evidence_id,
    "audit_log": AuditLog.evidence_ref,
    "principal_changes": PrincipalChange.evidence_ref,
    "transfer_events": TransferEvent.evidence_ref,
}
_REF_KINDS = tuple(_REF_COLUMNS)

# 凭证原图 content-type：按扩展名判定（spec D2 只承诺 png/jpg/jpeg/webp）
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_FALLBACK_MEDIA_TYPE = "application/octet-stream"


def default_evidence_root() -> str:
    """凭证根目录默认值（与 `routers/screenshots.py:_evidence_root()` 同一路径）。

    两处各写一遍是刻意的：路由侧那份是既有的事实来源、被多处 monkeypatch
    注入 tmp 目录（如 `tests/test_ingest_fix.py`），本模块不能反向 import
    路由（会成环）。路由调用本模块的取图函数时**总是显式传入** `_evidence_root()`，
    所以测试替身照样生效；本默认值只服务「没传 root」的直接调用。
    """
    here = os.path.dirname(os.path.abspath(__file__))        # backend/services
    backend = os.path.dirname(here)                          # backend
    project = os.path.dirname(backend)                       # 仓库根
    return os.path.join(project, "data", "evidence")


# ---------------------------------------------------------------- D4 判定（单源）

def used_expr(evidence_id_col):
    """D4「已使用」的**唯一**定义：四处反查任一非空即已使用。

    **签名稳定（票 03 会 import 本函数，勿改参数形状）**：

        used_expr(evidence_id_col) -> ColumnElement[bool]

    `evidence_id_col`＝待判定的凭证 id 列表达式，通常是 `ScreenshotEvidence.id`
    （也接受外层 join 出来的等价列）。返回可直接放进 `.where()` 的布尔表达式：
    True＝已使用；判「未使用」请用 `~used_expr(...)`（`NOT (EXISTS.. OR ..)`）。

    引用源（spec D4，四处全空才算未使用）：
      1. `assets.source_evidence_id`
      2. `audit_log.evidence_ref`
      3. `principal_changes.evidence_ref`
      4. `transfer_events.evidence_ref`

    后三者当前引用数为 0，但纳入判定是保守正确：一旦审计/本金变更挂上凭证，
    那些图就不该被当垃圾清掉。

    实现用 EXISTS 子查询、判定留在 SQL 层（禁 Python 侧过滤——先拉全量再过滤
    会让分页 `total` 与 `items` 一起错）。子查询只从被引用表取数，对
    `screenshot_evidence` 的引用靠 SQLAlchemy 自动去关联（correlate）到外层。
    """
    return or_(
        select(Asset.id).where(Asset.source_evidence_id == evidence_id_col).exists(),
        select(AuditLog.id).where(AuditLog.evidence_ref == evidence_id_col).exists(),
        select(PrincipalChange.id)
        .where(PrincipalChange.evidence_ref == evidence_id_col).exists(),
        select(TransferEvent.id)
        .where(TransferEvent.evidence_ref == evidence_id_col).exists(),
    )


def used_rows_expr(evidence_id_col):
    """`used_rows`＝`count(assets) where source_evidence_id = e.id` 的标量子查询。

    **全行口径**：不加 `superseded_by IS NULL`（见模块 docstring 的口径表）。
    一次 SELECT 里随主查询算完，**禁止 N+1**（逐条再查会把 100 条列表变成
    101 次往返）。
    """
    return (
        select(func.count(Asset.id))
        .where(Asset.source_evidence_id == evidence_id_col)
        .scalar_subquery()
    )


# ---------------------------------------------------------------- D2/D7 JSON 列序列化

def _json_object(raw: Optional[str], field: str) -> Optional[dict]:
    """TEXT 列 → dict；NULL / 坏 JSON / 非对象 一律 None（列表不因脏数据 500）。

    这两列是**旁证**：解析失败不该让凭证列表整体打不开——与写入侧「可信 > 完备」
    同一条取舍（spec D2：读失败 → NULL，不阻断存证）。
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("evidence_json_unparsable", field=field)
        return None
    return parsed if isinstance(parsed, dict) else None


def image_meta_of(raw: Optional[str]) -> Optional[dict]:
    """`image_meta` 列 → 对象（ADR-0022 白名单 JSON）；未回填/读不出 → None。"""
    return _json_object(raw, "image_meta")


def recognition_summary_of(raw: Optional[str]) -> Optional[dict]:
    """`recognition_json` 列 → **摘要**（ADR-0023 D7）；未识别过/读不出 → None。

    列表只吐摘要、不吐快照本体：完整 JSON 含 OCR 原始块与全部识别行，体积上限 1 MiB，
    100 条一页就是 100 MB 的响应。摘要回答的是「哪一次、什么引擎、识别多少行、
    采纳多少行、块有没有被丢」——够列表用，也够日后识别率粗筛；要逐行对照读库/下载。
    """
    snap = _json_object(raw, "recognition_json")
    if snap is None:
        return None
    rec = snap.get("recognized") if isinstance(snap.get("recognized"), dict) else {}
    adp = snap.get("adopted") if isinstance(snap.get("adopted"), dict) else {}

    def _item_count(container: dict) -> Optional[int]:
        items = container.get("items")
        if isinstance(items, list):
            return len(items)
        # 体积降级后 items 被摘掉、只留条数（见 `_shrink_snapshot`）：条数仍要答得出来
        count = container.get("item_count")
        return count if isinstance(count, int) else None

    return {
        "schema": snap.get("schema"),
        "engine": rec.get("engine") or adp.get("engine"),
        "source": rec.get("source"),
        "parser_version": rec.get("parser_version"),
        "parsed_at": rec.get("parsed_at"),
        "blocks_dropped": bool(rec.get("blocks_dropped", False)),
        "recognized_items": _item_count(rec),
        "adopted_items": _item_count(adp),
        "corrected_rows": adp.get("corrected_rows"),
    }


# ---------------------------------------------------------------- D7 识别快照
# 快照结构（ADR-0023 D7，`schema: 1`）：两段对照——识别出什么（`recognized`）
# 与最终采纳什么（`adopted`）。**本分区是全项目唯一的快照组装/写入点**：
# `routers/screenshots.py` 的三个写入点（/preview、预览 job 完成、/apply）只许
# 调用这里的函数，不得各自拼 dict——两套组装一旦分裂，识别率的分子分母就不再同源。

RECOGNITION_SCHEMA = 1
# 单条快照的硬上限（1 MiB）。**存证优先于存全**：超限降级，绝不因快照太大让上传失败。
RECOGNITION_MAX_BYTES = 1024 * 1024
_RECOGNITION_CLIENT_NOTE = "client-side"


def _now_iso() -> str:
    """快照时间戳：本地时区、秒级 ISO（`2026-11-05T16:40:12`），与全库口径一致。"""
    return datetime.now().isoformat(timespec="seconds")


def _amount_str(value) -> Optional[str]:
    """金额 → 字符串（全项目 JSON 金额口径：不用 float，避免二进制精度尾巴）。"""
    if value is None:
        return None
    return str(value)


def parse_client_recognition(raw: Optional[str]) -> Optional[dict]:
    """前端回传的 `recognition` 表单字段 → 规范化快照原料（ADR-0023 D7 AI 路径）。

    入参是 **JSON 字符串**（multipart 表单字段），两种合法形态：

    - 数组：`[{"name": "…", "amount": "1234.56"}, …]`（最简，前端逐行回传）；
    - 对象：`{"items": [...], "total": "…", "date_text": "…"}`（多带了锚点/日期原文）。

    返回 `{"items": [...], "total": str|None, "date_text": str|None}`；空串/None
    （未传）→ None（上层据此记 `items: null` + `note`，不算错）。

    形状不合法 → `ValueError`（**不静默吞**：调用方转 422 `VALIDATION_ERROR`）。
    容忍度刻意放宽（多键忽略、amount 允许数字/字符串），但 items 必须是对象数组且
    name 非空——回传了空名字说明前端契约已经坏了，早报比存下一堆空行有用。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError("recognition 不是合法 JSON")
    total = None
    date_text = None
    if isinstance(payload, dict):
        items_raw = payload.get("items")
        total = _amount_str(payload.get("total"))
        date_text = payload.get("date_text") if isinstance(
            payload.get("date_text"), str) else None
    elif isinstance(payload, list):
        items_raw = payload
    else:
        raise ValueError("recognition 必须为数组或 {items: [...]} 对象")
    if items_raw is None:
        return {"items": None, "total": total, "date_text": date_text}
    if not isinstance(items_raw, list):
        raise ValueError("recognition.items 必须为数组")
    items: list[dict] = []
    for i, entry in enumerate(items_raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"recognition 第 {i} 条必须为对象")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"recognition 第 {i} 条名称不能为空")
        items.append({"name": name.strip(), "amount": _amount_str(entry.get("amount"))})
    return {"items": items, "total": total, "date_text": date_text}


def recognized_of(parsed, *, engine: str = "ocr") -> dict:
    """服务端识别结果 → `recognized` 段（OCR 路径）。

    `parsed` 是 `screenshot_service.ParsedScreenshot`（管线产物：items / total /
    date_text / raw_blocks）。`blocks` 是该类首次被序列化——存它是为了换规则后
    离线重解析同一张图不必重跑 OCR（ADR-0023 D7）。`blocks_dropped=False` 是
    「块完整保留」的显式标记（体积降级由 `_shrink_snapshot` 翻成 True）。

    `parser_version` 取当时代码的 `PARSER_VERSION`：它回答「这份识别是哪个规则
    版本给的」，是「规则改版前后对比」的锚点，AI 路径同样带（记代码版本）。
    """
    from services import screenshot_service       # 延迟 import：本模块被路由与测试
    # 广泛 import，反向拉入重量级解析模块放到调用时更稳（先例：source_group_rules_service）
    return {
        "engine": engine,
        "source": "server",
        "parser_version": screenshot_service.PARSER_VERSION,
        "parsed_at": _now_iso(),
        "items": [{"name": it.name, "amount": _amount_str(it.amount)}
                  for it in (getattr(parsed, "items", None) or [])],
        "total": _amount_str(getattr(parsed, "total", None)),
        "date_text": getattr(parsed, "date_text", None),
        "blocks": screenshot_service.raw_blocks_to_json(
            getattr(parsed, "raw_blocks", None)),
        "blocks_dropped": False,
    }


def recognized_of_client(client: Optional[dict]) -> dict:
    """前端回传的识别行 → `recognized` 段（AI 路径，`source="client"`）。

    `client` 是 `parse_client_recognition()` 的产物；`None`（未回传）→
    `items: null` + `note: "client-side"`，**不报错**（ADR-0023 D7：要么存得下来，
    要么明确标出这条没存，不允许「AI 结果查不到」这种半覆盖）。

    AI 路径服务端不跑 OCR，因此没有 `blocks`（置 None）——`blocks_dropped` 仍写
    False 表示「不是被体积累计丢掉的，而是本来就没有」。
    """
    from services import screenshot_service

    items = (client or {}).get("items")
    snapshot = {
        "engine": "ai",
        "source": "client",
        "parser_version": screenshot_service.PARSER_VERSION,
        "parsed_at": _now_iso(),
        "items": items,
        "total": (client or {}).get("total"),
        "date_text": (client or {}).get("date_text"),
        "blocks": None,
        "blocks_dropped": False,
    }
    if items is None:
        snapshot["note"] = _RECOGNITION_CLIENT_NOTE
    return snapshot


def adopted_of(rows, *, engine: str, corrected_rows: Optional[int] = None) -> dict:
    """落账本的行 → `adopted` 段（人改后真正写进账本的内容）。

    `rows` 是 `screenshot_service.apply_preview()` 返回的 `result.rows`（已写成
    `asset_name` ＝账本全名的 `PreviewRow` 列表），因此名称与外层那份逐条一致。
    金额按**实际写库**的口径：create/update 取 `source_amount`，zero 写 0
    （与 `apply_preview` 里的 `amount = r.source_amount if r.action in
    ("create","update") else Decimal("0")` 同源，不另立一套换算）。

    `corrected_rows` 由调用方从 `screenshot_service.count_corrected_rows()` 取
    （唯一口径），本函数只负责忠实记录。
    """
    items: list[dict] = []
    for r in rows or []:
        amount = (r.source_amount if getattr(r, "action", None) in ("create", "update")
                  else Decimal("0"))
        try:
            amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            pass                      # 异常金额原样字符串化，绝不因摘要阻断采纳
        items.append({"name": getattr(r, "asset_name", None) or r.source_name,
                      "amount": str(amount)})
    return {
        "engine": engine,
        "item_count": len(items),
        "corrected_rows": corrected_rows,
        "items": items,
    }


def _snapshot_size(payload: dict) -> int:
    """快照序列化后的 UTF-8 字节数（体积上限的判定口径；比字符数更保守）。"""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
               .encode("utf-8"))


def _shrink_snapshot(snapshot: dict) -> dict:
    """超 1 MiB 时按 ADR-0023 D7 逐级降级，返回**必定**可存的快照 dict。

    三级（前一级不达标才进下一级，任何一级都不抛错）：

      1. 原样（块完整）；
      2. 丢 `recognized.blocks`、`blocks_dropped = True`（块是体积大头；识别行仍完整）；
      3. 连行也摘掉，只留条数与摘要：`recognized.items = None` + `item_count`，
         `adopted.items = None` + `item_count`（`corrected_rows` 保留）；
      4. 兜底（理论不可达）：只留 `{"schema", "note": "size_limit"}`。

    「存证优先于存全」：宁可快照残缺，也不能让上传/采纳失败。
    """
    if _snapshot_size(snapshot) <= RECOGNITION_MAX_BYTES:
        return snapshot
    snap = dict(snapshot)
    rec = snap.get("recognized")
    if isinstance(rec, dict) and rec.get("blocks"):
        rec = dict(rec)
        rec["blocks"] = None                 # 丢块但**留键**：读侧一眼看出「块没存」
        rec["blocks_dropped"] = True         # 与原图 sha 一起构成「可复跑 OCR」的线索
        snap["recognized"] = rec
        if _snapshot_size(snap) <= RECOGNITION_MAX_BYTES:
            return snap
    # 仍超限：只留条数与摘要（items 本身可能就是巨量行）
    if isinstance(rec, dict) and isinstance(rec.get("items"), list):
        rec = dict(rec)
        rec["item_count"] = len(rec["items"])
        rec["items"] = None
        rec.setdefault("blocks_dropped", True)
        snap["recognized"] = rec
    adp = snap.get("adopted")
    if isinstance(adp, dict) and isinstance(adp.get("items"), list):
        adp = dict(adp)
        adp["item_count"] = len(adp["items"])
        adp["items"] = None
        snap["adopted"] = adp
    if _snapshot_size(snap) <= RECOGNITION_MAX_BYTES:
        return snap
    logger.warning("evidence_recognition_snapshot_oversize_fallback size=%d",
                   _snapshot_size(snap))
    return {"schema": RECOGNITION_SCHEMA, "note": "size_limit"}


def record_recognition(db: Session, evidence_id: Optional[int], *,
                       recognized: Optional[dict] = None,
                       adopted: Optional[dict] = None) -> None:
    """**唯一**的识别快照写入函数（ADR-0023 D7）——三个写入点都只调这里。

    合并语义（**按段覆盖，不追加**）：
      - 传了 `recognized` → 覆盖 `recognized` 段；
      - 传了 `adopted` → 覆盖 `adopted` 段；
      - 为 None 的段**保留既有值**（不清空）。
    理由：`/preview` 只写 `recognized`、`/apply` 写两段；同 sha 的图再次上传会复用
    同一凭证行（`get_or_create_evidence` 一 sha 一行），如果预览连 `adopted` 一起
    清掉，「机器给的 vs 人落账的」这份对照就没了——那正是快照存在的意义。
    **同一段永远是最新一次覆盖**（同一凭证不按次追加：快照回答「这张图最近一次
    识别/采纳是什么」，历史多次采纳的留痕在 `audit_log`）。

    边界（全部**不抛错**，绝不阻断主流程——上传/采纳优先）：
      - `evidence_id` 为 None（legacy 直调路径）或行不存在（凭证已被清理）→ 静默 no-op；
      - 体积超 `RECOGNITION_MAX_BYTES` → 由 `_shrink_snapshot` 降级后照存；
      - 任何写库异常 → 记 warning 并回滚本次写入后返回。
    调用点的前置保证：进入本函数前，本轮的业务写入（凭证行 / 账本行）均已 commit，
    故这里的 `rollback()` 只丢弃快照本身。
    """
    if evidence_id is None or (recognized is None and adopted is None):
        return
    try:
        ev = db.get(ScreenshotEvidence, evidence_id)
        if ev is None:
            logger.info("evidence_recognition_row_missing id=%s", evidence_id)
            return
        snapshot = _json_object(ev.recognition_json, "recognition_json") or {}
        snapshot["schema"] = RECOGNITION_SCHEMA
        if recognized is not None:
            snapshot["recognized"] = recognized
        if adopted is not None:
            snapshot["adopted"] = adopted
        ev.recognition_json = json.dumps(
            _shrink_snapshot(snapshot), ensure_ascii=False, separators=(",", ":"))
        db.commit()
    except Exception as exc:      # 旁证失败不阻断主流程（与 image_meta 同一条取舍）
        db.rollback()
        logger.warning("evidence_recognition_write_failed id=%s err=%s",
                       evidence_id, exc)


# ---------------------------------------------------------------- D7 单一构造

def evidence_item(ev: ScreenshotEvidence, *, group_id: int, group_slug: str,
                  group_label: str, used_rows: Optional[int]) -> dict:
    """一条凭证的完整形状（spec D7 的**唯一**构造点）。

    列表（`list_evidence`）与单条详情（`get_evidence_detail`）都走这里：形状只写
    一遍，「改一处漏一处」的分叉就没有落脚点——详情端点此前返回另一套扁平形状
    （`source_group` 是 slug 字符串、缺后三个字段），正是两份 dict 各写一遍的结果。

    `file_sha256` **不在**这里：列表项不含它，单条详情按 spec §3 第 5 条
    （单条 = 列表项 ∪ `{file_sha256}`）在自己的出口叠加。

    `used_rows` 由调用方 SQL 的 `used_rows_expr()` 算好传进来（全行口径，禁止 N+1），
    这里只做 `None → 0` 归一：空表时标量子查询可能回 NULL，而契约要求整数 0。
    """
    return {
        "id": ev.id,
        "source_group": {"id": group_id, "slug": group_slug,
                         "label": group_label},
        "file_path": ev.file_path,
        "original_filename": ev.original_filename,
        "data_date": ev.data_date,
        "engine": ev.engine,
        "created_at": ev.created_at,
        "used_rows": used_rows or 0,
        # 加法字段（ADR-0022 / ADR-0023）：未回填/未识别过 → None，不报错
        "image_meta": image_meta_of(ev.image_meta),
        "recognition_summary": recognition_summary_of(ev.recognition_json),
    }


def _evidence_rows_stmt():
    """列表与单条共用的列集：凭证行 + 来源组三列 + `used_rows`。

    只放「两侧必须一致」的部分；筛选/排序/分页仍由 `list_evidence` 自己加，
    故单条详情与列表项不可能因列集不同而形状分裂。
    """
    return (
        select(
            ScreenshotEvidence,
            SourceGroup.id.label("group_id"),
            SourceGroup.slug.label("group_slug"),
            SourceGroup.label.label("group_label"),
            used_rows_expr(ScreenshotEvidence.id).label("used_rows"),
        )
        .join(SourceGroup, SourceGroup.id == ScreenshotEvidence.source_group_id)
    )


# ---------------------------------------------------------------- D1 列表

def list_evidence(db: Session, *,
                  source_group_id: Optional[int] = None,
                  date_from: Optional[date] = None,
                  date_to: Optional[date] = None,
                  usage: str = USAGE_ALL,
                  q: Optional[str] = None,
                  page: int = 1,
                  page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict], int]:
    """凭证列表（spec D1）：返回 `(items, total)`，排序 `id DESC`（新的在前）。

    筛选（全部在 SQL 层，`total` 与 `items` 用同一组条件）：
      - `source_group_id`：来源组 id（精确）
      - `date_from` / `date_to`：按 `data_date`（含端点；`data_date IS NULL` 的
        未采纳凭证不会命中任何日期区间——SQL 三值逻辑的必然结果，属预期）
      - `usage`：`all` | `used` | `unused`（判定走 `used_expr()`，与删除守卫同源）
      - `q`：对 `original_filename` 与 `file_path` 做 `LIKE %q%`（`%`/`_` 不转义，
        视为通配——凭证文件名不受用户控制写入，实际无注入面；如需精确子串匹配再说）

    每条返回 dict：`id / source_group{id,slug,label} / file_path / original_filename /
    data_date / engine / created_at / used_rows / image_meta / recognition_summary`。
    `source_group` 吐组的**当前** slug 与 label（join 取现值，组改名后展示自动跟随）。
    后两个是 evidence-naming 的加法字段（ADR-0022 / ADR-0023）：`image_meta` 是列里的
    白名单对象，`recognition_summary` 是识别快照的**摘要**（完整 JSON 不回列表，见
    `recognition_summary_of`）。

    形状本身由 `evidence_item()` 单点构造（spec D7）：本函数只管筛选/分页/排序，
    单条详情走同一个构造器，两侧不再各写一份 dict。

    非法 `usage` → 422 `VALIDATION_ERROR`（不静默降级为 all：静默会把筛选失效
    藏起来，用户以为筛过了）。
    """
    usage = (usage or USAGE_ALL).strip().lower()
    if usage not in USAGES:
        message = f"usage 必须为 {USAGE_ALL}、{USAGE_USED} 或 {USAGE_UNUSED}"
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, message,
                       {"fields": [{"field": "usage", "message": message}]})
    page = max(1, int(page or 1))
    page_size = min(MAX_PAGE_SIZE, max(1, int(page_size or DEFAULT_PAGE_SIZE)))

    conds = []
    if source_group_id is not None:
        conds.append(ScreenshotEvidence.source_group_id == source_group_id)
    if date_from is not None:
        conds.append(ScreenshotEvidence.data_date >= date_from)
    if date_to is not None:
        conds.append(ScreenshotEvidence.data_date <= date_to)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        conds.append(or_(ScreenshotEvidence.original_filename.like(like),
                         ScreenshotEvidence.file_path.like(like)))
    if usage == USAGE_USED:
        conds.append(used_expr(ScreenshotEvidence.id))
    elif usage == USAGE_UNUSED:
        conds.append(~used_expr(ScreenshotEvidence.id))

    # total 的过滤条件与 items 完全相同；条件不涉及 source_groups，故 count 不 join
    total = db.execute(
        select(func.count(ScreenshotEvidence.id)).where(*conds)
    ).scalar_one()

    rows = db.execute(
        _evidence_rows_stmt()
        .where(*conds)
        .order_by(ScreenshotEvidence.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [
        evidence_item(ev, group_id=group_id, group_slug=group_slug,
                      group_label=group_label, used_rows=used_rows)
        for ev, group_id, group_slug, group_label, used_rows in rows
    ]
    return items, total


def get_evidence_detail(db: Session, evidence_id: int) -> Optional[dict]:
    """单条凭证详情：`evidence_item()` 的产物 + `file_sha256`。

    查不到返回 None，404 文案留给路由（端点语义只有路由层知道）。`file_sha256`
    在这里叠加而不进构造器：它是列表项之外的**唯一**加法字段（spec §6 冻结后
    修订 4），塞进构造器等于让列表也跟着多背一列无人消费的哈希。
    """
    row = db.execute(
        _evidence_rows_stmt().where(ScreenshotEvidence.id == evidence_id)
    ).first()
    if row is None:
        return None
    ev, group_id, group_slug, group_label, used_rows = row
    return {
        **evidence_item(ev, group_id=group_id, group_slug=group_slug,
                        group_label=group_label, used_rows=used_rows),
        "file_sha256": ev.file_sha256,
    }


# ---------------------------------------------------------------- D2 原图

@dataclass(frozen=True)
class EvidenceImage:
    """校验通过的原图定位结果（一次 DB 查询拿到文件与元数据，避免二次往返）。"""

    path: str                            # 已 realpath 的绝对路径
    original_filename: Optional[str]     # 上传时的原始文件名（download 时用）
    media_type: str                      # 按扩展名判定的 content-type


def media_type_for(file_path: str) -> str:
    """按扩展名定 content-type（png/jpg/jpeg/webp，其它 `application/octet-stream`）。"""
    return _MEDIA_TYPES.get(os.path.splitext(file_path or "")[1].lower(),
                            _FALLBACK_MEDIA_TYPE)


def _contained_path(root: str, rel_path: Optional[str]) -> tuple[str, bool]:
    """`root + rel_path` 的 realpath，以及是否仍在 root 之下（路径安全的单点）。

    读（D2 取图）与删（D5 删文件）共用同一判定：判据用
    `target.startswith(root + os.sep)` 而非 `in target`——后者会把
    `/data/evidence-x/...` 误判成根内。`root` 必须是已 realpath 的目录。

    返回 `(target, inside)`：`inside=False` 时调用方各自决定是 400（读）还是
    只记 warning 不删（删）。
    """
    target = os.path.realpath(os.path.join(root, rel_path or ""))
    inside = target == root or target.startswith(root + os.sep)
    return target, inside


def resolve_image_file(db: Session, evidence_id: int, *,
                       evidence_root: Optional[str] = None) -> EvidenceImage:
    """凭证原图的**校验后**绝对路径 + 元数据（spec D2）。

    三道关，顺序不可换（先容器再存在性，否则「存在性」会变成越界探测）：

      1. 行不存在 → 404 `NOT_FOUND`
      2. `realpath(join(root, file_path))` 不在 `realpath(root)` 之下 →
         400 `EVIDENCE_PATH_INVALID`（防 `../` 穿越与符号链接逃逸）。
         判据用 `target.startswith(root + os.sep)`，而不是 `in target`——
         后者会把 `/data/evidence-x/...` 误判成根内。
      3. 文件不存在（或不是普通文件）→ 404 `EVIDENCE_FILE_MISSING`
         （行在盘空是可诊断状态，不该 500）。

    `evidence_root` 缺省用 `default_evidence_root()`；路由侧**显式传入**
    `_evidence_root()`，这样测试对该函数的 monkeypatch 才能生效。
    """
    ev = db.get(ScreenshotEvidence, evidence_id)
    if ev is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"凭证不存在: {evidence_id}")

    root = os.path.realpath(evidence_root or default_evidence_root())
    target, inside = _contained_path(root, ev.file_path)
    if not inside:
        raise ApiError(
            ErrorCode.EVIDENCE_PATH_INVALID, 400,
            "凭证文件路径越出凭证根目录，已阻断读取",
            {"evidence_id": evidence_id})

    if not os.path.isfile(target):
        raise ApiError(
            ErrorCode.EVIDENCE_FILE_MISSING, 404,
            "凭证文件不存在（库里有行、盘上没有文件）",
            {"evidence_id": evidence_id, "file_path": ev.file_path})

    return EvidenceImage(path=target, original_filename=ev.original_filename,
                         media_type=media_type_for(target))


def resolve_image_path(db: Session, evidence_id: int, *,
                       evidence_root: Optional[str] = None) -> str:
    """同上，但只返回绝对路径（票面签名；`resolve_image_file` 的薄封装）。"""
    return resolve_image_file(db, evidence_id,
                              evidence_root=evidence_root).path


# ---------------------------------------------------------------- D5/D6 删除与清理

def is_used(db: Session, evidence_id: int) -> bool:
    """单条凭证是否「已使用」——判定**只能**走 `used_expr()`（spec D4 单源）。

    行不存在返回 False（存在性由调用方先判，二者职责分开：这里只答
    「有没有引用」，不答「在不在」）。
    """
    return bool(db.execute(
        select(used_expr(ScreenshotEvidence.id))
        .where(ScreenshotEvidence.id == evidence_id)
    ).scalar_one_or_none())


def reference_counts(db: Session, evidence_id: int) -> dict[str, int]:
    """四处反查各自的引用计数（只给 409 details 用，**不含任何业务金额**）。

    判定仍以 `used_expr()` 为准，这里是为了让 409 报文能说清「被哪几处、
    各几条引用」——计数查询与人读信息不是第二套判定。
    """
    row = db.execute(select(*[
        select(func.count(col)).where(col == evidence_id).scalar_subquery()
        for col in _REF_COLUMNS.values()
    ])).one()
    return {kind: int(value or 0) for kind, value in zip(_REF_KINDS, row)}


def _audit_evidence_delete(db: Session, ev: ScreenshotEvidence, *,
                           reason: str | None = None,
                           reason_note: str | None = None) -> None:
    """删除凭证的审计行（`entity=screenshot_evidence`, `action=delete`）。

    **`evidence_ref` 必须显式留空**：本行随后被删，审计再指回去就是悬空外键
    （SQLite 开了 `PRAGMA foreign_keys` 时直接 FK 报错、删都删不掉）。
    业务标识放 `before_json`（file_sha256 / file_path / source_group_id）——
    删掉之后「原来那张图是什么」只剩这里可查。

    **3 个删除端点的单点收口**（spec E3）：`action='delete'` 属纠正类，缺 reason
    由 `audit_service.record()` 422 拦下（`AUDIT_REASON_REQUIRED`）。批量 purge
    逐条调用、每条明细行**都带同一 reason**（无头行场景，spec 验收 5）。
    """
    audit_service.record(db, audit_service.make_change(
        "screenshot_evidence", ev.id, "delete",
        before={"file_sha256": ev.file_sha256, "file_path": ev.file_path,
                "source_group_id": ev.source_group_id},
        after=None,
        evidence_ref=None,          # ← 见 docstring：绝不可指向即将被删的凭证
        reason=reason, reason_note=reason_note,
    ))


def _remove_evidence_file(root: str, rel_path: Optional[str]) -> tuple[int, bool]:
    """删一个凭证文件，返回 `(释放字节, 是否真删到了文件)`。

    删除语义（spec D5）：**文件不存在也算成功**（只记日志）——行已提交删除，
    盘上本来就没有文件是可诊断状态，不该让删除失败。越界路径一律不删
    （只 warning）：行照删（它已被判定为垃圾），但绝不因一行脏数据去删根外文件。
    """
    target, inside = _contained_path(root, rel_path)
    if not inside:
        logger.warning("evidence_delete_path_outside_root path=%s", rel_path)
        return 0, False
    try:
        size = os.path.getsize(target)
        os.remove(target)
        return size, True
    except FileNotFoundError:
        logger.info("evidence_delete_file_missing path=%s", rel_path)
        return 0, False
    except OSError as exc:      # 权限/占用/IO：只警告，不回滚已提交的行删除
        logger.warning("evidence_delete_file_failed path=%s err=%s", rel_path, exc)
        return 0, False


def remove_evidence_files(rel_paths, *, evidence_root: Optional[str] = None) -> int:
    """批量删盘上文件（调用方**必须**在 DB commit 之后调），返回释放字节数。"""
    root = os.path.realpath(evidence_root or default_evidence_root())
    freed = 0
    for rel_path in rel_paths or []:
        size, _removed = _remove_evidence_file(root, rel_path)
        freed += size
    return freed


def _file_size(root: str, rel_path: Optional[str]) -> int:
    """文件字节数（不存在/越界 → 0；dry_run 的 freed_bytes 合计用）。"""
    target, inside = _contained_path(root, rel_path)
    if not inside:
        return 0
    try:
        return os.path.getsize(target) if os.path.isfile(target) else 0
    except OSError:
        return 0


def delete_evidence(db: Session, evidence_id: int, *,
                    evidence_root: Optional[str] = None,
                    reason: str | None = None,
                    reason_note: str | None = None) -> dict:
    """删单条凭证（spec D5）：未使用 → 删行 + 审计 + 删文件；已使用 → 409。

    三道关的顺序不可换：

      1. 行不存在 → 404 `NOT_FOUND`
      2. 已使用 → 409 `EVIDENCE_IN_USE`（details 给四处引用计数与首条引用类型，
         **不含业务金额**）。证据链底线，不提供 `force=true` 后门（spec §4）。
      3. 未使用 → 写审计（`evidence_ref` 留空，见 `_audit_evidence_delete`）→
         删行 → **commit** → 再删磁盘文件。

    删除顺序固定「先行后文件」：反过来的话删文件成功、删行失败就留下
    「行还在、文件没了」的假证据；而「行没了、文件还在」只是浪费一块磁盘，
    下次 `purge` 也不会再看见它——两害相权取轻（spec D5）。

    返回 `{ok, id, freed_bytes, file_deleted}`；`file_deleted=False` 表示盘上
    本来就没这个文件或删不掉（均不算失败，行已按未使用删掉）。
    """
    ev = db.get(ScreenshotEvidence, evidence_id)
    if ev is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"凭证不存在: {evidence_id}")

    if is_used(db, evidence_id):
        counts = reference_counts(db, evidence_id)
        first_kind = next((k for k in _REF_KINDS if counts.get(k)), None)
        raise ApiError(
            ErrorCode.EVIDENCE_IN_USE, 409,
            f"凭证 {evidence_id} 已被引用，不能删除（证据链）",
            {"evidence_id": evidence_id, "ref_counts": counts,
             "first_ref_kind": first_kind})

    rel_path = ev.file_path
    _audit_evidence_delete(db, ev, reason=reason, reason_note=reason_note)
    db.delete(ev)
    db.commit()

    freed, removed = _remove_evidence_file(
        os.path.realpath(evidence_root or default_evidence_root()), rel_path)
    return {"ok": True, "id": evidence_id,
            "freed_bytes": freed, "file_deleted": removed}


def purge_unused(db: Session, *, source_group_id: Optional[int] = None,
                 dry_run: bool = True, limit: int = DEFAULT_PURGE_LIMIT,
                 evidence_root: Optional[str] = None,
                 reason: str | None = None,
                 reason_note: str | None = None) -> dict:
    """批量清理未使用凭证（spec D5）：`dry_run` 默认 **True**（只出清单不删）。

    - 候选集＝`~used_expr()`（+ 可选 `source_group_id`），按 id 升序，`limit` 截断；
      `total` 是**未截断**的候选总数，`truncated` 标出本次是否只拿到前 `limit` 条。
    - `dry_run=True`：库与盘零变化，只 stat 文件算 `freed_bytes`（`file_bytes`
      按磁盘实际大小，盘上缺文件的记 0）。
    - `dry_run=False`：逐条调 `delete_evidence()` —— **每条一个独立事务**
      （内部 commit），单条失败进 `failed[]` 且不影响其余；`freed_bytes` 是
      实际删掉的文件字节合计（验收要求：磁盘下降量 == 该值）。

    响应（`items` 供前端把「将删清单」摆给用户确认，ticket 04）：
    `{dry_run, deleted, failed[{id,reason}], freed_bytes, items[{id,file_path,file_bytes}],
      total, truncated, limit}`
    """
    root = os.path.realpath(evidence_root or default_evidence_root())
    limit = max(1, int(limit or DEFAULT_PURGE_LIMIT))

    conds = [~used_expr(ScreenshotEvidence.id)]
    if source_group_id is not None:
        conds.append(ScreenshotEvidence.source_group_id == source_group_id)

    total = int(db.execute(
        select(func.count(ScreenshotEvidence.id)).where(*conds)
    ).scalar_one())
    rows = db.execute(
        select(ScreenshotEvidence.id, ScreenshotEvidence.file_path)
        .where(*conds).order_by(ScreenshotEvidence.id).limit(limit)
    ).all()
    items = [{"id": row.id, "file_path": row.file_path,
              "file_bytes": _file_size(root, row.file_path)} for row in rows]
    truncated = total > len(items)

    if dry_run:
        return {"dry_run": True, "deleted": 0, "failed": [],
                "freed_bytes": sum(i["file_bytes"] for i in items),
                "items": items, "total": total, "truncated": truncated,
                "limit": limit}

    deleted, freed, failed = 0, 0, []
    for item in items:
        try:
            result = delete_evidence(db, item["id"], evidence_root=root,
                                     reason=reason, reason_note=reason_note)
        except ApiError as exc:                     # 已被引用/已被删：只跳过这一条
            db.rollback()
            failed.append({"id": item["id"], "reason": exc.message})
        except Exception as exc:                    # 其它单条故障同样不拖累其余
            db.rollback()
            logger.warning("evidence_purge_item_failed id=%s err=%s", item["id"], exc)
            failed.append({"id": item["id"],
                           "reason": f"{type(exc).__name__}: {exc}"})
        else:
            deleted += 1
            freed += result["freed_bytes"]

    return {"dry_run": False, "deleted": deleted, "failed": failed,
            "freed_bytes": freed, "items": items, "total": total,
            "truncated": truncated, "limit": limit}


def purge_group_evidence(db: Session, source_group_id: int, *,
                         evidence_root: Optional[str] = None,
                         reason: str | None = None,
                         reason_note: str | None = None) -> tuple[int, list[str]]:
    """删组用（spec D6 `purge_unused=true`）：删该组**未使用**凭证的行，返回
    `(条数, 待删文件相对路径)`。

    **不提交**：行删除与「删组」必须同一事务（要么组和凭证一起没了，要么都没动）。
    文件删除是另一件事——由调用方在 `commit()` 之后调 `remove_evidence_files()`：
    先删文件后回滚就留下「行还在、文件没了」的假证据（spec D5 的删除顺序）。

    已使用凭证**永不**在此路径被删（`~used_expr()` 硬拦）；组里有已使用凭证时
    调用方应直接 409，根本走不到这里。
    """
    root = os.path.realpath(evidence_root or default_evidence_root())
    rows = db.execute(
        select(ScreenshotEvidence)
        .where(ScreenshotEvidence.source_group_id == source_group_id,
               ~used_expr(ScreenshotEvidence.id))
        .order_by(ScreenshotEvidence.id)
    ).scalars().all()
    for ev in rows:
        # 与单条删除同一审计形状（evidence_ref 留空）；每条明细行都带同一 reason
        _audit_evidence_delete(db, ev, reason=reason, reason_note=reason_note)
        db.delete(ev)
    if rows:
        db.flush()                          # 子行先落 DELETE，再删组（FK 依赖顺序）
    return len(rows), [ev.file_path for ev in rows]