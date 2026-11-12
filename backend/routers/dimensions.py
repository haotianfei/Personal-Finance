"""维度只读端点——三组旧维度已废弃（ADR-0019），`/asset-names` 保留。

本模块原有四个 GET，其中三个（fund-types / accounts / liquidity-ratings）与
`routers/management.py` 完全重复，且都读 `attributes(type=...)`。重复即两个真相源，
正是这次重构要消灭的病，因此一并 410 指路到 `/api/classification/*`：

- `fund-types` → `asset_classes`（类别树，含 `code`/`level`/`leaf`）
- `accounts` → 拆成 `institutions`（机构）与 `channels`（渠道）两张表：旧 `account`
  一维里同时装着账户、机构、App/子钱包，这是 ADR-0019 第（3）条问题本身
- `liquidity-ratings` → `liquidity_levels`（8 档固定常量，只读）

`/asset-names` 不是分类维度，是历史记录里的名字清单（`assets.asset_name` 去重），
没有任何替代者，保持原样。
"""
from typing import NoReturn

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError
from models import Asset

router = APIRouter()

REMOVED = "LEGACY_DIMENSION_REMOVED"
CLASSES = "/api/classification/asset-classes"
LIQUIDITY = "/api/classification/liquidity-levels"
INSTITUTIONS = "/api/classification/institutions"
CHANNELS = "/api/classification/channels"


def _gone(message: str, use: str, **extra) -> NoReturn:
    raise ApiError(REMOVED, 410, message, {"use": use, **extra})


@router.get("/fund-types")
def get_fund_types():
    """旧：资产类型树（EAV `type='fund_type'`）。"""
    _gone("资产类型已由资产类别树取代（ADR-0019：8 大类 29 叶子，叶子性由触发器守）。",
          CLASSES)


@router.get("/accounts")
def get_accounts():
    """旧：账户/机构/渠道混装的一维。"""
    _gone("旧的「账户」一维同时装着账户、机构与渠道，已拆成两张字典表。",
          INSTITUTIONS, alternatives=[CHANNELS])


@router.get("/asset-names")
def get_asset_names(db: Session = Depends(get_db)):
    """历史记录里出现过的资产名（去重升序）。分类重构未涉及，保持原语义。"""
    names = db.execute(
        select(distinct(Asset.asset_name)).order_by(Asset.asset_name)
    ).scalars().all()
    return list(names)


@router.get("/liquidity-ratings")
def get_liquidity_ratings():
    """旧：流动性档位（EAV `type='liquidity_rating'`）。"""
    _gone("流动性档位改为产品定义的 8 个固定常量（只读，不建 lookup 表）。", LIQUIDITY)