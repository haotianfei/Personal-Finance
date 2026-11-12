"""支出录入业务逻辑（契约见 .scratch/expense-entry/spec.md §3-§5）。

- 支出是流水、资产是快照；金额正数存储、两位小数字符串序列化（沿用 assets 约定）。
- 生成记录与模板以 (template_id, period) 唯一约束保证幂等。
- 错误一律 HTTPException(status_code, detail=中文说明)。

ADR-0027（审计留痕契约）——本模块是「值桶」与部分「口径桶」的写入面：
- 每个写函数在 `db.commit()` **之前** `audit_service.record()`，与业务写入同事务
  （业务回滚 → 审计不留；审计写失败 → 整事务失败，可信 > 便利）。
- 快照分型（D5）：create→`after` 关键字段；update→**仅变更字段**；
  delete→`before` 关键字段全集、`after=None`。
- 金额一律 `fmt_amount()`（字符串两位小数）——审计行里绝不出现 float。
- 校验失败（400）与不存在（404）都在**任何写入之前**抛出 → 天然不产审计行；
  无实际变更的 update（同值重放）也不落行（无事实可记）。
- `entity` 统一 `'expense'`（分类/模板/流水三类正交，靠 `after` 形状区分）。
"""
import calendar
import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Expense, ExpenseCategory, FixedExpenseTemplate, Institution
from services import audit_service
from services.audit_service import make_change as _change

EXPENSE_TYPES = {"fixed", "large_oneoff"}
EXPENSE_CYCLES = {"monthly", "quarterly", "yearly"}
CONFLICT_RESOLUTIONS = {"skip", "overwrite"}

TWO_DP = Decimal("0.01")


# ---------------- 通用校验/序列化 ----------------

def _q2(amount: Decimal) -> Decimal:
    return amount.quantize(TWO_DP)


def parse_amount(value, field: str = "金额") -> Decimal:
    """字符串 → Decimal(0.01)，≤0 或非法一律 400「金额必须大于0」（§5.2）。"""
    if value is None or str(value).strip() == "":
        raise HTTPException(status_code=400, detail="金额必须大于0")
    try:
        amt = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="金额必须大于0")
    if amt <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于0")
    return _q2(amt)


def parse_date(value, field: str = "日期") -> date:
    """ISO 日期字符串解析，失败 → 400。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field}格式非法，应为 YYYY-MM-DD")


def validate_category(db: Session, category_id) -> "ExpenseCategory | None":
    if category_id is None:
        return None
    cat = db.get(ExpenseCategory, int(category_id))
    if not cat:
        raise HTTPException(status_code=400, detail="支出分类不存在")
    return cat


def validate_account(db: Session, account_id) -> "Institution | None":
    """账户维度＝机构（0027 起 `expenses.account_id` 外键指 institutions）。"""
    if account_id is None:
        return None
    inst = db.get(Institution, int(account_id))
    if not inst:
        raise HTTPException(status_code=400, detail="支出账户不存在或不是机构")
    return inst


def fmt_amount(value) -> str:
    return str(_q2(Decimal(str(value))))


def fmt_dt(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _category_name(db: Session, category_id) -> str | None:
    if category_id is None:
        return None
    cat = db.get(ExpenseCategory, category_id)
    return cat.name if cat else None


def _account_name(db: Session, account_id) -> str | None:
    if account_id is None:
        return None
    inst = db.get(Institution, account_id)
    return inst.name if inst else None


def serialize_category(cat: ExpenseCategory) -> dict:
    return {
        "id": cat.id,
        "name": cat.name,
        "description": cat.description,
        "created_at": fmt_dt(cat.created_at),
        "updated_at": fmt_dt(cat.updated_at),
    }


def serialize_template(db: Session, t: FixedExpenseTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "amount": fmt_amount(t.amount),
        "cycle": t.cycle,
        "start_date": fmt_dt(t.start_date),
        "end_date": fmt_dt(t.end_date),
        "category_id": t.category_id,
        "category_name": _category_name(db, t.category_id),
        "account_id": t.account_id,
        "account_name": _account_name(db, t.account_id),
        "note": t.note,
        "active": bool(t.active),
        "created_at": fmt_dt(t.created_at),
        "updated_at": fmt_dt(t.updated_at),
    }


def serialize_expense(db: Session, e: Expense) -> dict:
    return {
        "id": e.id,
        "expense_date": fmt_dt(e.expense_date),
        "expense_name": e.expense_name,
        "amount": fmt_amount(e.amount),
        "expense_type": e.expense_type,
        "category_id": e.category_id,
        "category_name": _category_name(db, e.category_id),
        "account_id": e.account_id,
        "account_name": _account_name(db, e.account_id),
        "note": e.note,
        "template_id": e.template_id,
        "period": e.period,
        "created_at": fmt_dt(e.created_at),
        "updated_at": fmt_dt(e.updated_at),
    }


# ---------------- 审计留痕（ADR-0027：值桶 + 口径桶） ----------------

def ids_fingerprint(ids) -> str:
    """批次指纹：排序去重后 id 清单的 sha256 前 16 位。

    与 `asset_service.ids_fingerprint` **同口径**（同一份 canonical 串），此处本地
    实现是为了不把 `asset_service` 的重依赖链（分类/分析/归属）拉进支出路径；
    口径一致性由 `tests/test_audit_coverage_expense_export.py` 直接比对两侧结果守护。
    """
    payload = ",".join(str(i) for i in sorted({int(i) for i in ids}))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def expense_key_fields(e: Expense) -> dict:
    """支出流水审计明细的「关键字段」（D6：create/delete 口径）。

    金额走 `fmt_amount()` 出两位小数字符串（Decimal 直出保精度，绝不经 float）；
    日期出 ISO 串（与 `serialize_expense` 一致，审计读者不必解析类型）。
    """
    return {
        "id": e.id,
        "expense_date": fmt_dt(e.expense_date),
        "expense_name": e.expense_name,
        "amount": fmt_amount(e.amount),
        "expense_type": e.expense_type,
        "category_id": e.category_id,
        "account_id": e.account_id,
        "note": e.note,
        "template_id": e.template_id,
        "period": e.period,
    }


def category_key_fields(cat: ExpenseCategory) -> dict:
    """支出分类（口径桶）审计关键字段。"""
    return {"id": cat.id, "name": cat.name, "description": cat.description}


def template_key_fields(t: FixedExpenseTemplate) -> dict:
    """固定支出模板（口径桶）审计关键字段；金额/日期同 expense 的序列化约定。"""
    return {
        "id": t.id,
        "name": t.name,
        "amount": fmt_amount(t.amount),
        "cycle": t.cycle,
        "start_date": fmt_dt(t.start_date),
        "end_date": fmt_dt(t.end_date),
        "category_id": t.category_id,
        "account_id": t.account_id,
        "note": t.note,
        "active": bool(t.active),
    }


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """update 分型（D5）：**只留真变了的字段**，before/after 同键集。

    同值重放（前端把未改的字段一起回传）不产噪声行；两侧都空＝无事实变更。
    """
    changed = [key for key in after if before.get(key) != after[key]]
    return ({key: before.get(key) for key in changed},
            {key: after[key] for key in changed})


def _create_change(fields: dict) -> dict:
    """create 行：`after`＝关键字段快照（`id` 同时充当 `row_id`）。"""
    return _change("expense", fields["id"], "create", after=fields)


def _update_change(fields_before: dict, fields_after: dict) -> dict | None:
    """update 行：仅变更字段；无变更返回 None（调用方据此决定是否落行）。"""
    before_changed, after_changed = _changed_fields(fields_before, fields_after)
    if not after_changed:
        return None
    return _change("expense", fields_after["id"], "update",
                   before=before_changed, after=after_changed)


def _delete_change(fields: dict, reason: str | None = None,
                   reason_note: str | None = None) -> dict:
    """delete 行：`before`＝关键字段全集，`after=None`。

    `delete` 属纠正类动作（ADR-0027 D3）：**应当**说明原因，但运行时绝不强制
    ——`reason` 由调用方给，缺省 None＝NULL「未记录原因」（现网请求体还没有该字段）。
    管道先铺好：等 UI 补 reason 时只改请求层，服务层零改。
    """
    return _change("expense", fields["id"], "delete", before=fields, after=None,
                   reason=reason, reason_note=reason_note)


# ---------------- 支出分类 ----------------

def _ensure_category_name_unique(db: Session, name: str, exclude_id: int | None = None):
    q = select(ExpenseCategory).where(func.lower(ExpenseCategory.name) == func.lower(name))
    if exclude_id is not None:
        q = q.where(ExpenseCategory.id != exclude_id)
    if db.execute(q).scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"支出分类 '{name}' 已存在")


def list_categories(db: Session) -> list[dict]:
    cats = db.execute(
        select(ExpenseCategory).order_by(ExpenseCategory.id)
    ).scalars().all()
    return [serialize_category(c) for c in cats]


def create_category(db: Session, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    _ensure_category_name_unique(db, name)
    cat = ExpenseCategory(name=name, description=data.get("description"))
    db.add(cat)
    db.flush()                                    # 先取主键，审计行要 row_id
    audit_service.record(db, _create_change(category_key_fields(cat)))
    db.commit()
    db.refresh(cat)
    return serialize_category(cat)


def update_category(db: Session, category_id: int, data: dict) -> dict:
    cat = db.get(ExpenseCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="支出分类不存在")
    before = category_key_fields(cat)
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="分类名称不能为空")
        if name != cat.name:
            _ensure_category_name_unique(db, name, exclude_id=category_id)
            cat.name = name
    if "description" in data:
        cat.description = data["description"]
    change = _update_change(before, category_key_fields(cat))
    if change is not None:
        audit_service.record(db, change)
    db.commit()
    db.refresh(cat)
    return serialize_category(cat)


def delete_category(db: Session, category_id: int, *,
                    reason: str | None = None,
                    reason_note: str | None = None) -> None:
    cat = db.get(ExpenseCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="支出分类不存在")
    used_expense = db.execute(
        select(func.count()).select_from(Expense).where(Expense.category_id == category_id)
    ).scalar() or 0
    used_template = db.execute(
        select(func.count()).select_from(FixedExpenseTemplate)
        .where(FixedExpenseTemplate.category_id == category_id)
    ).scalar() or 0
    if used_expense or used_template:
        raise HTTPException(status_code=400, detail="该分类已被使用，无法删除")
    change = _delete_change(category_key_fields(cat),      # 先取快照：删除后属性不可靠
                            reason=reason, reason_note=reason_note)
    db.delete(cat)
    db.flush()
    audit_service.record(db, change)
    db.commit()


# ---------------- 固定支出模板 ----------------

def _validate_cycle(cycle) -> str:
    if cycle not in EXPENSE_CYCLES:
        raise HTTPException(status_code=400, detail="周期非法，须为 monthly/quarterly/yearly")
    return cycle


def _validate_date_range(start: date, end: date | None):
    if end is not None and end < start:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")


def list_templates(db: Session, active: bool | None = None) -> list[dict]:
    q = select(FixedExpenseTemplate).order_by(FixedExpenseTemplate.id)
    if active is not None:
        q = q.where(FixedExpenseTemplate.active == active)
    rows = db.execute(q).scalars().all()
    return [serialize_template(db, t) for t in rows]


def create_template(db: Session, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    amount = parse_amount(data.get("amount"))
    cycle = _validate_cycle(data.get("cycle"))
    start = parse_date(data.get("start_date"), "开始日期")
    end = parse_date(data["end_date"], "结束日期") if data.get("end_date") else None
    _validate_date_range(start, end)
    validate_category(db, data.get("category_id"))
    validate_account(db, data.get("account_id"))
    t = FixedExpenseTemplate(
        name=name,
        amount=amount,
        cycle=cycle,
        start_date=start,
        end_date=end,
        category_id=data.get("category_id"),
        account_id=data.get("account_id"),
        note=data.get("note"),
        active=bool(data.get("active", True)),
    )
    db.add(t)
    db.flush()                                    # 先取主键，审计行要 row_id
    audit_service.record(db, _create_change(template_key_fields(t)))
    db.commit()
    db.refresh(t)
    return serialize_template(db, t)


def update_template(db: Session, template_id: int, data: dict) -> dict:
    t = db.get(FixedExpenseTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="固定支出项目不存在")
    before = template_key_fields(t)
    # 仅校验本次提供的字段（§4.2）
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        t.name = name
    if "amount" in data and data["amount"] is not None:
        t.amount = parse_amount(data["amount"])
    if "cycle" in data and data["cycle"] is not None:
        t.cycle = _validate_cycle(data["cycle"])
    start = t.start_date
    if "start_date" in data and data["start_date"] is not None:
        start = parse_date(data["start_date"], "开始日期")
    end = t.end_date
    if "end_date" in data:
        end = parse_date(data["end_date"], "结束日期") if data["end_date"] else None
    _validate_date_range(start, end)
    t.start_date = start
    t.end_date = end
    if "category_id" in data:
        validate_category(db, data["category_id"])
        t.category_id = data["category_id"]
    if "account_id" in data:
        validate_account(db, data["account_id"])
        t.account_id = data["account_id"]
    if "note" in data:
        t.note = data["note"]
    if "active" in data and data["active"] is not None:
        t.active = bool(data["active"])
    change = _update_change(before, template_key_fields(t))
    if change is not None:
        audit_service.record(db, change)
    db.commit()
    db.refresh(t)
    return serialize_template(db, t)


def delete_template(db: Session, template_id: int, *,
                    reason: str | None = None,
                    reason_note: str | None = None) -> None:
    """删除模板：先将其生成记录解绑（template_id=NULL），不删已生成支出（§5.3）。"""
    t = db.get(FixedExpenseTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="固定支出项目不存在")
    change = _delete_change(template_key_fields(t),        # 删除前取快照
                            reason=reason, reason_note=reason_note)
    db.query(Expense).filter(Expense.template_id == template_id).update(
        {Expense.template_id: None}, synchronize_session=False
    )
    db.delete(t)
    db.flush()
    audit_service.record(db, change)
    db.commit()


# ---------------- 支出记录 ----------------

def _validate_expense_type(expense_type) -> str:
    if expense_type not in EXPENSE_TYPES:
        raise HTTPException(status_code=400, detail="支出类型非法，须为 fixed/large_oneoff")
    return expense_type


def list_expenses(db: Session, page: int = 1, page_size: int = 100,
                  expense_type: str | None = None,
                  category_id: int | None = None,
                  account_id: int | None = None,
                  date_from: str | None = None,
                  date_to: str | None = None,
                  name: str | None = None) -> dict:
    conds = []
    if expense_type is not None:
        conds.append(Expense.expense_type == _validate_expense_type(expense_type))
    if category_id is not None:
        conds.append(Expense.category_id == category_id)
    if account_id is not None:
        conds.append(Expense.account_id == account_id)
    if date_from:
        conds.append(Expense.expense_date >= parse_date(date_from, "起始日期"))
    if date_to:
        conds.append(Expense.expense_date <= parse_date(date_to, "截止日期"))
    if name:
        conds.append(func.lower(Expense.expense_name).contains(name.lower()))

    total = db.execute(
        select(func.count()).select_from(Expense).where(*conds)
    ).scalar() or 0
    rows = db.execute(
        select(Expense).where(*conds)
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    total_pages = (total + page_size - 1) // page_size
    return {
        "items": [serialize_expense(db, e) for e in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def create_expense(db: Session, data: dict) -> dict:
    expense_name = (data.get("expense_name") or "").strip()
    if not expense_name:
        raise HTTPException(status_code=400, detail="支出名称不能为空")
    e = Expense(
        expense_date=parse_date(data.get("expense_date"), "支出日期"),
        expense_name=expense_name,
        amount=parse_amount(data.get("amount")),
        expense_type=_validate_expense_type(data.get("expense_type")),
        category_id=validate_category(db, data.get("category_id")).id
        if data.get("category_id") is not None else None,
        account_id=validate_account(db, data.get("account_id")).id
        if data.get("account_id") is not None else None,
        note=data.get("note"),
    )
    db.add(e)
    db.flush()                                    # 先取主键，审计行要 row_id
    audit_service.record(db, _create_change(expense_key_fields(e)))
    db.commit()
    db.refresh(e)
    return serialize_expense(db, e)


def update_expense(db: Session, expense_id: int, data: dict) -> dict:
    e = db.get(Expense, expense_id)
    if not e:
        raise HTTPException(status_code=404, detail="支出记录不存在")
    before = expense_key_fields(e)
    if "expense_name" in data and data["expense_name"] is not None:
        name = str(data["expense_name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="支出名称不能为空")
        e.expense_name = name
    if "expense_date" in data and data["expense_date"] is not None:
        e.expense_date = parse_date(data["expense_date"], "支出日期")
    if "amount" in data and data["amount"] is not None:
        e.amount = parse_amount(data["amount"])
    if "expense_type" in data and data["expense_type"] is not None:
        e.expense_type = _validate_expense_type(data["expense_type"])
    if "category_id" in data:
        validate_category(db, data["category_id"])
        e.category_id = data["category_id"]
    if "account_id" in data:
        validate_account(db, data["account_id"])
        e.account_id = data["account_id"]
    if "note" in data:
        e.note = data["note"]
    change = _update_change(before, expense_key_fields(e))
    if change is not None:
        audit_service.record(db, change)
    db.commit()
    db.refresh(e)
    return serialize_expense(db, e)


def delete_expense(db: Session, expense_id: int, *,
                   reason: str | None = None,
                   reason_note: str | None = None) -> None:
    e = db.get(Expense, expense_id)
    if not e:
        raise HTTPException(status_code=404, detail="支出记录不存在")
    change = _delete_change(expense_key_fields(e),         # 删除前取快照
                            reason=reason, reason_note=reason_note)
    db.delete(e)
    db.flush()
    audit_service.record(db, change)
    db.commit()


# ---------------- 一键生成（§4.3 / §5.1） ----------------

def parse_period(period: str) -> tuple[date, date]:
    """YYYY-MM → (当月首日, 当月末日)，非法 → 400。"""
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=400, detail="period 格式非法，应为 YYYY-MM")
    year, month = int(period[:4]), int(period[5:])
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="period 格式非法，应为 YYYY-MM")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def month_index(d: date) -> int:
    return d.year * 12 + (d.month - 1)


def is_eligible(t: FixedExpenseTemplate, period_start: date, period_end: date) -> bool:
    """生成资格（§5.1）：激活 + 有效期与当月重叠 + 周期对齐。"""
    if not t.active:
        return False
    if t.start_date > period_end:
        return False
    if t.end_date is not None and t.end_date < period_start:
        return False
    if t.cycle == "monthly":
        return True
    if t.cycle == "quarterly":
        return (month_index(period_start) - month_index(t.start_date)) % 3 == 0
    if t.cycle == "yearly":
        return period_start.month == t.start_date.month
    return False


def clamped_expense_date(t: FixedExpenseTemplate, period_start: date,
                         period_end: date) -> date:
    """模板 start_date 的「日」clamp 到当月最后一天（31 日 → 2 月 28/29）。"""
    day = min(t.start_date.day, period_end.day)
    return date(period_start.year, period_start.month, day)


def _find_existing(db: Session, template_id: int, period: str) -> Expense | None:
    return db.execute(
        select(Expense).where(Expense.template_id == template_id, Expense.period == period)
    ).scalar_one_or_none()


def generate_preview(db: Session, period: str) -> dict:
    period_start, period_end = parse_period(period)
    templates = db.execute(
        select(FixedExpenseTemplate)
        .where(FixedExpenseTemplate.active == True)  # noqa: E712
        .order_by(FixedExpenseTemplate.id)
    ).scalars().all()
    items = []
    for t in templates:
        if not is_eligible(t, period_start, period_end):
            continue
        existing = _find_existing(db, t.id, period)
        items.append({
            "template_id": t.id,
            "name": t.name,
            "amount": fmt_amount(t.amount),
            "cycle": t.cycle,
            "category_id": t.category_id,
            "category_name": _category_name(db, t.category_id),
            "account_id": t.account_id,
            "account_name": _account_name(db, t.account_id),
            "note": t.note,
            "status": "exists" if existing else "new",
            "existing_expense_id": existing.id if existing else None,
            "expense_date": clamped_expense_date(t, period_start, period_end).isoformat(),
        })
    return {
        "period": period,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "items": items,
    }


def generate_expenses(db: Session, data: dict) -> dict:
    """按 period 生成固定支出记录；每条 item 独立处理（skip/overwrite）。

    审计（ADR-0027 批次约定，本端点 entity 固定 `expense`）：
    - **逐行明细**：新增行 `expense/create`（after＝关键字段），覆盖行
      `expense/update`（仅变更字段）——明细回答「动了哪几行」。
    - **批次头**：`expense/execute`，`row_id=None`，`after={op:'create', count,
      ids_hash, scope:{template_ids, months, created/overwritten/skipped, 冲突策略}}`
      ——头行回答「这一次点了什么」。
    - 头行在明细**之后**落库：`count`/`ids_hash` 只有跑完循环才确定（与
      asset_service 的批删不同，那里 ids 是入参）。头行始终由 `after.op='create'`
      标记（契约表冻结值），混合覆盖时靠 `scope.overwritten` 区分。
    - 全部 item 都被跳过（零写入）→ 不落任何审计行（无事实可记）。
    - 中途抛错 → 本函数不 commit，调用方回滚后**审计与业务行一起消失**。
    """
    period = data.get("period")
    period_start, period_end = parse_period(period)
    conflict = data.get("conflict_resolution") or "skip"
    if conflict not in CONFLICT_RESOLUTIONS:
        raise HTTPException(status_code=400, detail="冲突处理方式非法，须为 skip/overwrite")
    items = data.get("items")
    if items is None or not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items 必须为列表")

    created = overwritten = skipped = 0
    records: list[dict] = []
    touched_ids: list[int] = []
    touched_template_ids: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="items 元素必须为对象")
        template_id = item.get("template_id")
        t = db.get(FixedExpenseTemplate, int(template_id)) if template_id is not None else None
        if t is None or not is_eligible(t, period_start, period_end):
            skipped += 1
            continue
        # 覆盖值先校验（非法 → 400，不静默跳过）
        amount = parse_amount(item["amount"]) if item.get("amount") is not None \
            else _q2(Decimal(str(t.amount)))
        if item.get("expense_date") is not None:
            expense_date = parse_date(item["expense_date"], "支出日期")
        else:
            expense_date = clamped_expense_date(t, period_start, period_end)

        existing = _find_existing(db, t.id, period)
        if existing is not None:
            if conflict == "skip":
                skipped += 1
                continue
            before = expense_key_fields(existing)
            existing.expense_name = t.name
            existing.amount = amount
            existing.expense_date = expense_date
            existing.expense_type = "fixed"
            existing.category_id = t.category_id
            existing.account_id = t.account_id
            existing.note = t.note
            overwritten += 1
            db.flush()
            change = _update_change(before, expense_key_fields(existing))
            if change is not None:
                audit_service.record(db, change)
            touched_ids.append(existing.id)
            touched_template_ids.append(t.id)
            records.append(serialize_expense(db, existing))
        else:
            e = Expense(
                expense_date=expense_date,
                expense_name=t.name,
                amount=amount,
                expense_type="fixed",
                category_id=t.category_id,
                account_id=t.account_id,
                note=t.note,
                template_id=t.id,
                period=period,
            )
            db.add(e)
            db.flush()
            created += 1
            audit_service.record(db, _create_change(expense_key_fields(e)))
            touched_ids.append(e.id)
            touched_template_ids.append(t.id)
            records.append(serialize_expense(db, e))
    if touched_ids:
        audit_service.record(db, _change("expense", None, "execute", after={
            "op": "create",
            "count": len(touched_ids),
            "ids_hash": ids_fingerprint(touched_ids),
            "scope": {
                "template_ids": sorted(set(touched_template_ids)),
                "months": [period],
                "created": created,
                "overwritten": overwritten,
                "skipped": skipped,
                "conflict_resolution": conflict,
            },
        }))
    db.commit()
    return {
        "success": True,
        "created_count": created,
        "skipped_count": skipped,
        "overwritten_count": overwritten,
        "period": period,
        "records": records,
        "message": f"生成完成：新增 {created} 条，覆盖 {overwritten} 条，跳过 {skipped} 条",
    }


# ---------------- 汇总（§4.3） ----------------

def _aggregate(db: Session, rows: list[Expense]) -> dict:
    total = fixed_total = large_total = Decimal("0")
    by_cat: dict[int | None, dict] = {}
    for e in rows:
        amt = Decimal(str(e.amount))
        total += amt
        if e.expense_type == "fixed":
            fixed_total += amt
        else:
            large_total += amt
        bucket = by_cat.setdefault(e.category_id, {"total": Decimal("0"), "count": 0})
        bucket["total"] += amt
        bucket["count"] += 1
    by_category = [
        {
            "category_id": cid,
            "category_name": _category_name(db, cid) or "未分类",
            "total": str(_q2(bucket["total"])),
            "count": bucket["count"],
        }
        for cid, bucket in by_cat.items()
    ]
    by_category.sort(key=lambda x: Decimal(x["total"]), reverse=True)
    return {
        "total": str(_q2(total)),
        "fixed_total": str(_q2(fixed_total)),
        "large_total": str(_q2(large_total)),
        "count": len(rows),
        "by_category": by_category,
    }


def fixed_monthly_burden(db: Session, today: date | None = None) -> Decimal:
    """当前有效模板（active 且未过期）月负担归一化：monthly=1、quarterly=/3、yearly=/12。"""
    today = today or date.today()
    templates = db.execute(
        select(FixedExpenseTemplate).where(FixedExpenseTemplate.active == True)  # noqa: E712
    ).scalars().all()
    burden = Decimal("0")
    for t in templates:
        if t.end_date is not None and t.end_date < today:
            continue
        amt = Decimal(str(t.amount))
        if t.cycle == "monthly":
            burden += amt
        elif t.cycle == "quarterly":
            burden += amt / 3
        elif t.cycle == "yearly":
            burden += amt / 12
    return _q2(burden)


def get_summary(db: Session, year: int | None = None,
                month: int | None = None) -> dict:
    today = date.today()
    y = int(year) if year is not None else today.year
    m = int(month) if month is not None else today.month
    if not 1 <= m <= 12:
        raise HTTPException(status_code=400, detail="月份非法，应为 1-12")
    try:
        month_start = date(y, m, 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="年份非法")
    month_end = date(y, m, calendar.monthrange(y, m)[1])
    year_start = date(y, 1, 1)
    year_end = date(y, 12, 31)

    all_rows = db.execute(select(Expense)).scalars().all()
    month_rows = [r for r in all_rows if month_start <= r.expense_date <= month_end]
    year_rows = [r for r in all_rows if year_start <= r.expense_date <= year_end]

    month_stats = _aggregate(db, month_rows)
    month_stats = {"period": f"{y}-{m:02d}", **month_stats}
    year_stats = {"year": y, **_aggregate(db, year_rows)}
    return {
        "month": month_stats,
        "year": year_stats,
        "fixed_monthly_burden": str(fixed_monthly_burden(db)),
    }
