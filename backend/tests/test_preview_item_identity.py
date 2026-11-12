"""预览把「名称」提升成「资产项身份」：item 级现值 + 行 item_id 回填。

背景（ADR-0026 补遗）：识别行关联到已有项后，行内差额与顶部「系统合计」都得以
**目标项的现值**为基线。只给名称做不到——同一项可能因改名未回写而以多个名称出现，
且同一项多行会被重复计入。故 preview 暴露 `existing_items`（item 级）并在行上
回填 `item_id`。
"""
from datetime import date
from decimal import Decimal

from models import Asset, AssetItem
from services import screenshot_service as ss

PREFIX = "合成甲-"


def mk_item(db, name: str, status: str = "draft") -> AssetItem:
    """建项：默认 draft（四维必填 CHECK 对 draft 豁免，见 ck_asset_items_dims_required）。"""
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def add_row(db, item: AssetItem, name: str, amount, day: date) -> Asset:
    row = Asset(asset_name=name, amount=Decimal(str(amount)),
                asset_date=day, asset_item_id=item.id)
    db.add(row)
    db.commit()
    return row


def test_group_existing_items_collapses_to_latest_row_per_item(db):
    item = mk_item(db, PREFIX + "产品A")
    add_row(db, item, PREFIX + "产品A", 100, date(2026, 1, 1))
    add_row(db, item, PREFIX + "产品A", 300, date(2026, 2, 1))
    # 改名未回写：同一项历史上以旧名出现过
    add_row(db, item, PREFIX + "产品A旧名", 50, date(2025, 12, 1))

    items = ss.get_group_existing_items(db, PREFIX)
    assert len(items) == 1, "同一 asset_item 只应出现一次"
    only = items[0]
    assert only["item_id"] == item.id
    assert Decimal(only["amount"]) == Decimal("300")
    assert only["name"] == PREFIX + "产品A"     # 取日期最新那行的名称做代表
    assert only["as_of"] == "2026-02-01"


def test_group_existing_items_keeps_legacy_rows_without_item_id(db):
    mk_item(db, PREFIX + "产品A")
    db.add(Asset(asset_name=PREFIX + "无主行", amount=Decimal("12.34"),
                 asset_date=date(2026, 3, 1), asset_item_id=None))
    db.commit()

    by_name = {e["name"]: e for e in ss.get_group_existing_items(db, PREFIX)}
    assert by_name[PREFIX + "无主行"]["item_id"] is None
    assert Decimal(by_name[PREFIX + "无主行"]["amount"]) == Decimal("12.34")


def test_attach_item_ids_backfills_hit_rows_and_leaves_create_empty(db):
    item = mk_item(db, PREFIX + "产品A")
    add_row(db, item, PREFIX + "产品A", 100, date(2026, 1, 1))

    existing = ss.get_group_existing(db, PREFIX)
    preview = ss.compute_preview(
        [ss.ParsedItem(name="产品A", amount=Decimal("120")),
         ss.ParsedItem(name="产品B", amount=Decimal("7"))],
        existing, PREFIX)
    out = ss.attach_item_ids(preview, ss.get_group_existing_items(db, PREFIX))

    kinds = {r.action: r for r in out.rows}
    assert kinds["update"].item_id == item.id
    assert kinds["create"].item_id is None
    assert out.existing_items and out.existing_items[0]["item_id"] == item.id


def test_attach_item_ids_unresolved_name_stays_none(db):
    """名称在该来源组的 item 级清单里查不到（例如历史行未挂项）→ 不回填，交前端按名兜底。"""
    preview = ss.ScreenshotPreview(rows=[
        ss.PreviewRow(source_name="产品C", source_amount=Decimal("1"),
                      action="update", asset_name=PREFIX + "查无此项"),
    ])
    out = ss.attach_item_ids(preview, [])
    assert out.rows[0].item_id is None
    assert out.existing_items == []