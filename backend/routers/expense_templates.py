"""固定支出模板 API（契约 §4.2）。"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from database import get_db
from schemas import (AUDIT_REASON_NOTE_MAX, AuditReason, FixedExpenseTemplateCreate,
                     FixedExpenseTemplateUpdate)
from services import expense_service

router = APIRouter()


@router.get("")
def list_expense_templates(
    active: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    """模板列表（id 升序），可按激活状态过滤；反查分类/账户名称"""
    return expense_service.list_templates(db, active=active)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_expense_template(
    payload: FixedExpenseTemplateCreate,
    db: Session = Depends(get_db),
):
    """新建模板；amount>0、cycle 枚举、end≥start、分类/账户校验，非法 → 400；

    同事务留痕 `expense/create`（口径桶）。
    """
    return expense_service.create_template(db, payload.model_dump())


@router.put("/{template_id}")
def update_expense_template(
    template_id: int,
    payload: FixedExpenseTemplateUpdate,
    db: Session = Depends(get_db),
):
    """更新模板；仅校验本次提供的字段；404 不存在；留痕 `expense/update`（仅变更字段）"""
    return expense_service.update_template(db, template_id,
                                           payload.model_dump(exclude_unset=True))


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense_template(
    template_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除模板；生成记录解绑（template_id 置 NULL），不删已生成支出；

    留痕 `expense/delete`（before 全集、after=None）。`delete` 属纠正类
    （spec E1）：缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    expense_service.delete_template(db, template_id, reason=reason,
                                    reason_note=reason_note)
    return None
