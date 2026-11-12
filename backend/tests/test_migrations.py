"""v5 一期·步骤 2「迁移机制升级」测试（spec §4 六条 + 预留面）。

沙箱：tmp_path + monkeypatch DATABASE_PATH；runner/backup 调用时解析该 env。
真实 backend/migrations/ 只读——探针脚本一律拷进 tmp scripts dir 后追加，
绝不写入真实目录（测试零污染）。
"""
import json
import os
import shutil
import sqlite3
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全部表注册进 Base.metadata
from database import Base
from services import backup_service
from services import db_migration_service as dbs

CATALOG = [1, 2, 3, 4, 5, 6, 7]  # 现有编号 0001~0007


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "person_fin.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    return {"data": data, "db": db}


def _touch_empty(db_path):
    """造一个空库文件（无业务表）——全新库前置状态。"""
    sqlite3.connect(str(db_path)).close()


def _make_v4_assets(db_path, probe=True):
    """v4 形态库：assets 无 side 列，amount=-5 探针行。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name TEXT, amount VARCHAR(20))")
    if probe:
        conn.execute("INSERT INTO assets (name, amount) VALUES ('probe-liability', -5)")
    conn.commit()
    conn.close()


def _scripts_dir(tmp_path, extra=None):
    """拷贝真实 migrations/ 的 **v4 编目（0001~0007）** 到 tmp（行为为真），可选追加探针脚本。

    0008 及以后（身份层/审计/幂等）属步骤 3/4/5 主题且首跑必实跑（无 CLAIM），
    本文件锁定步骤 2 的 runner/认领机制，目录范围只保留 0001~0007（tmp 库也不含
    audit_log 表，runner 的 migration_run 记账按「表不存在则跳过」守卫自动静默）；
    v5 脚本行为由 test_identity.py / test_audit.py / test_contract.py 覆盖。
    探针脚本一律写入 tmp 目录，绝不触碰真实 migrations/（测试零污染）。
    """
    d = tmp_path / "migrations"
    d.mkdir(exist_ok=True)
    for fn in os.listdir(dbs._MIGRATIONS_DIR):
        if fn.endswith(".py") and "0001" <= fn[:4] <= "0007":
            shutil.copy(os.path.join(dbs._MIGRATIONS_DIR, fn), d / fn)
    for name, content in (extra or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    return str(d)


def _add_scripts(scripts_dir, mapping):
    """在已认领的 scripts 目录追加新编号脚本（模拟「既有库 + 未来新增迁移」）。"""
    for name, content in mapping.items():
        with open(os.path.join(scripts_dir, name), "w", encoding="utf-8") as f:
            f.write(content)


def _sv_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT version, baseline FROM schema_version ORDER BY version").fetchall()
    finally:
        conn.close()


def _tables(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _bundles(data_dir):
    bdir = data_dir / "backup"
    if not bdir.is_dir():
        return []
    return [f for f in sorted(bdir.iterdir()) if f.name.startswith("backup_") and f.name.endswith(".zip")]


def _manifest(bundle):
    with zipfile.ZipFile(bundle) as zf:
        return json.loads(zf.read("manifest.json"))


# ① 全新库：runner 后 7 条全登记（baseline=0）、seed 生效、无 pre_migration bundle
def test_01_fresh_db_registers_all_baseline0_no_bundle(env, tmp_path):
    _touch_empty(env["db"])
    scripts = _scripts_dir(tmp_path)
    report = dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)

    assert sorted(report.applied) == CATALOG
    assert report.baseline == []
    rows = _sv_rows(env["db"])
    assert [v for v, _ in rows] == CATALOG
    assert all(b == 0 for _, b in rows)
    assert _bundles(env["data"]) == []  # 全新库不前置备份

    # seed 生效：create_all 出结构后 seed_default_llm_providers 落库
    eng = create_engine(f"sqlite:///{env['db']}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    from services.llm_verification_service import seed_default_llm_providers
    with Session() as s:
        seed_default_llm_providers(s)
        cnt = s.execute(sqltext("SELECT count(*) FROM llm_providers")).scalar()
    eng.dispose()
    assert cnt == 2


# ② v4 形态库：认领登记 baseline=1、探针未被回填（证明未执行本体）
def test_02_v4_claim_does_not_execute(env, tmp_path):
    _make_v4_assets(env["db"], probe=True)
    scripts = _scripts_dir(tmp_path)
    report = dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)

    assert report.baseline == CATALOG
    assert report.applied == []
    rows = _sv_rows(env["db"])
    assert all(b == 1 for _, b in rows)

    # 探针未被回填：amount 仍 -5 且 side 列仍未添加（真实 0002 会改这两处）
    conn = sqlite3.connect(str(env["db"]))
    amount = conn.execute("SELECT amount FROM assets WHERE name='probe-liability'").fetchone()[0]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    conn.close()
    assert str(amount) == "-5"
    assert "side" not in cols
    assert _bundles(env["data"]) == []  # v4 认领不前置备份


# ③ 既有库 + 探针脚本 0008：生成 bundle 且 reason=pre_migration、prune=False（spy）
def test_03_incremental_runs_probe_with_pre_migration_backup(env, tmp_path, monkeypatch):
    # 先用真实 0001~0007 认领成既有库（v4 形态）
    scripts = _scripts_dir(tmp_path)
    _make_v4_assets(env["db"], probe=False)
    dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)
    assert {v for v, _ in _sv_rows(env["db"])} == set(CATALOG)

    # 追加未来编号 0008（真实目录不受影响）
    _add_scripts(scripts, {
        "0008_probe.py": "def upgrade(conn):\n    conn.execute('CREATE TABLE probe_marker (x)')\n",
    })

    calls = []
    real = backup_service.create_backup
    def spy(*a, **k):
        calls.append(k)
        return real(*a, **k)
    monkeypatch.setattr(backup_service, "create_backup", spy)

    report = dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)
    assert report.applied == [8]
    assert report.baseline == []
    assert calls and calls[-1]["reason"] == "pre_migration" and calls[-1]["prune"] is False

    bundles = _bundles(env["data"])
    assert bundles, "应生成 pre_migration bundle"
    assert _manifest(bundles[-1])["reason"] == "pre_migration"
    assert "probe_marker" in _tables(env["db"])


# ④ runner 连跑两次：第二次 no-op、schema_version 无重复
def test_04_rerun_is_noop(env, tmp_path):
    _touch_empty(env["db"])
    scripts = _scripts_dir(tmp_path)
    r1 = dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)
    r2 = dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)

    assert r1.applied == CATALOG
    assert r2.noop  # applied/baseline 皆空、failed None
    assert len(_sv_rows(env["db"])) == 7  # 无重复


# ⑤ 探针脚本 raise：中止、该脚本未登记、已成功脚本保留、备份存在
def test_05_failure_aborts_keeps_success(env, tmp_path):
    scripts = _scripts_dir(tmp_path)
    _make_v4_assets(env["db"], probe=False)
    dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)  # 认领 1-7
    _add_scripts(scripts, {
        "0008_ok.py": "def upgrade(conn):\n    conn.execute('CREATE TABLE marker_ok (x)')\n",
        "0009_bad.py": "def upgrade(conn):\n    raise RuntimeError('boom')\n",
    })

    with pytest.raises(RuntimeError) as ei:
        dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)
    assert "0009" in str(ei.value)

    versions = {v for v, _ in _sv_rows(env["db"])}
    assert 8 in versions      # 已成功脚本保留
    assert 9 not in versions  # 失败脚本未登记
    assert "marker_ok" in _tables(env["db"])
    assert _bundles(env["data"])  # 前置备份已生成


# ⑥ mock 备份失败：任何脚本未执行
def test_06_backup_failure_blocks_execution(env, tmp_path, monkeypatch):
    scripts = _scripts_dir(tmp_path)
    _make_v4_assets(env["db"], probe=False)
    dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)  # 认领 1-7
    _add_scripts(scripts, {
        "0008_probe.py": "def upgrade(conn):\n    conn.execute('CREATE TABLE probe_marker (x)')\n",
    })

    def boom(*a, **k):
        raise OSError("backup dir not writable")
    monkeypatch.setattr(backup_service, "create_backup", boom)

    with pytest.raises(RuntimeError):
        dbs.run_migrations(db_path=str(env["db"]), scripts_dir=scripts)

    assert {v for v, _ in _sv_rows(env["db"])} == set(CATALOG)  # 8 未登记
    assert "probe_marker" not in _tables(env["db"])             # 本体未执行


# 预留面：run_migrations() 无参可调用（默认解析 DATABASE_PATH 与真实 migrations 目录）
def test_07_public_surface_default_args(env, tmp_path, monkeypatch):
    # 把 tmp 里的库指向真实 catalog 目录（只读，不污染）
    _touch_empty(env["db"])
    monkeypatch.setattr(dbs, "_MIGRATIONS_DIR", _scripts_dir(tmp_path))
    report = dbs.run_migrations()
    assert sorted(report.applied) == CATALOG
