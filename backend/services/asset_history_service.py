"""资产项变更史（取证视图）——票 02 `.scratch/audit-best-practice/issues/02-forensics-view.md`。

一条时间线 ＝ 两个来源合并（裁决 J2/J5）：

- `source="audit"`：`audit_log` 下钻（**操作记录**，两者都有时以审计为准）；
- `source="trace"`：数据自述的四张痕迹表——`asset_item_attr_history`（分类改判）、
  `asset_item_owner_changes`（归属）、`principal_changes`（累计投入本金口径，见 CONTEXT「本金」词条）、
  `asset_item_names`（名史）。

本模块是**唯一出口**：去重、排序、中文词表（`kind`→`title`、字段→`label`）、
游标分页只在这里实现一次（裁决 J4/J5：前端不自造词、不背归并脏活）。

响应契约（前端车道按此编码，逐字冻结，不得增删字段名）：

    {"item_id": 42, "display_name": "…",
     "entries": [{"source": "audit"|"trace", "occurred_at": "…", "kind": "correct",
                  "title": "更正", "actor": "owner", "actor_label": "我",
                  "row_id": 123, "evidence_ref": null, "note": null,
                  "changes": [{"field": "amount", "label": "金额",
                               "from": "1000.00", "to": "1200.00"}],
                  "tech": {"entity": "…", "action": "…", "request_id": "…",
                           "idempotency_key": "…", "prev_hash": "8位", "row_hash": null}}],
     "next_cursor": null, "truncated": false}

归属口径（本域实测，见交付回复偏离清单）：`audit_log.row_id` 的语义随 entity 变——
`asset_item`/`classification` 是资产项 id；`asset`/`asset_snapshot` 是 `assets.id`；
`asset_item_owner` 是 `asset_item_owner_changes.id`；`principal_change` 是
`principal_changes.id`。因此不能只看 `row_id == item_id`，必须按 entity 分派并
用「该资产项名下的行 id 集合」比对；更正链尾行被删除后，靠 `assets.superseded_by`
链仍能归属（否则删除类审计行会永久失联）。
"""
import base64
import binascii
import json
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (Asset, AssetItem, AssetItemAttrHistory, AssetItemName,
                    AssetItemOwnerChange, AuditLog, Member, PrincipalChange)

# ---------------------------------------------------------------- 常量（词表单源）

SOURCES = ("audit", "trace")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEDUPE_WINDOW_SECONDS = 2.0          # 同 kind 且 |Δt| ≤ 2s → 丢 trace（审计为准）

#: kind → 中文标题（受控词表，前端逐字展示，**不自行映射**）。未知 kind 回落「其他」。
TITLES: dict[str, str] = {
    "create": "新增",
    "correct": "更正",
    "delete": "删除",
    "archive": "归档",
    "unarchive": "解归档",
    "owner_change": "归属变更",
    "classification_change": "分类改判",
    "rename": "改名",
    "principal_change": "本金变更",
    "principal_set": "本金设定",
    "merge": "合并",
    "split": "拆分",
    "import": "导入",
    "import_seed": "导入",
    "import_confirm": "导入",
    "batch": "批量操作",
    "batch_create": "批量新增",
    "batch_delete": "批量删除",
    "execute": "批量操作",
    "transfer_register": "划转登记",
    "apply_row": "采纳",
    "restore": "还原",
    "update": "更新",
    "change": "变更",
}

#: 字段名 → 中文标签（`changes[].label` 单源）。未登记字段回落原名（不抛错）。
FIELD_LABELS: dict[str, str] = {
    "amount": "金额",
    "quantity": "数量",
    "price": "单价",
    "unit_price": "单价",
    "category": "分类",
    "asset_class": "资产类别",
    "asset_class_id": "资产类别",
    "owner": "归属",
    "owner_id": "归属",
    "owner_kind": "归属类型",
    "owner_name": "归属",
    "display_name": "显示名",
    "principal": "本金",
    "member": "主体",
    "member_id": "主体",
    "tag": "标签",
    "tags": "标签",
    "tag_id": "标签",
    "asset_date": "日期",
    "date": "日期",
    "effective_date": "日期",
    "name": "名称",
    "asset_name": "名称",
    "item_id": "资产项",
    "asset_item_id": "资产项",
    "note": "备注",
    "reason": "原因",
    "reason_note": "备注",
    "liquidity": "流动性",
    "liquidity_days": "流动性",
    "institution": "机构",
    "institution_id": "机构",
    "channel": "渠道",
    "channel_id": "渠道",
    "risk": "风险等级",
    "risk_level": "风险等级",
    "side": "方向",
    "status": "状态",
    "evidence_id": "凭证",
    "value": "值",
}

#: 项级分类改判的审计快照只含这些维度键（用于把「字典 CRUD 行」与「项级改判行」
#: 分开——两者的 entity 都是 `classification`，只靠 row_id 会与字典 id 撞车）。
_CLASSIFICATION_DIMS = frozenset({
    "asset_class", "liquidity", "institution", "channel", "owner", "risk",
    "tag", "tags",
})

#: 审计 kind 归一：现网拼写 → 取证口径（与 trace 的 kind 同词，去重才成立）。
_AUDIT_KIND = {
    ("asset_item_owner", "change"): "owner_change",
    ("classification", "update"): "classification_change",
    ("classification", "change"): "classification_change",
    ("principal_change", "principal_set"): "principal_change",
    ("batch", "execute"): "batch",
}

_SRC_RANK = {"audit": 0, "trace": 1}


# ---------------------------------------------------------------- 小工具


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _text(value):
    """取值 → 展示文本（金额一律字符串：库里是 Decimal/字符串，绝不经 float）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    return value          # dict/list 原样（前端自行折叠）


def _actor_label(actor: str | None) -> str:
    return {"owner": "我", "system": "系统"}.get(actor or "", actor)


def _to_dt(value) -> datetime | None:
    """ISO 文本/日期时间 → naive datetime（比较与去重统一口径）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _title(kind: str | None) -> str:
    return TITLES.get(kind or "", "其他")


def _clamp_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


# ---------------------------------------------------------------- 游标


def encode_cursor(entry: dict) -> str:
    """条目 → 不透明游标（base64url of `occurred_at|source|id`，无填充）。"""
    raw = f"{entry['occurred_at']}|{entry['source']}|{entry['row_id']}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _parse_cursor(cursor: str) -> tuple[str, str, int]:
    """游标 → (occurred_at, source, id)；非法一律 400 `INVALID_CURSOR`。"""
    raw = str(cursor).strip()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        occurred_at, source, row_id = decoded.split("|", 2)
        if not occurred_at or source not in SOURCES:
            raise ValueError("游标三段非法")
        return occurred_at, source, int(row_id)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ApiError(
            ErrorCode.INVALID_CURSOR, 400, "cursor 非法（应原样回传上一页的 next_cursor）",
            details={"cursor": cursor, "reason": str(exc)}) from exc


def _is_after(entry: dict, cursor: tuple[str, str, int]) -> bool:
    """条目是否落在游标之后（排序＝occurred_at 降序、同刻 audit 先、再 id 降序）。"""
    occurred_at, source, row_id = cursor
    if entry["occurred_at"] != occurred_at:
        return entry["occurred_at"] < occurred_at
    if entry["source"] != source:
        return _SRC_RANK[entry["source"]] > _SRC_RANK[source]
    return entry["row_id"] < row_id


def _sort(entries: list[dict]) -> None:
    """稳定两段排序：先定同刻次序（audit 前、id 大前），再按时间降序。"""
    entries.sort(key=lambda e: (_SRC_RANK[e["source"]], -e["row_id"]))
    entries.sort(key=lambda e: e["occurred_at"], reverse=True)


# ---------------------------------------------------------------- 归属判定（审计源）


def _asset_id_chain(db: Session, item_id: int) -> set[int]:
    """该资产项名下的 `assets.id` 集合（含更正链，尾行被删后仍可归属）。

    更正＝追加新行、旧行 `superseded_by` 指向新行。链上每一行都属于同一个资产项，
    但只有链头（当前 active 行）带 `asset_item_id`；尾行被删除后，它的 id 只能靠
    `superseded_by` 指回来——否则「谁删了这条」在取证视图里永久失联。
    """
    ids = {row[0] for row in db.execute(
        select(Asset.id).where(Asset.asset_item_id == item_id)).all()}
    frontier = set(ids)
    hops = 0
    while frontier and hops < 50:
        hops += 1
        rows = db.execute(
            select(Asset.id, Asset.superseded_by)
            .where(or_(Asset.id.in_(frontier),
                       Asset.superseded_by.in_(frontier)))
        ).all()
        fresh: set[int] = set()
        for asset_id, superseded_by in rows:
            if asset_id not in ids:
                fresh.add(asset_id)
            if superseded_by is not None and superseded_by not in ids:
                fresh.add(superseded_by)
        if not fresh:
            break
        ids |= fresh
        frontier = fresh
    return ids


def _mentions_item(value, item_id: int) -> bool:
    """payload 里是否点名该资产项（键 `asset_item_id`/`item_id`，值精确相等）。

    `assets` 明细行的关键字段里资产项键名是 `item_id`（`asset_service.asset_key_fields`），
    批次头把 `asset_item_id` 放在 `scope` 里——两种拼写都要认。
    """
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ("asset_item_id", "item_id") and isinstance(inner, int) \
                    and not isinstance(inner, bool) and inner == item_id:
                return True
            if _mentions_item(inner, item_id):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_mentions_item(item, item_id) for item in value)
    return False


def _dims_only(before, after) -> bool:
    keys = set()
    for side in (before, after):
        if isinstance(side, dict):
            keys |= set(side)
        elif side is not None:
            return False
    return bool(keys) and keys <= _CLASSIFICATION_DIMS


def _belongs(row: AuditLog, item_id: int, asset_ids: set[int],
             owner_ids: set[int], principal_ids: set[int]) -> bool:
    """该审计行是否属于该资产项（三选一＋real-world 分派；Python 精确判定）。"""
    if row.entity == "asset_item" and row.row_id == item_id:
        return True
    if row.entity in ("asset", "asset_snapshot") and row.row_id in asset_ids:
        return True
    if row.entity == "asset_item_owner" and row.row_id in owner_ids:
        return True
    if row.entity == "principal_change" and row.row_id in principal_ids:
        return True
    before, after = _loads(row.before_json), _loads(row.after_json)
    if row.entity == "classification" and row.row_id == item_id \
            and _dims_only(before, after):
        return True
    return _mentions_item(before, item_id) or _mentions_item(after, item_id)


def _audit_kind(row: AuditLog) -> str:
    mapped = _AUDIT_KIND.get((row.entity, row.action))
    return mapped or row.action or "unknown"


def _changes(before, after) -> list[dict]:
    """审计快照 → 逐字段「旧→新」（ADR-0027 语义：create 全集 / delete 全集 / 其余并集）。"""
    if before is None and after is None:
        return []
    if not isinstance(before, (dict, type(None))) \
            or not isinstance(after, (dict, type(None))):
        # 标量快照（旧改名路径的 before/after 是纯字符串）：单值一条出
        return [{"field": "value", "label": FIELD_LABELS["value"],
                 "from": _text(before), "to": _text(after)}]

    if before is None:
        keys = list(after or {})
    elif after is None:
        keys = list(before)
    else:
        keys = list(before) + [k for k in after if k not in before]
    return [{
        "field": key,
        "label": FIELD_LABELS.get(key, key),
        "from": _text(before.get(key)) if before is not None else None,
        "to": _text(after.get(key)) if after is not None else None,
    } for key in keys]


def _audit_entry(row: AuditLog) -> dict:
    kind = _audit_kind(row)
    before, after = _loads(row.before_json), _loads(row.after_json)
    prev_hash = getattr(row, "prev_hash", None)
    return {
        "source": "audit",
        "occurred_at": row.occurred_at,
        "kind": kind,
        "title": _title(kind),
        "actor": row.actor,
        "actor_label": _actor_label(row.actor),
        "row_id": row.id,
        "evidence_ref": row.evidence_ref,
        "note": getattr(row, "reason_note", None) or getattr(row, "reason", None),
        "changes": _changes(before, after),
        "tech": {
            "entity": row.entity,
            "action": row.action,
            "request_id": row.request_id,
            "idempotency_key": getattr(row, "idempotency_key", None),
            "prev_hash": prev_hash[:8] if prev_hash else None,
            "row_hash": None,
        },
    }


def _audit_entries(db: Session, item_id: int, asset_ids: set[int],
                   owner_ids: set[int], principal_ids: set[int]) -> list[dict]:
    """审计源：先 SQL 粗筛（结构条件＋JSON LIKE），再逐行 Python 精确判定。"""
    conditions = [
        and_(AuditLog.entity == "asset_item", AuditLog.row_id == item_id),
        and_(AuditLog.entity == "classification", AuditLog.row_id == item_id),
    ]
    if asset_ids:
        conditions.append(and_(AuditLog.entity.in_(("asset", "asset_snapshot")),
                               AuditLog.row_id.in_(asset_ids)))
    if owner_ids:
        conditions.append(and_(AuditLog.entity == "asset_item_owner",
                               AuditLog.row_id.in_(owner_ids)))
    if principal_ids:
        conditions.append(and_(AuditLog.entity == "principal_change",
                               AuditLog.row_id.in_(principal_ids)))
    # JSON 预筛：只看「点名了该资产项」的行，避免全表 json.loads（两万行级）。
    # 两种键名 × 两种分隔写法（json.dumps 默认带空格，手工写入的可能不带）；
    # 值前缀会误中 42→"4"，故只做粗筛，精确判定在 _belongs()。
    for key in ("asset_item_id", "item_id"):
        for sep in (": ", ":"):
            needle = f'%"{key}"{sep}{item_id}%'
            conditions.append(AuditLog.before_json.like(needle))
            conditions.append(AuditLog.after_json.like(needle))

    rows = db.execute(select(AuditLog).where(or_(*conditions))).scalars().all()
    return [_audit_entry(row) for row in rows
            if _belongs(row, item_id, asset_ids, owner_ids, principal_ids)]


# ---------------------------------------------------------------- 数据自述（痕迹源）


def _trace_entries(db: Session, item_id: int) -> list[dict]:
    """痕迹源：四张 append-only 表 ∪ 一条链。actor 由写入方语义落定。"""
    entries: list[dict] = []

    # ① 分类改判（asset_item_attr_history）
    for row in db.execute(
        select(AssetItemAttrHistory)
        .where(AssetItemAttrHistory.item_id == item_id)
        .order_by(AssetItemAttrHistory.id)
    ).scalars().all():
        entries.append(_trace_entry(
            occurred_at=_iso(row.changed_at), kind="classification_change",
            actor=row.changed_by or "system", row_id=row.id,
            note=row.reason, table="asset_item_attr_history",
            changes=[{"field": row.dim,
                      "label": FIELD_LABELS.get(row.dim, row.dim),
                      "from": row.old_value, "to": row.new_value}]))

    # ② 归属（asset_item_owner_changes）：按登记序把上一行作为「旧值」
    owner_rows = db.execute(
        select(AssetItemOwnerChange)
        .where(AssetItemOwnerChange.asset_item_id == item_id)
        .order_by(AssetItemOwnerChange.id)
    ).scalars().all()
    member_names = {row[0]: row[1] for row in db.execute(
        select(Member.id, Member.name)).all()}
    previous = None
    for row in owner_rows:
        current = _owner_text(row, member_names)
        entries.append(_trace_entry(
            occurred_at=_iso(row.created_at), kind="owner_change",
            actor="system" if row.origin == "migration" else "owner",
            row_id=row.id, note=row.reason, table="asset_item_owner_changes",
            changes=[{"field": "owner", "label": FIELD_LABELS["owner"],
                      "from": previous, "to": current}]))
        previous = current

    # ③ 本金口径（principal_changes）
    principal_rows = db.execute(
        select(PrincipalChange)
        .where(PrincipalChange.asset_item_id == item_id)
        .order_by(PrincipalChange.id)
    ).scalars().all()
    previous = None
    for row in principal_rows:
        amount = _text(row.new_amount)
        entries.append(_trace_entry(
            occurred_at=_iso(row.created_at), kind="principal_change",
            actor="system" if row.origin in ("transfer", "lineage") else "owner",
            row_id=row.id, note=row.reason, table="principal_changes",
            evidence_ref=row.evidence_ref,
            changes=[{"field": "principal", "label": FIELD_LABELS["principal"],
                      "from": previous, "to": amount}]))
        previous = amount

    # ④ 名史（asset_item_names）：别名沉淀（origin='alias'）不是改名，不入时间线
    name_rows = db.execute(
        select(AssetItemName)
        .where(AssetItemName.item_id == item_id,
               AssetItemName.origin != "alias")
        .order_by(AssetItemName.id)
    ).scalars().all()
    previous = None
    for row in name_rows:
        if previous == row.name:
            previous = row.name
            continue                      # 改名时补登的「旧名行」：与前一条同值，无信息量
        entries.append(_trace_entry(
            occurred_at=_iso(row.created_at), kind="rename",
            actor="system" if row.origin == "migration" else "owner",
            row_id=row.id, note=row.reason_note or row.reason,
            table="asset_item_names",
            changes=[{"field": "display_name", "label": FIELD_LABELS["display_name"],
                      "from": previous, "to": row.name}]))
        previous = row.name

    return entries


def _owner_text(row: AssetItemOwnerChange, member_names: dict) -> str:
    if row.owner_id is None:
        return "家庭池"                      # 存量形状（ADR-0019 D6 已退役的合成归属）
    return member_names.get(row.owner_id) or f"#{row.owner_id}"


def _trace_entry(*, occurred_at: str, kind: str, actor: str, row_id: int,
                 changes: list[dict], table: str, note: str | None = None,
                 evidence_ref: int | None = None) -> dict:
    return {
        "source": "trace",
        "occurred_at": occurred_at,
        "kind": kind,
        "title": _title(kind),
        "actor": actor,
        "actor_label": _actor_label(actor),
        "row_id": row_id,
        "evidence_ref": evidence_ref,
        "note": note,
        "changes": changes,
        "tech": {"entity": table, "action": kind, "request_id": None,
                 "idempotency_key": None, "prev_hash": None, "row_hash": None},
    }


def _drop_echoed_traces(audit_entries: list[dict],
                        trace_entries: list[dict]) -> list[dict]:
    """去重：同 kind 且 |occurred_at 差| ≤ 2 秒的 trace 丢弃（审计为准，裁决 J2）。"""
    index: dict[str, list[datetime]] = {}
    for entry in audit_entries:
        moment = _to_dt(entry["occurred_at"])
        if moment is not None:
            index.setdefault(entry["kind"], []).append(moment)
    for moments in index.values():
        moments.sort()

    kept: list[dict] = []
    for entry in trace_entries:
        moments = index.get(entry["kind"]) or []
        moment = _to_dt(entry["occurred_at"])
        if moment is not None and moments:
            pos = bisect_left(moments, moment)
            near = moments[max(0, pos - 1):pos + 1]
            if any(abs((other - moment).total_seconds()) <= DEDUPE_WINDOW_SECONDS
                   for other in near):
                continue                  # 同一件事：审计行已表达，痕迹行不再重复
        kept.append(entry)
    return kept


def _parse_sources(sources) -> set[str]:
    if sources is None or not str(sources).strip():
        return set(SOURCES)
    parts = {part.strip().lower() for part in str(sources).split(",") if part.strip()}
    unknown = sorted(parts - set(SOURCES))
    if unknown:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422, f"sources 含非法来源：{unknown}",
            details={"fields": [{"field": "sources",
                                 "message": f"只接受 {list(SOURCES)}（逗号分隔）"}]})
    return parts or set(SOURCES)


# ---------------------------------------------------------------- 唯一出口


def timeline(db: Session, item_id: int, *, limit: int = DEFAULT_LIMIT,
             cursor: str | None = None, sources: str | None = None) -> dict:
    """资产项变更史（审计 ∪ 痕迹，去重后单时间线，游标分页）。

    Args:
        item_id: 资产项 id（不存在 → 404，绝不返回空时间线冒充成功）
        limit: 条数上限（默认 50；>200 一律取 200，不做 422）
        cursor: 上一页的 `next_cursor`（不透明串；非法 → 400 INVALID_CURSOR）
        sources: `audit,trace`（默认两者）

    Returns:
        冻结形状的 dict；`truncated=true` 时有 `next_cursor`。
    """
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       details={"item_id": item_id})

    page_limit = _clamp_limit(limit)
    wanted = _parse_sources(sources)
    position = _parse_cursor(cursor) if cursor else None

    entries: list[dict] = []
    traces: list[dict] = []
    if "audit" in wanted:
        asset_ids = _asset_id_chain(db, item_id)
        owner_ids = {row[0] for row in db.execute(
            select(AssetItemOwnerChange.id)
            .where(AssetItemOwnerChange.asset_item_id == item_id)).all()}
        principal_ids = {row[0] for row in db.execute(
            select(PrincipalChange.id)
            .where(PrincipalChange.asset_item_id == item_id)).all()}
        entries = _audit_entries(db, item_id, asset_ids, owner_ids, principal_ids)
    if "trace" in wanted:
        traces = _trace_entries(db, item_id)
    if "audit" in wanted and "trace" in wanted:
        traces = _drop_echoed_traces(entries, traces)
    entries.extend(traces)

    _sort(entries)
    if position is not None:
        entries = [entry for entry in entries if _is_after(entry, position)]

    page = entries[:page_limit]
    truncated = len(entries) > page_limit
    return {
        "item_id": item.id,
        "display_name": item.display_name,
        "entries": page,
        "next_cursor": encode_cursor(page[-1]) if truncated and page else None,
        "truncated": truncated,
    }