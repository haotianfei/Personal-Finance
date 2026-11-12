"""支出记录 API（契约 §4.3）。

路由声明顺序：静态路径（/generate-preview、/generate、/summary）必须
声明在 /{expense_id} 之前。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from database import get_db
from services.idempotency_service import idempotency_guard
from schemas import (AUDIT_REASON_NOTE_MAX, AuditReason, ExpenseCreate,
                     ExpenseGenerateRequest, ExpenseUpdate)
from services import expense_service

router = APIRouter()


@router.get("")
def list_expenses(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    expense_type: Optional[str] = None,
    category_id: Optional[int] = None,
    account_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    name: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """分页列表（expense_date DESC, id DESC）；非法枚举/日期 → 400"""
    return expense_service.list_expenses(
        db, page=page, page_size=page_size, expense_type=expense_type,
        category_id=category_id, account_id=account_id,
        date_from=date_from, date_to=date_to, name=name,
    )


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(idempotency_guard("POST /api/expenses"))])
def create_expense(payload: ExpenseCreate, db: Session = Depends(get_db)):
    """手动新建支出；template_id/period 为 NULL；同事务留痕 `expense/create`"""
    return expense_service.create_expense(db, payload.model_dump())


@router.get("/generate-preview")
def get_generate_preview(period: str, db: Session = Depends(get_db)):
    """生成预览（仅符合周期与有效期条件的激活模板）；period 非法 → 400"""
    return expense_service.generate_preview(db, period)


@router.post("/generate", dependencies=[Depends(idempotency_guard("POST /api/expenses/generate"))])
def generate_fixed(payload: ExpenseGenerateRequest, db: Session = Depends(get_db)):
    """一键生成；skip/overwrite 冲突处理；(template_id, period) 幂等。

    留痕：1 条 `expense/execute` 头行（after=op/count/ids_hash/scope）＋逐行明细
    （新增 `expense/create`、覆盖 `expense/update`）；全跳过则零审计。
    """
    return expense_service.generate_expenses(db, payload.model_dump())


@router.get("/summary")
def get_expense_summary(
    year: Optional[int] = None,
    month: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """本月/本年汇总 + 固定支出月负担；缺省用服务器当天年月"""
    return expense_service.get_summary(db, year=year, month=month)


@router.put("/{expense_id}", dependencies=[Depends(idempotency_guard("PUT /api/expenses/id"))])
def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    db: Session = Depends(get_db),
):
    """更新支出；仅校验本次提供的字段；404 不存在；留痕 `expense/update`（仅变更字段）"""
    return expense_service.update_expense(db, expense_id,
                                          payload.model_dump(exclude_unset=True))


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(idempotency_guard("DELETE /api/expenses/id"))])
def delete_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删除支出记录；404 不存在；留痕 `expense/delete`（before 全集、after=None）。

    `delete` 属纠正类（spec E1）：缺 reason → 422 `AUDIT_REASON_REQUIRED`。
    """
    expense_service.delete_expense(db, expense_id, reason=reason,
                                   reason_note=reason_note)
    return None
