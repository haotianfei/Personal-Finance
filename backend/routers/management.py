"""基础数据管理端点——整组已废弃（ADR-0019），一律 410 + 指路。

本模块只有两组 CRUD，都直接读写 `attributes`（EAV）：

- `fund-types`（GET/POST/PUT/DELETE）→ 资产类别树
- `accounts`（GET/POST/PUT/DELETE）→ 机构 + 渠道

其中两个 GET 与 `routers/dimensions.py` 完全重复，四个写端点则能凭空造分类节点
（旧 POST 不校验父子一致性、不校验 `side_scope`、不写变更史）。新结构里这些约束
由 `asset_classes` 的触发器（叶子性 / side 一致性）+ `classification_service` 承担，
让旧写端点继续可用＝绕过全部约束，所以整组下线。

**没有第三组功能需要保留**：本文件 181 行里除这两组外没有别的端点。
`/api/management` 的注册暂时保留（返回 410 比 404 信息量大）；
若确认前端与脚本都已迁移，可把本 router 从 `main.py` 整体摘除。
"""
from typing import NoReturn

from fastapi import APIRouter

from errors import ApiError

router = APIRouter()

REMOVED = "LEGACY_DIMENSION_REMOVED"
CLASSES = "/api/classification/asset-classes"
INSTITUTIONS = "/api/classification/institutions"
CHANNELS = "/api/classification/channels"

_FUND_TYPE_MSG = (
    "资产类型字典（attributes.type='fund_type'）已由资产类别树取代（ADR-0019）："
    "类别树的叶子性、侧别一致性与父子环检测都在新服务里，旧的自由增删会绕过它们。"
)
_ACCOUNT_MSG = (
    "账户字典（attributes.type='account'）已拆成「机构」与「渠道」两张表（ADR-0019 D3）："
    "旧一维里混着账户、机构、App/子钱包，无法回答「在哪家机构」。"
)


def _gone(message: str, use: str, **extra) -> NoReturn:
    raise ApiError(REMOVED, 410, message, {"use": use, **extra})


# --- Fund Type Endpoints ---
@router.get("/fund-types")
def get_all_fund_types():
    """旧：列出资产类型。"""
    _gone(_FUND_TYPE_MSG, CLASSES)


@router.post("/fund-types")
def create_fund_type(data: dict | None = None):
    """旧：新建资产类型。"""
    _gone(_FUND_TYPE_MSG, CLASSES)


@router.put("/fund-types/{fund_type_id}")
def update_fund_type(fund_type_id: int, data: dict | None = None):
    """旧：改资产类型（名称/父级/层级，且不改子级 level）。"""
    _gone(_FUND_TYPE_MSG, CLASSES)


@router.delete("/fund-types/{fund_type_id}")
def delete_fund_type(fund_type_id: int):
    """旧：删资产类型。"""
    _gone(_FUND_TYPE_MSG, CLASSES)


# --- Account Endpoints ---
@router.get("/accounts")
def get_all_accounts():
    """旧：列出账户。"""
    _gone(_ACCOUNT_MSG, INSTITUTIONS, alternatives=[CHANNELS])


@router.post("/accounts")
def create_account(data: dict | None = None):
    """旧：新建账户。"""
    _gone(_ACCOUNT_MSG, INSTITUTIONS, alternatives=[CHANNELS])


@router.put("/accounts/{account_id}")
def update_account(account_id: int, data: dict | None = None):
    """旧：改账户名 / `remind_enabled`（该属于「是否纳入核对」，已随 attributes 取消）。"""
    _gone(_ACCOUNT_MSG, INSTITUTIONS, alternatives=[CHANNELS])


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int):
    """旧：删账户。"""
    _gone(_ACCOUNT_MSG, INSTITUTIONS, alternatives=[CHANNELS])