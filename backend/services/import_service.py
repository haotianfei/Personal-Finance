"""CSV 导入（ADR-0019：分类写在**资产项**上，记录行只读）。

三处与旧实现的语义差异，都是 schema 变更的直接后果：

1. **列集合换代。** 旧 8 列（`liquidity_rating` / `fund_type` / `account`）承载的是
   记录级 EAV 打点：同一个项在不同记录上可以挂不同值，导入端还会「发现新属性 →
   自动建字典节点」。新模型里四个单值维度是 `asset_items` 的 NOT NULL 列，字典
   （`asset_classes` / `institutions` / `channels` / `members` / `tags`）是受控数据
   ——录入路径**不得静默新建**（ADR-0019 D7）。所以旧列名直接报错并指路新列，不
   做静默兼容映射。

2. **分类只补空、不覆盖。** 导入是批量入口，一次误操作能把上百个项的既有分类
   改掉。因此只写「该项当前为空」的维度；CSV 与既有值不一致只记 `warnings`。

3. **账本即变更日志。** 同项同日已有有效行且 `(amount, side)` 相同 → 不建行、
   不写审计，计入 `skipped_unchanged`。同日已有行但值不同仍按 write-once 冻结
   语义进 `skipped`（`duplicate_active`）——导入不覆盖历史，更正走更正接口。

列定义是单一来源：`CSV_COLUMNS`（导出端 `export_service.EXPORT_COLUMNS` 与之同序，
导出即可回灌）。
"""
import csv
import io
import os
import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import select

from models import Asset, AssetClass, Channel, ImportBatch, Institution, Member, Tag
from schemas import ImportPreviewRow, NewAttribute
from services import audit_service, classification_service
from services.asset_service import (_normalize_side_amount, asset_key_fields,
                                    get_or_create_item, ids_fingerprint,
                                    resolve_item_id)
from errors import ApiError

# ---------------------------------------------------------------- 列定义（单一来源）

#: 新 CSV 列集合（顺序即模板列序）。改这里必须同步 `export_service.EXPORT_COLUMNS`。
CSV_COLUMNS: tuple[str, ...] = (
    "asset_date", "asset_name", "amount", "side", "asset_class",
    "liquidity_days", "institution", "channel", "owner", "risk_level", "tags",
)

#: 没有这些列就无法落一行快照。
REQUIRED_COLUMNS: tuple[str, ...] = ("asset_date", "asset_name", "amount")

#: 旧 EAV 列 → 新列指路（出现在表头即报错，不做静默映射：语义无法一一对应）
LEGACY_COLUMN_HINTS: dict[str, str] = {
    "liquidity_rating": "liquidity_days（整数天数：0/1/7/30/90/180/365/9999）",
    "fund_type": "asset_class（类别全路径，如 `B 固定收益/B1 货币基金`）",
    "account": "institution（机构名）与 channel（渠道名）",
}

SIDES: tuple[str, ...] = ("asset", "liability")
TAG_SEPARATOR = "|"
MAX_TAGS = classification_service.MAX_TAGS
_LIQUIDITY_DAYS = frozenset(d for d, _ in classification_service.LIQUIDITY_LEVELS)
_RISK_LEVELS = frozenset(classification_service.RISK_LEVELS)


# ---------------------------------------------------------------- 基础归一化

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


def normalize_side(raw: str) -> str:
    """side 列：空 → "asset"（金额符号仍由 `_normalize_side_amount` 兜底翻转）。"""
    s = (raw or "").strip().lower()
    if not s:
        return "asset"
    if s not in SIDES:
        raise ValueError(f"side 只能是 asset 或 liability，实际：{raw!r}")
    return s


def normalize_liquidity_days(raw: str) -> int | None:
    """流动性列：空 → None（该维留空）；否则必须是白名单档位之一（ADR-0019 D4）。"""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        days = int(s)
    except ValueError:
        raise ValueError(
            f"liquidity_days 必须是整数天数，实际：{raw!r}；"
            f"可选 {sorted(_LIQUIDITY_DAYS)}")
    if days not in _LIQUIDITY_DAYS:
        raise ValueError(
            f"liquidity_days={days} 不在白名单内，可选 {sorted(_LIQUIDITY_DAYS)}")
    return days


def normalize_risk_level(raw: str) -> str | None:
    s = (raw or "").strip().upper()
    if not s:
        return None
    if s not in _RISK_LEVELS:
        raise ValueError(f"risk_level 必须是 {sorted(_RISK_LEVELS)} 之一，实际：{raw!r}")
    return s


def split_tags(raw: str) -> list[str]:
    """`|` 分隔（沿用原约定）→ 去重保序的非空标签名。"""
    out: list[str] = []
    for part in (raw or "").split(TAG_SEPARATOR):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


# ---------------------------------------------------------------- 字典解析（受控：只查不建）

def resolve_asset_class(db: Session, raw: str) -> tuple[int | None, str | None]:
    """类别串 → (叶子 id, 失败原因)。接受全路径 > code > 唯一名称。

    全路径是导出端写法（`B 固定收益/B1 货币基金`），也是唯一能唯一定位叶子的串；
    code / name 作为人工手写 CSV 的便利入口，重名即视为无法定位（`class_ambiguous`）。
    """
    needle = (raw or "").strip()
    if not needle:
        return None, None
    rows = classification_service.list_asset_classes(db)
    node = next((r for r in rows if r["path"] == needle), None)
    if node is None:
        upper = needle.upper()
        hits = [r for r in rows if (r["code"] or "").upper() == upper or r["name"] == needle]
        if not hits:
            return None, "class_unknown"
        if len(hits) > 1:
            return None, "class_ambiguous"
        node = hits[0]
    if not node["leaf"]:
        return None, "class_not_leaf"
    return node["id"], None


def resolve_institution(db: Session, raw: str) -> int | None:
    name = (raw or "").strip()
    if not name:
        return None
    return db.execute(
        select(Institution.id).where(Institution.name == name)
    ).scalar_one_or_none()


def resolve_channel(db: Session, raw: str) -> int | None:
    name = (raw or "").strip()
    if not name:
        return None
    return db.execute(
        select(Channel.id).where(Channel.name == name)
    ).scalar_one_or_none()


def resolve_member(db: Session, raw: str) -> int | None:
    name = (raw or "").strip()
    if not name:
        return None
    return db.execute(
        select(Member.id).where(Member.name == name)
    ).scalar_one_or_none()


def resolve_tags(db: Session, names: list[str]) -> tuple[list[int], list[str]]:
    """标签名 → (已存在标签 id, 未找到的名字)。未找到**不建**（ADR-0019 D7）。"""
    if not names:
        return [], []
    rows = db.execute(select(Tag.id, Tag.name).where(Tag.name.in_(names))).all()
    by_name = {name: tid for tid, name in rows}
    found = [by_name[n] for n in names if n in by_name]
    missing = [n for n in names if n not in by_name]
    return found, missing


# ---------------------------------------------------------------- 解析

def _check_header(fieldnames: list[str]) -> None:
    legacy = [c for c in fieldnames if c in LEGACY_COLUMN_HINTS]
    if legacy:
        raise ValueError(
            "CSV 文件格式错误：以下列已随分类重构下线（ADR-0019）："
            + "；".join(f"`{c}` → {LEGACY_COLUMN_HINTS[c]}" for c in legacy)
            + f"。请改用新列：{', '.join(CSV_COLUMNS)}")
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(f"CSV文件格式错误：缺少必要的列 {', '.join(missing)}")


def parse_csv_content(content: str, filename: str) -> list[dict]:
    """CSV 文本 → 行 dict 列表（坏行带 `error`，由预览/导入各自消费）。"""
    rows: list[dict] = []
    if content.startswith("\ufeff"):
        content = content[1:]

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValueError("CSV文件格式错误：无法读取列标题，请确保文件是有效的CSV格式")

    fieldnames = list(reader.fieldnames)
    _check_header(fieldnames)

    for i, row in enumerate(reader, start=2):
        try:
            raw_asset_name = (row.get("asset_name") or "").strip()
            if not raw_asset_name:
                rows.append({"row_num": i, "error": "资产名称不能为空"})
                continue
            rows.append({
                "row_num": i,
                "asset_date": normalize_date(row.get("asset_date") or ""),
                "asset_name": raw_asset_name,
                "amount": normalize_amount(row.get("amount") or "0"),
                "side": normalize_side(row.get("side") or ""),
                "asset_class": (row.get("asset_class") or "").strip(),
                "liquidity_days": normalize_liquidity_days(row.get("liquidity_days") or ""),
                "institution": (row.get("institution") or "").strip(),
                "channel": (row.get("channel") or "").strip(),
                "owner": (row.get("owner") or "").strip(),
                "risk_level": normalize_risk_level(row.get("risk_level") or ""),
                "tags": split_tags(row.get("tags") or ""),
            })
        except Exception as e:
            rows.append({"row_num": i, "error": str(e)})
    return rows


def detect_new_attributes(db: Session, parsed_rows: list[dict]) -> list[NewAttribute]:
    """【已下线】旧实现会把 CSV 里的新分类值自动建进 `attributes` 字典。

    ADR-0019 D7 明确禁止录入路径静默新建字典项（否则标签/机构会退化成自由文本），
    字典只在设置页显式维护。保留函数签名只为 router 的响应形状不变，恒返回空表。
    """
    return []


def preview_import(
    db: Session, content: str, filename: str
) -> tuple[ImportBatch, list[ImportPreviewRow], list[ImportPreviewRow], list[NewAttribute]]:
    """预览：逐行校验 + 冲突标记。**不写任何数据**（除 batch 壳行）。"""
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
                owner="",
                amount=Decimal("0"),
                has_conflict=False,
                error=row["error"],
            ))
            continue

        item_id = resolve_item_id(db, row["asset_name"])
        has_conflict = item_id is not None and db.execute(
            select(Asset.id).where(
                Asset.asset_item_id == item_id,
                Asset.asset_date == row["asset_date"],
                Asset.superseded_by.is_(None),
            ).limit(1)
        ).first() is not None

        # 注：`ImportPreviewRow` 的字段名仍是旧 EAV 三列（schemas.py 属分类重构的
        # 另一文件，不在本次改动范围）。此处按新列语义填充载体字段：
        #   fund_type ← asset_class（全路径）/ account ← 机构（无则渠道）/
        #   liquidity_rating ← 整数天数。字段改名待 schemas.py 一并完成。
        valid.append(ImportPreviewRow(
            row_num=row["row_num"],
            asset_date=row["asset_date"],
            liquidity_rating="" if row["liquidity_days"] is None else str(row["liquidity_days"]),
            fund_type=row["asset_class"],
            asset_name=row["asset_name"],
            account=row["institution"] or row["channel"],
            owner=row.get("owner", ""),
            amount=row["amount"],
            has_conflict=has_conflict,
        ))

    return batch, valid, invalid, detect_new_attributes(db, parsed)


# ---------------------------------------------------------------- 分类补齐（只补空，不覆盖）

def _warn(warnings: list[dict], row: dict, field: str, reason: str, **extra) -> None:
    warnings.append({
        "row_num": row["row_num"],
        "asset_name": row["asset_name"],
        "field": field,
        "reason": reason,
        **extra,
    })


def _classification_payload(db: Session, item, row: dict, warnings: list[dict],
                            row_side: str) -> tuple[dict, list[int]]:
    """CSV 分类列 → 只含「该项当前为空」的维度 payload + 标签 id 列表。

    已有值 ≠ CSV 值 → 记 `classification_conflict` 且**不覆盖**（导入不得批量改分类）。
    字典查不到 → 记 `*_unknown`，该维留空（draft 允许缺维，ADR-0019 D9）。
    """
    payload: dict = {}

    def fill(dim: str, value, field: str):
        if value is None:
            return
        current = getattr(item, dim)
        if current is None:
            payload[dim] = value
        elif current != value:
            _warn(warnings, row, field, "classification_conflict",
                  current=current, csv=value)

    if row["asset_class"]:
        class_id, reason = resolve_asset_class(db, row["asset_class"])
        if reason is not None:
            _warn(warnings, row, "asset_class", reason, csv=row["asset_class"])
        else:
            node = db.get(AssetClass, class_id)
            # side 一致性（ADR-0019 D8 触发器兜底，这里先给人话）
            if node is not None and node.side_scope not in ("any", row_side):
                _warn(warnings, row, "asset_class", "class_side_conflict",
                      csv=row["asset_class"], side_scope=node.side_scope,
                      side=row_side)
            else:
                fill("asset_class_id", class_id, "asset_class")

    if row["institution"]:
        institution_id = resolve_institution(db, row["institution"])
        if institution_id is None:
            _warn(warnings, row, "institution", "institution_unknown",
                  csv=row["institution"])
        else:
            fill("institution_id", institution_id, "institution")

    if row["channel"]:
        channel_id = resolve_channel(db, row["channel"])
        if channel_id is None:
            _warn(warnings, row, "channel", "channel_unknown", csv=row["channel"])
        else:
            fill("channel_id", channel_id, "channel")

    if row["owner"]:
        owner_id = resolve_member(db, row["owner"])
        if owner_id is None:
            _warn(warnings, row, "owner", "owner_unknown", csv=row["owner"])
        else:
            fill("owner_id", owner_id, "owner")

    if row["liquidity_days"] is not None:
        fill("liquidity_days", row["liquidity_days"], "liquidity_days")

    if row["risk_level"] is not None:
        fill("risk_level", row["risk_level"], "risk_level")

    tag_ids: list[int] = []
    if row["tags"]:
        tag_ids, missing = resolve_tags(db, row["tags"])
        for name in missing:
            _warn(warnings, row, "tags", "tag_unknown", csv=name)
        if len(tag_ids) > MAX_TAGS:
            _warn(warnings, row, "tags", "too_many_tags",
                  csv=row["tags"], limit=MAX_TAGS)
            tag_ids = tag_ids[:MAX_TAGS]
        existing = [t.id for t in item.tags]
        if existing:
            if sorted(existing) != sorted(tag_ids):
                _warn(warnings, row, "tags", "tags_conflict",
                      current=existing, csv=tag_ids)
            tag_ids = []      # 已有标签不覆盖

    return payload, tag_ids


def _supplement_item(db: Session, item, row: dict, warnings: list[dict],
                     row_side: str) -> None:
    """把 CSV 的分类补到资产项的空维度上（唯一写入口＝classification_service）。"""
    payload, tag_ids = _classification_payload(db, item, row, warnings, row_side)
    if payload:
        try:
            classification_service.set_classification(
                db, item.id, payload, reason="CSV 导入", actor="import")
        except ApiError as exc:
            # 叶子/side/合法值等校验失败：整维不写，记 warning（不阻断整批导入）
            _warn(warnings, row, "classification", "classification_rejected",
                  code=exc.code, message=exc.message)
    if tag_ids:
        try:
            classification_service.set_tags(db, item.id, tag_ids,
                                            reason="CSV 导入", actor="import")
        except ApiError as exc:
            _warn(warnings, row, "tags", "tags_rejected",
                  code=exc.code, message=exc.message)


# ---------------------------------------------------------------- 导入

def import_csv_records(
    db: Session, content: str, filename: str, conflict_resolution: str = "skip",
    attribute_actions: dict = None, audit_action: str = "import_confirm"
) -> ImportBatch:
    """CSV 导入：写快照行 + 把分类补到资产项（只补空）。

    - `conflict_resolution`：write-once 冻结后只认 skip 语义（参数保留兼容路由）。
    - `attribute_actions`：旧「新属性确认」交互已随 EAV 字典下线（D7），参数保留但
      不再生效——字典项不再由导入创建。
    - 瞬态回报（不落库列，router 可透出）：`batch.skipped`（同日已有行且值不同）、
      `batch.skipped_unchanged`（同日已有行且 (amount, side) 相同）、
      `batch.warnings`（分类冲突/字典未知，不覆盖）。

    留痕（ADR-0027 ①值桶）：`entity='import'` 头行 1 条（action 由调用方指定：
    `import_confirm` / `import_seed`，`after={count, source, ids_hash, ...}`）＋逐行
    `entity='asset', action='create'` 明细。**导入行不设 500 上限**（ADR-0027 D4 只是
    界面级批量操作的护栏）：导入不是可碎片化的界面点击，而是「一次导入一个文件」，
    拆开反而丢失批次语义。

    明细行逐行写在对应资产行之后（而非批次末尾）：本函数内部分类写入
    （`classification_service.set_classification`）自带 commit，逐行紧跟可保证
    「资产行 + 它的留痕」永远被同一次 flush 提交，不会出现有行无痕的断链。
    """
    parsed = parse_csv_content(content, filename)
    valid_rows = [r for r in parsed if "error" not in r]

    batch = ImportBatch(filename=filename, record_count=0, status="processing")
    db.add(batch)
    db.flush()

    imported_count = 0
    skipped: list[dict] = []
    skipped_unchanged: list[dict] = []
    warnings: list[dict] = []
    created: list[Asset] = []
    for row in valid_rows:
        # CSV 金额带符号 + side 列：负数或 side=liability 都归一为负债（金额恒正）
        side, amt = _normalize_side_amount(row["amount"], row.get("side") or "asset")

        item = get_or_create_item(db, row["asset_name"])
        # 分类只补空维度（不覆盖、不新建字典项）
        _supplement_item(db, item, row, warnings, side)

        existing = db.execute(
            select(Asset).where(
                Asset.asset_item_id == item.id,
                Asset.asset_date == row["asset_date"],
                Asset.superseded_by.is_(None),
            )
        ).scalar_one_or_none()

        if existing:
            # 账本即变更日志：同值同 side → 不建行不写审计，只回报
            if existing.amount == amt and (existing.side or "asset") == side:
                skipped_unchanged.append({
                    "asset_item_id": item.id,
                    "asset_name": item.display_name,
                    "asset_date": str(row["asset_date"]),
                    "amount": str(amt),
                    "side": side,
                    "existing_id": existing.id,
                })
            else:
                # overwrite 分支已冻结移除（write-once）：冲突显式上报，绝不静默覆盖
                skipped.append({
                    "asset_name": item.display_name,
                    "asset_date": str(row["asset_date"]),
                    "existing_id": existing.id,
                    "reason": "duplicate_active",
                })
            continue

        asset = Asset(
            asset_date=row["asset_date"],
            asset_name=item.display_name,
            asset_item_id=item.id,
            amount=amt,
            side=side,
        )
        db.add(asset)
        db.flush()      # 取 id 供明细行使用
        created.append(asset)
        # 明细行紧跟业务行（同一 flush 提交，见 docstring 末段）
        audit_service.record(db, audit_service.make_change(
            "asset", asset.id, "create",
            before=None, after=asset_key_fields(asset),
            evidence_ref=asset.source_evidence_id))
        imported_count += 1

    batch.record_count = imported_count
    batch.status = "success"
    if created:
        # 批头行（ADR-0027 D3）：一次导入一个文件＝一次操作，count 即实际落库行数
        audit_service.record(db, audit_service.make_change(
            "import", batch.id, audit_action, before=None, after={
                "count": len(created),
                "source": filename,
                "ids_hash": ids_fingerprint([a.id for a in created]),
                "batch_id": batch.id,
                "skipped": len(skipped),
                "skipped_unchanged": len(skipped_unchanged),
            }))
    db.commit()
    # 瞬态属性（非映射列）：router 透出显式冲突/警告清单；ORM 实例可安全携带
    batch.skipped = skipped
    batch.skipped_unchanged = skipped_unchanged
    batch.warnings = warnings
    return batch


def seed_import_directory(db: Session, import_dir: str) -> list[ImportBatch]:
    """把 `import/` 目录下的历史 CSV 逐个导入（幂等：同值即 skip）。

    留痕：每文件一条 `entity='import', action='import_seed'` 头行 + 逐行 create 明细，
    与 `import_confirm` 同形状——两个入口的批次语义一致，只是 action 区分来源。
    """
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
        batch = import_csv_records(db, content, filename, conflict_resolution="skip",
                                   audit_action="import_seed")
        batches.append(batch)

    for filename in dated_files:
        filepath = os.path.join(import_dir, filename)
        with open(filepath, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        # v5：overwrite 已冻结移除（write-once）；重放同值即幂等 skip
        batch = import_csv_records(db, content, filename, conflict_resolution="skip",
                                   audit_action="import_seed")
        batches.append(batch)

    return batches