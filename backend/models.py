from datetime import date, datetime
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Numeric, ForeignKey, Index, UniqueConstraint, Boolean
)
from sqlalchemy.orm import relationship
from database import Base


class FundType(Base):
    __tablename__ = "fund_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    parent_id = Column(Integer, ForeignKey("fund_types.id"), nullable=True)
    level = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    parent = relationship("FundType", remote_side=[id], backref="children")
    records = relationship("AssetRecord", back_populates="fund_type")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    records = relationship("AssetRecord", back_populates="account")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    record_count = Column(Integer, nullable=False, default=0)
    imported_at = Column(DateTime, nullable=False, default=datetime.now)
    status = Column(String(20), nullable=False, default="success")

    records = relationship("AssetRecord", back_populates="import_batch")


class LiquidityRating(Base):
    __tablename__ = "liquidity_ratings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    records = relationship("AssetRecord", back_populates="liquidity_rating")


class AssetOwner(Base):
    __tablename__ = "asset_owners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    records = relationship("AssetRecord", back_populates="owner")


class AssetRecord(Base):
    __tablename__ = "asset_records"
    __table_args__ = (
        UniqueConstraint("asset_date", "asset_name", name="uq_date_asset_name"),
        Index("ix_asset_records_date", "asset_date"),
        Index("ix_asset_records_fund_type", "fund_type_id"),
        Index("ix_asset_records_account", "account_id"),
        Index("ix_asset_records_liquidity_rating", "liquidity_rating_id"),
        Index("ix_asset_records_owner", "owner_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_date = Column(Date, nullable=False)
    liquidity_rating_id = Column(Integer, ForeignKey("liquidity_ratings.id"), nullable=False)
    fund_type_id = Column(Integer, ForeignKey("fund_types.id"), nullable=False)
    asset_name = Column(String(100), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    owner_id = Column(Integer, ForeignKey("asset_owners.id"), nullable=True)
    amount = Column(Numeric(15, 2), nullable=False)
    import_batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    liquidity_rating = relationship("LiquidityRating", back_populates="records")
    fund_type = relationship("FundType", back_populates="records")
    account = relationship("Account", back_populates="records")
    owner = relationship("AssetOwner", back_populates="records")
    import_batch = relationship("ImportBatch", back_populates="records")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    dimension = Column(String(50), nullable=False)
    target_id = Column(String(100), nullable=True)
    period_type = Column(String(20), nullable=False)
    compare_type = Column(String(20), nullable=False)
    compare_period = Column(String(50), nullable=True)
    amount_threshold = Column(Numeric(15, 2), nullable=True)
    percent_threshold = Column(Numeric(5, 2), nullable=True)
    direction = Column(String(10), default="both")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class ExportHistory(Base):
    """导出历史记录"""
    __tablename__ = "export_history"
    __table_args__ = (
        Index("ix_export_history_export_time", "export_time"),
        Index("ix_export_history_export_type", "export_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    export_time = Column(DateTime, nullable=False, default=datetime.now)
    export_type = Column(String(20), nullable=False)  # manual, auto
    filename = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=True)
    operator = Column(String(100), nullable=True)  # 操作人，自动导出时为 null
    rule_name = Column(String(100), nullable=True)  # 自动导出时的规则名称
    file_path = Column(String(500), nullable=True)  # 文件存储路径
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AutoExportRule(Base):
    """自动导出规则"""
    __tablename__ = "auto_export_rules"
    __table_args__ = (
        Index("ix_auto_export_rules_is_active", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    cron_expression = Column(String(100), nullable=False)
    export_format = Column(String(20), nullable=False, default="csv")  # csv, json
    filename_template = Column(String(255), nullable=True)  # 文件名模板
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AllocationTarget(Base):
    __tablename__ = "allocation_targets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dimension = Column(String(50), nullable=False)  # fund_type, liquidity_rating, account
    target_id = Column(String(100), nullable=False)  # 类型ID、评级名称、账户ID
    parent_id = Column(Integer, ForeignKey("allocation_targets.id"), nullable=True)
    target_percent = Column(Numeric(5, 2), nullable=False)  # 目标比例 0-100
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    parent = relationship("AllocationTarget", remote_side=[id], backref="children")
