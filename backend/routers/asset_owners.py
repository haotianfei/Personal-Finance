from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import AssetOwner, AssetRecord
from schemas import AssetOwnerCreate, AssetOwnerUpdate, AssetOwnerOut

router = APIRouter()


@router.get("", response_model=List[AssetOwnerOut])
def list_asset_owners(
    db: Session = Depends(get_db)
):
    """获取所有资产所有者，按名称排序"""
    owners = db.query(AssetOwner).order_by(AssetOwner.name).all()
    return owners


@router.post("", response_model=AssetOwnerOut, status_code=status.HTTP_201_CREATED)
def create_asset_owner(
    data: AssetOwnerCreate,
    db: Session = Depends(get_db)
):
    """创建新的资产所有者"""
    # 检查名称是否已存在
    existing = db.query(AssetOwner).filter(
        func.lower(AssetOwner.name) == func.lower(data.name)
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"资产所有者 '{data.name}' 已存在"
        )

    owner = AssetOwner(**data.model_dump())
    db.add(owner)
    db.commit()
    db.refresh(owner)
    return owner


@router.put("/{owner_id}", response_model=AssetOwnerOut)
def update_asset_owner(
    owner_id: int,
    data: AssetOwnerUpdate,
    db: Session = Depends(get_db)
):
    """更新资产所有者"""
    owner = db.query(AssetOwner).filter(AssetOwner.id == owner_id).first()
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="资产所有者不存在"
        )

    # 如果修改了名称，检查是否与其他重复
    if data.name is not None and data.name != owner.name:
        existing = db.query(AssetOwner).filter(
            func.lower(AssetOwner.name) == func.lower(data.name),
            AssetOwner.id != owner_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"资产所有者 '{data.name}' 已存在"
            )
        owner.name = data.name

    if data.description is not None:
        owner.description = data.description

    db.commit()
    db.refresh(owner)
    return owner


@router.delete("/{owner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset_owner(
    owner_id: int,
    db: Session = Depends(get_db)
):
    """删除资产所有者（检查是否被使用）"""
    owner = db.query(AssetOwner).filter(AssetOwner.id == owner_id).first()
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="资产所有者不存在"
        )

    # 检查是否被资产记录使用
    usage_count = db.query(AssetRecord).filter(
        AssetRecord.owner_id == owner_id
    ).count()

    if usage_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该资产所有者已被 {usage_count} 条资产记录使用，无法删除"
        )

    db.delete(owner)
    db.commit()
    return None
