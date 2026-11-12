"""资产所有者端点 → 家庭成员字典（ADR-0019 D6）：路径保留，语义转接。

旧实现读写 `attributes(type='owner')`——值域是真人姓名行，还夹一个「家庭池」保留名
（ADR-0011 的合成归属）。新模型里归属主体是 `members`，`role ∈ self/spouse/joint/
child/nominee`，`asset_item_owner_changes.owner_id` 外键直指它；「家庭池」概念按 D6
取消，「真人姓名/家庭池混在同一命名空间」的问题随之消失。

**这是转接层，不是第二份实现**：增删改查一律委托 `classification_service` 的 members
接口（与 `/api/classification/members` 同一份校验、同一份引用计数、同一套删除保护），
本模块只做两件事：

1. 响应形态对齐——保留 `{id, name, ...}`（前端仍在读），并补 `role` / `active`；
   其中 **`role` 就是旧代码里的 `owner_kind`**（self/spouse/joint/child/nominee），
   不再有 `family_pool`；
2. 路径兼容——`/api/asset-owners` 继续可用，不新增任何语义。

留痕与 `/api/classification/members` **共用同一批审计行**（ADR-0027）：
`entity='classification'`、`action=create|update|delete` 由 `classification_service`
在同一事务里写。本模块刻意不重复记——否则同一次删除会留两行，读者无法分辨
「转接」与「原生」两套口径。

要写「合成归属」（如家庭共有），走归属变更史 `/api/asset-items/{item_id}/owner`，
那是另一条 record，不该伪装成字典项。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models import Member
from schemas import AUDIT_REASON_NOTE_MAX, AuditReason
from services import classification_service as svc

router = APIRouter()


def _view(db: Session, member_id: int) -> dict:
    """成员 → 旧端点响应形态（id/name 必须保留，role＝旧 owner_kind）。"""
    member = db.get(Member, member_id)
    return {"id": member.id, "name": member.name,
            "role": member.role, "active": member.active}


@router.get("")
def list_asset_owners(db: Session = Depends(get_db)):
    """成员列表（与 `/api/classification/members` 同一份数据）：`[{id,name,role,active}]`。"""
    return svc.list_members(db)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_asset_owner(data: dict, db: Session = Depends(get_db)):
    """新建成员。`role` 必填（self/spouse/joint/child/nominee）——由服务层校验，
    不再有「家庭池」保留名拦截（D6 已退役该保留名）。"""
    created = svc.create_member(db, {"name": data.get("name"), "role": data.get("role")})
    return _view(db, created["id"])


@router.put("/{owner_id}")
def update_asset_owner(owner_id: int, data: dict, db: Session = Depends(get_db)):
    """改名 / 改角色 / 启停用（只透传传进来的字段）。"""
    payload = {k: data.get(k) for k in ("name", "role", "active")
               if data.get(k) is not None}
    svc.update_member(db, owner_id, payload)
    return _view(db, owner_id)


@router.delete("/{owner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset_owner(
    owner_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除成员。被资产项（`asset_items.owner_id`）或归属变更史
    （`asset_item_owner_changes.owner_id`）引用时 409 DICT_IN_USE，不做级联。

    审计原因（spec E3 第 4 组）：与 `/api/classification/members` 共用同一批审计行
    （`entity='classification', action='delete'`），缺 reason → 422
    `AUDIT_REASON_REQUIRED`。转接层只透传，不复制校验。"""
    svc.delete_member(db, owner_id, reason=reason, reason_note=reason_note)