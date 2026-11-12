"""库角色四档推导测试（spec `.scratch/db-dev-suffix` D1/D3、§3 第 2–3 条）。

覆盖：默认开发库文件名、四档推导（dev/staging/prod/unknown）、`DB_ROLE`
环境变量覆盖、非法值原样透出且不抛错（不硬失败）。

只测纯函数与模块常量，不建库、不建 engine 文件、不碰 data/。
"""
import os

import pytest

import database


# ---------------------------------------------------------------- 默认路径


@pytest.mark.skipif(
    os.environ.get("DATABASE_PATH"),
    reason="环境注入了 DATABASE_PATH，默认路径断言不适用（§3-2 用 env -u 验证）",
)
def test_default_db_path_is_dev_suffix():
    """默认库文件名带 `_dev` 后缀（D1）；非 env 注入时 DB_PATH 与 URL 同源。"""
    assert database.DB_PATH.endswith(os.path.join("data", "person_fin_dev.db"))
    assert database.DATABASE_URL == f"sqlite:///{database.DB_PATH}"


# ---------------------------------------------------------------- 四档推导


def test_derive_dev_role():
    assert database.derive_db_role("/app/data/person_fin_dev.db") == "dev"


def test_derive_staging_role():
    assert database.derive_db_role("/tmp/xyz/person_fin_staging.db") == "staging"


def test_derive_prod_role():
    assert database.derive_db_role("deploy/data/person_fin.db") == "prod"


def test_derive_unknown_role():
    """临时副本/测试夹具/归档文件不冒认 prod（业主 2026-11-09 裁决）。"""
    assert database.derive_db_role("/tmp/fixture-xyz.db") == "unknown"
    # 后缀判定必须严格结尾：`_dev.db.bak` 不是 dev
    assert database.derive_db_role("/tmp/person_fin_dev.db.bak") == "unknown"
    assert database.derive_db_role("") == "unknown"


# ---------------------------------------------------------------- 环境变量


def test_env_role_overrides_filename(monkeypatch):
    """`DB_ROLE` 优先于文件名推导。"""
    monkeypatch.setenv("DB_ROLE", "staging")
    assert database.resolve_db_role("/app/data/person_fin_dev.db") == "staging"


def test_invalid_env_role_passes_through_without_raising(monkeypatch):
    """非法值原样透出（只告警），不抛异常、不回退到文件名推导。"""
    monkeypatch.setenv("DB_ROLE", "Staging-Typo")
    assert database.resolve_db_role("/app/data/person_fin_dev.db") == "Staging-Typo"

    # 空值视为未设置 → 回到文件名推导
    monkeypatch.setenv("DB_ROLE", "  ")
    assert database.resolve_db_role("/app/data/person_fin_dev.db") == "dev"


# ---------------------------------------------------------------- 模块常量


def test_module_role_matches_resolver():
    """模块级 DB_ROLE 与解析函数在同一路径上一致（启动日志打的就是它）。"""
    assert database.DB_ROLE == database.resolve_db_role(database.DB_PATH)