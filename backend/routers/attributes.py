"""EAV 属性端点——整组已废弃（ADR-0019），一律 410 + 指路。

为什么是 410 而不是删除路由、也不是返回空列表：`attributes` / `asset_attributes`
两表已由 0027 迁移删除，分类改成资产项级六维单值列 + 标签。任何「还能用一点」的
假象都比 404 更坏——旧 POST 曾能凭空造出一个 `fund_type` 节点，调用方会以为写成功。
410 Gone 的语义正好是「这个资源曾经存在，现在永久没有了」，同时给出新去处。

`Attribute` / `AssetAttribute` 两个 ORM 类已随 0027 从 models.py 删除，本模块不引用
任何 ORM，因此也不再有「路由与模型半新半旧」的中间态。
"""
from typing import NoReturn

from fastapi import APIRouter

from errors import ApiError

router = APIRouter()

REMOVED = "EAV_ATTRIBUTES_REMOVED"
MESSAGE = (
    "EAV 属性表（attributes / asset_attributes）已随资产分类重构（ADR-0019）下线："
    "分类改为资产项级六维（类别/流动性/机构/渠道/归属/风险）+ 标签，"
    "值域改为 asset_classes / institutions / channels / members / tags 五张独立字典表。"
)

# 旧 `type` 参数 → 新端点：旧调用方按 type 区分维度，这里给出逐一对照。
BY_OLD_TYPE = {
    "fund_type": "/api/classification/asset-classes",
    "liquidity_rating": "/api/classification/liquidity-levels",
    "account": "/api/classification/institutions",
    "owner": "/api/classification/members",
    "tag": "/api/classification/tags",
}
# 分类挂到资产项上（不再有记录级打点）
ITEM_USE = "/api/asset-items/{item_id}/classification"


def _gone(use: str) -> NoReturn:
    raise ApiError(REMOVED, 410, MESSAGE,
                   {"use": use, "by_old_type": BY_OLD_TYPE, "item_use": ITEM_USE})


@router.get("")
def list_attributes():
    """旧：按 type 分页列 EAV 属性。"""
    _gone("/api/classification/asset-classes")


@router.get("/{attribute_id}")
def get_attribute(attribute_id: int):
    """旧：单个 EAV 属性详情。"""
    _gone("/api/classification/asset-classes")


@router.post("")
def create_attribute(data: dict | None = None):
    """旧：新建 EAV 属性（曾是无校验的字典写入口）。"""
    _gone("/api/classification/asset-classes")


@router.put("/{attribute_id}")
def update_attribute(attribute_id: int, data: dict | None = None):
    """旧：改名/改描述。"""
    _gone("/api/classification/asset-classes")


@router.delete("/{attribute_id}")
def delete_attribute(attribute_id: int):
    """旧：删除属性。"""
    _gone("/api/classification/asset-classes")