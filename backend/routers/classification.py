"""资产分类端点（ADR-0019）。

两个 router：
- `router`：四张字典表 + 流动性档位枚举（前缀 `/api/classification`）
- `item_router`：资产项级的分类与标签读写（前缀 `/api/asset-items`）

为什么分类挂在项上、记录只能读：一个资产项有多条历史记录，分类是这项资产的固有属性，
不是某一天快照的属性。旧模型把分类打到每条记录上，导致同一项前后分类不一致
（实测 229 对只覆盖部分记录）。记录侧保留只读回显（见 assets 响应组装）。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError, ErrorCode
from schemas import (
    AUDIT_REASON_NOTE_MAX,
    AssetClassCreate,
    AssetClassOut,
    AssetClassUpdate,
    AuditReason,
    BatchClassificationInput,
    ChannelCreate,
    ChannelUpdate,
    InstitutionCreate,
    InstitutionUpdate,
    ItemClassificationOut,
    ItemClassificationUpdate,
    ItemTagsUpdate,
    MemberCreate,
    MemberUpdate,
    TagCreate,
    TagUpdate,
)
from services import classification_service as svc

router = APIRouter()
item_router = APIRouter()


# ---------------------------------------------------------------- 资产类别树

@router.get("/asset-classes", response_model=dict)
def list_asset_classes(db: Session = Depends(get_db)):
    """全量返回（平铺 + parent_id/level），树由调用方拼。"""
    rows = svc.list_asset_classes(db)
    return {"items": rows, "total": len(rows)}


@router.post("/asset-classes", status_code=status.HTTP_201_CREATED,
             response_model=AssetClassOut)
def create_asset_class(data: AssetClassCreate, db: Session = Depends(get_db)):
    """返回与列表项同形的完整节点（含 `path`/`leaf`），不是六个字段的阉割版。"""
    return svc.create_asset_class(db, data.model_dump(exclude_none=True))


@router.put("/asset-classes/{class_id}", response_model=AssetClassOut)
def update_asset_class(class_id: int, data: AssetClassUpdate, db: Session = Depends(get_db)):
    return svc.update_asset_class(db, class_id, data.model_dump(exclude_unset=True))


@router.delete("/asset-classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset_class(
    class_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删类别（`action='delete'` 属纠正类：缺 reason → 422 AUDIT_REASON_REQUIRED）。"""
    svc.delete_asset_class(db, class_id, reason=reason, reason_note=reason_note)


# ---------------------------------------------------------------- 流动性档位

@router.get("/liquidity-levels", response_model=dict)
def list_liquidity_levels():
    """固定 8 档。流动性是产品定义不是用户数据，故只读、无增删改。"""
    return {"items": svc.liquidity_levels()}


# ---------------------------------------------------------------- 机构 / 渠道 / 成员 / 标签

@router.get("/institutions", response_model=dict)
def list_institutions(db: Session = Depends(get_db)):
    rows = svc.list_institutions(db)
    return {"items": rows, "total": len(rows)}


@router.post("/institutions", status_code=status.HTTP_201_CREATED)
def create_institution(data: InstitutionCreate, db: Session = Depends(get_db)):
    return svc.create_institution(db, data.model_dump(exclude_none=True))


@router.put("/institutions/{institution_id}")
def update_institution(institution_id: int, data: InstitutionUpdate,
                       db: Session = Depends(get_db)):
    return svc.update_institution(db, institution_id, data.model_dump(exclude_unset=True))


@router.delete("/institutions/{institution_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_institution(
    institution_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    svc.delete_institution(db, institution_id, reason=reason,
                           reason_note=reason_note)


@router.get("/channels", response_model=dict)
def list_channels(db: Session = Depends(get_db)):
    rows = svc.list_channels(db)
    return {"items": rows, "total": len(rows)}


@router.post("/channels", status_code=status.HTTP_201_CREATED)
def create_channel(data: ChannelCreate, db: Session = Depends(get_db)):
    return svc.create_channel(db, data.model_dump(exclude_none=True))


@router.put("/channels/{channel_id}")
def update_channel(channel_id: int, data: ChannelUpdate, db: Session = Depends(get_db)):
    return svc.update_channel(db, channel_id, data.model_dump(exclude_unset=True))


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    svc.delete_channel(db, channel_id, reason=reason, reason_note=reason_note)


@router.get("/members", response_model=dict)
def list_members(db: Session = Depends(get_db)):
    rows = svc.list_members(db)
    return {"items": rows, "total": len(rows)}


@router.post("/members", status_code=status.HTTP_201_CREATED)
def create_member(data: MemberCreate, db: Session = Depends(get_db)):
    return svc.create_member(db, data.model_dump(exclude_none=True))


@router.put("/members/{member_id}")
def update_member(member_id: int, data: MemberUpdate, db: Session = Depends(get_db)):
    return svc.update_member(db, member_id, data.model_dump(exclude_unset=True))


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(
    member_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    svc.delete_member(db, member_id, reason=reason, reason_note=reason_note)


@router.get("/tags", response_model=dict)
def list_tags(db: Session = Depends(get_db)):
    rows = svc.list_tags(db)
    return {"items": rows, "total": len(rows)}


@router.post("/tags", status_code=status.HTTP_201_CREATED)
def create_tag(data: TagCreate, db: Session = Depends(get_db)):
    return svc.create_tag(db, data.model_dump(exclude_none=True))


@router.put("/tags/{tag_id}")
def update_tag(tag_id: int, data: TagUpdate, db: Session = Depends(get_db)):
    return svc.update_tag(db, tag_id, data.model_dump(exclude_unset=True))


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    svc.delete_tag(db, tag_id, reason=reason, reason_note=reason_note)


# ---------------------------------------------------------------- 项级分类

def _parse_ids(ids: str) -> list[int]:
    raw = [part.strip() for part in (ids or "").split(",") if part.strip()]
    if not raw:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 不能为空",
                       details={"fields": [{"field": "ids", "message": "ids 不能为空"}]})
    if len(raw) > 200:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 最多 200 个",
                       details={"fields": [{"field": "ids", "message": "最多 200 个 id"}]})
    try:
        return [int(part) for part in raw]
    except ValueError:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "ids 含非法 id")


@item_router.get("/classifications", response_model=dict)
def bulk_classifications(
    ids: str = Query(..., description="逗号分隔 item id，≤200 个"),
    db: Session = Depends(get_db),
):
    """批量读分类，避免记录页逐项 N+1。

    返回 `{"items": {item_id: <ItemClassificationOut>}}`（对象按 id 索引，不是数组）；
    每项都带 `record_count`，由一次 GROUP BY 取齐，逐项 COUNT 就是 N+1。
    """
    parsed = _parse_ids(ids)
    return {"items": svc.classifications_for_items(db, parsed)}


@item_router.post("/classifications/batch", response_model=dict)
def batch_set_classification(data: BatchClassificationInput,
                             db: Session = Depends(get_db)):
    """按项批量改分类：逐项独立成败，返回 updated 与 failed 清单。

    留痕（ADR-0027 口径桶）：1 条批次头行（`batch/execute`）＋ 逐项
    `classification/update` 明细，由 `classification_service` 在同一事务里写。
    """
    payload = data.model_dump(exclude_unset=True)
    item_ids = payload.pop("item_ids")
    reason = payload.pop("reason", None)
    payload = {k: v for k, v in payload.items() if v is not None}
    if not payload:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "没有要修改的维度")
    return svc.batch_set_classification(db, item_ids, payload, reason=reason)


@item_router.get("/{item_id}/classification", response_model=ItemClassificationOut)
def get_item_classification(item_id: int, db: Session = Depends(get_db)):
    item = svc._require_item(db, item_id)
    return svc.classification_of(db, item)


@item_router.put("/{item_id}/classification", response_model=ItemClassificationOut)
def set_item_classification(
    item_id: int,
    data: ItemClassificationUpdate,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举）。与请求体 `reason`（变更史自由"
                    "文本）不是同一个字段：这里是 query，那边是 body")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """改项级分类。请求体的 `reason` 只给变更史（自由文本），不进审计的封闭枚举。

    审计的 `reason`/`reason_note` 走 **query 参数**（`?reason=&reason_note=`）：
    请求体那个 `reason` 是既有自由文本（进 `asset_item_attr_history.reason`），
    同名不同义，塞进同一个位置会让人以为「随便填」也能进审计枚举。
    缺省（不传）→ 审计行 `reason=NULL`，行为与改版前完全一致。
    """
    payload = data.model_dump(exclude_unset=True)
    history_reason = payload.pop("reason", None)
    return svc.set_classification(
        db, item_id, payload, reason=history_reason,
        audit_reason=reason, audit_reason_note=reason_note or None)


@item_router.get("/{item_id}/classification/history", response_model=dict)
def get_classification_history(item_id: int, limit: int = 200,
                               db: Session = Depends(get_db)):
    return {"items": svc.classification_history(db, item_id, limit)}


@item_router.put("/{item_id}/tags", response_model=dict)
def set_item_tags(item_id: int, data: ItemTagsUpdate, db: Session = Depends(get_db)):
    """整组替换（不是增量）。上限 5 个——与旧记录级标签上限一致。

    留痕：变更史（`dim='tag'`）＋ 审计行（`classification/update`）同事务双写，
    由 `classification_service.set_tags` 统一给。
    """
    return svc.set_tags(db, item_id, data.tag_ids, reason=data.reason)