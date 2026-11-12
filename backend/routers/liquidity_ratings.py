"""流动性档位端点——读转接分类服务，写全部 410（ADR-0019 D4）。

档位不再是用户数据：它是产品定义的 8 个固定常量（0/1/7/30/90/180/365/9999 天为
`asset_items.liquidity_days`，CHECK 白名单结构性兜住）。旧实现把它当 EAV 字典允许
增删改——删掉一档，已经选了这档的资产项就成了悬空引用，改个名字则等于悄悄篡改
历史口径。因此写端点全部 410。

GET 保留旧路径（前端仍在调），但数据源只有一处：`classification_service.liquidity_levels()`，
与 `/api/classification/liquidity-levels` 同源同值，本模块不做任何加工或改名。
要「改档位」请改资产项本身：`PUT /api/asset-items/{item_id}/classification` 的 `liquidity_days`。
"""
from typing import NoReturn

from fastapi import APIRouter

from errors import ApiError
from services import classification_service

router = APIRouter()

READ_ONLY = "LIQUIDITY_LEVELS_READ_ONLY"
MESSAGE = (
    "流动性档位是产品定义的 8 个固定常量（ADR-0019 D4），不是用户数据：不提供增/改/删。"
    "选档位＝设置资产项的 liquidity_days。"
)
USE = "/api/classification/liquidity-levels"
ITEM_USE = "/api/asset-items/{item_id}/classification"


def _gone() -> NoReturn:
    raise ApiError(READ_ONLY, 410, MESSAGE, {"use": USE, "item_use": ITEM_USE})


@router.get("")
def list_liquidity_ratings():
    """兼容旧路径的只读视图：8 档固定常量，`[{days, label}]`。"""
    return classification_service.liquidity_levels()


@router.post("")
def create_liquidity_rating(data: dict | None = None):
    """旧：新建档位（已禁止）。"""
    _gone()


@router.put("/{rating_id}")
def update_liquidity_rating(rating_id: int, data: dict | None = None):
    """旧：改档位名/描述（已禁止）。"""
    _gone()


@router.delete("/{rating_id}")
def delete_liquidity_rating(rating_id: int):
    """旧：删除档位（已禁止）。"""
    _gone()