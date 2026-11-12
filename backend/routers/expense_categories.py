"""支出分类管理 API（契约 §4.1）。"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from database import get_db
from schemas import (AUDIT_REASON_NOTE_MAX, AuditReason, ExpenseCategoryCreate,
                     ExpenseCategoryUpdate)
from services import expense_service

router = APIRouter()


@router.get("")
def list_expense_categories(db: Session = Depends(get_db)):
    """全部分类（id 升序）"""
    return expense_service.list_categories(db)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_expense_category(
    payload: ExpenseCategoryCreate,
    db: Session = Depends(get_db),
):
    """新建分类；名称重复 → 400；同事务留痕 `expense/create`（口径桶）"""
    return expense_service.create_category(db, payload.model_dump())


@router.put("/{category_id}")
def update_expense_category(
    category_id: int,
    payload: ExpenseCategoryUpdate,
    db: Session = Depends(get_db),
):
    """更新分类；404 不存在；400 名称重复；留痕 `expense/update`（仅变更字段）"""
    return expense_service.update_category(db, category_id,
                                           payload.model_dump(exclude_unset=True))


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense_category(
    category_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除分类；404 不存在；400 被支出或模板引用；留痕 `expense/delete`（before 全集）。

    `delete` 属纠正类（spec E1）：缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    expense_service.delete_category(db, category_id, reason=reason,
                                    reason_note=reason_note)
    return None
