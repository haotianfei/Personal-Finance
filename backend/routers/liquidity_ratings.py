from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import LiquidityRating, AssetRecord
from schemas import LiquidityRatingCreate, LiquidityRatingUpdate, LiquidityRatingOut

router = APIRouter()


@router.get("", response_model=List[LiquidityRatingOut])
def list_liquidity_ratings(
    db: Session = Depends(get_db)
):
    """获取所有流动性评级，按排序顺序返回"""
    ratings = db.query(LiquidityRating).order_by(LiquidityRating.sort_order).all()
    return ratings


@router.post("", response_model=LiquidityRatingOut, status_code=status.HTTP_201_CREATED)
def create_liquidity_rating(
    data: LiquidityRatingCreate,
    db: Session = Depends(get_db)
):
    """创建新的流动性评级"""
    # 检查名称是否已存在
    existing = db.query(LiquidityRating).filter(
        func.lower(LiquidityRating.name) == func.lower(data.name)
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"流动性评级 '{data.name}' 已存在"
        )
    
    # 如果没有指定排序顺序，设置为最大值+1
    if data.sort_order == 0:
        max_order = db.query(func.max(LiquidityRating.sort_order)).scalar() or 0
        data.sort_order = max_order + 1
    
    rating = LiquidityRating(**data.model_dump())
    db.add(rating)
    db.commit()
    db.refresh(rating)
    return rating


@router.put("/{rating_id}", response_model=LiquidityRatingOut)
def update_liquidity_rating(
    rating_id: int,
    data: LiquidityRatingUpdate,
    db: Session = Depends(get_db)
):
    """更新流动性评级"""
    rating = db.query(LiquidityRating).filter(LiquidityRating.id == rating_id).first()
    if not rating:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="流动性评级不存在"
        )
    
    # 如果修改了名称，检查是否与其他重复
    if data.name and data.name != rating.name:
        existing = db.query(LiquidityRating).filter(
            func.lower(LiquidityRating.name) == func.lower(data.name),
            LiquidityRating.id != rating_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"流动性评级 '{data.name}' 已存在"
            )
        rating.name = data.name
    
    if data.sort_order is not None:
        rating.sort_order = data.sort_order
    
    db.commit()
    db.refresh(rating)
    return rating


@router.delete("/{rating_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_liquidity_rating(
    rating_id: int,
    db: Session = Depends(get_db)
):
    """删除流动性评级（检查是否被使用）"""
    rating = db.query(LiquidityRating).filter(LiquidityRating.id == rating_id).first()
    if not rating:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="流动性评级不存在"
        )
    
    # 检查是否被资产记录使用
    usage_count = db.query(AssetRecord).filter(
        AssetRecord.liquidity_rating_id == rating_id
    ).count()
    
    if usage_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该流动性评级已被 {usage_count} 条资产记录使用，无法删除"
        )
    
    db.delete(rating)
    db.commit()
    return None
