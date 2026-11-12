import os
import sys

# 让 backend/ 进入 sys.path，测试可 import database/models/services
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401  —— 在 Base.metadata 上注册全部表
from database import Base


@pytest.fixture()
def db():
    """每个测试独立的内存 SQLite 会话（StaticPool 保证单连接共享内存库）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def link_items(db):
    """v5 fixture 补 item 解析（spec §6-⑧）。

    测试直接 `Asset(...)` 直插的行没有 asset_item_id；迁移后的库里每一行都
    有 item（0008 回填效果）。本助手把无 item 的行按 name 经 ledger 单点
    （get_or_create_item）补挂，使 fixture 数据形态与迁移后真库一致。
    """
    from sqlalchemy import select
    from models import Asset
    from services.asset_service import get_or_create_item

    for a in db.execute(select(Asset).where(Asset.asset_item_id.is_(None))).scalars().all():
        a.asset_item_id = get_or_create_item(db, a.asset_name).id
    db.flush()


def get_or_create_item_for(db, name):
    """按名经 ledger 单点取 item id（fixture 直插 Asset 行挂接用）。"""
    from services.asset_service import get_or_create_item

    return get_or_create_item(db, name).id


def legacy_eav_tables(db) -> set[str]:
    """列出现存旧 EAV 表名（`attributes` / `asset_attributes`）。

    0027 起两张表已从 schema 消失：记录级分类不再是「零写入」，而是「无处可写」。
    断言写侧正确性时用它（`== set()` 即记录级承载物不存在）。
    """
    from sqlalchemy import text

    return {r[0] for r in db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('attributes','asset_attributes')"))}