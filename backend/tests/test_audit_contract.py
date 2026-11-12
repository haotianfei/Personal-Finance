"""审计留痕契约（ADR-0027）测试——枚举 / 脱敏 / reason / 迁移 0036 / 哈希口径不变。

覆盖：
1. `ENTITY`/`ACTION` 非空且 `make_change` 对未登记值抛 ValueError；
2. `scrub()`：顶层与嵌套 dict 的 api_key/token/secret/password/authorization 被替换成
   `"<redacted>"` 且带 `_fp`（8 位 hex）/`_len`；普通字段原样；原值不出现在序列化结果里；
   入参不被就地修改；重复 scrub 幂等；
3. `reason`/`reason_note` 校验（合法通过、非法/超长抛错）；
4. 迁移 0036：两列存在、存量行 reason 为 NULL（不倒填）、append-only 触发器仍在、
   `audit_chain.verify()` 仍 ok、重跑幂等；
5. **哈希口径不变**：带 reason 写入后按 `audit_chain` 口径重算 row_hash 与链状态一致，
   且 `FIELDS` 不含 reason/reason_note；
6. **纠正类调用点必须传 reason**（spec E7 正向断言）：`KNOWN_DEBT` 债务清单已清零
   并删除，扫描面改为「凡 `action ∈ REASON_REQUIRED_ACTIONS` 的调用点都必须显式传
   `reason=`」；运行时强制在 `audit_service.record()`（`actor='owner'` 缺则 422）。

数据全部合成，不触真实 data/。
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from errors import ApiError, ErrorCode
from models import AuditChainState, AuditLog
from services import audit_chain, audit_service
from services.audit_contract import (
    ACTION, ENTITY, REASON, REASON_REQUIRED_ACTIONS, SENSITIVE_KEY_PATTERNS,
    fingerprint, redact_value, scrub)
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0009 = _load_script_module(f"{_MIGRATIONS_DIR}/0009_audit_log.py")
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
M0018 = _load_script_module(f"{_MIGRATIONS_DIR}/0018_audit_chain.py")
M0036 = _load_script_module(f"{_MIGRATIONS_DIR}/0036_audit_reason.py")

REDACTED = "<redacted>"
HEX8 = re.compile(r"^[0-9a-f]{8}$")


# ---------------------------------------------------------------- fixtures


def _run_script(conn, module) -> str:
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        result = module.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


@pytest.fixture()
def chain_env(tmp_path):
    """create_all（含 0036 的新列）＋ 0018 种链状态；返回 (path, Session)。"""
    path = str(tmp_path / "contract.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    _run_script(conn, M0018)
    conn.close()
    return path, sessionmaker(bind=engine)


# ---------------------------------------------------------------- 1. 枚举


def test_enums_are_registered_controlled_sets():
    assert ENTITY and ACTION and REASON
    assert all(isinstance(v, str) and v for v in ENTITY | ACTION | REASON)
    assert REASON == {"correction", "evolution", "normalization",
                      "import", "migration", "other"}
    # 纠正类动作必须是已登记动作的子集（否则规则永远不可能触发）
    assert REASON_REQUIRED_ACTIONS <= ACTION
    assert REASON_REQUIRED_ACTIONS


def test_parallel_lane_values_are_registered():
    """并行车道（批次/导入/备份/LLM 供应商）的取值必须已登记，否则写入即 ValueError。"""
    assert {"batch", "import", "backup", "llm_provider"} <= ENTITY
    assert {"execute", "import_seed", "import_confirm", "restore", "upload_db",
            "rotate_key", "batch_delete", "batch_create"} <= ACTION
    # 批次头固定 action；逐行明细仍用各自业务 action
    assert "execute" in ACTION
    # 纠正类：新拼写与现网别名都在 ACTION 里（否则校验自相矛盾）
    assert {"correct", "delete", "archive", "restore", "owner_change",
            "classification_change", "change"} <= REASON_REQUIRED_ACTIONS
    assert {"owner_change", "classification_change", "change"} <= ACTION


def test_make_change_accepts_registered_values():
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct",
        before={"amount": "1.00"}, after={"amount": "2.00"},
        evidence_ref=None, reason="correction", reason_note="笔误更正")
    assert change["entity"] == "asset_snapshot" and change["action"] == "correct"
    assert change["reason"] == "correction" and change["reason_note"] == "笔误更正"
    # 未传 reason 时键仍在（None），调用方无需 getattr 兜底
    plain = audit_service.make_change("asset_snapshot", 1, "create", None, {"amount": "1.00"})
    assert plain["reason"] is None and plain["reason_note"] is None


@pytest.mark.parametrize("entity,action", [
    ("not_registered_entity", "create"),
    ("asset_snapshot", "not_registered_action"),
    ("asset_Snapshot", "create"),          # 大小写不宽松：枚举是精确匹配
])
def test_make_change_rejects_unregistered_entity_or_action(entity, action):
    with pytest.raises(ValueError) as ei:
        audit_service.make_change(entity, 1, action)
    assert "未登记" in str(ei.value)


# ---------------------------------------------------------------- 2. 脱敏


def test_scrub_redacts_top_level_and_nested_with_fp_and_len():
    original = {
        "api_key": "sk-EXAMPLE-0123456789",
        "token": "tok-EXAMPLE-abcdef",
        "authorization": "Bearer EXAMPLE",
        "amount": "100.00",
        "side": "asset",
        "nested": {"password": "EXAMPLE-pass", "secret": 123456789, "note": "ok"},
        "rows": [{"secret": "EXAMPLE-in-list"}, "plain"],
    }
    snapshot = json.loads(json.dumps(original))     # 入参快照（用于断言未被就地修改）

    out = scrub(original)

    # 顶层：值被替换、伴生指纹/长度齐全
    for key in ("api_key", "token", "authorization"):
        assert out[key] == REDACTED
        assert HEX8.match(out[f"{key}_fp"]), out[f"{key}_fp"]
        assert out[f"{key}_len"] == len(original[key])
    # 指纹＝sha256 前 8 位（同一值稳定、异值不同）
    assert out["api_key_fp"] == fingerprint(original["api_key"])
    assert out["api_key_fp"] != out["token_fp"]
    # 普通字段原样透传、不生成伴生键
    assert out["amount"] == "100.00" and out["side"] == "asset"
    assert "amount_fp" not in out and "side_len" not in out
    # 嵌套 dict：同样脱敏；非字符串值→ str() 后再算指纹/长度
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["secret"] == REDACTED
    assert out["nested"]["secret_len"] == len(str(original["nested"]["secret"]))
    assert HEX8.match(out["nested"]["secret_fp"])
    assert out["nested"]["note"] == "ok"
    # list 内的 dict 也递归；list 内的标量原样
    assert out["rows"][0]["secret"] == REDACTED
    assert isinstance(out["rows"][0]["secret_len"], int)
    assert out["rows"][1] == "plain"

    # 原值不得以任何形式出现在返回值里
    dumped = json.dumps(out, ensure_ascii=False)
    for secret_value in ("sk-EXAMPLE-0123456789", "tok-EXAMPLE-abcdef",
                         "Bearer EXAMPLE", "EXAMPLE-pass", "123456789",
                         "EXAMPLE-in-list"):
        assert secret_value not in dumped

    # 入参未被就地修改
    assert original == snapshot
    assert "api_key_fp" not in original and "api_key_fp" not in original["nested"]

    # 幂等：重复 scrub 无副作用（不把 "<redacted>" 的指纹覆盖掉原值指纹）
    assert scrub(out) == out


def test_sensitive_key_patterns_are_case_insensitive_substrings():
    assert all(p == p.lower() for p in SENSITIVE_KEY_PATTERNS)
    out = scrub({"API_KEY": "sk-x", "Dedupe_Key": "manual:1", "max_tokens": 5,
                 "authorizationHeader": "Bearer x", "amount": "1.00",
                 "client_secret": "s3cr3t", "idempotency_key": "manual:1",
                 "token": "abc", "token_count": 12, "vocab_key": "amount"})
    assert out["API_KEY"] == REDACTED
    assert out["client_secret"] == REDACTED and out["token"] == REDACTED
    assert out["Dedupe_Key"] == "manual:1"          # 决策：裸 key 家族可读（ADR-0027 D7 修订）
    assert out["idempotency_key"] == "manual:1"
    assert out["vocab_key"] == "amount"
    assert out["max_tokens"] == 5                   # 配置值不脱敏
    assert out["token_count"] == 12
    assert out["authorizationHeader"] == "Bearer x"  # 非审计载荷字段，不做猜测
    assert out["amount"] == "1.00"


def test_redact_value_returns_tuple():
    redacted, fp, length = redact_value("secret-value")
    assert redacted == REDACTED and HEX8.match(fp) and length == len("secret-value")


def test_make_change_scrubs_before_and_after():
    change = audit_service.make_change(
        "advisor_settings", 1, "update",
        before={"api_key": "sk-OLD-example", "model": "m1"},
        after={"api_key": "sk-NEW-example", "model": "m2"},
        reason="evolution")
    assert change["before"]["api_key"] == REDACTED
    assert change["after"]["api_key"] == REDACTED
    assert change["before"]["model"] == "m1" and change["after"]["model"] == "m2"
    dumped = json.dumps(change, ensure_ascii=False)
    assert "sk-OLD-example" not in dumped and "sk-NEW-example" not in dumped


def test_record_scrubs_dict_path_before_after(chain_env):
    """绕过 make_change 直接拼 dict 的老调用点：record() 兜底脱敏。"""
    path, Session = chain_env
    db = Session()
    audit_service.record(db, {
        "entity": "asset_snapshot", "row_id": 1, "action": "create",
        "before": None,
        "after": {"amount": "1.00", "token": "tok-EXAMPLE-raw"},
        "evidence_ref": None})
    db.commit()
    row = db.execute(select(AuditLog)).scalars().one()
    after = json.loads(row.after_json)
    assert after["token"] == REDACTED and HEX8.match(after["token_fp"])
    assert "tok-EXAMPLE-raw" not in (row.after_json or "")
    db.close()


def test_scrub_is_idempotent_through_record(chain_env):
    """make_change 已脱敏 → record 再 scrub 一次不得改动指纹/长度。"""
    path, Session = chain_env
    db = Session()
    change = audit_service.make_change(
        "asset_snapshot", 1, "create", None, {"api_key": "sk-EXAMPLE-idem"})
    audit_service.record(db, change)
    db.commit()
    row = db.execute(select(AuditLog)).scalars().one()
    after = json.loads(row.after_json)
    assert after["api_key"] == REDACTED
    assert after["api_key_fp"] == fingerprint("sk-EXAMPLE-idem")
    assert after["api_key_len"] == len("sk-EXAMPLE-idem")
    db.close()


# ---------------------------------------------------------------- 3. reason


@pytest.mark.parametrize("reason", sorted(REASON))
def test_reason_accepts_all_registered_values(reason):
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct", None, None, reason=reason)
    assert change["reason"] == reason


def test_make_change_still_accepts_missing_reason():
    """`make_change()` **不**强制（它拿不到 actor，spec E1 的否决项）——强制在 record()。"""
    assert "correct" in REASON_REQUIRED_ACTIONS
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct",
        before={"amount": "100.00"}, after={"amount": "180.00"})
    assert change["reason"] is None and change["reason_note"] is None


def test_owner_correction_without_reason_is_422(chain_env):
    """spec 验收 1/E1：`actor='owner'` 的纠正类缺 reason → 422，**零审计行**。"""
    path, Session = chain_env
    db = Session()
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct",
        before={"amount": "100.00"}, after={"amount": "180.00"})
    with pytest.raises(ApiError) as ei:
        audit_service.record(db, change)
    assert ei.value.status_code == 422
    assert ei.value.code == ErrorCode.AUDIT_REASON_REQUIRED
    assert "action=correct" in ei.value.message
    db.rollback()
    assert db.execute(select(AuditLog)).scalars().all() == []

    # dict 路径（没走 make_change）同样拦
    with pytest.raises(ApiError) as ei2:
        audit_service.record(db, {
            "entity": "asset_snapshot", "row_id": 2, "action": "delete",
            "before": {"amount": "1.00"}, "after": None, "evidence_ref": None})
    assert ei2.value.code == ErrorCode.AUDIT_REASON_REQUIRED
    assert "action=delete" in ei2.value.message
    db.rollback()
    assert db.execute(select(AuditLog)).scalars().all() == []
    db.close()


def test_system_actor_correction_without_reason_is_allowed(chain_env):
    """spec 验收 3/E1：`actor='system'` 不拦，reason 落 NULL（＝未记录原因）。"""
    path, Session = chain_env
    db = Session()
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct",
        before={"amount": "100.00"}, after={"amount": "180.00"})
    row = audit_service.record(db, change, actor="system")
    db.commit()
    assert row.actor == "system" and row.reason is None
    assert audit_chain.verify(db)["ok"] is True
    db.close()


def test_owner_with_reason_writes_row(chain_env):
    """带 reason → 正常落行，链仍 ok。"""
    path, Session = chain_env
    db = Session()
    change = audit_service.make_change(
        "asset_snapshot", 1, "delete", before={"amount": "1.00"}, after=None,
        reason="correction")
    row = audit_service.record(db, change)
    db.commit()
    assert row.reason == "correction"
    assert audit_chain.verify(db)["ok"] is True
    db.close()


def test_reason_rejects_unknown_value():
    with pytest.raises(ValueError) as ei:
        audit_service.make_change("asset_snapshot", 1, "correct", reason="because")
    assert "非法审计 reason" in str(ei.value)


def test_reason_note_length_limit():
    ok = "x" * 200
    assert audit_service.make_change(
        "asset_snapshot", 1, "correct", reason="correction",
        reason_note=ok)["reason_note"] == ok
    with pytest.raises(ValueError) as ei:
        audit_service.make_change(
            "asset_snapshot", 1, "correct", reason="correction",
            reason_note="x" * 201)
    assert "reason_note 超长" in str(ei.value)


def test_record_rejects_bad_reason_on_dict_path(chain_env):
    """dict 路径（未经 make_change）也不放非法 reason 进库。"""
    path, Session = chain_env
    db = Session()
    with pytest.raises(ValueError):
        audit_service.record(db, {
            "entity": "asset_snapshot", "row_id": 1, "action": "create",
            "before": None, "after": None, "evidence_ref": None,
            "reason": "because"})
    db.rollback()
    db.close()


# ---------------------------------------------------------------- 4. 哈希口径不变


def test_record_persists_reason_without_changing_row_hash(chain_env):
    path, Session = chain_env
    db = Session()
    change = audit_service.make_change(
        "asset_snapshot", 1, "correct",
        before={"amount": "100.00"}, after={"amount": "180.00"},
        reason="correction", reason_note="示例：录错金额")
    row = audit_service.record(db, change)
    db.commit()

    assert row.reason == "correction" and row.reason_note == "示例：录错金额"
    # 加列不进链口径
    assert "reason" not in audit_chain.FIELDS
    assert "reason_note" not in audit_chain.FIELDS

    db.expire_all()
    stored = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().one()
    values = {f: getattr(stored, f) for f in audit_chain.FIELDS}
    recomputed = audit_chain.row_hash(stored.prev_hash, values)
    assert recomputed == audit_chain.row_hash(stored.prev_hash, stored)
    assert db.get(AuditChainState, 1).last_hash == recomputed
    assert audit_chain.verify(db)["ok"] is True

    # 只读输出与按 reason 过滤（关键字参数，不破老签名）
    out = audit_service.get_audit_log(db, stored.id)
    assert out["reason"] == "correction" and out["reason_note"] == "示例：录错金额"
    items, total = audit_service.list_audit_log(db, reason="correction")
    assert total == 1 and items[0]["id"] == stored.id
    assert audit_service.list_audit_log(db, reason="evolution")[1] == 0
    db.close()


# ---------------------------------------------------------------- 5. 迁移 0036


def _legacy_audit_db(path, rows=3):
    """0036 之前的真实形状：0009 建表 → 插入存量行 → 0010 → 0018 建链。"""
    conn = sqlite3.connect(path)
    _run_script(conn, M0009)
    for i in range(rows):
        conn.execute(
            "INSERT INTO audit_log (occurred_at, actor, entity, action, row_id,"
            " after_json) VALUES (?, 'owner', 'asset_snapshot', 'create', ?, ?)",
            (f"2026-03-0{i + 1}T00:00:00", i + 1, '{"amount":"%d.00"}' % (100 + i)))
    conn.commit()
    _run_script(conn, M0010)
    _run_script(conn, M0018)
    return conn


def test_0036_adds_nullable_columns_and_keeps_chain_ok(tmp_path):
    path = str(tmp_path / "legacy_0036.db")
    conn = _legacy_audit_db(path, rows=3)

    assert _run_script(conn, M0036) == "added=reason,reason_note"

    cols = {r[1]: (r[2].upper(), int(r[3]), r[4])
            for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert cols["reason"] == ("VARCHAR(20)", 0, None)
    assert cols["reason_note"] == ("VARCHAR(200)", 0, None)

    # 存量行不倒填：NULL＝未记录原因
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE reason IS NOT NULL"
        " OR reason_note IS NOT NULL").fetchone()[0] == 0

    # append-only 触发器仍在（UPDATE/DELETE 仍 ABORT）
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET reason='correction' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=1")

    # 重跑幂等
    assert _run_script(conn, M0036) == "noop"
    conn.close()

    # 加列不改链：verify 仍 ok
    engine = create_engine(f"sqlite:///{path}")
    db = sessionmaker(bind=engine)()
    report = audit_chain.verify(db)
    assert report["ok"] is True and report["checked"] == 3
    db.close()


def test_0036_skips_when_table_missing(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert _run_script(conn, M0036) == "skipped"
    conn.close()


# ------------------------------------------------ 6. 纠正类调用点必须传 reason


def _scan_missing_reason() -> set[str]:
    """静态扫描 `services/**/*.py`：纠正类 action 的调用点是否都传了 `reason`。

    spec E7（`audit-reason-enforce`）：`KNOWN_DEBT` 债务清单**已清零并删除**，
    这里改为**正向断言**——凡是 `action ∈ REASON_REQUIRED_ACTIONS` 的
    `make_change()/_change()` 调用，必须显式写 `reason=`（可以是变量/None 表达式，
    但键必须在场：键在场＝调用方想过「这条有没有理由」）。

    为什么键在场就够：真正的「有没有值」由运行时把守（`record()` 对 `actor='owner'`
    缺 reason 抛 422 `AUDIT_REASON_REQUIRED`），静态扫描只负责让**新调用点**
    无法悄悄绕过这次收口（spec E1 的「新增调用点无法绕过」）。
    """
    import ast
    root = Path(__file__).resolve().parents[1]
    missing: set[str] = set()
    for path in sorted(root.glob("services/**/*.py")):
        if path.name == "audit_service.py":     # 定义处，不是调用点
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name not in ("make_change", "_change"):
                continue
            action = None
            if len(node.args) > 2 and isinstance(node.args[2], ast.Constant):
                action = node.args[2].value
            for kw in node.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                    action = kw.value.value
            if action in REASON_REQUIRED_ACTIONS and not any(
                    kw.arg == "reason" for kw in node.keywords):
                rel = path.relative_to(root).as_posix()
                missing.add(f"{rel}:{action}")
    return missing


def test_reason_required_call_sites_pass_reason():
    """**正向断言**（spec E7）：纠正类调用点一律显式传 `reason=`，缺口必须为空。"""
    missing = _scan_missing_reason()
    assert missing == set(), (
        "以下纠正类审计调用点未传 reason（必须补上，或把该动作从 "
        f"REASON_REQUIRED_ACTIONS 里裁决掉）: {sorted(missing)}")


def test_known_debt_set_is_gone():
    """债务清单是过渡态不是永久设施（spec E7）：集合与其分支必须已删除。"""
    src = Path(__file__).read_text(encoding="utf-8")
    # 拼接构造，避免本断言自身把字面量写进被扫描的源码
    debt_symbol = "KNOWN" + "_DEBT"
    assert f"{debt_symbol} = frozenset" not in src
    assert f"set({debt_symbol})" not in src
