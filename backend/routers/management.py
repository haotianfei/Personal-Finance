from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Optional

from database import get_db
from models import FundType, Account
from schemas import FundTypeOut, AccountOut, BaseModel

router = APIRouter()


# --- Fund Type Schemas ---
class FundTypeCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None
    level: int = 0


class FundTypeUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    level: Optional[int] = None


# --- Account Schemas ---
class AccountCreate(BaseModel):
    name: str


class AccountUpdate(BaseModel):
    name: Optional[str] = None


# --- Fund Type Endpoints ---
@router.get("/fund-types", response_model=List[FundTypeOut])
def get_all_fund_types(db: Session = Depends(get_db)):
    """获取所有资产类型"""
    types = db.execute(
        select(FundType).order_by(FundType.level, FundType.name)
    ).scalars().all()
    return types


@router.post("/fund-types", response_model=FundTypeOut)
def create_fund_type(data: FundTypeCreate, db: Session = Depends(get_db)):
    """创建新的资产类型"""
    # Check if name already exists
    existing = db.execute(
        select(FundType).where(FundType.name == data.name)
    ).scalar_one_or_none()
    
    if existing:
        raise HTTPException(status_code=400, detail="资产类型名称已存在")
    
    # Validate parent_id if provided
    parent = None
    if data.parent_id is not None:
        parent = db.get(FundType, data.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="父级资产类型不存在")
    
    fund_type = FundType(
        name=data.name,
        parent_id=data.parent_id,
        level=data.level or (parent.level + 1 if parent else 0)
    )
    db.add(fund_type)
    db.commit()
    db.refresh(fund_type)
    return fund_type


@router.put("/fund-types/{fund_type_id}", response_model=FundTypeOut)
def update_fund_type(fund_type_id: int, data: FundTypeUpdate, db: Session = Depends(get_db)):
    """更新资产类型"""
    fund_type = db.get(FundType, fund_type_id)
    if not fund_type:
        raise HTTPException(status_code=404, detail="资产类型不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(fund_type, field, value)
    
    db.commit()
    db.refresh(fund_type)
    return fund_type


@router.delete("/fund-types/{fund_type_id}")
def delete_fund_type(fund_type_id: int, db: Session = Depends(get_db)):
    """删除资产类型"""
    fund_type = db.get(FundType, fund_type_id)
    if not fund_type:
        raise HTTPException(status_code=404, detail="资产类型不存在")
    
    # Check if it has children
    children = db.execute(
        select(FundType).where(FundType.parent_id == fund_type_id)
    ).scalars().all()
    
    if children:
        raise HTTPException(status_code=400, detail="无法删除包含子级的资产类型")
    
    # Check if it's used in any records
    if fund_type.records:
        raise HTTPException(status_code=400, detail="该资产类型已被使用，无法删除")
    
    db.delete(fund_type)
    db.commit()
    return {"ok": True}


# --- Account Endpoints ---
@router.get("/accounts", response_model=List[AccountOut])
def get_all_accounts(db: Session = Depends(get_db)):
    """获取所有账户"""
    accounts = db.execute(
        select(Account).order_by(Account.name)
    ).scalars().all()
    return accounts


@router.post("/accounts", response_model=AccountOut)
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    """创建新账户"""
    # Check if name already exists
    existing = db.execute(
        select(Account).where(Account.name == data.name)
    ).scalar_one_or_none()
    
    if existing:
        raise HTTPException(status_code=400, detail="账户名称已存在")
    
    account = Account(name=data.name)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/accounts/{account_id}", response_model=AccountOut)
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    """更新账户"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(account, field, value)
    
    db.commit()
    db.refresh(account)
    return account


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账户"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    # Check if it's used in any records
    if account.records:
        raise HTTPException(status_code=400, detail="该账户已被使用，无法删除")
    
    db.delete(account)
    db.commit()
    return {"ok": True}
