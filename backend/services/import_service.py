import csv
import io
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from models import AssetRecord, FundType, Account, ImportBatch, LiquidityRating
from schemas import ImportPreviewRow, NewAttribute


def normalize_amount(raw: str) -> Decimal:
    s = raw.strip().strip('"').strip("'")
    s = s.replace("\uff65", "").replace("\u00a5", "")
    s = s.replace("\uffe5", "").replace("\u00a5", "")
    # Handle both ￥ and ¥
    for ch in ["￥", "¥"]:
        s = s.replace(ch, "")
    s = s.replace(",", "").strip()
    if not s or s == "-":
        return Decimal("0.00")
    return Decimal(s).quantize(Decimal("0.01"))


def normalize_date(raw: str) -> date:
    s = raw.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Fallback: split by / or -
    parts = re.split(r"[/\-]", s)
    if len(parts) == 3:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    raise ValueError(f"Cannot parse date: {raw}")


def normalize_liquidity(raw: str) -> str:
    s = raw.strip()
    m = re.match(r"(T\+\d+)", s)
    if m:
        return m.group(1)
    return s


def get_or_create_fund_type(db: Session, name: str) -> FundType:
    existing = db.execute(select(FundType).where(FundType.name == name)).scalar_one_or_none()
    if existing:
        return existing

    parts = name.split("-")
    parent_id = None
    for i in range(len(parts)):
        partial_name = "-".join(parts[: i + 1])
        ft = db.execute(select(FundType).where(FundType.name == partial_name)).scalar_one_or_none()
        if not ft:
            ft = FundType(name=partial_name, parent_id=parent_id, level=i)
            db.add(ft)
            db.flush()
        parent_id = ft.id

    return db.execute(select(FundType).where(FundType.name == name)).scalar_one()


def get_or_create_account(db: Session, name: str) -> Account:
    existing = db.execute(select(Account).where(Account.name == name)).scalar_one_or_none()
    if existing:
        return existing
    acc = Account(name=name)
    db.add(acc)
    db.flush()
    return acc


def get_or_create_liquidity_rating(db: Session, name: str) -> LiquidityRating:
    """获取或创建流动性评级"""
    existing = db.execute(select(LiquidityRating).where(LiquidityRating.name == name)).scalar_one_or_none()
    if existing:
        return existing
    # 自动创建新的流动性评级
    rating = LiquidityRating(name=name, sort_order=0)
    db.add(rating)
    db.flush()
    return rating


def parse_csv_content(content: str, filename: str) -> list[dict]:
    rows = []
    if content.startswith("\ufeff"):
        content = content[1:]

    reader = csv.DictReader(io.StringIO(content))
    for i, row in enumerate(reader, start=2):
        try:
            raw_date = row.get("asset_date", "").strip()
            raw_amount = row.get("amount", "0").strip()
            raw_liquidity = row.get("liquidity_rating", "").strip()
            raw_fund_type = row.get("fund_type", "").strip()
            raw_asset_name = row.get("asset_name", "").strip()
            raw_account = row.get("account", "").strip()

            if not raw_asset_name:
                continue

            rows.append({
                "row_num": i,
                "asset_date": normalize_date(raw_date),
                "liquidity_rating": normalize_liquidity(raw_liquidity),
                "fund_type": raw_fund_type,
                "asset_name": raw_asset_name,
                "account": raw_account,
                "amount": normalize_amount(raw_amount),
            })
        except Exception as e:
            rows.append({
                "row_num": i,
                "error": str(e),
            })
    return rows


def detect_new_attributes(db: Session, parsed_rows: list[dict]) -> list[NewAttribute]:
    """检测导入数据中的新属性"""
    # 获取现有属性
    existing_liquidity = {r.name for r in db.execute(select(LiquidityRating)).scalars().all()}
    existing_fund_types = {ft.name for ft in db.execute(select(FundType)).scalars().all()}
    existing_accounts = {a.name for a in db.execute(select(Account)).scalars().all()}

    new_attrs = {}

    for row in parsed_rows:
        if "error" in row:
            continue

        # 检查流动性评级
        liquidity = row.get("liquidity_rating", "")
        if liquidity and liquidity not in existing_liquidity:
            key = f"liquidity_rating:{liquidity}"
            if key not in new_attrs:
                new_attrs[key] = NewAttribute(
                    type="liquidity_rating",
                    name=liquidity,
                    rows=[]
                )
            if row["row_num"] not in new_attrs[key].rows:
                new_attrs[key].rows.append(row["row_num"])

        # 检查资产类型
        fund_type = row.get("fund_type", "")
        if fund_type and fund_type not in existing_fund_types:
            key = f"fund_type:{fund_type}"
            if key not in new_attrs:
                new_attrs[key] = NewAttribute(
                    type="fund_type",
                    name=fund_type,
                    rows=[]
                )
            if row["row_num"] not in new_attrs[key].rows:
                new_attrs[key].rows.append(row["row_num"])

        # 检查账户
        account = row.get("account", "")
        if account and account not in existing_accounts:
            key = f"account:{account}"
            if key not in new_attrs:
                new_attrs[key] = NewAttribute(
                    type="account",
                    name=account,
                    rows=[]
                )
            if row["row_num"] not in new_attrs[key].rows:
                new_attrs[key].rows.append(row["row_num"])

    return list(new_attrs.values())


def preview_import(
    db: Session, content: str, filename: str
) -> tuple[ImportBatch, list[ImportPreviewRow], list[ImportPreviewRow], list[NewAttribute]]:
    batch = ImportBatch(filename=filename, record_count=0, status="pending")
    db.add(batch)
    db.flush()

    parsed = parse_csv_content(content, filename)
    valid = []
    invalid = []

    for row in parsed:
        if "error" in row:
            invalid.append(ImportPreviewRow(
                row_num=row["row_num"],
                asset_date=date.today(),
                liquidity_rating="",
                fund_type="",
                asset_name="",
                account="",
                amount=Decimal("0"),
                error=row["error"],
            ))
            continue

        has_conflict = db.execute(
            select(AssetRecord).where(
                AssetRecord.asset_date == row["asset_date"],
                AssetRecord.asset_name == row["asset_name"],
            )
        ).scalar_one_or_none() is not None

        valid.append(ImportPreviewRow(
            row_num=row["row_num"],
            asset_date=row["asset_date"],
            liquidity_rating=row["liquidity_rating"],
            fund_type=row["fund_type"],
            asset_name=row["asset_name"],
            account=row["account"],
            amount=row["amount"],
            has_conflict=has_conflict,
        ))

    # 检测新属性
    new_attributes = detect_new_attributes(db, parsed)

    return batch, valid, invalid, new_attributes


def import_csv_records(
    db: Session, content: str, filename: str, conflict_resolution: str = "skip", attribute_actions: dict = None
) -> ImportBatch:
    parsed = parse_csv_content(content, filename)
    valid_rows = [r for r in parsed if "error" not in r]

    batch = ImportBatch(filename=filename, record_count=0, status="processing")
    db.add(batch)
    db.flush()

    imported_count = 0
    for row in valid_rows:
        # 根据用户选择处理新属性
        fund_type = _get_or_handle_fund_type(db, row["fund_type"], attribute_actions)
        account = _get_or_handle_account(db, row["account"], attribute_actions)
        liquidity_rating = _get_or_handle_liquidity_rating(db, row["liquidity_rating"], attribute_actions)

        # 如果属性被忽略且不存在，跳过该行
        if fund_type is None or account is None or liquidity_rating is None:
            continue

        existing = db.execute(
            select(AssetRecord).where(
                AssetRecord.asset_date == row["asset_date"],
                AssetRecord.asset_name == row["asset_name"],
            )
        ).scalar_one_or_none()

        if existing:
            if conflict_resolution == "overwrite":
                existing.liquidity_rating_id = liquidity_rating.id
                existing.fund_type_id = fund_type.id
                existing.account_id = account.id
                existing.amount = row["amount"]
                existing.import_batch_id = batch.id
                existing.updated_at = datetime.now()
                imported_count += 1
        else:
            record = AssetRecord(
                asset_date=row["asset_date"],
                liquidity_rating_id=liquidity_rating.id,
                fund_type_id=fund_type.id,
                asset_name=row["asset_name"],
                account_id=account.id,
                amount=row["amount"],
                import_batch_id=batch.id,
            )
            db.add(record)
            imported_count += 1

    batch.record_count = imported_count
    batch.status = "success"
    db.commit()
    return batch


def _get_or_handle_fund_type(db: Session, name: str, attribute_actions: dict = None) -> FundType | None:
    """根据用户选择获取或处理资产类型"""
    existing = db.execute(select(FundType).where(FundType.name == name)).scalar_one_or_none()
    if existing:
        return existing

    # 检查用户选择
    action = attribute_actions.get(name, "create") if attribute_actions else "create"
    if action == "ignore":
        return None

    # 默认创建
    return get_or_create_fund_type(db, name)


def _get_or_handle_account(db: Session, name: str, attribute_actions: dict = None) -> Account | None:
    """根据用户选择获取或处理账户"""
    existing = db.execute(select(Account).where(Account.name == name)).scalar_one_or_none()
    if existing:
        return existing

    # 检查用户选择
    action = attribute_actions.get(name, "create") if attribute_actions else "create"
    if action == "ignore":
        return None

    # 默认创建
    return get_or_create_account(db, name)


def _get_or_handle_liquidity_rating(db: Session, name: str, attribute_actions: dict = None) -> LiquidityRating | None:
    """根据用户选择获取或处理流动性评级"""
    existing = db.execute(select(LiquidityRating).where(LiquidityRating.name == name)).scalar_one_or_none()
    if existing:
        return existing

    # 检查用户选择
    action = attribute_actions.get(name, "create") if attribute_actions else "create"
    if action == "ignore":
        return None

    # 默认创建
    return get_or_create_liquidity_rating(db, name)


def seed_import_directory(db: Session, import_dir: str) -> list[ImportBatch]:
    batches = []
    files = sorted(os.listdir(import_dir))

    # Separate export file (historical) from dated files
    export_files = [f for f in files if not f[0].isdigit() and f.endswith(".csv")]
    dated_files = [f for f in files if f[0].isdigit() and f.endswith(".csv")]

    # Import export file first (historical baseline), then dated files (overwrite on conflict)
    for filename in export_files:
        filepath = os.path.join(import_dir, filename)
        with open(filepath, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        batch = import_csv_records(db, content, filename, conflict_resolution="skip")
        batches.append(batch)

    for filename in dated_files:
        filepath = os.path.join(import_dir, filename)
        with open(filepath, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        batch = import_csv_records(db, content, filename, conflict_resolution="overwrite")
        batches.append(batch)

    return batches
