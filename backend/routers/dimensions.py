from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select, distinct

from database import get_db
from models import FundType, Account, AssetRecord, LiquidityRating
from schemas import FundTypeOut, AccountOut, LiquidityRatingOut

router = APIRouter()


@router.get("/fund-types", response_model=list[FundTypeOut])
def get_fund_types(db: Session = Depends(get_db)):
    # Return flat list; frontend can build tree from parent_id
    types = db.execute(
        select(FundType).order_by(FundType.level, FundType.name)
    ).scalars().all()
    return types


@router.get("/accounts", response_model=list[AccountOut])
def get_accounts(db: Session = Depends(get_db)):
    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()
    return accounts


@router.get("/asset-names", response_model=list[str])
def get_asset_names(db: Session = Depends(get_db)):
    names = db.execute(
        select(distinct(AssetRecord.asset_name)).order_by(AssetRecord.asset_name)
    ).scalars().all()
    return list(names)


@router.get("/liquidity-ratings", response_model=list[LiquidityRatingOut])
def get_liquidity_ratings(db: Session = Depends(get_db)):
    """获取流动性评级列表（从独立表）"""
    ratings = db.execute(
        select(LiquidityRating).order_by(LiquidityRating.sort_order)
    ).scalars().all()
    return list(ratings)
