"""支出录入（分类/模板/流水/一键生成/汇总）——契约 §7 场景覆盖。

示例数据全部虚构（示例房贷、示例储蓄账户等），不含真实机构/账号/金额语义。
测试风格照 test_source_groups.py：router 函数直调 + conftest 的 db fixture。
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from models import Channel, Expense, FixedExpenseTemplate, Institution
from schemas import (
    ExpenseCategoryCreate,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseGenerateRequest,
    ExpenseUpdate,
    FixedExpenseTemplateCreate,
    FixedExpenseTemplateUpdate,
)
from routers.expense_categories import (
    create_expense_category,
    delete_expense_category,
    list_expense_categories,
    update_expense_category,
)
from routers.expense_templates import (
    create_expense_template,
    delete_expense_template,
    list_expense_templates,
    update_expense_template,
)
from routers.expenses import (
    create_expense,
    delete_expense,
    generate_fixed,
    get_expense_summary,
    get_generate_preview,
    list_expenses,
    update_expense,
)


# ---------------- 夹具辅助 ----------------

def make_account(db, name="示例储蓄账户"):
    """账户维度＝机构（0027 起 `expenses.account_id` 外键指 institutions）。"""
    inst = Institution(name=name, kind="bank")
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def make_channel(db, name="示例渠道"):
    """非机构字典行：用于「账户必须是机构」的负例（值域不对即 400）。"""
    channel = Channel(name=name)
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def make_category(db, name="房租房贷"):
    return create_expense_category(ExpenseCategoryCreate(name=name), db)


def make_template(db, name="示例房贷", amount="5000.00", cycle="monthly",
                  start_date="2026-01-15", **kw):
    payload = FixedExpenseTemplateCreate(
        name=name, amount=amount, cycle=cycle, start_date=start_date, **kw
    )
    return create_expense_template(payload, db)


def err(exc_info):
    return exc_info.value.detail


# ---------------- 1. 分类 CRUD ----------------

def test_category_crud_flow(db):
    created = make_category(db, "餐饮外卖")
    assert created["id"] and created["name"] == "餐饮外卖"
    assert created["created_at"] and created["updated_at"]

    items = list_expense_categories(db)
    assert [c["id"] for c in items] == sorted(c["id"] for c in items)
    assert items[0]["description"] is None

    updated = update_expense_category(
        created["id"], ExpenseCategoryUpdate(name="居家日用", description="示例描述"), db)
    assert updated["name"] == "居家日用"
    assert updated["description"] == "示例描述"

    assert delete_expense_category(created["id"], db, reason="normalization") is None
    assert list_expense_categories(db) == []


def test_category_duplicate_name_400(db):
    make_category(db, "交通出行")
    with pytest.raises(HTTPException) as ei:
        make_category(db, "交通出行")
    assert ei.value.status_code == 400
    # 更新撞名同样 400
    other = create_expense_category(ExpenseCategoryCreate(name="示例教育"), db)
    with pytest.raises(HTTPException) as ei:
        update_expense_category(other["id"], ExpenseCategoryUpdate(name="交通出行"), db)
    assert ei.value.status_code == 400


def test_category_delete_referenced_by_expense_400(db):
    cat = make_category(db, "医疗保健")
    make_account(db)
    create_expense(ExpenseCreate(
        expense_date="2026-05-01", expense_name="示例手术费",
        amount="30000.00", expense_type="large_oneoff", category_id=cat["id"],
    ), db)
    with pytest.raises(HTTPException) as ei:
        delete_expense_category(cat["id"], db, reason="normalization")
    assert ei.value.status_code == 400
    assert err(ei) == "该分类已被使用，无法删除"


def test_category_delete_referenced_by_template_400(db):
    cat = make_category(db, "保险")
    make_template(db, name="示例车险", category_id=cat["id"])
    with pytest.raises(HTTPException) as ei:
        delete_expense_category(cat["id"], db, reason="normalization")
    assert ei.value.status_code == 400


def test_category_404(db):
    with pytest.raises(HTTPException) as ei:
        update_expense_category(999, ExpenseCategoryUpdate(name="x"), db)
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        delete_expense_category(999, db, reason="normalization")
    assert ei.value.status_code == 404


# ---------------- 2. 支出 CRUD ----------------

def _expense_payload(**kw):
    base = dict(expense_date="2026-09-10", expense_name="示例买车",
                amount="88000.00", expense_type="large_oneoff")
    base.update(kw)
    return ExpenseCreate(**base)


def test_expense_create_and_serialize(db):
    cat = make_category(db, "大额支出")
    acc = make_account(db)
    e = create_expense(_expense_payload(category_id=cat["id"], account_id=acc.id,
                                        note="示例备注"), db)
    assert e["amount"] == "88000.00"
    assert e["expense_type"] == "large_oneoff"
    assert e["category_name"] == "大额支出"
    assert e["account_name"] == "示例储蓄账户"
    # 手动记录：template_id / period 均为 NULL
    assert e["template_id"] is None
    assert e["period"] is None


def test_expense_validation_400(db):
    not_an_account = make_channel(db)
    for bad in ("0", "-100", "abc", ""):
        with pytest.raises(HTTPException) as ei:
            create_expense(_expense_payload(amount=bad), db)
        assert ei.value.status_code == 400
        assert err(ei) == "金额必须大于0"
    with pytest.raises(HTTPException) as ei:
        create_expense(_expense_payload(expense_type="income"), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        create_expense(_expense_payload(expense_date="2026-13-01"), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        create_expense(_expense_payload(category_id=999), db)
    assert ei.value.status_code == 400
    # account 必须落在 institutions（渠道 id 属另一张字典 → 400）
    with pytest.raises(HTTPException) as ei:
        create_expense(_expense_payload(account_id=not_an_account.id), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        create_expense(_expense_payload(account_id=999999), db)
    assert ei.value.status_code == 400


def test_expense_update_partial_and_404(db):
    acc = make_account(db)
    e = create_expense(_expense_payload(account_id=acc.id), db)
    updated = update_expense(e["id"], ExpenseUpdate(amount="90000.00"), db)
    assert updated["amount"] == "90000.00"
    assert updated["expense_name"] == "示例买车"      # 未提供字段不变
    assert updated["account_id"] == acc.id
    with pytest.raises(HTTPException) as ei:
        update_expense(e["id"], ExpenseUpdate(amount="-1"), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        update_expense(999, ExpenseUpdate(note="x"), db)
    assert ei.value.status_code == 404
    assert delete_expense(e["id"], db, reason="normalization") is None
    with pytest.raises(HTTPException) as ei:
        delete_expense(e["id"], db, reason="normalization")
    assert ei.value.status_code == 404


def test_expense_pagination_and_filters(db):
    cat_a = make_category(db, "分类甲")
    cat_b = make_category(db, "分类乙")
    acc = make_account(db)
    rows = [
        ("2026-09-01", "示例房贷一期", "5000.00", "fixed", cat_a["id"], acc.id),
        ("2026-09-15", "示例装修", "60000.00", "large_oneoff", cat_b["id"], None),
        ("2026-08-20", "示例房贷二期", "5000.00", "fixed", cat_a["id"], acc.id),
        ("2026-07-05", "示例门诊", "200.00", "large_oneoff", None, None),
        ("2026-06-01", "示例房贷三期", "5000.00", "fixed", cat_a["id"], None),
    ]
    for d, n, a, t, c, ac in rows:
        create_expense(ExpenseCreate(expense_date=d, expense_name=n, amount=a,
                                     expense_type=t, category_id=c, account_id=ac), db)

    page = list_expenses(db=db, page=1, page_size=2)
    assert page["total"] == 5 and page["total_pages"] == 3
    assert [i["expense_name"] for i in page["items"]] == ["示例装修", "示例房贷一期"]
    page2 = list_expenses(db=db, page=3, page_size=2)
    assert [i["expense_name"] for i in page2["items"]] == ["示例房贷三期"]

    assert list_expenses(db=db, page=1, page_size=100, expense_type="fixed")["total"] == 3
    assert list_expenses(db=db, page=1, page_size=100, category_id=cat_b["id"])["total"] == 1
    assert list_expenses(db=db, page=1, page_size=100, account_id=acc.id)["total"] == 2
    rng = list_expenses(db=db, page=1, page_size=100,
                        date_from="2026-07-01", date_to="2026-08-31")
    assert [i["expense_name"] for i in rng["items"]] == ["示例房贷二期", "示例门诊"]
    named = list_expenses(db=db, page=1, page_size=100, name="房贷")
    assert named["total"] == 3
    with pytest.raises(HTTPException) as ei:
        list_expenses(db=db, page=1, page_size=100, expense_type="bad")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        list_expenses(db=db, page=1, page_size=100, date_from="not-a-date")
    assert ei.value.status_code == 400


# ---------------- 3. 模板 CRUD ----------------

def test_template_crud_and_list_filter(db):
    cat = make_category(db, "房租房贷")
    acc = make_account(db)
    t = make_template(db, category_id=cat["id"], account_id=acc.id,
                      end_date="2030-12-31", note="示例备注")
    assert t["amount"] == "5000.00"
    assert t["cycle"] == "monthly"
    assert t["category_name"] == "房租房贷"
    assert t["account_name"] == "示例储蓄账户"
    assert t["active"] is True
    assert t["start_date"] == "2026-01-15" and t["end_date"] == "2030-12-31"

    make_template(db, name="示例订阅", amount="30.00", active=False)
    assert len(list_expense_templates(db=db)) == 2
    assert [x["name"] for x in list_expense_templates(db=db, active=True)] == ["示例房贷"]
    assert [x["name"] for x in list_expense_templates(db=db, active=False)] == ["示例订阅"]

    upd = update_expense_template(t["id"], FixedExpenseTemplateUpdate(
        amount="5200.00", active=False), db)
    assert upd["amount"] == "5200.00" and upd["active"] is False
    assert upd["name"] == "示例房贷"                  # 未提供字段不变
    assert delete_expense_template(t["id"], db, reason="normalization") is None
    with pytest.raises(HTTPException) as ei:
        update_expense_template(t["id"], FixedExpenseTemplateUpdate(name="x"), db)
    assert ei.value.status_code == 404


def test_template_validation_400(db):
    not_an_account = make_channel(db)
    with pytest.raises(HTTPException) as ei:
        make_template(db, amount="0")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        make_template(db, cycle="weekly")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        make_template(db, start_date="2026-05-01", end_date="2026-04-01")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        make_template(db, start_date="2026/01/15x")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        make_template(db, category_id=999)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        make_template(db, account_id=not_an_account.id)
    assert ei.value.status_code == 400
    # 更新同样校验本次提供的字段
    t = make_template(db)
    with pytest.raises(HTTPException) as ei:
        update_expense_template(t["id"], FixedExpenseTemplateUpdate(cycle="daily"), db)
    assert ei.value.status_code == 400


def test_delete_template_unbinds_generated_expenses(db):
    t = make_template(db)
    generate_fixed(ExpenseGenerateRequest(
        period="2026-09", items=[{"template_id": t["id"]}]), db)
    e = db.query(Expense).one()
    assert e.template_id == t["id"]
    delete_expense_template(t["id"], db, reason="normalization")
    db.expire_all()
    e = db.query(Expense).one()                       # 已生成支出不删
    assert e.template_id is None


# ---------------- 4. 一键生成 ----------------

def test_preview_invalid_period_400(db):
    for bad in ("2026-13", "202609", "abc", "2026-9"):
        with pytest.raises(HTTPException) as ei:
            get_generate_preview(bad, db)
        assert ei.value.status_code == 400


def test_preview_eligibility_and_clamped_date(db):
    acc = make_account(db)
    cat = make_category(db)
    monthly = make_template(db, name="示例房贷", start_date="2026-01-31",
                            category_id=cat["id"], account_id=acc.id)
    quarterly = make_template(db, name="示例季费", cycle="quarterly",
                              amount="900.00", start_date="2026-01-15")
    yearly = make_template(db, name="示例年费", cycle="yearly",
                           amount="12000.00", start_date="2026-05-10")
    make_template(db, name="示例停用", active=False)

    pv = get_generate_preview("2026-02", db)          # 平年 2 月
    assert pv["period_start"] == "2026-02-01" and pv["period_end"] == "2026-02-28"
    ids = [i["template_id"] for i in pv["items"]]
    assert ids == sorted(ids)
    assert monthly["id"] in ids and quarterly["id"] not in ids and yearly["id"] not in ids
    m_item = next(i for i in pv["items"] if i["template_id"] == monthly["id"])
    assert m_item["expense_date"] == "2026-02-28"     # 31 日 clamp
    assert m_item["status"] == "new" and m_item["existing_expense_id"] is None
    assert m_item["category_name"] == "房租房贷" and m_item["account_name"] == "示例储蓄账户"
    # yearly 对齐
    pv5 = get_generate_preview("2027-05", db)
    pv5_ids = {i["template_id"] for i in pv5["items"]}
    assert {monthly["id"], yearly["id"]} == pv5_ids  # 2027-05：季差不对齐（16%3=1）
    pv6 = get_generate_preview("2027-06", db)
    assert yearly["id"] not in {i["template_id"] for i in pv6["items"]}
    assert quarterly["id"] not in {i["template_id"] for i in pv6["items"]}


def test_quarterly_alignment_months(db):
    t = make_template(db, name="示例季费", cycle="quarterly",
                      amount="900.00", start_date="2026-01-10")
    aligned = {1, 4, 7, 10}
    for m in range(1, 13):
        period = f"2026-{m:02d}"
        pv = get_generate_preview(period, db)
        present = t["id"] in {i["template_id"] for i in pv["items"]}
        assert present == (m in aligned), period


def test_yearly_alignment_months(db):
    t = make_template(db, name="示例年费", cycle="yearly",
                      amount="12000.00", start_date="2026-08-20")
    for m in range(1, 13):
        pv = get_generate_preview(f"2027-{m:02d}", db)
        present = t["id"] in {i["template_id"] for i in pv["items"]}
        assert present == (m == 8), m


def test_validity_window_trimming(db):
    t = make_template(db, name="示例短期", cycle="monthly",
                      start_date="2026-03-10", end_date="2026-06-15")
    for m in range(1, 13):
        pv = get_generate_preview(f"2026-{m:02d}", db)
        present = t["id"] in {i["template_id"] for i in pv["items"]}
        assert present == (3 <= m <= 6), m


def test_generate_monthly_and_idempotent_skip(db):
    t = make_template(db, start_date="2026-09-16")
    res = generate_fixed(ExpenseGenerateRequest(
        period="2026-09", conflict_resolution="skip",
        items=[{"template_id": t["id"]}]), db)
    assert res["success"] is True
    assert (res["created_count"], res["overwritten_count"], res["skipped_count"]) == (1, 0, 0)
    assert res["message"] == "生成完成：新增 1 条，覆盖 0 条，跳过 0 条"
    rec = res["records"][0]
    assert rec["expense_type"] == "fixed"
    assert rec["template_id"] == t["id"] and rec["period"] == "2026-09"
    assert rec["amount"] == "5000.00" and rec["expense_date"] == "2026-09-16"
    assert rec["expense_name"] == "示例房贷"

    # 幂等：重复生成 skip，不再新增
    res2 = generate_fixed(ExpenseGenerateRequest(
        period="2026-09", conflict_resolution="skip",
        items=[{"template_id": t["id"]}]), db)
    assert res2["created_count"] == 0 and res2["skipped_count"] == 1
    assert res2["records"] == []
    assert db.query(Expense).count() == 1
    # 预览显示 exists
    pv = get_generate_preview("2026-09", db)
    item = pv["items"][0]
    assert item["status"] == "exists"
    assert item["existing_expense_id"] == rec["id"]


def test_generate_overwrite_updates_not_duplicates(db):
    cat = make_category(db, "保险")
    t = make_template(db, category_id=cat["id"])
    generate_fixed(ExpenseGenerateRequest(
        period="2026-09", items=[{"template_id": t["id"], "amount": "4800.00"}]), db)
    res = generate_fixed(ExpenseGenerateRequest(
        period="2026-09", conflict_resolution="overwrite",
        items=[{"template_id": t["id"]}]), db)
    assert res["created_count"] == 0 and res["overwritten_count"] == 1
    assert db.query(Expense).count() == 1
    e = db.query(Expense).one()
    assert Decimal(str(e.amount)) == Decimal("5000.00")  # 覆盖回模板金额
    assert e.category_id == cat["id"]
    assert res["records"][0]["amount"] == "5000.00"


def test_generate_item_overrides_and_skips(db):
    t1 = make_template(db, name="示例房贷")
    t2 = make_template(db, name="示例物业费", amount="300.00",
                       start_date="2026-09-05")
    res = generate_fixed(ExpenseGenerateRequest(
        period="2026-09",
        items=[
            {"template_id": t1["id"], "amount": "5200.00", "expense_date": "2026-09-20"},
            {"template_id": t2["id"]},
            {"template_id": 999},                     # 不存在 → skip
        ]), db)
    assert res["created_count"] == 2 and res["skipped_count"] == 1
    amounts = sorted(r["amount"] for r in res["records"])
    assert amounts == ["300.00", "5200.00"]
    overridden = next(r for r in res["records"] if r["amount"] == "5200.00")
    assert overridden["expense_date"] == "2026-09-20"
    # 非法覆盖值 → 400（不静默跳过）
    with pytest.raises(HTTPException) as ei:
        generate_fixed(ExpenseGenerateRequest(
            period="2026-09", items=[{"template_id": t1["id"], "amount": "-5"}]), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        generate_fixed(ExpenseGenerateRequest(
            period="2026-09",
            items=[{"template_id": t1["id"], "expense_date": "bad"}]), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        generate_fixed(ExpenseGenerateRequest(
            period="2026-13", items=[]), db)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        generate_fixed(ExpenseGenerateRequest(
            period="2026-10", conflict_resolution="merge", items=[]), db)
    assert ei.value.status_code == 400


def test_generate_respects_eligibility(db):
    t = make_template(db, cycle="quarterly", start_date="2026-01-15")
    res = generate_fixed(ExpenseGenerateRequest(     # 2 月非季初月 → 不符合条件
        period="2026-02", items=[{"template_id": t["id"]}]), db)
    assert res["created_count"] == 0 and res["skipped_count"] == 1
    assert db.query(Expense).count() == 0


# ---------------- 5. 汇总 ----------------

def test_summary_totals_and_by_category(db):
    cat_fixed = make_category(db, "房租房贷")
    cat_misc = make_category(db, "医疗保健")
    rows = [
        ("2026-09-05", "示例房贷", "5200.00", "fixed", cat_fixed["id"]),
        ("2026-09-10", "示例手术", "2800.00", "large_oneoff", cat_misc["id"]),
        ("2026-09-12", "示例打车", "300.00", "large_oneoff", None),
        ("2026-09-20", "示例买菜", "100.00", "large_oneoff", None),
        ("2026-08-15", "示例上月", "700.00", "fixed", cat_fixed["id"]),
        ("2025-12-31", "示例去年", "9999.00", "large_oneoff", None),
    ]
    for d, n, a, t, c in rows:
        create_expense(ExpenseCreate(expense_date=d, expense_name=n, amount=a,
                                     expense_type=t, category_id=c), db)
    s = get_expense_summary(db=db, year=2026, month=9)
    m = s["month"]
    assert m["period"] == "2026-09"
    assert m["total"] == "8400.00" and m["fixed_total"] == "5200.00"
    assert m["large_total"] == "3200.00" and m["count"] == 4
    cats = {c["category_name"]: c for c in m["by_category"]}
    assert cats["房租房贷"]["total"] == "5200.00" and cats["房租房贷"]["count"] == 1
    assert cats["未分类"]["total"] == "400.00" and cats["未分类"]["count"] == 2
    assert cats["未分类"]["category_id"] is None
    totals = [Decimal(c["total"]) for c in m["by_category"]]
    assert totals == sorted(totals, reverse=True)
    # 年区间 1/1~12/31（2026 不含 2025 行；含 8 月）
    y = s["year"]
    assert y["year"] == 2026 and y["total"] == "9100.00" and y["count"] == 5
    assert s["fixed_monthly_burden"] == "0.00"      # 无模板


def test_summary_month_range_validation(db):
    with pytest.raises(HTTPException) as ei:
        get_expense_summary(db=db, year=2026, month=13)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        get_expense_summary(db=db, year=2026, month=0)
    assert ei.value.status_code == 400
    # 缺省 = 服务器当天年月，形状完整
    s = get_expense_summary(db=db)
    assert set(s) == {"month", "year", "fixed_monthly_burden"}
    assert s["month"]["period"].startswith(str(date.today().year))


def test_fixed_monthly_burden_normalization(db):
    make_template(db, name="示例房贷", amount="5200.00", cycle="monthly",
                  start_date="2026-01-15")
    make_template(db, name="示例季费", amount="900.00", cycle="quarterly",
                  start_date="2026-01-15")
    make_template(db, name="示例年费", amount="12000.00", cycle="yearly",
                  start_date="2026-01-15")
    make_template(db, name="示例停用", amount="999.00", active=False)
    past = (date.today() - timedelta(days=30)).isoformat()
    make_template(db, name="示例已到期", amount="888.00", end_date=past)
    # 5200 + 900/3 + 12000/12 = 5200 + 300 + 1000（停用/已到期不计）
    s = get_expense_summary(db=db, year=2026, month=9)
    assert s["fixed_monthly_burden"] == "6500.00"
