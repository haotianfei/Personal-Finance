"""留痕覆盖率：值桶（支出）＋ 离场桶（导出历史/规则）——ADR-0027。

本文件锁的是「写入面真的留了痕」，契约表与路由的静态一致性在
`test_audit_guard.py`，这里补的是**行为**：

1. 支出 create / update / delete 三种快照分型（D5）：create 只给 after、
   update **只给变更字段**、delete before 全集＋after=NULL；金额一律字符串；
2. `expenses/generate` 的批次形状：1 条 `expense/execute` 头行（after.count=N）
   ＋ 逐行明细；`ids_hash` 与 `asset_service.ids_fingerprint` 同口径；
3. 生成中途抛错 → 同事务回滚，审计行与支出行**一起**消失；
4. 支出分类 / 模板 CRUD（口径桶）各 1 条对应 action（entity 同为 `expense`，
   靠快照形状区分——这是契约冻结值，不是本测试的发明）；
5. 导出历史 `export/create` 只记「壳」、**不记文件内容**；导出规则 CRUD 3 条；
6. 这些写入之后 `audit_chain.verify()` 仍 ok（链能续接）。

沙箱：conftest 的内存库 `db` fixture（不触真实 data/）；文件名一律示例，
`reload_scheduler` 一律 monkeypatch —— 真实现会开 APScheduler 并连真库。
"""
import json
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from models import (
    AuditLog,
    AutoExportRule,
    Expense,
    ExpenseCategory,
    ExportHistory,
    FixedExpenseTemplate,
)
from routers.expense_categories import (
    create_expense_category,
    delete_expense_category,
    update_expense_category,
)
from routers.expense_templates import (
    create_expense_template,
    delete_expense_template,
    update_expense_template,
)
from routers.expenses import (
    create_expense,
    delete_expense,
    generate_fixed,
    update_expense,
)
from routers.export_history import (
    create_auto_export_rule,
    create_export_history,
    delete_auto_export_rule,
    update_auto_export_rule,
)
from schemas import (
    AutoExportRuleCreate,
    AutoExportRuleUpdate,
    ExpenseCategoryCreate,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseGenerateRequest,
    ExpenseUpdate,
    FixedExpenseTemplateCreate,
    FixedExpenseTemplateUpdate,
)
from services import (
    asset_service,
    audit_chain,
    auto_export_service,
    expense_service,
    export_history_service,
)
from services.audit_contract import rule_for


# ---------------------------------------------------------------- 助手

def _audits(db, entity, action=None):
    q = select(AuditLog).where(AuditLog.entity == entity)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars().all())


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _expense_count(db) -> int:
    return db.execute(select(func.count()).select_from(Expense)).scalar() or 0


def _payload(row):
    """删除行只有 before，其余只有 after——统一取「有快照的那一侧」。"""
    return (json.loads(row.after_json) if row.after_json is not None
            else json.loads(row.before_json) if row.before_json is not None
            else None)


def _after(row):
    return json.loads(row.after_json) if row.after_json else None


def _before(row):
    return json.loads(row.before_json) if row.before_json else None


def _rows_with(db, entity, action, keys=(), row_id=None):
    """按「快照里出现的键」挑行。

    `entity='expense'` 同时承载流水/分类/模板三类（契约冻结取值），
    `entity='export'` 同时承载导出历史与规则——只能靠快照形状 + row_id 区分。
    """
    picked = []
    for row in _audits(db, entity, action):
        if row_id is not None and row.row_id != row_id:
            continue
        payload = _payload(row)
        if payload and all(key in payload for key in keys):
            picked.append(row)
    return picked


def _make_category(db, name="示例餐饮"):
    return create_expense_category(ExpenseCategoryCreate(name=name, description="示例说明"), db)


def _make_template(db, index=1, amount="3000.00"):
    return create_expense_template(FixedExpenseTemplateCreate(
        name=f"示例固定支出{index}",
        amount=amount,
        cycle="monthly",
        start_date="2026-01-01",
    ), db)


# ---------------------------------------------------------------- 1. 支出 CRUD

def test_expense_crud_snapshot_shapes(db):
    cat = _make_category(db)
    out = create_expense(ExpenseCreate(
        expense_date="2026-03-05",
        expense_name="示例午餐",
        amount="32.50",
        expense_type="large_oneoff",
        category_id=cat["id"],
        note="示例备注",
    ), db)

    creates = _rows_with(db, "expense", "create", keys=("expense_name",), row_id=out["id"])
    assert len(creates) == 1
    row = creates[0]
    assert row.before_json is None                      # create 无 before
    payload = _after(row)
    assert payload["id"] == out["id"]
    assert payload["expense_name"] == "示例午餐"
    assert payload["expense_date"] == "2026-03-05"
    assert payload["amount"] == "32.50"                 # 两位小数字符串，不是 float
    assert isinstance(payload["amount"], str)
    assert payload["template_id"] is None and payload["period"] is None

    # 改金额 → 只含金额键（同值重放不留行：无事实变更）
    update_expense(out["id"], ExpenseUpdate(amount="40.00"), db)
    updates = _rows_with(db, "expense", "update", row_id=out["id"])
    assert len(updates) == 1
    assert _after(updates[0]) == {"amount": "40.00"}
    assert _before(updates[0]) == {"amount": "32.50"}
    assert isinstance(_after(updates[0])["amount"], str)          # 不是 float
    assert Decimal(_after(updates[0])["amount"]) == Decimal("40.00")  # 精度未被截断
    update_expense(out["id"], ExpenseUpdate(amount="40.00"), db)
    assert len(_rows_with(db, "expense", "update", row_id=out["id"])) == 1

    # 删 → before 全集、after=NULL
    delete_expense(out["id"], db, reason="normalization")
    deletes = _rows_with(db, "expense", "delete", row_id=out["id"])
    assert len(deletes) == 1
    assert deletes[0].after_json is None
    before = _before(deletes[0])
    assert before["amount"] == "40.00"
    assert before["expense_name"] == "示例午餐"
    assert before["expense_date"] == "2026-03-05"
    assert before["category_id"] == cat["id"]
    assert db.get(Expense, out["id"]) is None


def test_expense_rejected_write_leaves_no_audit(db):
    """校验失败（400）在写入前拒绝 → 零审计行。"""
    before = _count(db)
    with pytest.raises(Exception):
        create_expense(ExpenseCreate(
            expense_date="2026-03-05", expense_name="示例午餐",
            amount="0", expense_type="large_oneoff"), db)
    db.rollback()
    assert _count(db) == before
    assert _expense_count(db) == 0


# ---------------------------------------------------------------- 2. 批量生成

def test_generate_leaves_head_and_detail_rows(db):
    templates = [_make_template(db, i) for i in (1, 2, 3)]
    tids = [t["id"] for t in templates]
    before_audit = _count(db)

    result = generate_fixed(ExpenseGenerateRequest(
        period="2026-03",
        conflict_resolution="skip",
        items=[{"template_id": tid} for tid in tids],
    ), db)
    assert result["created_count"] == 3 and result["overwritten_count"] == 0
    assert result["skipped_count"] == 0

    heads = _audits(db, "expense", "execute")
    assert len(heads) == 1
    head = heads[0]
    assert head.row_id is None and head.before_json is None
    payload = _after(head)
    assert payload["op"] == "create"
    assert payload["count"] == 3
    created_ids = [record["id"] for record in result["records"]]
    assert payload["ids_hash"] == asset_service.ids_fingerprint(created_ids)  # 与资产侧同口径
    assert len(payload["ids_hash"]) == 16
    assert payload["scope"]["template_ids"] == sorted(tids)
    assert payload["scope"]["months"] == ["2026-03"]

    creates = _rows_with(db, "expense", "create", keys=("expense_name",))
    detail = [row for row in creates if row.row_id in set(created_ids)]
    assert len(detail) == 3
    assert all(row.before_json is None for row in detail)
    assert {_after(row)["id"] for row in detail} == set(created_ids)
    assert all(isinstance(_after(row)["amount"], str) for row in detail)
    assert all(_after(row)["period"] == "2026-03" for row in detail)
    assert _count(db) == before_audit + 4          # 1 头行 + 3 明细

    # 同期间重放：全部 skip ＝ 零写入 → 既不新增明细，也不新增头行
    again = generate_fixed(ExpenseGenerateRequest(
        period="2026-03",
        conflict_resolution="skip",
        items=[{"template_id": tid} for tid in tids],
    ), db)
    assert again["created_count"] == 0 and again["skipped_count"] == 3
    assert len(_audits(db, "expense", "execute")) == 1
    assert _count(db) == before_audit + 4
    assert _expense_count(db) == 3


def test_generate_failure_rolls_back_audit_too(db, monkeypatch):
    templates = [_make_template(db, i) for i in (1, 2, 3)]
    tids = [t["id"] for t in templates]
    before_audit, before_rows = _count(db), _expense_count(db)

    real = expense_service.serialize_expense
    calls = {"n": 0}

    def boom(db_session, row):
        calls["n"] += 1
        if calls["n"] >= 2:                 # 第 1 行审计已写，第 2 行炸掉
            raise RuntimeError("生成中途失败")
        return real(db_session, row)

    monkeypatch.setattr(expense_service, "serialize_expense", boom)
    with pytest.raises(RuntimeError):
        generate_fixed(ExpenseGenerateRequest(
            period="2026-03",
            conflict_resolution="skip",
            items=[{"template_id": tid} for tid in tids],
        ), db)
    db.rollback()                           # 同事务回滚 → 审计不留

    assert _count(db) == before_audit       # 审计行数不变
    assert _expense_count(db) == before_rows  # 支出行数不变
    assert _audits(db, "expense", "execute") == []


# ---------------------------------------------------------------- 4. 口径桶：分类 / 模板

def test_category_crud_leaves_audit(db):
    cat = _make_category(db, name="示例交通")
    creates = _rows_with(db, "expense", "create", keys=("name", "description"),
                         row_id=cat["id"])
    assert len(creates) == 1 and creates[0].before_json is None
    assert _after(creates[0])["name"] == "示例交通"

    update_expense_category(cat["id"], ExpenseCategoryUpdate(name="示例交通费"), db)
    updates = _rows_with(db, "expense", "update", keys=("name",), row_id=cat["id"])
    assert len(updates) == 1
    assert _after(updates[0]) == {"name": "示例交通费"}
    assert _before(updates[0]) == {"name": "示例交通"}

    delete_expense_category(cat["id"], db, reason="normalization")
    deletes = _rows_with(db, "expense", "delete", keys=("description",), row_id=cat["id"])
    assert len(deletes) == 1 and deletes[0].after_json is None
    assert _before(deletes[0])["name"] == "示例交通费"
    assert db.get(ExpenseCategory, cat["id"]) is None

    assert [row.action for row in _audits(db, "expense")] == ["create", "update", "delete"]


def test_template_crud_leaves_audit(db):
    tpl = _make_template(db, index=1)
    creates = _rows_with(db, "expense", "create", keys=("cycle", "start_date"),
                         row_id=tpl["id"])
    assert len(creates) == 1 and creates[0].before_json is None
    payload = _after(creates[0])
    assert payload["amount"] == "3000.00" and isinstance(payload["amount"], str)
    assert payload["cycle"] == "monthly" and payload["active"] is True

    update_expense_template(tpl["id"], FixedExpenseTemplateUpdate(amount="3200.00"), db)
    updates = _rows_with(db, "expense", "update", keys=("amount",), row_id=tpl["id"])
    assert len(updates) == 1
    assert _after(updates[0]) == {"amount": "3200.00"}
    assert _before(updates[0]) == {"amount": "3000.00"}

    delete_expense_template(tpl["id"], db, reason="normalization")
    deletes = _rows_with(db, "expense", "delete", keys=("cycle",), row_id=tpl["id"])
    assert len(deletes) == 1 and deletes[0].after_json is None
    assert _before(deletes[0])["amount"] == "3200.00"
    assert db.get(FixedExpenseTemplate, tpl["id"]) is None

    assert [row.action for row in _audits(db, "expense")] == ["create", "update", "delete"]


# ---------------------------------------------------------------- 5. 离场桶：导出

# 审计行允许出现的导出壳字段（白名单）——文件内容一律不许进审计。
_EXPORT_ALLOWED = {"id", "export_type", "filename", "file_size", "operator",
                   "rule_name", "file_path"}
_RULE_ALLOWED = {"id", "name", "cron_expression", "export_format",
                 "filename_template", "is_active"}


def test_export_history_leaves_shell_only(db):
    history = create_export_history(
        export_type="manual",
        filename="示例导出.csv",
        file_size=1024,
        operator="owner",
        db=db,
    )
    rows = _rows_with(db, "export", "create", keys=("filename",), row_id=history.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.before_json is None
    payload = _after(row)
    assert payload["filename"] == "示例导出.csv"
    assert payload["export_type"] == "manual"
    assert payload["file_size"] == 1024
    assert set(payload) == _EXPORT_ALLOWED            # 只有壳，没有内容
    assert not set(payload) & {"content", "csv", "body", "text", "data",
                               "preview", "rows", "sample", "bytes"}
    assert db.get(ExportHistory, history.id) is not None


def test_export_rule_crud_leaves_audit(db, monkeypatch):
    monkeypatch.setattr(export_history_service, "reload_scheduler", lambda: None)

    rule = create_auto_export_rule(AutoExportRuleCreate(
        name="示例每月导出",
        cron_expression="0 2 1 * *",
        export_format="csv",
        filename_template="示例-{date}.csv",
    ), db)

    creates = _rows_with(db, "export", "create", keys=("cron_expression",),
                         row_id=rule.id)
    assert len(creates) == 1 and creates[0].before_json is None
    payload = _after(creates[0])
    assert payload["name"] == "示例每月导出"
    assert payload["cron_expression"] == "0 2 1 * *"
    assert set(payload) == _RULE_ALLOWED

    update_auto_export_rule(rule.id, AutoExportRuleUpdate(export_format="json"), db)
    updates = _rows_with(db, "export", "update", keys=("export_format",),
                         row_id=rule.id)
    assert len(updates) == 1
    assert _after(updates[0]) == {"export_format": "json"}
    assert _before(updates[0]) == {"export_format": "csv"}

    delete_auto_export_rule(rule.id, db, reason="normalization")
    deletes = _rows_with(db, "export", "delete", row_id=rule.id)
    assert len(deletes) == 1 and deletes[0].after_json is None
    assert _before(deletes[0])["name"] == "示例每月导出"
    assert db.get(AutoExportRule, rule.id) is None

    assert [row.action for row in _audits(db, "export")] == ["create", "update", "delete"]


def test_export_rule_invalid_cron_leaves_no_audit(db, monkeypatch):
    """非法 cron → 400（写入前拒绝）→ 零审计行。"""
    monkeypatch.setattr(export_history_service, "reload_scheduler", lambda: None)
    before = _count(db)
    with pytest.raises(Exception):
        create_auto_export_rule(AutoExportRuleCreate(
            name="示例坏规则", cron_expression="* * *"), db)
    db.rollback()
    assert _count(db) == before
    assert db.execute(select(func.count()).select_from(AutoExportRule)).scalar() == 0


# ---------------------------------------------------------------- 6. 自动导出（调度器）

# 自动导出比手工导出多三个字段：source（区分来源）/ rule_id（哪条规则）/
# filters（本次导出口径）；其余壳字段与手工导出同一份 key_fields。
_AUTO_EXPORT_ALLOWED = _EXPORT_ALLOWED | {"source", "rule_id", "rule_name", "filters"}

_CONTENT_KEYS = {"content", "csv", "csv_content", "body", "text", "data",
                 "preview", "rows", "sample", "bytes"}


def _auto_export_sandbox(db, monkeypatch, tmp_path):
    """把调度器导出钉在沙箱：内存库会话 + 临时导出目录（不触真库、不写真实 data/）。

    规则直插（不走 create_rule）——本组要数的是「导出动作」的审计行，不是建规则的。
    """
    monkeypatch.setattr(auto_export_service, "AUTO_EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(
        auto_export_service, "SessionLocal", sessionmaker(bind=db.get_bind()))
    rule = AutoExportRule(
        name="示例自动导出",
        cron_expression="0 2 1 * *",
        export_format="csv",
        filename_template="示例-auto-{date}.csv",
    )
    db.add(rule)
    db.commit()
    db.rollback()          # 收掉测试会话的事务：调度器那条会话共用同一连接
    return rule.id


def test_auto_export_leaves_shell_only_audit(db, monkeypatch, tmp_path):
    rule_id = _auto_export_sandbox(db, monkeypatch, tmp_path)

    auto_export_service.execute_auto_export(rule_id)
    db.expire_all()

    rows = _rows_with(db, "export", "create", keys=("source",))
    assert len(rows) == 1                       # 与手工导出同形：一条 export/create
    row = rows[0]
    assert row.before_json is None
    payload = _after(row)
    assert payload["source"] == "auto"
    assert payload["rule_id"] == rule_id
    assert payload["rule_name"] == "示例自动导出"
    assert payload["export_type"] == "auto"
    assert payload["operator"] is None          # 调度器路径：沿用现有「自动导出无操作人」
    assert payload["file_path"] and os.path.basename(payload["file_path"]).endswith(".csv")
    assert os.path.exists(payload["file_path"])
    assert payload["filters"] == {"period_type": "all"}
    assert set(payload) == _AUTO_EXPORT_ALLOWED  # 只有壳
    assert not set(payload) & _CONTENT_KEYS      # 不记文件内容

    # 审计行与导出历史同事务：row_id 指回那条历史行，壳字段对得上
    history = db.get(ExportHistory, row.row_id)
    assert history is not None and history.export_type == "auto"
    assert history.file_path == payload["file_path"]
    assert history.file_size == payload["file_size"]


def test_auto_export_failure_leaves_no_audit(db, monkeypatch, tmp_path):
    rule_id = _auto_export_sandbox(db, monkeypatch, tmp_path)
    before_audit = _count(db)

    def boom(_records):
        raise RuntimeError("生成 CSV 失败")

    monkeypatch.setattr(auto_export_service.export_service, "generate_csv", boom)
    auto_export_service.execute_auto_export(rule_id)   # 作业自行吞异常并 rollback
    db.expire_all()

    assert _count(db) == before_audit            # 零审计行
    assert db.execute(select(func.count()).select_from(ExportHistory)).scalar() == 0
    assert not list(tmp_path.glob("*.csv"))      # 连文件都没落盘
    # 确认不是「规则没找到」的早退：确实跑到了导出、然后整事务回滚
    assert db.get(AutoExportRule, rule_id).last_run_at is None


# ---------------------------------------------------------------- 契约冻结值 + 链

FROZEN_ROWS = [
    # 值桶（expense 流水）
    ("POST", "/api/expenses", "expense", "create"),
    ("PUT", "/api/expenses/{expense_id}", "expense", "update"),
    ("DELETE", "/api/expenses/{expense_id}", "expense", "delete"),
    ("POST", "/api/expenses/generate", "expense", "execute"),
    # 口径桶（支出分类 / 模板）
    ("POST", "/api/expense-categories", "expense", "create"),
    ("PUT", "/api/expense-categories/{category_id}", "expense", "update"),
    ("DELETE", "/api/expense-categories/{category_id}", "expense", "delete"),
    ("POST", "/api/expense-templates", "expense", "create"),
    ("PUT", "/api/expense-templates/{template_id}", "expense", "update"),
    ("DELETE", "/api/expense-templates/{template_id}", "expense", "delete"),
    # 离场桶（导出历史 / 规则）
    ("POST", "/api/export-history/history", "export", "create"),
    ("POST", "/api/export-history/rules", "export", "create"),
    ("PUT", "/api/export-history/rules/{rule_id}", "export", "update"),
    ("DELETE", "/api/export-history/rules/{rule_id}", "export", "delete"),
]


@pytest.mark.parametrize("method,path,entity,action", FROZEN_ROWS)
def test_contract_rows_are_frozen(method, path, entity, action):
    """落地留痕的 14 行必须与契约表逐字一致（entity/action 是冻结值）。

    第 15 行是 `POST /api/export-history/validate-cron`：契约表 exempt（纯校验、
    零写入），不产审计行，单独见 `test_validate_cron_is_exempt_and_writes_nothing`。
    """
    rule = rule_for(method, path)
    assert rule is not None, f"{method} {path} 未登记进契约表"
    assert (rule.entity, rule.action) == (entity, action)


def test_validate_cron_is_exempt_and_writes_nothing(db):
    from routers.export_history import validate_cron
    from schemas import CronValidationRequest

    rule = rule_for("POST", "/api/export-history/validate-cron")
    assert rule is not None and rule.status == "exempt"
    before = _count(db)
    out = validate_cron(CronValidationRequest(cron_expression="0 3 * * *"))
    assert out.valid is True
    assert _count(db) == before


def test_audit_chain_still_verifies_after_expense_and_export_writes(db, monkeypatch):
    monkeypatch.setattr(export_history_service, "reload_scheduler", lambda: None)

    cat = _make_category(db)
    tpl = _make_template(db, index=1)
    created = create_expense(ExpenseCreate(
        expense_date="2026-03-06", expense_name="示例晚餐", amount="12.30",
        expense_type="large_oneoff", category_id=cat["id"]), db)
    update_expense(created["id"], ExpenseUpdate(amount="13.30"), db)
    generate_fixed(ExpenseGenerateRequest(
        period="2026-03", items=[{"template_id": tpl["id"]}]), db)
    delete_expense(created["id"], db, reason="normalization")
    create_export_history(export_type="auto", filename="示例-auto.csv",
                          rule_name="示例每月导出", db=db)

    out = audit_chain.verify(db)
    assert out["ok"] is True, out
    assert out["first_broken"] is None
    assert out["unchained_rows"] == 0
    assert out["head"]["row_count"] == _count(db)
    # 本场景应留痕：分类/模板/流水 create + 流水 update/delete + 生成头行与明细 + 导出
    assert len(_audits(db, "expense", "execute")) == 1
    assert len(_audits(db, "export", "create")) == 1