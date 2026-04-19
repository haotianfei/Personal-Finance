import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import shutil
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Any, Dict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session
from sqlalchemy import select, inspect, text

from models import AssetRecord, FundType, Account, ImportBatch, LiquidityRating, AssetOwner
from schemas import ImportPreviewRow, NewAttribute


# 配置表外键映射关系
CONFIG_TABLES = {
    'accounts': {'model': Account, 'key_field': 'name', 'id_field': 'id'},
    'fund_types': {'model': FundType, 'key_field': 'name', 'id_field': 'id'},
    'liquidity_ratings': {'model': LiquidityRating, 'key_field': 'name', 'id_field': 'id'},
    'asset_owners': {'model': AssetOwner, 'key_field': 'name', 'id_field': 'id'},
}

# asset_records 的外键依赖
ASSET_RECORDS_FOREIGN_KEYS = {
    'account_id': {'table': 'accounts', 'model': Account, 'key_field': 'name'},
    'fund_type_id': {'table': 'fund_types', 'model': FundType, 'key_field': 'name'},
    'liquidity_rating_id': {'table': 'liquidity_ratings', 'model': LiquidityRating, 'key_field': 'name'},
    'owner_id': {'table': 'asset_owners', 'model': AssetOwner, 'key_field': 'name'},
}


class ForeignKeyMapper:
    """外键映射管理器 - 用于智能映射模式"""
    
    def __init__(self):
        # 存储源ID到目标ID的映射
        # 格式: {table_name: {source_id: target_id}}
        self.id_mappings: Dict[str, Dict[int, int]] = {}
        # 存储无法映射的ID
        self.unmapped_ids: Dict[str, set] = {}
    
    def add_mapping(self, table_name: str, source_id: int, target_id: int):
        """添加ID映射关系"""
        if table_name not in self.id_mappings:
            self.id_mappings[table_name] = {}
        self.id_mappings[table_name][source_id] = target_id
        print(f"[MAPPER] {table_name}: {source_id} -> {target_id}")
    
    def get_target_id(self, table_name: str, source_id: Optional[int]) -> Optional[int]:
        """获取目标ID，如果无法映射返回None"""
        if source_id is None:
            return None
        if table_name not in self.id_mappings:
            return None
        return self.id_mappings[table_name].get(source_id)
    
    def add_unmapped(self, table_name: str, source_id: int):
        """记录无法映射的ID"""
        if table_name not in self.unmapped_ids:
            self.unmapped_ids[table_name] = set()
        self.unmapped_ids[table_name].add(source_id)
        print(f"[MAPPER] {table_name}: {source_id} 无法映射")
    
    def is_unmapped(self, table_name: str, source_id: int) -> bool:
        """检查ID是否无法映射"""
        return table_name in self.unmapped_ids and source_id in self.unmapped_ids[table_name]
    
    def build_mapping_from_config_table(self, db: Session, table_name: str, source_data: list[dict]):
        """从配置表数据构建ID映射
        
        基于key_field（如name）匹配源记录和目标记录，建立ID映射
        """
        if table_name not in CONFIG_TABLES:
            print(f"[MAPPER] 未知的配置表: {table_name}")
            return
        
        config = CONFIG_TABLES[table_name]
        model = config['model']
        key_field = config['key_field']
        id_field = config['id_field']
        
        print(f"[MAPPER] 构建 {table_name} 的ID映射...")
        
        for source_row in source_data:
            source_id = source_row.get(id_field)
            key_value = source_row.get(key_field)
            
            if source_id is None or key_value is None:
                continue
            
            # 在目标数据库中查找匹配的记录
            target_record = db.execute(
                select(model).where(getattr(model, key_field) == key_value)
            ).scalar_one_or_none()
            
            if target_record:
                target_id = getattr(target_record, id_field)
                self.add_mapping(table_name, source_id, target_id)
            else:
                self.add_unmapped(table_name, source_id)
        
        mapped_count = len(self.id_mappings.get(table_name, {}))
        unmapped_count = len(self.unmapped_ids.get(table_name, set()))
        print(f"[MAPPER] {table_name} 映射完成: {mapped_count} 个已映射, {unmapped_count} 个无法映射")
    
    def remap_asset_record_foreign_keys(self, row: dict) -> dict:
        """重新映射asset_records的外键ID
        
        将源数据中的外键ID替换为目标系统的ID
        """
        new_row = row.copy()
        
        for fk_field, fk_config in ASSET_RECORDS_FOREIGN_KEYS.items():
            if fk_field not in row:
                continue
            
            source_id = row[fk_field]
            if source_id is None:
                continue
            
            table_name = fk_config['table']
            target_id = self.get_target_id(table_name, source_id)
            
            if target_id is not None:
                new_row[fk_field] = target_id
                print(f"[MAPPER] 外键映射: {fk_field} {source_id} -> {target_id}")
            elif self.is_unmapped(table_name, source_id):
                # 无法映射，标记为None（后续会跳过这条记录）
                new_row[fk_field] = None
                print(f"[MAPPER] 外键无法映射: {fk_field}={source_id}")
        
        return new_row
    
    def has_unmapped_foreign_keys(self, row: dict) -> bool:
        """检查asset_record是否有无法映射的外键"""
        for fk_field, fk_config in ASSET_RECORDS_FOREIGN_KEYS.items():
            if fk_field not in row or row[fk_field] is None:
                continue
            
            source_id = row[fk_field]
            table_name = fk_config['table']
            
            # 检查是否无法映射
            if self.is_unmapped(table_name, source_id):
                return True
            
            # 检查是否有映射（如果没有映射且不是None，说明未处理）
            if self.get_target_id(table_name, source_id) is None:
                # 可能是新数据，需要检查是否在目标数据库中存在
                pass
        
        return False


def normalize_amount(raw: str) -> Decimal:
    s = raw.strip().strip('"').strip("'")
    s = s.replace("\uff65", "").replace("\u00a5", "")
    s = s.replace("\uffe5", "").replace("\u00a5", "")
    # Handle both \uffe5 and \u00a5
    for ch in ["\uffe5", "\u00a5"]:
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


# ==================== Database Import Functionality ====================

@dataclass
class TableStructure:
    """表结构信息"""
    name: str
    columns: dict[str, dict] = field(default_factory=dict)
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)


@dataclass
class StructureDifference:
    """表结构差异"""
    new_columns: list[str] = field(default_factory=list)  # 源表有但目标表没有的列
    missing_columns: list[str] = field(default_factory=list)  # 目标表有但源表没有的列
    type_mismatches: list[dict] = field(default_factory=list)  # 类型不匹配的列


@dataclass
class ConflictInfo:
    """冲突信息"""
    table_name: str
    primary_key_values: list[dict] = field(default_factory=list)
    conflict_count: int = 0


@dataclass
class ImportResult:
    """导入结果"""
    table_name: str
    total_rows: int = 0
    imported_rows: int = 0
    skipped_rows: int = 0
    overwritten_rows: int = 0
    merged_rows: int = 0
    errors: list[str] = field(default_factory=list)


def get_table_structure_sqlite(conn: sqlite3.Connection, table_name: str) -> TableStructure:
    """获取 SQLite 表结构"""
    structure = TableStructure(name=table_name)

    # 获取列信息
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    for row in cursor.fetchall():
        col_info = {
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "default_value": row[4],
            "pk": row[5]
        }
        structure.columns[row[1]] = col_info
        if row[5]:  # 是主键
            structure.primary_keys.append(row[1])

    # 获取外键信息
    cursor = conn.execute(f"PRAGMA foreign_key_list({table_name})")
    for row in cursor.fetchall():
        fk_info = {
            "id": row[0],
            "seq": row[1],
            "table": row[2],
            "from": row[3],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
            "match": row[7]
        }
        structure.foreign_keys.append(fk_info)

    return structure


def analyze_database(source_db_path: str, target_db: Session) -> dict:
    """分析源数据库结构并与当前数据库比较

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话

    Returns:
        分析结果，包含表结构差异等信息
    """
    if not os.path.exists(source_db_path):
        raise FileNotFoundError(f"Source database not found: {source_db_path}")

    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row

    try:
        # 获取源数据库的所有表
        cursor = source_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        source_tables = [row[0] for row in cursor.fetchall()]

        # 获取目标数据库的所有表
        target_inspector = inspect(target_db.bind)
        target_tables = target_inspector.get_table_names()

        result = {
            "tables": [],
            "common_tables": [],
            "missing_tables": [],  # 目标数据库中不存在的表
            "structure_differences": {},
            "table_row_counts": {}
        }

        for table_name in source_tables:
            # 获取源表结构
            source_structure = get_table_structure_sqlite(source_conn, table_name)

            # 获取行数
            cursor = source_conn.execute(f"SELECT COUNT(*) FROM {table_name}")
            row_count = cursor.fetchone()[0]

            table_info = {
                "name": table_name,
                "columns": list(source_structure.columns.keys()),
                "primary_keys": source_structure.primary_keys,
                "row_count": row_count
            }

            result["table_row_counts"][table_name] = row_count

            if table_name in target_tables:
                result["common_tables"].append(table_name)

                # 比较表结构
                diff = compare_table_structures(source_db_path, target_db, table_name)
                if diff.new_columns or diff.missing_columns or diff.type_mismatches:
                    result["structure_differences"][table_name] = {
                        "new_columns": diff.new_columns,
                        "missing_columns": diff.missing_columns,
                        "type_mismatches": diff.type_mismatches
                    }
            else:
                result["missing_tables"].append(table_name)

            result["tables"].append(table_info)

        return result

    finally:
        source_conn.close()


def compare_table_structures(source_db_path: str, target_db: Session, table_name: str) -> StructureDifference:
    """比较源表和目标表的结构差异"""
    diff = StructureDifference()

    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    try:
        source_structure = get_table_structure_sqlite(source_conn, table_name)

        # 获取目标表结构
        target_inspector = inspect(target_db.bind)
        target_columns = target_inspector.get_columns(table_name)
        target_col_dict = {col["name"]: col for col in target_columns}

        # 比较列
        source_col_names = set(source_structure.columns.keys())
        target_col_names = set(target_col_dict.keys())

        diff.new_columns = list(source_col_names - target_col_names)
        diff.missing_columns = list(target_col_names - source_col_names)

        # 检查类型不匹配
        common_cols = source_col_names & target_col_names
        for col_name in common_cols:
            source_type = source_structure.columns[col_name]["type"]
            target_type = str(target_col_dict[col_name]["type"])

            # 简化类型比较
            if not are_types_compatible(source_type, target_type):
                diff.type_mismatches.append({
                    "column": col_name,
                    "source_type": source_type,
                    "target_type": target_type
                })

        return diff
    finally:
        source_conn.close()


def are_types_compatible(source_type: str, target_type: str) -> bool:
    """检查两种类型是否兼容"""
    source_type = source_type.upper() if source_type else ""
    target_type = target_type.upper() if target_type else ""

    # 数值类型兼容
    numeric_types = ["INTEGER", "INT", "BIGINT", "SMALLINT", "NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE"]
    if any(t in source_type for t in numeric_types) and any(t in target_type for t in numeric_types):
        return True

    # 文本类型兼容
    text_types = ["TEXT", "VARCHAR", "CHAR", "STRING"]
    if any(t in source_type for t in text_types) and any(t in target_type for t in text_types):
        return True

    # 日期时间类型兼容
    datetime_types = ["DATE", "TIME", "DATETIME", "TIMESTAMP"]
    if any(t in source_type for t in datetime_types) and any(t in target_type for t in datetime_types):
        return True

    # 布尔类型兼容
    bool_types = ["BOOLEAN", "BOOL"]
    if any(t in source_type for t in bool_types) and any(t in target_type for t in bool_types):
        return True

    return source_type == target_type or not source_type or not target_type


def detect_conflicts(source_db_path: str, target_db: Session, table_name: str) -> ConflictInfo:
    """检测主键冲突

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话
        table_name: 表名

    Returns:
        冲突信息
    """
    conflict_info = ConflictInfo(table_name=table_name)

    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row

    try:
        # 获取源表结构
        source_structure = get_table_structure_sqlite(source_conn, table_name)

        if not source_structure.primary_keys:
            return conflict_info  # 没有主键，无法检测冲突

        # 获取目标表主键列
        target_inspector = inspect(target_db.bind)
        target_pk = target_inspector.get_pk_constraint(table_name)
        target_pk_cols = target_pk.get("constrained_columns", [])

        if not target_pk_cols:
            return conflict_info

        # 只检查共同的主键列
        common_pk_cols = [col for col in source_structure.primary_keys if col in target_pk_cols]

        if not common_pk_cols:
            return conflict_info

        # 获取源表的主键值
        pk_cols_str = ", ".join(common_pk_cols)
        cursor = source_conn.execute(f"SELECT {pk_cols_str} FROM {table_name}")
        source_pk_values = [dict(row) for row in cursor.fetchall()]

        # 检查目标表中是否存在相同的主键
        for pk_value in source_pk_values:
            conditions = []
            params = {}
            for col in common_pk_cols:
                conditions.append(f"{col} = :{col}")
                params[col] = pk_value[col]

            where_clause = " AND ".join(conditions)
            query = text(f"SELECT 1 FROM {table_name} WHERE {where_clause}")
            result = target_db.execute(query, params).fetchone()

            if result:
                conflict_info.primary_key_values.append(pk_value)
                conflict_info.conflict_count += 1

        return conflict_info

    finally:
        source_conn.close()


def import_table(
    source_db_path: str,
    target_db: Session,
    table_name: str,
    conflict_strategy: str = "skip",
    merge_rules: dict = None
) -> ImportResult:
    """导入指定表的数据

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话
        table_name: 表名
        conflict_strategy: 冲突处理策略 (skip, overwrite, merge)
        merge_rules: 合并规则，指定如何合并冲突数据

    Returns:
        导入结果
    """
    result = ImportResult(table_name=table_name)
    merge_rules = merge_rules or {}

    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row

    try:
        # 获取源表结构
        source_structure = get_table_structure_sqlite(source_conn, table_name)

        # 获取目标表结构
        target_inspector = inspect(target_db.bind)
        target_columns = target_inspector.get_columns(table_name)
        target_col_names = {col["name"] for col in target_columns}

        # 获取共同列（源表和目标表都有的列）
        common_columns = [col for col in source_structure.columns.keys() if col in target_col_names]

        if not common_columns:
            result.errors.append("No common columns found between source and target tables")
            return result

        # 获取主键列
        target_pk = target_inspector.get_pk_constraint(table_name)
        target_pk_cols = set(target_pk.get("constrained_columns", []))

        # 读取源表数据
        cols_str = ", ".join(common_columns)
        cursor = source_conn.execute(f"SELECT {cols_str} FROM {table_name}")
        rows = cursor.fetchall()

        result.total_rows = len(rows)

        # 获取目标表的默认值
        target_col_info = {col["name"]: col for col in target_columns}

        # 对 fund_types 表进行特殊处理：按层级顺序导入（先父后子）
        if table_name == "fund_types":
            # 将行数据转换为列表
            rows_list = [dict(row) for row in rows]
            print(f"[IMPORT DEBUG] fund_types 原始数据行数: {len(rows_list)}")
            for row in rows_list:
                print(f"[IMPORT DEBUG] 原始数据: id={row.get('id')}, name={row.get('name')}, parent_id={row.get('parent_id')}, level={row.get('level')}")
            
            # 构建 id 到 row 的映射
            id_to_row = {row.get("id"): row for row in rows_list if row.get("id") is not None}
            print(f"[IMPORT DEBUG] id_to_row 映射: {list(id_to_row.keys())}")
            
            # 计算每个节点的实际层级深度（从根节点开始）
            def get_depth(row, visited=None):
                """递归计算节点的深度"""
                if visited is None:
                    visited = set()
                
                row_id = row.get("id")
                if row_id in visited:
                    print(f"[IMPORT DEBUG] 检测到循环引用: id={row_id}")
                    return 0  # 避免循环引用
                visited.add(row_id)
                
                parent_id = row.get("parent_id")
                if parent_id is None:
                    return 0  # 根节点
                elif parent_id not in id_to_row:
                    print(f"[IMPORT DEBUG] 父节点不存在于当前导入数据: id={row_id}, parent_id={parent_id}")
                    return 0  # 父节点不在当前导入集合中，视为根节点
                else:
                    parent = id_to_row[parent_id]
                    return 1 + get_depth(parent, visited.copy())
            
            # 计算每个节点的深度
            for row in rows_list:
                row['_depth'] = get_depth(row)
                print(f"[IMPORT DEBUG] 计算深度: id={row.get('id')}, name={row.get('name')}, depth={row['_depth']}")
            
            # 按深度排序，深度小的先导入（根节点优先）
            rows_list.sort(key=lambda x: (x['_depth'], x.get("id") or 0))
            print(f"[IMPORT DEBUG] 排序后顺序:")
            for row in rows_list:
                print(f"[IMPORT DEBUG] 排序后: id={row.get('id')}, name={row.get('name')}, parent_id={row.get('parent_id')}, depth={row['_depth']}")
            
            # 移除临时字段
            for row in rows_list:
                del row['_depth']
            
            rows = rows_list
        else:
            rows = [dict(row) for row in rows]

        for row in rows:
            row_dict = dict(row)

            # 处理缺失列的默认值
            for col in target_col_names:
                if col not in row_dict:
                    # 使用目标表的默认值
                    col_info = target_col_info.get(col, {})
                    default = col_info.get("default")
                    if default is not None:
                        row_dict[col] = default
                    else:
                        # 根据类型设置默认值
                        col_type = str(col_info.get("type", "")).upper()
                        if "INT" in col_type or "NUMERIC" in col_type or "DECIMAL" in col_type or "FLOAT" in col_type:
                            row_dict[col] = 0
                        elif "BOOL" in col_type:
                            row_dict[col] = False
                        elif "DATE" in col_type or "TIME" in col_type:
                            row_dict[col] = datetime.now()
                        else:
                            row_dict[col] = ""

            # 检查主键冲突
            has_conflict = False
            if target_pk_cols:
                conditions = []
                params = {}
                for col in target_pk_cols:
                    if col in row_dict:
                        conditions.append(f"{col} = :{col}")
                        params[col] = row_dict[col]

                if conditions:
                    where_clause = " AND ".join(conditions)
                    query = text(f"SELECT * FROM {table_name} WHERE {where_clause}")
                    existing = target_db.execute(query, params).fetchone()
                    has_conflict = existing is not None

            try:
                if has_conflict:
                    if conflict_strategy == "skip":
                        result.skipped_rows += 1
                        continue
                    elif conflict_strategy == "overwrite":
                        # 更新现有记录
                        update_cols = [col for col in common_columns if col not in target_pk_cols]
                        if update_cols:
                            set_clause = ", ".join([f"{col} = :{col}" for col in update_cols])
                            where_clause = " AND ".join([f"{col} = :pk_{col}" for col in target_pk_cols if col in row_dict])

                            update_params = {col: row_dict[col] for col in update_cols}
                            for col in target_pk_cols:
                                if col in row_dict:
                                    update_params[f"pk_{col}"] = row_dict[col]

                            query = text(f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}")
                            target_db.execute(query, update_params)
                            result.overwritten_rows += 1
                    elif conflict_strategy == "merge":
                        # 合并数据
                        merged = merge_row_data(dict(existing), row_dict, merge_rules, common_columns)
                        update_cols = [col for col in common_columns if col not in target_pk_cols]
                        if update_cols:
                            set_clause = ", ".join([f"{col} = :{col}" for col in update_cols])
                            where_clause = " AND ".join([f"{col} = :pk_{col}" for col in target_pk_cols if col in row_dict])

                            update_params = {col: merged[col] for col in update_cols}
                            for col in target_pk_cols:
                                if col in row_dict:
                                    update_params[f"pk_{col}"] = row_dict[col]

                            query = text(f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}")
                            target_db.execute(query, update_params)
                            result.merged_rows += 1
                else:
                    # 插入新记录
                    cols = list(row_dict.keys())
                    placeholders = [f":{col}" for col in cols]

                    insert_query = text(f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})")
                    target_db.execute(insert_query, row_dict)
                    result.imported_rows += 1

            except Exception as e:
                result.errors.append(f"Error importing row: {str(e)}")

        target_db.commit()
        return result

    finally:
        source_conn.close()


def merge_row_data(existing: dict, new: dict, merge_rules: dict, columns: list) -> dict:
    """合并两行数据

    Args:
        existing: 现有数据
        new: 新数据
        merge_rules: 合并规则 {列名: 规则}
            规则可以是: "keep_existing", "use_new", "sum", "max", "min"
        columns: 列名列表

    Returns:
        合并后的数据
    """
    result = dict(existing)

    for col in columns:
        if col not in new:
            continue

        rule = merge_rules.get(col, "use_new")

        if rule == "keep_existing":
            continue
        elif rule == "use_new":
            result[col] = new[col]
        elif rule == "sum":
            try:
                result[col] = (existing.get(col) or 0) + (new[col] or 0)
            except (TypeError, ValueError):
                result[col] = new[col]
        elif rule == "max":
            try:
                result[col] = max(existing.get(col) or 0, new[col] or 0)
            except (TypeError, ValueError):
                result[col] = new[col]
        elif rule == "min":
            try:
                result[col] = min(existing.get(col) or float('inf'), new[col] or float('inf'))
            except (TypeError, ValueError):
                result[col] = new[col]

    return result


def preview_table_import(
    source_db_path: str,
    target_db: Session,
    table_name: str,
    limit: int = 10
) -> dict:
    """预览表导入数据

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话
        table_name: 表名
        limit: 预览行数限制

    Returns:
        预览数据
    """
    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row

    try:
        # 获取源表结构
        source_structure = get_table_structure_sqlite(source_conn, table_name)

        # 获取目标表结构
        target_inspector = inspect(target_db.bind)
        target_columns = target_inspector.get_columns(table_name)
        target_col_names = {col["name"] for col in target_columns}

        # 获取共同列
        common_columns = [col for col in source_structure.columns.keys() if col in target_col_names]

        # 获取主键列
        target_pk = target_inspector.get_pk_constraint(table_name)
        target_pk_cols = set(target_pk.get("constrained_columns", []))

        # 读取源表数据（限制行数）
        cols_str = ", ".join(common_columns) if common_columns else "*"
        cursor = source_conn.execute(f"SELECT {cols_str} FROM {table_name} LIMIT {limit}")
        rows = cursor.fetchall()

        preview_data = []
        for row in rows:
            row_dict = dict(row)

            # 检查是否有冲突
            has_conflict = False
            if target_pk_cols:
                conditions = []
                params = {}
                for col in target_pk_cols:
                    if col in row_dict:
                        conditions.append(f"{col} = :{col}")
                        params[col] = row_dict[col]

                if conditions:
                    where_clause = " AND ".join(conditions)
                    query = text(f"SELECT 1 FROM {table_name} WHERE {where_clause}")
                    existing = target_db.execute(query, params).fetchone()
                    has_conflict = existing is not None

            preview_data.append({
                "data": row_dict,
                "has_conflict": has_conflict,
                "new_columns": [col for col in row_dict.keys() if col not in target_col_names],
                "missing_columns": list(target_col_names - set(row_dict.keys()))
            })

        # 获取总行数
        cursor = source_conn.execute(f"SELECT COUNT(*) FROM {table_name}")
        total_count = cursor.fetchone()[0]

        return {
            "table_name": table_name,
            "total_rows": total_count,
            "preview_rows": preview_data,
            "common_columns": common_columns,
            "source_only_columns": [col for col in source_structure.columns.keys() if col not in target_col_names],
            "target_only_columns": list(target_col_names - set(source_structure.columns.keys()))
        }

    finally:
        source_conn.close()


def import_multiple_tables(
    source_db_path: str,
    target_db: Session,
    table_configs: list[dict],
    fk_mapper: Optional[ForeignKeyMapper] = None
) -> list[ImportResult]:
    """导入多个表（按依赖关系排序）

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话
        table_configs: 表配置列表，每个配置包含:
            - table_name: 表名
            - conflict_strategy: 冲突策略
            - merge_rules: 合并规则
        fk_mapper: 外键映射器，用于智能映射模式。如果为None，则自动创建

    Returns:
        导入结果列表
    """
    # 如果没有提供外键映射器，创建一个
    if fk_mapper is None:
        fk_mapper = ForeignKeyMapper()
    # 定义表的依赖关系（被依赖的表先导入）
    # asset_records 依赖: liquidity_ratings, fund_types, accounts, asset_owners
    # allocation_targets 依赖: allocation_targets (自引用)
    table_dependencies = {
        "asset_records": ["liquidity_ratings", "fund_types", "accounts", "asset_owners"],
        "allocation_targets": ["allocation_targets"],  # 自引用，需要特殊处理
    }

    # 拓扑排序：确保依赖的表先导入
    def topological_sort(configs: list[dict]) -> list[dict]:
        """对表配置进行拓扑排序"""
        # 构建图
        graph = {config.get("table_name"): [] for config in configs}
        in_degree = {config.get("table_name"): 0 for config in configs}
        
        # 只考虑在 configs 中的表之间的依赖
        table_names = set(graph.keys())
        for config in configs:
            table_name = config.get("table_name")
            deps = table_dependencies.get(table_name, [])
            for dep in deps:
                if dep in table_names and dep != table_name:  # 排除自引用
                    graph[dep].append(table_name)
                    in_degree[table_name] += 1
        
        # Kahn 算法
        queue = [name for name, degree in in_degree.items() if degree == 0]
        sorted_names = []
        
        while queue:
            # 按名称排序，确保确定性顺序
            queue.sort()
            current = queue.pop(0)
            sorted_names.append(current)
            
            for neighbor in graph[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        # 如果有环，剩下的就是环中的节点
        if len(sorted_names) != len(configs):
            remaining = [name for name in in_degree if name not in sorted_names]
            print(f"[IMPORT WARNING] 检测到循环依赖或无法排序的表: {remaining}")
            # 将剩余的表按名称排序后添加
            sorted_names.extend(sorted(remaining))
        
        # 构建结果
        name_to_config = {config.get("table_name"): config for config in configs}
        return [name_to_config[name] for name in sorted_names]

    # 对配置进行拓扑排序
    sorted_configs = topological_sort(table_configs)
    
    print(f"[IMPORT DEBUG] 导入顺序: {[c.get('table_name') for c in sorted_configs]}")

    # 定义配置表（asset_records 依赖的表）
    config_tables = {"accounts", "fund_types", "liquidity_ratings", "asset_owners"}

    results = []
    config_table_errors = []  # 记录配置表的导入错误

    for config in sorted_configs:
        table_name = config.get("table_name")
        conflict_strategy = config.get("conflict_strategy", "skip")
        merge_rules = config.get("merge_rules", {})

        # 如果配置表有错误，跳过 asset_records 的导入
        if table_name == "asset_records" and config_table_errors:
            error_msg = f"配置表导入有错误或警告，跳过导入: {', '.join(config_table_errors)}"
            print(f"[IMPORT WARNING] {error_msg}")
            results.append(ImportResult(
                table_name=table_name,
                errors=[error_msg],
                message=error_msg
            ))
            continue

        try:
            # 对于配置表，先读取源数据建立ID映射
            if table_name in config_tables:
                # 从源数据库读取数据以建立ID映射
                source_conn = sqlite3.connect(source_db_path)
                source_conn.row_factory = sqlite3.Row
                try:
                    cursor = source_conn.execute(f"SELECT * FROM {table_name}")
                    source_data = [dict(row) for row in cursor.fetchall()]
                    fk_mapper.build_mapping_from_config_table(target_db, table_name, source_data)
                finally:
                    source_conn.close()

            # 导入表数据
            if table_name == "asset_records":
                # 使用支持外键映射的导入函数
                result = _import_asset_records_with_mapping(
                    source_db_path, target_db, table_name, conflict_strategy, fk_mapper
                )
            else:
                result = import_table(source_db_path, target_db, table_name, conflict_strategy, merge_rules)
            results.append(result)

            # 检查配置表是否有错误或警告
            if table_name in config_tables:
                if result.errors or result.error_count > 0:
                    config_table_errors.append(f"{table_name}({result.error_count}个错误)")
                    print(f"[IMPORT WARNING] 配置表 {table_name} 导入有 {result.error_count} 个错误")
        except Exception as e:
            results.append(ImportResult(
                table_name=table_name,
                errors=[str(e)]
            ))
            if table_name in config_tables:
                config_table_errors.append(f"{table_name}(异常)")

    return results


def save_uploaded_db_file(uploaded_file: bytes, filename: str) -> str:
    """保存上传的数据库文件到临时目录

    Args:
        uploaded_file: 上传的文件内容
        filename: 文件名

    Returns:
        保存的文件路径
    """
    # 创建临时目录
    temp_dir = tempfile.mkdtemp(prefix="import_db_")
    file_path = os.path.join(temp_dir, filename)

    # 保存文件
    with open(file_path, "wb") as f:
        f.write(uploaded_file)

    return file_path


def cleanup_temp_db_file(file_path: str):
    """清理临时数据库文件"""
    if os.path.exists(file_path):
        temp_dir = os.path.dirname(file_path)
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==================== CSV/JSON File Import Functions ====================

def save_uploaded_data_file(uploaded_file: bytes, filename: str) -> str:
    """保存上传的数据文件到临时目录

    Args:
        uploaded_file: 上传的文件内容
        filename: 原始文件名

    Returns:
        临时文件路径
    """
    # 创建临时目录
    temp_dir = tempfile.mkdtemp(prefix="import_data_")
    file_path = os.path.join(temp_dir, filename)

    # 保存文件
    with open(file_path, "wb") as f:
        f.write(uploaded_file)

    return file_path


def cleanup_temp_data_file(file_path: str):
    """清理临时数据文件"""
    if os.path.exists(file_path):
        temp_dir = os.path.dirname(file_path)
        shutil.rmtree(temp_dir, ignore_errors=True)


def analyze_csv_file(file_path: str) -> Dict:
    """分析 CSV 文件内容，根据列名推断表名

    Args:
        file_path: CSV 文件路径

    Returns:
        文件分析结果
    """
    import csv

    # 表字段映射（用于识别 CSV 对应的表）
    TABLE_FIELD_PATTERNS = {
        "accounts": {"required": ["name"], "optional": ["id", "created_at"]},
        "fund_types": {"required": ["name"], "optional": ["id", "parent_id", "level", "created_at"]},
        "liquidity_ratings": {"required": ["name"], "optional": ["id", "sort_order", "created_at"]},
        "asset_owners": {"required": ["name"], "optional": ["id", "description", "created_at", "updated_at"]},
        "alert_rules": {"required": ["name", "dimension", "period_type", "compare_type"], "optional": ["id", "target_id", "amount_threshold", "percent_threshold", "direction", "is_active", "created_at", "updated_at"]},
        "allocation_targets": {"required": ["dimension", "target_id", "target_percent"], "optional": ["id", "parent_id", "created_at", "updated_at"]},
        "auto_export_rules": {"required": ["name", "cron_expression", "export_format"], "optional": ["id", "filename_template", "is_active", "last_run_at", "next_run_at", "created_at", "updated_at"]},
        "export_history": {"required": ["export_time", "export_type", "filename"], "optional": ["id", "file_size", "operator", "rule_name", "file_path", "created_at"]},
        "asset_records": {"required": ["asset_date", "asset_name", "amount"], "optional": ["id", "liquidity_rating_id", "fund_type_id", "account_id", "owner_id", "import_batch_id", "created_at", "updated_at"]},
    }

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        # 读取表头
        sample = f.read(8192)
        f.seek(0)

        reader = csv.reader(f)
        headers = next(reader, [])
        headers_set = set(headers)

        # 读取前10行数据作为预览
        preview_rows = []
        for i, row in enumerate(reader):
            if i >= 10:
                break
            preview_rows.append(dict(zip(headers, row)))

        # 统计总行数
        f.seek(0)
        total_rows = sum(1 for _ in f) - 1  # 减去表头

    # 根据列名推断表名
    detected_table = "asset_records"  # 默认表名
    best_match_score = 0

    for table_name, patterns in TABLE_FIELD_PATTERNS.items():
        required_fields = set(patterns["required"])
        optional_fields = set(patterns["optional"])
        all_fields = required_fields | optional_fields

        # 检查是否包含所有必需字段
        if required_fields.issubset(headers_set):
            # 计算匹配分数：匹配字段数 / 总字段数
            matched_fields = headers_set & all_fields
            match_score = len(matched_fields) / len(headers_set) if headers_set else 0

            # 优先选择匹配度更高的表
            if match_score > best_match_score:
                best_match_score = match_score
                detected_table = table_name

    print(f"[CSV ANALYZE] 检测到的表: {detected_table}, 列名: {headers}, 匹配分数: {best_match_score:.2f}")

    return {
        "tables": [detected_table],
        "headers": headers,
        "total_rows": total_rows,
        "preview_rows": preview_rows,
        "file_type": "csv"
    }


def analyze_json_file(file_path: str) -> Dict:
    """分析 JSON 文件内容

    Args:
        file_path: JSON 文件路径

    Returns:
        文件分析结果
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 支持导出格式：
    # { "exported_at": "...", "tables": { "table_name": { "schema": {...}, "data": [...] } } }
    # 或简化格式：
    # { "table_name": { "columns": [...], "data": [...] } }

    tables = []
    preview = {}

    if isinstance(data, dict):
        # 检查是否是导出格式（包含 tables 键）
        if "tables" in data and isinstance(data["tables"], dict):
            tables_data = data["tables"]
            for table_name, table_info in tables_data.items():
                if isinstance(table_info, dict) and "data" in table_info:
                    tables.append(table_name)
                    # 从 schema 中获取列名
                    schema = table_info.get("schema", {})
                    columns = list(schema.get("columns", {}).keys()) if isinstance(schema, dict) else []
                    preview[table_name] = {
                        "columns": columns,
                        "row_count": len(table_info.get("data", [])),
                        "preview_rows": table_info.get("data", [])[:10]
                    }
        else:
            # 直接遍历字典
            for key, value in data.items():
                if isinstance(value, dict) and "data" in value:
                    tables.append(key)
                    # 从 schema 或 data 中获取列名
                    schema = value.get("schema", {})
                    if isinstance(schema, dict) and "columns" in schema:
                        columns = list(schema["columns"].keys())
                    elif value.get("data") and len(value["data"]) > 0:
                        columns = list(value["data"][0].keys())
                    else:
                        columns = []
                    preview[key] = {
                        "columns": columns,
                        "row_count": len(value.get("data", [])),
                        "preview_rows": value.get("data", [])[:10]
                    }

    return {
        "tables": tables,
        "preview": preview,
        "file_type": "json"
    }


def preview_csv_import(file_path: str, db: Session, table_name: str) -> Dict:
    """预览 CSV 文件导入

    Args:
        file_path: CSV 文件路径
        db: 数据库会话
        table_name: 目标表名

    Returns:
        预览结果
    """
    import csv

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 检测冲突（基于主键）
    conflict_count = 0
    if table_name == "asset_records":
        for row in rows:
            # 检查是否存在相同日期和名称的记录 - 支持 date 和 asset_date 两种字段名
            date_value = row.get("date") or row.get("asset_date")
            existing = db.query(AssetRecord).filter(
                AssetRecord.asset_date == date_value,
                AssetRecord.asset_name == row.get("asset_name")
            ).first()
            if existing:
                conflict_count += 1

    return {
        "table_name": table_name,
        "total_count": len(rows),
        "sample_data": rows[:10],
        "conflict_count": conflict_count
    }


def preview_json_import(file_path: str, db: Session, table_name: str) -> Dict:
    """预览 JSON 文件导入

    Args:
        file_path: JSON 文件路径
        db: 数据库会话
        table_name: 目标表名

    Returns:
        预览结果
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 获取指定表的数据
    # 支持格式: { "tables": { "table_name": { "schema": {...}, "data": [...] } } }
    rows = []
    if isinstance(data, dict):
        # 首先检查是否是导出格式（包含 tables 键）
        if "tables" in data and isinstance(data["tables"], dict):
            tables_data = data["tables"]
            if table_name in tables_data and isinstance(tables_data[table_name], dict):
                rows = tables_data[table_name].get("data", [])
        # 直接检查表名
        elif table_name in data and isinstance(data[table_name], dict):
            rows = data[table_name].get("data", [])

    # 检测冲突
    conflict_count = 0
    if table_name == "asset_records":
        for row in rows:
            # 支持 date 和 asset_date 两种字段名
            date_value = row.get("date") or row.get("asset_date")
            existing = db.query(AssetRecord).filter(
                AssetRecord.asset_date == date_value,
                AssetRecord.asset_name == row.get("asset_name")
            ).first()
            if existing:
                conflict_count += 1

    return {
        "table_name": table_name,
        "total_count": len(rows),
        "sample_data": rows[:10],
        "conflict_count": conflict_count
    }


def import_csv_data_file(
    file_path: str,
    db: Session,
    table_name: str,
    conflict_strategy: str,
    fk_mapper: Optional[ForeignKeyMapper] = None
) -> Dict:
    """导入 CSV 数据文件

    Args:
        file_path: CSV 文件路径
        db: 数据库会话
        table_name: 目标表名
        conflict_strategy: 冲突处理策略 (skip/overwrite/merge)
        fk_mapper: 外键映射器，用于智能映射模式

    Returns:
        导入结果
    """
    import csv

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 配置表：建立ID映射（如果提供了fk_mapper）
    if fk_mapper and table_name in CONFIG_TABLES and rows:
        print(f"[IMPORT DEBUG] CSV import_csv_data_file - 为配置表 {table_name} 建立ID映射")
        fk_mapper.build_mapping_from_config_table(db, table_name, rows)

    # 对 fund_types 表进行特殊处理：按层级顺序导入（先父后子）
    if table_name == "fund_types" and rows:
        print(f"[IMPORT DEBUG] CSV import_csv_data_file - fund_types 原始数据行数: {len(rows)}")
        
        # 构建 id 到 row 的映射
        id_to_row = {int(row.get("id")): row for row in rows if row.get("id") is not None}
        
        # 计算每个节点的实际层级深度（从根节点开始）
        def get_depth(row, visited=None):
            """递归计算节点的深度"""
            if visited is None:
                visited = set()
            
            row_id = int(row.get("id")) if row.get("id") else None
            if row_id in visited:
                return 0  # 避免循环引用
            visited.add(row_id)
            
            parent_id = int(row.get("parent_id")) if row.get("parent_id") else None
            if parent_id is None:
                return 0  # 根节点
            elif parent_id not in id_to_row:
                return 0  # 父节点不在当前导入集合中，视为根节点
            else:
                parent = id_to_row[parent_id]
                return 1 + get_depth(parent, visited.copy())
        
        # 计算每个节点的深度
        for row in rows:
            row['_depth'] = get_depth(row)
        
        # 按深度排序，深度小的先导入（根节点优先）
        rows.sort(key=lambda x: (int(x.get('_depth', 0)) if x.get('_depth') else 0, int(x.get("id") or 0)))
        
        # 移除临时字段
        for row in rows:
            if '_depth' in row:
                del row['_depth']
        
        print(f"[IMPORT DEBUG] CSV import_csv_data_file - 排序后顺序:")
        for row in rows:
            print(f"[IMPORT DEBUG] 排序后: id={row.get('id')}, name={row.get('name')}, parent_id={row.get('parent_id')}")

    imported = 0
    skipped = 0
    overwritten = 0
    errors = []

    # 对 fund_types 使用特殊导入逻辑，维护 ID 映射关系
    if table_name == "fund_types":
        id_mapping = {}  # 旧 ID -> 新 ID 的映射
        for row in rows:
            try:
                result, old_id, new_id = _import_fund_type_row(db, row, conflict_strategy, id_mapping)
                if old_id and new_id:
                    id_mapping[old_id] = new_id
                if result == "imported":
                    imported += 1
                elif result == "skipped":
                    skipped += 1
                elif result == "overwritten":
                    overwritten += 1
                # 逐行提交，避免批量更新导致的唯一约束冲突
                db.commit()
            except Exception as e:
                db.rollback()
                errors.append(f"Row {row}: {str(e)}")
    else:
        for row in rows:
            try:
                if table_name == "asset_records":
                    # 使用外键映射器重新映射外键ID（如果提供了fk_mapper）
                    if fk_mapper:
                        # 检查是否有无法映射的外键
                        if fk_mapper.has_unmapped_foreign_keys(row):
                            unmapped_fks = []
                            for fk_field, fk_config in ASSET_RECORDS_FOREIGN_KEYS.items():
                                if fk_field in row and row[fk_field] is not None:
                                    source_id = row[fk_field]
                                    table_name_fk = fk_config['table']
                                    if fk_mapper.is_unmapped(table_name_fk, source_id):
                                        unmapped_fks.append(f"{fk_field}={source_id}")
                            error_msg = f"外键无法映射: {', '.join(unmapped_fks)}, asset_name={row.get('asset_name')}"
                            print(f"[IMPORT ERROR] {error_msg}")
                            errors.append(error_msg)
                            skipped += 1
                            continue

                        # 重新映射外键
                        row = fk_mapper.remap_asset_record_foreign_keys(row)

                    result = _import_asset_record_row(db, row, conflict_strategy)
                    if result == "imported":
                        imported += 1
                    elif result == "skipped":
                        skipped += 1
                    elif result == "overwritten":
                        overwritten += 1
                else:
                    # 其他表的导入逻辑 - 根据表名处理冲突
                    result = _import_generic_row(db, table_name, row, conflict_strategy)
                    if result == "imported":
                        imported += 1
                    elif result == "skipped":
                        skipped += 1
                    elif result == "overwritten":
                        overwritten += 1
                # 逐行提交，避免批量更新导致的唯一约束冲突
                db.commit()
            except Exception as e:
                db.rollback()
                error_msg = f"Row {row}: {str(e)}"
                errors.append(error_msg)
                print(f"[IMPORT ERROR] {table_name}: {error_msg}")

    # 打印导入结果摘要
    print(f"[IMPORT SUMMARY] 表: {table_name}, 总计: {len(rows)}, 新导入: {imported}, 覆盖: {overwritten}, 跳过: {skipped}, 错误: {len(errors)}")
    if errors:
        print(f"[IMPORT ERRORS] {table_name} 错误详情:")
        for i, error in enumerate(errors[:10]):  # 只打印前10个错误
            print(f"  [{i+1}] {error}")
        if len(errors) > 10:
            print(f"  ... 还有 {len(errors) - 10} 个错误")

    return {
        "success": len(errors) == 0,
        "imported_count": imported,
        "skipped_count": skipped,
        "overwritten_count": overwritten,
        "error_count": len(errors),
        "message": f"导入完成：新导入 {imported} 条，覆盖 {overwritten} 条，跳过 {skipped} 条" + (f"，错误 {len(errors)} 条" if errors else ""),
        "table_name": table_name,
        "total_rows": len(rows),
        "imported_rows": imported,
        "skipped_rows": skipped,
        "overwritten_rows": overwritten,
        "errors": errors[:10],  # 只返回前10个错误
        "results": [
            {
                "table_name": table_name,
                "total_rows": len(rows),
                "imported_rows": imported,
                "skipped_rows": skipped,
                "overwritten_rows": overwritten,
                "merged_rows": 0,
                "errors": errors[:10]
            }
        ]
    }


def import_json_data_file(
    file_path: str,
    db: Session,
    table_name: str,
    conflict_strategy: str,
    fk_mapper: Optional[ForeignKeyMapper] = None
) -> Dict:
    """导入 JSON 数据文件

    Args:
        file_path: JSON 文件路径
        db: 数据库会话
        table_name: 目标表名
        conflict_strategy: 冲突处理策略
        fk_mapper: 外键映射器，用于智能映射模式

    Returns:
        导入结果
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 获取指定表的数据
    # 支持格式: { "tables": { "table_name": { "schema": {...}, "data": [...] } } }
    rows = []
    if isinstance(data, dict):
        # 首先检查是否是导出格式（包含 tables 键）
        if "tables" in data and isinstance(data["tables"], dict):
            tables_data = data["tables"]
            if table_name in tables_data and isinstance(tables_data[table_name], dict):
                rows = tables_data[table_name].get("data", [])
        # 直接检查表名
        elif table_name in data and isinstance(data[table_name], dict):
            rows = data[table_name].get("data", [])

    # 配置表：建立ID映射（如果提供了fk_mapper）
    if fk_mapper and table_name in CONFIG_TABLES and rows:
        print(f"[IMPORT DEBUG] JSON import_json_data_file - 为配置表 {table_name} 建立ID映射")
        fk_mapper.build_mapping_from_config_table(db, table_name, rows)

    # 对 fund_types 表进行特殊处理：按层级顺序导入（先父后子）
    if table_name == "fund_types" and rows:
        print(f"[IMPORT DEBUG] JSON import_json_data_file - fund_types 原始数据行数: {len(rows)}")

        # 构建 id 到 row 的映射
        id_to_row = {row.get("id"): row for row in rows if row.get("id") is not None}

        # 计算每个节点的实际层级深度（从根节点开始）
        def get_depth(row, visited=None):
            """递归计算节点的深度"""
            if visited is None:
                visited = set()

            row_id = row.get("id")
            if row_id in visited:
                return 0  # 避免循环引用
            visited.add(row_id)

            parent_id = row.get("parent_id")
            if parent_id is None:
                return 0  # 根节点
            elif parent_id not in id_to_row:
                return 0  # 父节点不在当前导入集合中，视为根节点
            else:
                parent = id_to_row[parent_id]
                return 1 + get_depth(parent, visited.copy())

        # 计算每个节点的深度
        for row in rows:
            row['_depth'] = get_depth(row)

        # 按深度排序，深度小的先导入（根节点优先）
        rows.sort(key=lambda x: (x.get('_depth', 0), x.get("id") or 0))

        # 移除临时字段
        for row in rows:
            if '_depth' in row:
                del row['_depth']

        print(f"[IMPORT DEBUG] JSON import_json_data_file - 排序后顺序:")
        for row in rows:
            print(f"[IMPORT DEBUG] 排序后: id={row.get('id')}, name={row.get('name')}, parent_id={row.get('parent_id')}")

    imported = 0
    skipped = 0
    overwritten = 0
    errors = []

    # 对 fund_types 使用特殊导入逻辑，维护 ID 映射关系
    if table_name == "fund_types":
        id_mapping = {}  # 旧 ID -> 新 ID 的映射
        for row in rows:
            try:
                result, old_id, new_id = _import_fund_type_row(db, row, conflict_strategy, id_mapping)
                if old_id and new_id:
                    id_mapping[old_id] = new_id
                if result == "imported":
                    imported += 1
                elif result == "skipped":
                    skipped += 1
                elif result == "overwritten":
                    overwritten += 1
                # 逐行提交，避免批量更新导致的唯一约束冲突
                db.commit()
            except Exception as e:
                db.rollback()
                errors.append(f"Row {row}: {str(e)}")
    else:
        for row in rows:
            try:
                if table_name == "asset_records":
                    result = _import_asset_record_row(db, row, conflict_strategy)
                    if result == "imported":
                        imported += 1
                    elif result == "skipped":
                        skipped += 1
                    elif result == "overwritten":
                        overwritten += 1
                else:
                    # 其他表的导入逻辑 - 根据表名处理冲突
                    result = _import_generic_row(db, table_name, row, conflict_strategy)
                    if result == "imported":
                        imported += 1
                    elif result == "skipped":
                        skipped += 1
                    elif result == "overwritten":
                        overwritten += 1
                # 逐行提交，避免批量更新导致的唯一约束冲突
                db.commit()
            except Exception as e:
                db.rollback()
                error_msg = f"Row {row}: {str(e)}"
                errors.append(error_msg)
                print(f"[IMPORT ERROR] {table_name}: {error_msg}")

    # 打印导入结果摘要
    print(f"[IMPORT SUMMARY] 表: {table_name}, 总计: {len(rows)}, 新导入: {imported}, 覆盖: {overwritten}, 跳过: {skipped}, 错误: {len(errors)}")
    if errors:
        print(f"[IMPORT ERRORS] {table_name} 错误详情:")
        for i, error in enumerate(errors[:10]):  # 只打印前10个错误
            print(f"  [{i+1}] {error}")
        if len(errors) > 10:
            print(f"  ... 还有 {len(errors) - 10} 个错误")

    return {
        "success": len(errors) == 0,
        "imported_count": imported,
        "skipped_count": skipped,
        "overwritten_count": overwritten,
        "error_count": len(errors),
        "message": f"导入完成：新导入 {imported} 条，覆盖 {overwritten} 条，跳过 {skipped} 条" + (f"，错误 {len(errors)} 条" if errors else ""),
        "table_name": table_name,
        "total_rows": len(rows),
        "imported_rows": imported,
        "skipped_rows": skipped,
        "overwritten_rows": overwritten,
        "errors": errors[:10],
        "results": [
            {
                "table_name": table_name,
                "total_rows": len(rows),
                "imported_rows": imported,
                "skipped_rows": skipped,
                "overwritten_rows": overwritten,
                "merged_rows": 0,
                "errors": errors[:10]
            }
        ]
    }


def _import_asset_record_row(db: Session, row: Dict, conflict_strategy: str) -> str:
    """导入单条资产记录

    Args:
        db: 数据库会话
        row: 行数据
        conflict_strategy: 冲突处理策略

    Returns:
        处理结果: imported/skipped/overwritten
    """
    from datetime import datetime
    from sqlalchemy import select
    from models import LiquidityRating, FundType, Account, AssetOwner

    # 刷新会话，确保能看到其他会话提交的数据
    db.expire_all()

    # 检查外键引用的记录是否存在
    liquidity_rating_id = row.get("liquidity_rating_id")
    fund_type_id = row.get("fund_type_id")
    account_id = row.get("account_id")
    owner_id = row.get("owner_id")

    missing_refs = []
    
    print(f"[IMPORT DEBUG] 检查 asset_records 外键: asset_name={row.get('asset_name')}, liquidity_rating_id={liquidity_rating_id}, fund_type_id={fund_type_id}, account_id={account_id}, owner_id={owner_id}")
    
    if liquidity_rating_id is not None:
        lr = db.execute(select(LiquidityRating).where(LiquidityRating.id == liquidity_rating_id)).scalar_one_or_none()
        if not lr:
            missing_refs.append(f"liquidity_rating_id={liquidity_rating_id}")
            print(f"[IMPORT DEBUG]   -> liquidity_rating_id={liquidity_rating_id} 不存在")
        else:
            print(f"[IMPORT DEBUG]   -> liquidity_rating_id={liquidity_rating_id} 存在")
    
    if fund_type_id is not None:
        ft = db.execute(select(FundType).where(FundType.id == fund_type_id)).scalar_one_or_none()
        if not ft:
            missing_refs.append(f"fund_type_id={fund_type_id}")
            print(f"[IMPORT DEBUG]   -> fund_type_id={fund_type_id} 不存在")
        else:
            print(f"[IMPORT DEBUG]   -> fund_type_id={fund_type_id} 存在")
    
    if account_id is not None:
        acc = db.execute(select(Account).where(Account.id == account_id)).scalar_one_or_none()
        if not acc:
            missing_refs.append(f"account_id={account_id}")
            print(f"[IMPORT DEBUG]   -> account_id={account_id} 不存在")
        else:
            print(f"[IMPORT DEBUG]   -> account_id={account_id} 存在")
    
    if owner_id is not None:
        ao = db.execute(select(AssetOwner).where(AssetOwner.id == owner_id)).scalar_one_or_none()
        if not ao:
            missing_refs.append(f"owner_id={owner_id}")
            print(f"[IMPORT DEBUG]   -> owner_id={owner_id} 不存在")
        else:
            print(f"[IMPORT DEBUG]   -> owner_id={owner_id} 存在")
    
    if missing_refs:
        print(f"[IMPORT ERROR] asset_records 外键引用不存在: {', '.join(missing_refs)}, asset_name={row.get('asset_name')}")
        raise ValueError(f"外键引用不存在: {', '.join(missing_refs)}")

    # 检查是否存在冲突 - 支持 date 和 asset_date 两种字段名
    date_value = row.get("date") or row.get("asset_date")
    existing = db.query(AssetRecord).filter(
        AssetRecord.asset_date == date_value,
        AssetRecord.asset_name == row.get("asset_name")
    ).first()

    if existing:
        if conflict_strategy == "skip":
            return "skipped"
        elif conflict_strategy == "overwrite":
            # 更新现有记录
            for key, value in row.items():
                if hasattr(existing, key) and key != "id":
                    # 将 import_batch_id 设置为 None，因为导入的批次记录可能不存在
                    if key == "import_batch_id":
                        value = None
                    # 转换日期字符串为 date 对象
                    if key in ["asset_date", "date"] and isinstance(value, str):
                        try:
                            value = datetime.strptime(value, "%Y-%m-%d").date()
                        except ValueError:
                            pass
                    # 转换 datetime 字符串为 datetime 对象
                    if key in ["created_at", "updated_at"] and isinstance(value, str):
                        try:
                            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        except ValueError:
                            pass
                    setattr(existing, key, value)
            return "overwritten"
        elif conflict_strategy == "merge":
            # 合并逻辑：金额累加
            if "amount" in row and existing.amount is not None:
                try:
                    existing.amount = float(existing.amount) + float(row["amount"])
                except (ValueError, TypeError):
                    pass
            return "overwritten"

    # 创建新记录 - 转换日期字段
    record_data = {k: v for k, v in row.items() if k != "id"}
    # 将 import_batch_id 设置为 None，因为导入的批次记录可能不存在
    if "import_batch_id" in record_data:
        record_data["import_batch_id"] = None
    # 转换 asset_date
    if "asset_date" in record_data and isinstance(record_data["asset_date"], str):
        try:
            record_data["asset_date"] = datetime.strptime(record_data["asset_date"], "%Y-%m-%d").date()
        except ValueError:
            pass
    # 转换 created_at 和 updated_at
    for dt_field in ["created_at", "updated_at"]:
        if dt_field in record_data and isinstance(record_data[dt_field], str):
            try:
                record_data[dt_field] = datetime.fromisoformat(record_data[dt_field].replace('Z', '+00:00'))
            except ValueError:
                pass
    new_record = AssetRecord(**record_data)
    db.add(new_record)
    return "imported"


def _import_generic_row(db: Session, table_name: str, row: Dict, conflict_strategy: str) -> str:
    """导入通用表的单行数据

    Args:
        db: 数据库会话
        table_name: 表名
        row: 行数据
        conflict_strategy: 冲突处理策略

    Returns:
        处理结果: imported/skipped/overwritten
    """
    from sqlalchemy import select
    from models import Account, FundType, LiquidityRating, AssetOwner, AlertRule, AutoExportRule, AllocationTarget

    # 根据表名获取模型和主键字段
    # 注意：被其他表作为外键引用的表（accounts, fund_types, liquidity_ratings, asset_owners）
    # 必须使用 "id" 作为唯一字段，以保留原始 ID，确保外键关联正确
    model_map = {
        "accounts": (Account, "id"),  # 被 asset_records.account_id 引用
        "fund_types": (FundType, "id"),  # 被 asset_records.fund_type_id 引用
        "liquidity_ratings": (LiquidityRating, "id"),  # 被 asset_records.liquidity_rating_id 引用
        "asset_owners": (AssetOwner, "id"),  # 被 asset_records.owner_id 引用
        "alert_rules": (AlertRule, "id"),
        "auto_export_rules": (AutoExportRule, "id"),
        "allocation_targets": (AllocationTarget, "id"),
    }

    if table_name not in model_map:
        # 对于不支持的表，直接返回失败
        print(f"[IMPORT ERROR] 不支持的表: {table_name}")
        return "skipped"

    model, unique_field = model_map[table_name]

    # 检查是否存在冲突
    # 对于配置表（accounts, fund_types等），同时检查 id 和 name
    existing = None
    row_id = row.get("id")
    row_name = row.get("name")
    
    # 首先基于 id 检查
    if row_id is not None:
        existing = db.execute(
            select(model).where(model.id == row_id)
        ).scalar_one_or_none()
    
    # 如果 id 没有冲突，再基于 name 检查（对于有 name 字段的表）
    if existing is None and row_name is not None and hasattr(model, 'name'):
        existing = db.execute(
            select(model).where(model.name == row_name)
        ).scalar_one_or_none()

    if existing:
        if conflict_strategy == "skip":
            return "skipped"
        elif conflict_strategy == "overwrite":
            # 更新现有记录
            try:
                datetime_fields = ["created_at", "updated_at", "last_run_at", "next_run_at", "export_time"]
                
                # 检查更新后的名称是否会导致唯一约束冲突
                if 'name' in row and hasattr(model, 'name'):
                    new_name = row['name']
                    if new_name != existing.name:
                        # 检查新名称是否被其他记录使用
                        name_conflict = db.execute(
                            select(model).where(model.name == new_name, model.id != existing.id)
                        ).scalar_one_or_none()
                        if name_conflict:
                            # 名称已被其他记录使用，跳过更新
                            print(f"[IMPORT WARNING] {table_name}: 名称 '{new_name}' 已被记录(id={name_conflict.id})使用，跳过更新")
                            return "skipped"
                
                for key, value in row.items():
                    if hasattr(existing, key) and key != 'id':
                        # 将空字符串转换为 None（用于外键字段）
                        if value == '':
                            value = None
                        # 处理布尔值字段
                        if key == "is_active" and isinstance(value, str):
                            value = value.lower() == 'true'
                        # 处理日期时间字段
                        if key in datetime_fields and value:
                            try:
                                from datetime import datetime
                                if isinstance(value, str):
                                    # 尝试解析 ISO 格式日期
                                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            except Exception:
                                pass  # 如果解析失败，保持原值
                        setattr(existing, key, value)
                # 立即刷新，避免批量更新导致的唯一约束冲突
                db.flush()
                return "overwritten"
            except Exception as e:
                print(f"Error overwriting record: {e}")
                raise
        elif conflict_strategy == "merge":
            # 合并逻辑：对于配置表，合并等同于覆盖
            try:
                datetime_fields = ["created_at", "updated_at", "last_run_at", "next_run_at", "export_time"]
                
                # 检查更新后的名称是否会导致唯一约束冲突
                if 'name' in row and hasattr(model, 'name'):
                    new_name = row['name']
                    if new_name != existing.name:
                        # 检查新名称是否被其他记录使用
                        name_conflict = db.execute(
                            select(model).where(model.name == new_name, model.id != existing.id)
                        ).scalar_one_or_none()
                        if name_conflict:
                            # 名称已被其他记录使用，跳过更新
                            print(f"[IMPORT WARNING] {table_name}: 名称 '{new_name}' 已被记录(id={name_conflict.id})使用，跳过更新")
                            return "skipped"
                
                for key, value in row.items():
                    if hasattr(existing, key) and key != 'id':
                        # 将空字符串转换为 None（用于外键字段）
                        if value == '':
                            value = None
                        # 处理布尔值字段
                        if key == "is_active" and isinstance(value, str):
                            value = value.lower() == 'true'
                        # 处理日期时间字段
                        if key in datetime_fields and value:
                            try:
                                from datetime import datetime
                                if isinstance(value, str):
                                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            except Exception:
                                pass
                        setattr(existing, key, value)
                # 立即刷新，避免批量更新导致的唯一约束冲突
                db.flush()
                return "overwritten"
            except Exception as e:
                print(f"Error merging record: {e}")
                raise

    # 创建新记录
    try:
        record_data = {}
        for k, v in row.items():
            # 将空字符串转换为 None（用于外键字段）
            if v == '':
                v = None
            # 处理布尔值字段
            if k == "is_active" and isinstance(v, str):
                v = v.lower() == 'true'
            # 处理日期时间字段
            datetime_fields = ["created_at", "updated_at", "last_run_at", "next_run_at", "export_time"]
            if k in datetime_fields and v:
                try:
                    from datetime import datetime
                    if isinstance(v, str):
                        v = datetime.fromisoformat(v.replace('Z', '+00:00'))
                except Exception:
                    pass
            record_data[k] = v
        new_record = model(**record_data)
        db.add(new_record)
        # 立即刷新，避免批量更新导致的唯一约束冲突
        db.flush()
        return "imported"
    except Exception as e:
        print(f"Error creating record: {e}, data: {record_data}")
        raise


def _import_fund_type_row(db: Session, row: Dict, conflict_strategy: str, id_mapping: Dict[int, int]) -> tuple:
    """导入 fund_types 单行数据，直接保留原始 ID

    Args:
        db: 数据库会话
        row: 行数据
        conflict_strategy: 冲突处理策略
        id_mapping: 旧 ID 到新 ID 的映射字典（为保持接口一致，实际不使用）

    Returns:
        (处理结果, 旧 ID, 新 ID) 元组
    """
    from sqlalchemy import select
    from models import FundType

    old_id = row.get("id")

    # 检查是否存在冲突（基于 id 字段）
    existing_by_id = db.execute(
        select(FundType).where(FundType.id == old_id)
    ).scalar_one_or_none()

    # 检查是否存在冲突（基于 name 字段）
    name = row.get("name")
    existing_by_name = db.execute(
        select(FundType).where(FundType.name == name)
    ).scalar_one_or_none()

    # 如果 ID 冲突或 name 冲突
    if existing_by_id or existing_by_name:
        existing = existing_by_id or existing_by_name
        if conflict_strategy == "skip":
            return "skipped", old_id, existing.id
        elif conflict_strategy == "overwrite":
            # 更新现有记录
            try:
                for key, value in row.items():
                    if hasattr(existing, key):
                        # 将空字符串转换为 None（用于外键字段）
                        if value == '':
                            value = None
                        # 处理日期时间字段
                        if key in ["created_at", "updated_at"] and value:
                            try:
                                from datetime import datetime
                                if isinstance(value, str):
                                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            except Exception:
                                pass
                        setattr(existing, key, value)
                # 立即刷新，避免批量更新导致的唯一约束冲突
                db.flush()
                return "overwritten", old_id, existing.id
            except Exception as e:
                print(f"Error overwriting fund_type: {e}")
                raise
        elif conflict_strategy == "merge":
            # 合并逻辑：对于配置表，合并等同于覆盖
            try:
                for key, value in row.items():
                    if hasattr(existing, key):
                        # 将空字符串转换为 None（用于外键字段）
                        if value == '':
                            value = None
                        # 处理日期时间字段
                        if key in ["created_at", "updated_at"] and value:
                            try:
                                from datetime import datetime
                                if isinstance(value, str):
                                    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            except Exception:
                                pass
                        setattr(existing, key, value)
                # 立即刷新，避免批量更新导致的唯一约束冲突
                db.flush()
                return "overwritten", old_id, existing.id
            except Exception as e:
                print(f"Error merging fund_type: {e}")
                raise

    # 创建新记录，保留原始 ID
    try:
        record_data = {}
        for k, v in row.items():
            # 将空字符串转换为 None（用于外键字段）
            if v == '':
                v = None
            # 处理日期时间字段
            if k in ["created_at", "updated_at"] and v:
                try:
                    from datetime import datetime
                    if isinstance(v, str):
                        v = datetime.fromisoformat(v.replace('Z', '+00:00'))
                except Exception:
                    pass
            record_data[k] = v

        new_record = FundType(**record_data)
        db.add(new_record)
        db.flush()  # 立即执行插入

        print(f"[IMPORT DEBUG] Created fund_type: id={old_id}, name={name}, parent_id={record_data.get('parent_id')}")
        return "imported", old_id, old_id
    except Exception as e:
        print(f"Error creating fund_type: {e}, data: {record_data}")
        raise


def _import_asset_records_with_mapping(
    source_db_path: str,
    target_db: Session,
    table_name: str,
    conflict_strategy: str,
    fk_mapper: ForeignKeyMapper
) -> ImportResult:
    """导入 asset_records 表，使用外键映射器重新映射外键ID

    Args:
        source_db_path: 源数据库文件路径
        target_db: 目标数据库会话
        table_name: 表名（应为 asset_records）
        conflict_strategy: 冲突处理策略
        fk_mapper: 外键映射器

    Returns:
        导入结果
    """
    result = ImportResult(table_name=table_name)

    # 连接源数据库
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row

    try:
        # 获取源表结构
        source_structure = get_table_structure_sqlite(source_conn, table_name)

        # 获取目标表结构
        target_inspector = inspect(target_db.bind)
        target_columns = target_inspector.get_columns(table_name)
        target_col_names = {col["name"] for col in target_columns}

        # 获取共同列
        common_columns = [col for col in source_structure.columns.keys() if col in target_col_names]

        if not common_columns:
            result.errors.append("No common columns found between source and target tables")
            return result

        # 获取主键列
        target_pk = target_inspector.get_pk_constraint(table_name)
        target_pk_cols = set(target_pk.get("constrained_columns", []))

        # 读取源表数据
        cols_str = ", ".join(common_columns)
        cursor = source_conn.execute(f"SELECT {cols_str} FROM {table_name}")
        rows = cursor.fetchall()

        result.total_rows = len(rows)

        # 获取目标表的默认值
        target_col_info = {col["name"]: col for col in target_columns}

        for row in rows:
            row_dict = dict(row)

            # 处理缺失列的默认值
            for col in target_col_names:
                if col not in row_dict:
                    col_info = target_col_info.get(col, {})
                    default = col_info.get("default")
                    if default is not None:
                        row_dict[col] = default
                    else:
                        col_type = str(col_info.get("type", "")).upper()
                        if "INT" in col_type or "NUMERIC" in col_type or "DECIMAL" in col_type or "FLOAT" in col_type:
                            row_dict[col] = 0
                        elif "BOOL" in col_type:
                            row_dict[col] = False
                        elif "DATE" in col_type or "TIME" in col_type:
                            row_dict[col] = datetime.now()
                        else:
                            row_dict[col] = ""

            # 检查是否有无法映射的外键
            if fk_mapper.has_unmapped_foreign_keys(row_dict):
                unmapped_fks = []
                for fk_field, fk_config in ASSET_RECORDS_FOREIGN_KEYS.items():
                    if fk_field in row_dict and row_dict[fk_field] is not None:
                        source_id = row_dict[fk_field]
                        table_name_fk = fk_config['table']
                        if fk_mapper.is_unmapped(table_name_fk, source_id):
                            unmapped_fks.append(f"{fk_field}={source_id}")
                error_msg = f"外键无法映射: {', '.join(unmapped_fks)}, asset_name={row_dict.get('asset_name')}"
                print(f"[IMPORT ERROR] {error_msg}")
                result.errors.append(error_msg)
                result.skipped_rows += 1
                continue

            # 重新映射外键
            row_dict = fk_mapper.remap_asset_record_foreign_keys(row_dict)

            # 检查主键冲突
            has_conflict = False
            if target_pk_cols:
                conditions = []
                params = {}
                for col in target_pk_cols:
                    if col in row_dict:
                        conditions.append(f"{col} = :{col}")
                        params[col] = row_dict[col]

                if conditions:
                    where_clause = " AND ".join(conditions)
                    query = text(f"SELECT * FROM {table_name} WHERE {where_clause}")
                    existing = target_db.execute(query, params).fetchone()
                    has_conflict = existing is not None

            try:
                if has_conflict:
                    if conflict_strategy == "skip":
                        result.skipped_rows += 1
                        continue
                    elif conflict_strategy == "overwrite":
                        # 更新现有记录
                        update_cols = [col for col in common_columns if col not in target_pk_cols]
                        if update_cols:
                            set_clause = ", ".join([f"{col} = :{col}" for col in update_cols])
                            where_clause = " AND ".join([f"{col} = :pk_{col}" for col in target_pk_cols if col in row_dict])

                            update_params = {col: row_dict[col] for col in update_cols}
                            for col in target_pk_cols:
                                if col in row_dict:
                                    update_params[f"pk_{col}"] = row_dict[col]

                            query = text(f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}")
                            target_db.execute(query, update_params)
                            result.overwritten_rows += 1
                else:
                    # 插入新记录
                    cols = list(row_dict.keys())
                    placeholders = [f":{col}" for col in cols]

                    insert_query = text(f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})")
                    target_db.execute(insert_query, row_dict)
                    result.imported_rows += 1

            except Exception as e:
                result.errors.append(f"Error importing row: {str(e)}")

        target_db.commit()
        return result

    finally:
        source_conn.close()
