from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from database import get_db
from errors import ApiError, ErrorCode
from schemas import AUDIT_REASON_NOTE_MAX, AuditReason
from services.idempotency_service import idempotency_guard
from services import asset_service


router = APIRouter()

# 记录级分类写入已于 ADR-0019 整体取消（分类只存在于资产项）。旧字段一律显式 422：
# 静默忽略会让前端以为分类存上了，而这正是本次重构要根治的双真源问题。
_CLASSIFICATION_FIELDS = (
    "attribute_ids", "fund_type_id", "fund_type_name", "account_id", "account_name",
    "liquidity_rating_id", "liquidity_rating_name", "owner_id", "tag_id", "tag_ids",
    "asset_class_id", "liquidity_days", "institution_id", "channel_id", "risk_level",
)


def _reject_classification_fields(payload: dict) -> dict:
    """请求体里出现分类字段 → 422（写明新入口）；返回原 payload 供校验后使用。"""
    asset_service._reject_legacy_classification(
        [k for k in payload if k in _CLASSIFICATION_FIELDS], where="资产记录")
    return payload


class AssetCreateRequest(BaseModel):
    """创建资产记录（账本事实）。

    分类（类别/流动性/机构/渠道/归属/标签）**不在记录上**：ADR-0019 把它上移到资产项，
    记录侧只读回显。旧客户端若仍传 attribute_ids/fund_type_id/account_id/… → 422。
    """
    asset_name: str
    amount: str
    asset_date: str
    side: str = "asset"

    @model_validator(mode="before")
    @classmethod
    def _reject_classification(cls, values):
        if isinstance(values, dict):
            _reject_classification_fields(values)
        return values


class AssetUpdateRequest(BaseModel):
    """更新资产记录。账本事实 write-once；分类字段 → 422（改分类去资产项端点）。"""
    asset_name: Optional[str] = None
    amount: Optional[str] = None
    asset_date: Optional[str] = None
    side: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_classification(cls, values):
        if isinstance(values, dict):
            _reject_classification_fields(values)
        return values


class AssetTagsRequest(BaseModel):
    tag_ids: list[int] = []


class AssetTagsBatchRequest(BaseModel):
    record_ids: list[int]
    add_tag_ids: list[int] = []
    remove_tag_ids: list[int] = []


def parse_date(date_str: str | None) -> date | None:
    """解析日期字符串"""
    if date_str is None:
        return None
    if isinstance(date_str, date):
        return date_str
    return date.fromisoformat(str(date_str))


def response_context(db: Session, assets) -> dict:
    """批量取分类展示上下文（列表/快照/批量返回共用，避免 N+1）。"""
    return asset_service.classification_context(
        db, [a.asset_item_id for a in assets if getattr(a, "asset_item_id", None)])


def build_asset_response(db: Session, asset, context: dict | None = None) -> dict:
    """构建资产记录响应（ADR-0019 契约）。

    记录侧**只读回显**分类：类别/流动性/机构/渠道/归属/风险/标签一律取自资产项
    （`classification_service`，批量场景由 `response_context` 一次取齐），记录上不再
    存在 fund_type_*/account_*/liquidity_rating_*/attributes[] 这些旧 EAV 字段。

    `context` 为 `asset_service.classification_context()` 的结果；缺省时按需现取
    （单条端点用，等价于一次分类查询）。
    """
    if context is None:
        context = response_context(db, [asset])
    item_id = asset.asset_item_id
    cls = context["by_item"].get(item_id) if item_id is not None else {}
    cls = cls or {}

    # 处理日期格式
    def format_date(d):
        if d is None:
            return None
        if isinstance(d, date):
            return d.isoformat()
        if isinstance(d, str):
            return d
        return str(d)

    return {
        "id": asset.id,
        "asset_item_id": item_id,
        "asset_name": asset.asset_name,
        # 该项当前显示名（与写入时 name 快照区分；无 item 的存量行回落 asset_name）
        "display_name": cls.get("display_name") or asset.asset_name,
        "amount": str(asset.amount),
        "side": asset.side,
        "asset_date": format_date(asset.asset_date),
        # v5 更正链：非空＝被更正历史行，指向承接新行
        "source_evidence_id": asset.source_evidence_id,
        "superseded_by": asset.superseded_by,
        "is_superseded": asset.superseded_by is not None,
        # ---- 分类（项级只读回显） ----
        "asset_class_id": cls.get("asset_class_id"),
        "asset_class_name": cls.get("asset_class_name"),
        "asset_class_code": cls.get("asset_class_code"),
        "asset_class_path": asset_service.class_path_of(
            context, cls.get("asset_class_id")),
        "liquidity_days": cls.get("liquidity_days"),
        "liquidity_label": cls.get("liquidity_label"),
        "institution_id": cls.get("institution_id"),
        "institution_name": cls.get("institution_name"),
        "channel_id": cls.get("channel_id"),
        "channel_name": asset_service.channel_name_of(context, cls.get("channel_id")),
        "owner_id": cls.get("owner_id"),
        "owner_name": cls.get("owner_name"),
        "owner_kind": asset_service.owner_kind_of(context, cls.get("owner_id")),
        "risk_level": cls.get("risk_level"),
        "tags": cls.get("tags") or [],
        # 必填四维缺哪几个（draft 项补齐即转 active）
        "classification_missing": cls.get("missing_dims") or [],
        "created_at": format_date(asset.created_at),
        "updated_at": format_date(asset.updated_at),
    }


def _q(value, default=None):
    """把未显式传入的 `Query(...)` 默认值归一为 default。

    测试与内部复用会直接调用路由函数（不经 HTTP），此时带 `Query(...)` 默认值的
    形参拿到的是 Query 对象本身而不是 None——不归一就会被当成「传了值」。
    """
    from fastapi.params import Query as _QueryParam
    return default if isinstance(value, _QueryParam) else value


def _id_list(raw: str | None, field: str) -> list[int] | None:
    """逗号分隔 id 串 → list[int]；非法 → 422（不静默丢弃过滤条件）。"""
    raw = _q(raw)
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, f"{field} 含非法 id",
                       details={"fields": [{"field": field,
                                            "message": "id 须为整数，逗号分隔"}]})


def _reject_deprecated_filters(pairs) -> None:
    """旧筛选参数名 → 422（静默忽略会让前端拿到未过滤的全量结果）。

    `pairs` = [(旧参数名, 当前值, 新参数名), ...]；只对真正传了值的旧参数报错。
    """
    present = [(old, new) for old, value, new in pairs if value is not None]
    if not present:
        return
    fields = [{"field": old, "message": f"{old} 已废弃，请改用 {new}"}
              for old, new in present]
    raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                   f"筛选参数已升级：{fields[0]['message']}",
                   {"fields": fields})


@router.get("", response_model=dict)
def list_assets(
    asset_date: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    asset_class_id: Optional[str] = Query(
        None, description="资产类别 id（逗号分隔；含该类别的所有后代）"),
    institution_id: Optional[str] = Query(None, description="机构 id（逗号分隔）"),
    channel_id: Optional[str] = Query(None, description="渠道 id（逗号分隔）"),
    owner_id: Optional[str] = Query(None, description="归属成员 id（逗号分隔）"),
    liquidity_days: Optional[int] = Query(None, description="流动性档位天数（精确匹配）"),
    asset_name: Optional[str] = None,
    item_id: Optional[int] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    sort_field: Optional[str] = None,
    sort_order: Optional[str] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    include_superseded: bool = Query(False, description="含被更正历史行（默认仅 active）"),
    owner_kind: Optional[str] = Query(
        None, description="归属类型过滤：self|spouse|joint|child|nominee（members.role）"),
    tag_ids: Optional[str] = Query(None, description="标签 id（逗号分隔，AND 语义）"),
    source_evidence_id: Optional[int] = Query(
        None, description="来源凭证 id（按截图凭证反查关联行；spec D8，只读筛选）"),
    # ---- 已废弃（保留声明只为把「静默忽略」变成显式 422） ----
    attribute_ids: Optional[str] = None,
    fund_type_id: Optional[str] = None,
    account_id: Optional[str] = None,
    liquidity_rating_id: Optional[str] = None,
    tag_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """列出资产记录（分类过滤走资产项，ADR-0019）。"""
    owner_kind = _q(owner_kind)
    liquidity_days = _q(liquidity_days)
    # 直接调用路由函数（测试/内部复用）时 Query 默认值是对象而非 None，须归一
    source_evidence_id = _q(source_evidence_id)
    if attribute_ids:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 422,
            "attribute_ids 已废弃，请用 asset_class_id / liquidity_days / "
            "institution_id / owner_id / tag_ids",
            details={"fields": [{
                "field": "attribute_ids",
                "message": "attribute_ids 已废弃，请用 asset_class_id / liquidity_days / "
                           "institution_id / owner_id / tag_ids"}]})
    _reject_deprecated_filters([
        ("fund_type_id", fund_type_id, "asset_class_id"),
        ("account_id", account_id, "institution_id"),
        ("liquidity_rating_id", liquidity_rating_id, "liquidity_days"),
        ("tag_id", tag_id, "tag_ids"),
    ])

    assets, total = asset_service.list_assets(
        db, asset_date, date_from, date_to,
        asset_class_ids=_id_list(asset_class_id, "asset_class_id"),
        institution_ids=_id_list(institution_id, "institution_id"),
        channel_ids=_id_list(channel_id, "channel_id"),
        owner_ids=_id_list(owner_id, "owner_id"),
        liquidity_days=liquidity_days,
        asset_name=asset_name, amount_min=amount_min, amount_max=amount_max,
        sort_field=sort_field, sort_order=sort_order, page=page,
        page_size=page_size, item_id=item_id,
        include_superseded=include_superseded,
        owner_kind=owner_kind,
        tag_ids=_id_list(tag_ids, "tag_ids"),
        source_evidence_id=source_evidence_id,
    )

    context = response_context(db, assets)
    items = [build_asset_response(db, asset, context) for asset in assets]
    total_pages = (total + page_size - 1) // page_size

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


@router.get("/dates", response_model=list)
def get_dates(db: Session = Depends(get_db)):
    """获取所有资产日期"""
    dates = asset_service.get_snapshot_dates(db)
    result = []
    for d in dates:
        if isinstance(d, date):
            result.append(d.isoformat())
        elif isinstance(d, str):
            result.append(d)
        else:
            result.append(str(d))
    return result


@router.get("/snapshot/{snapshot_date}", response_model=list)
def get_snapshot(snapshot_date: date, db: Session = Depends(get_db)):
    """获取指定日期的资产快照"""
    assets = asset_service.get_snapshot_assets(db, snapshot_date)
    context = response_context(db, assets)
    return [build_asset_response(db, asset, context)
            for asset in assets]


@router.get("/last-snapshot")
def get_last_snapshot(
    before_date: str,
    period_type: str = "month",
    db: Session = Depends(get_db)
):
    """获取上一个快照"""
    target_date = parse_date(before_date)
    if not target_date:
        raise HTTPException(status_code=400, detail="Invalid before_date")
    
    last_date = asset_service.find_last_snapshot_date(db, target_date, period_type)
    if not last_date:
        return {"date": None, "count": 0}
    
    assets = asset_service.get_snapshot_assets(db, last_date)
    return {"date": last_date.isoformat(), "count": len(assets)}


class CurrentValueUpdateRequest(BaseModel):
    """当前值就地写请求（ADR-0016 §4.2）。

    `idempotency_key` 仅为契约字段兼容（spec 请求示例）；去重仍由既有
    `Idempotency-Key` 请求头机制承担，未新增第二套幂等。
    """
    amount: str
    as_of: Optional[date] = None
    mode: str = "auto"
    side: Optional[str] = None
    idempotency_key: Optional[str] = None


@router.get("/current", response_model=dict)
def get_current_assets(
    as_of: Optional[date] = Query(None, description="参考日，缺省=今天"),
    include_archived: bool = Query(False, description="含已归档资产项（默认不含）"),
    db: Session = Depends(get_db),
):
    """当前值读模型：每资产项一行（截至 as_of 的最新有效记录）。

    每行给出估值事实：`valuation_date`（值来自哪一天）＋ `valuation_age_days`
    （距今多少天）＋ `valuation_stale`（age > 90 天，仅供前端打标）；
    无记录时为 null。旧三态 `freshness` 已随 ADR-0019 D12 删除。
    """
    return asset_service.get_current_values(
        db, as_of=as_of, include_archived=include_archived)


@router.put("/current/{item_id}", response_model=dict,
            dependencies=[Depends(idempotency_guard("PUT /api/assets/current"))])
def update_current_asset(
    item_id: int,
    request: CurrentValueUpdateRequest,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """当前值就地写（auto＝新建或更正；correct_only＝仅更正）。

    走**更正**分支时是纠正类写入（spec E1）：缺 `reason` → 422
    `AUDIT_REASON_REQUIRED`；**新建**分支是 create，不受强制。
    """
    return asset_service.set_current_value(
        db, item_id, amount=request.amount, as_of=request.as_of,
        mode=request.mode, side=request.side, reason=reason,
        reason_note=reason_note or None)


@router.get("/{asset_id}", response_model=dict)
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    """获取单个资产"""
    asset = asset_service.get_asset(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return build_asset_response(db, asset)


@router.post("", response_model=dict, dependencies=[Depends(idempotency_guard("POST /api/assets"))])
def create_asset(request: AssetCreateRequest, db: Session = Depends(get_db)):
    """创建资产记录（分类在资产项上，记录侧不收分类字段）。"""
    from sqlalchemy.exc import IntegrityError as _SAIE
    try:
        asset = asset_service.create_asset(
            db,
            asset_name=request.asset_name,
            amount=request.amount,
            asset_date=parse_date(request.asset_date),
            side=request.side,
        )
    except _SAIE:
        # v5 契约层（spec §2/§3）：同项同日 active 唯一——幂等层失活（旁路/TTL 后）时
        # 天然幂等兜底为显式 409（可安全重试），绝不留 500
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict_detected",
                "message": "同资产项同日已存在有效记录",
                "conflict_records": [{
                    "asset_name": request.asset_name,
                    "amount": str(request.amount),
                    "asset_date": request.asset_date,
                }],
            },
        )

    return build_asset_response(db, asset)


@router.post("/batch", response_model=dict, dependencies=[Depends(idempotency_guard("POST /api/assets/batch"))])
def batch_create(data: dict, db: Session = Depends(get_db)):
    """批量创建资产记录（分类在资产项上，记录侧不收分类字段）。"""
    records = []
    for record in data.get("records", []):
        asset_service._reject_legacy_classification(
            ["attribute_ids"] if record.get("attribute_ids") else [],
            where="资产记录（批量创建）")
        records.append({
            "asset_name": record["asset_name"],
            "amount": record["amount"],
            "asset_date": parse_date(record["asset_date"]),
            "side": record.get("side", "asset"),
            "asset_item_id": record.get("asset_item_id"),
        })
    
    # 获取冲突处理策略，默认为"skip"（v5：overwrite 已冻结移除）
    conflict_strategy = data.get("conflict_strategy", "skip")
    
    try:
        assets, skipped = asset_service.batch_create_assets(db, records, conflict_strategy)
        context = response_context(db, assets)
        return {
            "success": True,
            "created_count": len(assets),
            "skipped": [
                {**s, "asset_date": s["asset_date"].isoformat()
                 if hasattr(s.get("asset_date"), "isoformat") else s.get("asset_date")}
                for s in skipped
            ],
            "records": [
                build_asset_response(db, asset, context) for asset in assets
            ],
        }
    except ValueError as e:
        # 旧客户端仍传 overwrite：功能已冻结移除 → 400（非破坏性引导）
        raise HTTPException(status_code=400, detail=str(e))
    except SAIntegrityError as e:
        # 当使用 "raise" 策略且存在冲突时，返回 409 Conflict
        if conflict_strategy == "raise":
            # 将日期对象转为字符串以便 JSON 序列化
            conflict_records = []
            for record in records:
                conflict_records.append({
                    "asset_name": record["asset_name"],
                    "amount": str(record["amount"]),
                    "asset_date": record["asset_date"].isoformat() if hasattr(record["asset_date"], 'isoformat') else str(record["asset_date"]),
                    "attribute_ids": record.get("attribute_ids", [])
                })
            
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "conflict_detected",
                    "message": "检测到重复记录",
                    "conflict_records": conflict_records
                }
            )
        raise


@router.put("/{asset_id}/tags", response_model=dict,
            dependencies=[Depends(idempotency_guard(
                "PUT /api/assets/{id}/tags"))])
def update_asset_tags(asset_id: int, request: AssetTagsRequest,
                      db: Session = Depends(get_db)):
    """替换**资产项级**标签（按记录反查项；标签挂项不挂记录，ADR-0019 D2/D7）。

    端点入参仍是记录 id（前端不改），但作用对象是该项：一次标注覆盖全历史。
    ≤5 个、归档项 409、审计 before/after 由 `classification_service.set_tags` 统一给。
    """
    result = asset_service.replace_asset_tags(db, asset_id, request.tag_ids)
    if result is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset, changed = result
    response = build_asset_response(db, asset)
    response["tag_ids"] = [t["id"] for t in response["tags"]]
    response["changed"] = changed
    # 作用对象声明：标签已上移到资产项（不是这条记录）
    response["scope"] = "asset_item"
    return response


@router.post("/tags/batch", response_model=dict,
             dependencies=[Depends(idempotency_guard(
                 "POST /api/assets/tags/batch", required=True))])
def batch_asset_tags(request: AssetTagsBatchRequest,
                     db: Session = Depends(get_db)):
    """批量加/减**资产项级**标签（按 asset_item_id 分组，同一项只写一次）。

    逐项独立事务；失败项返回 code。`updated`/`skipped` 仍按记录条数统计
    （入参是记录 id），作用对象是资产项。
    """
    result = asset_service.batch_tag_records(
        db, request.record_ids, request.add_tag_ids, request.remove_tag_ids)
    return {**result, "scope": "asset_item"}


@router.put("/{asset_id}", response_model=dict, dependencies=[Depends(idempotency_guard("PUT /api/assets/id"))])
def update_asset(asset_id: int, request: AssetUpdateRequest, db: Session = Depends(get_db)):
    """更新资产记录（账本事实 write-once → 409 走 correct；分类改资产项端点）"""
    data = request.model_dump(exclude_unset=True)
    
    # 解析日期
    if "asset_date" in data:
        data["asset_date"] = parse_date(data["asset_date"])
    
    try:
        asset = asset_service.update_asset(db, asset_id, **data)
    except asset_service.WriteOnceConflict as e:
        raise HTTPException(status_code=409, detail={
            "error": "write_once",
            "message": str(e),
            "hint": "改金额走 POST /api/assets/{id}/correct；改名走 POST /api/asset-items/{id}/rename",
        })
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    
    return build_asset_response(db, asset)


class AssetCorrectRequest(BaseModel):
    """更正请求（R-4）：金额/负债侧；同日追加＋旧行 supersede。

    `reason`/`reason_note` 也接受**请求体**形态（spec §3-① 的验收示例、以及前端
    `correctAsset(id, {amount, side?, reason, reason_note})` 的既有 payload 形状）；
    位置与取值校验同 query：非法枚举/超长同样 422。两处都给时 **query 优先**
    （query 是六个端点统一形态，body 只是本端点的兼容口）。
    """
    amount: str
    side: Optional[str] = None
    reason: Optional[AuditReason] = None
    reason_note: Optional[str] = Field(default=None, max_length=AUDIT_REASON_NOTE_MAX)


@router.post("/{asset_id}/correct", response_model=dict, dependencies=[Depends(idempotency_guard("POST /api/assets/correct"))])
def correct_asset(
    asset_id: int,
    request: AssetCorrectRequest,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录，不阻塞）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """更正＝追加（write-once 唯一入口）：返回新行＋结构化变更记录（spec §7 行形状）。

    审计原因：**六个端点统一走 query 参数**（`?reason=&reason_note=`）；本端点额外
    兼容请求体同名两字段（spec §3-① 的验收示例与前端 `correctAsset` 既有 payload），
    两处都给时 query 优先。请求体其余部分仍是写事实（amount/side）——理由不是事实的
    一部分；非法枚举/超长由 FastAPI 在进入 service 前 422，故不会留下半条审计行。
    """
    audit_reason = reason if reason is not None else request.reason
    audit_note = reason_note if reason_note is not None else request.reason_note
    try:
        result = asset_service.correct_asset(
            db, asset_id, amount=request.amount, side=request.side,
            reason=audit_reason, reason_note=audit_note or None)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset, change = result
    return {"record": build_asset_response(db, asset), "change": change}


@router.delete("/{asset_id}", dependencies=[Depends(idempotency_guard("DELETE /api/assets/id"))])
def delete_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录，不阻塞）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除资产（reason/reason_note 走 query 参数：DELETE 带 body 兼容性差）。"""
    if not asset_service.delete_asset(db, asset_id, reason=reason,
                                      reason_note=reason_note or None):
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"ok": True}


@router.post("/batch-update", dependencies=[Depends(idempotency_guard("POST /api/assets/batch-update"))])
def batch_update(
    data: dict,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """批量更新资产记录（amount/asset_date/asset_name 禁改 → 409；
    side 可选，携带＝逐行 append 更正（同事务）；非法 side → 400；
    分类字段 → 422，改分类去资产项端点）

    `side` 翻转产生 `action='correct'` 明细行（纠正类）→ 必须带理由，缺则 422
    `AUDIT_REASON_REQUIRED`（spec E1；头行与明细同 reason）。纯 amount/date 等
    只改 `updated_at` 的更新不产 audit 行，故那类调用不受影响。
    """
    record_ids = data.get("ids", [])
    updates = {k: v for k, v in data.items() if k != "ids"}

    try:
        updated_count = asset_service.batch_update_records(
            db, record_ids, updates, reason=reason,
            reason_note=reason_note or None)
    except asset_service.WriteOnceConflict as e:  # 必须先于 InvalidSideError（ValueError 同谱系）
        raise HTTPException(status_code=409, detail={
            "error": "write_once", "message": str(e),
        })
    except asset_service.InvalidSideError as e:  # 精确捕获：其余 ValueError 不再误标 400
        raise HTTPException(status_code=400, detail={
            "error": "invalid_side", "message": str(e),
        })
    return {"updated_count": updated_count}


@router.post("/batch-delete", dependencies=[Depends(idempotency_guard("POST /api/assets/batch-delete"))])
def batch_delete(
    data: dict,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录，不阻塞）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """批量删除资产记录（硬删；ADR-0027：1 条批次头行 + 逐行明细留痕）。

    单次上限 500 行（ADR-0027 D4）→ 超限 400 `BATCH_LIMIT_EXCEEDED`，
    提示「先导出再清理」：一次点错能清掉整本账正是要拦的。

    理由（query 参数）**只落批次头行**（R5）：全批共用一条，逐行明细不带，
    免得 500 行重复同一段文本把审计表灌满。
    """
    if isinstance(data, list):
        record_ids = data
    else:
        record_ids = data.get("ids", [])

    deleted_count = asset_service.batch_delete_records(
        db, record_ids, reason=reason, reason_note=reason_note or None)
    return {"deleted_count": deleted_count}


@router.post("/by-names", response_model=list)
def get_assets_by_names(asset_names: list[str], db: Session = Depends(get_db)):
    """根据名称列表获取资产"""
    assets = asset_service.get_assets_by_asset_names(db, asset_names)
    context = response_context(db, assets)
    return [build_asset_response(db, asset, context) for asset in assets]


@router.post("/history-by-name")
def get_asset_history_by_name(data: dict, db: Session = Depends(get_db)):
    """根据资产名称获取历史记录"""
    asset_name = data.get("asset_name")
    if not asset_name:
        raise HTTPException(status_code=400, detail="asset_name is required")
    
    result = asset_service.get_asset_history_by_name(db, asset_name)
    context = response_context(db, result["sample_records"])
    return {
        "total_count": result["total_count"],
        "sample_records": [
            build_asset_response(db, r, context)
            for r in result["sample_records"]
        ],
        "all_ids": result["all_ids"]
    }
