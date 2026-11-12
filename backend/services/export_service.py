"""CSV 导出（ADR-0019：分类读**资产项**，记录行只读）。

导出列与 `import_service.CSV_COLUMNS` 是同一套口径（导出即可回灌）：
`asset_class` 输出**完整路径**（如 `B 固定收益/B1 货币基金`）——路径是唯一能唯一定位
一个叶子的串（单独 name 在树里可能重名，code 又会随用户改 code 失效）；
`liquidity_days` 出整数档位而不是档位名（days 是权威，label 是展示层派生）；
`tags` 沿用 `|` 分隔约定。

`draft` 项的行**照常导出**（ADR-0020 D1：草稿态项计入合计，导出若丢掉这些行，
用户会对不上账——旧 ADR-0019 D9 的「草稿不进任何对外口径」已由 ADR-0020 D1 取代）。
"""
import csv
import io
import logging
import os
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, and_

from models import Asset
from services import classification_service

# 配置日志
logger = logging.getLogger(__name__)


# 导出目录配置
EXPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "exports")

# 确保导出目录存在
os.makedirs(EXPORTS_DIR, exist_ok=True)

# 导出列（顺序即 CSV 列序；与 import_service 的新列集合一致）
EXPORT_COLUMNS: tuple[str, ...] = (
    "asset_date", "asset_name", "amount", "side", "asset_class",
    "liquidity_days", "institution", "channel", "owner", "risk_level", "tags",
)

# `|` 分隔标签，沿用原约定（导入端按同一分隔符还原）
TAG_SEPARATOR = "|"


def get_export_records(
    db: Session,
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> list[dict]:
    """Get records for export with optional period filtering.

    Args:
        period_type: 'all', 'day', 'month', 'quarter', 'year', 'custom'
        year: Filter by year (required for month, quarter, day)
        month: Filter by month (1-12, required for day)
        quarter: Filter by quarter (1-4)
        day: Filter by day (1-31)
        date_from: Start date for custom range filter
        date_to: End date for custom range filter
    """
    # 构建基础查询（v5：导出 active 快照行，superseded 历史行不重复导出）
    query = select(Asset).where(Asset.superseded_by.is_(None)).order_by(Asset.asset_date, Asset.asset_name)

    # 应用时间过滤条件
    if period_type and period_type != "all":
        conditions = []

        if year:
            conditions.append(Asset.asset_date >= date(year, 1, 1))
            conditions.append(Asset.asset_date <= date(year, 12, 31))

        if period_type == "month" and month:
            conditions.append(Asset.asset_date >= date(year, month, 1))
            if month == 12:
                conditions.append(Asset.asset_date <= date(year + 1, 1, 1))
            else:
                conditions.append(Asset.asset_date < date(year, month + 1, 1))

        elif period_type == "quarter" and quarter:
            start_month = (quarter - 1) * 3 + 1
            end_month = quarter * 3 + 1
            conditions.append(Asset.asset_date >= date(year, start_month, 1))
            if end_month > 12:
                conditions.append(Asset.asset_date < date(year + 1, 1, 1))
            else:
                conditions.append(Asset.asset_date < date(year, end_month, 1))

        elif period_type == "day" and month and day:
            conditions.append(Asset.asset_date == date(year, month, day))

        elif period_type == "custom" and date_from and date_to:
            conditions.append(Asset.asset_date >= date_from)
            conditions.append(Asset.asset_date <= date_to)

        if conditions:
            query = query.where(and_(*conditions))

    # 草稿项**不排除**（ADR-0020 D1：draft 项计入合计，导出必须与合计同源）。
    # 旧实现按 ADR-0019 D9 排除 draft，导致导出 CSV 缺行、与首页合计对不上账。
    # 无 item 的存量行照旧导出。

    # 执行查询
    assets = db.execute(query).scalars().all()

    item_ids = sorted({a.asset_item_id for a in assets if a.asset_item_id})
    # 分类读项级（一次批量取，避免逐行懒加载）
    classifications = classification_service.classifications_for_items(db, item_ids)
    class_paths = {row["id"]: row["path"]
                   for row in classification_service.list_asset_classes(db)}
    # channel_id → name（classification_of 只回 id，不带名字）
    channel_names = {row["id"]: row["name"]
                     for row in classification_service.list_channels(db)}
    # 归属：唯一真相源＝项级 `owner_id`（ADR-0019 D1/D6 → members），由
    # `classification_of` 一次带出；旧 ADR-0011 的「变更史回退」已随归属收口删除
    # （史降级为纯审计，不再是读路径的数据源）。
    # 同一项的所有记录行输出同一归属（分类挂项，不挂记录）。

    # 构建导出记录
    records = []
    for asset in assets:
        cls = classifications.get(asset.asset_item_id) or {}
        owner_name = cls.get("owner_name") or ""
        tags = sorted(t["name"] for t in cls.get("tags") or [])
        records.append({
            "asset_date": asset.asset_date.strftime("%Y-%m-%d"),
            "asset_name": asset.asset_name,
            "amount": str(asset.amount),
            "side": asset.side or "asset",
            # 完整路径（唯一可定位叶子）；未分类（draft/存量行）留空串
            "asset_class": class_paths.get(cls.get("asset_class_id"), ""),
            "liquidity_days": "" if cls.get("liquidity_days") is None
                              else int(cls["liquidity_days"]),
            "institution": cls.get("institution_name") or "",
            "channel": channel_names.get(cls.get("channel_id"), ""),
            "owner": owner_name,
            "risk_level": cls.get("risk_level") or "",
            "tags": TAG_SEPARATOR.join(tags),
        })

    return records


def generate_csv(records: list[dict]) -> str:
    """Generate CSV content from records."""
    if not records:
        return ""

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=list(EXPORT_COLUMNS),
        lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def get_export_filename(
    period_type: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    quarter: Optional[int] = None,
    day: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> str:
    """Generate export filename based on filters."""
    if period_type == "all" or not period_type:
        return "assets_export_all.csv"

    if period_type == "custom" and date_from and date_to:
        return f"assets_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.csv"

    parts = ["assets"]
    if year:
        parts.append(str(year))
    if period_type == "quarter" and quarter:
        parts.append(f"Q{quarter}")
    elif period_type == "month" and month:
        parts.append(f"{month:02d}")
    elif period_type == "day" and month and day:
        parts.append(f"{month:02d}{day:02d}")

    return "_".join(parts) + ".csv"