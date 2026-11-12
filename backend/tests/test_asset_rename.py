"""资产项改名契约（ADR-0026 / 票 03）：原因、影响面、回写、令牌、撞名。

钉死的是「改名为什么可审计、可回退」，不是实现细节：

1. 没有原因/影响范围的改名**必须失败**（不能靠默认值猜）；
2. `evolution`/`normalization` 只改当前名，历史快照文本不动（时点语义仍在）；
3. `correction` 回写该 item **全部**历史行的名称文本（含 superseded），item id 不变；
4. 撞名按归一后判定，且历史名/别名与现名同等对待；自己的旧名不算撞名；
5. 预览令牌一次性、10 分钟过期、载荷与影响面哈希都必须对得上。

数据一律合成（「示例-」+ 虚构金额）。
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import Asset, AssetItem, AssetItemName, AssetItemRenameToken, AuditLog
from services import asset_rename_service

D = date(2026, 3, 31)
D2 = date(2026, 4, 30)


def mk_item(db, name, status="draft"):
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.flush()
    db.commit()
    return item


def add_row(db, item, amount, day=D, name=None, superseded_by=None):
    row = Asset(asset_name=name or item.display_name, amount=Decimal(amount),
                asset_date=day, asset_item_id=item.id, superseded_by=superseded_by)
    db.add(row)
    db.flush()
    db.commit()
    return row


def preview(db, item, name, reason="evolution", effect="current", note=None):
    return asset_rename_service.preview(
        db, item.id, name=name, reason=reason, effect=effect, reason_note=note)


def execute(db, item, name, token, reason="evolution", effect="current", note=None):
    return asset_rename_service.execute(
        db, item.id, name=name, reason=reason, effect=effect,
        reason_note=note, confirm_token=token)


def err_code(exc_info):
    return exc_info.value.code


# ---------------------------------------------------------------- 校验


def test_preview_rejects_missing_reason_and_effect(db):
    item = mk_item(db, "示例-产品A")
    with pytest.raises(ApiError) as e:
        preview(db, item, "示例-产品B", reason=None, effect=None)
    assert err_code(e) == ErrorCode.RENAME_REASON_REQUIRED
    assert e.value.status_code == 400


def test_preview_rejects_unknown_reason(db):
    item = mk_item(db, "示例-产品A")
    with pytest.raises(ApiError) as e:
        preview(db, item, "示例-产品B", reason="whatever", effect="current")
    assert err_code(e) == ErrorCode.RENAME_REASON_REQUIRED


def test_non_default_combination_requires_note(db):
    item = mk_item(db, "示例-产品A")
    with pytest.raises(ApiError) as e:
        preview(db, item, "示例-产品B", reason="correction", effect="current")
    assert err_code(e) == ErrorCode.RENAME_NOTE_REQUIRED
    # 写了备注就放行，且备注落进预览结果
    out = preview(db, item, "示例-产品B", reason="correction", effect="current",
                  note="只订正当前显示名，历史快照另有来源")
    assert out["reason_note"] == "只订正当前显示名，历史快照另有来源"


def test_default_effect_per_reason(db):
    item = mk_item(db, "示例-产品A")
    for reason, expected in (("correction", "history_backfill"),
                             ("evolution", "current"),
                             ("normalization", "current")):
        out = preview(db, item, f"示例-产品A{reason}", reason=reason,
                      effect=None)
        assert out["default_effect"] == expected
        assert out["effect"] == expected


def test_preview_same_name_rejected(db):
    item = mk_item(db, "示例-产品A")
    with pytest.raises(ApiError) as e:
        preview(db, item, "示例-产品A")
    assert e.value.status_code == 400


# ---------------------------------------------------------------- 影响面与回写


def test_preview_impact_counts_rows_and_span(db):
    item = mk_item(db, "示例-产品A")
    add_row(db, item, "100", D)
    add_row(db, item, "200", D2)
    out = preview(db, item, "示例-产品B")
    assert out["impact"] == {"rows": 2, "first_date": D.isoformat(),
                             "last_date": D2.isoformat(),
                             "superseded_rows": 0, "archived_item": False}
    assert out["confirm_token"]
    assert out["item_id"] == item.id


def test_evolution_keeps_history_snapshot_text(db):
    item = mk_item(db, "示例-产品A")
    add_row(db, item, "100", D)
    out = preview(db, item, "示例-产品B", reason="evolution", effect="current")
    result = execute(db, item, "示例-产品B", out["confirm_token"],
                     reason="evolution", effect="current")
    assert result["backfill_rows"] == 0
    assert db.get(AssetItem, item.id).display_name == "示例-产品B"
    row = db.execute(select(Asset)).scalars().one()
    assert row.asset_name == "示例-产品A"      # 历史快照文本不动
    names = db.execute(select(AssetItemName)
                       .where(AssetItemName.item_id == item.id)
                       .order_by(AssetItemName.id)).scalars().all()
    assert [n.name for n in names] == ["示例-产品A", "示例-产品B"]
    assert all(n.origin == "rename" for n in names)
    assert names[1].reason == "evolution" and names[1].effect == "current"


def test_correction_backfills_every_row_including_superseded(db):
    item = mk_item(db, "示例-产品A")
    first = add_row(db, item, "100", D)
    second = add_row(db, item, "200", D2)
    second.superseded_by = first.id
    db.commit()

    out = preview(db, item, "示例-产品B", reason="correction",
                  effect="history_backfill")
    assert out["impact"]["superseded_rows"] == 1
    result = execute(db, item, "示例-产品B", out["confirm_token"],
                     reason="correction", effect="history_backfill")

    assert result["backfill_rows"] == 2
    assert {r.asset_name for r in db.execute(select(Asset)).scalars()} == {"示例-产品B"}
    # id 不变：item 与行都没被删/重建
    assert db.get(AssetItem, item.id) is not None
    assert {r.id for r in db.execute(select(Asset)).scalars()} == {first.id, second.id}
    names = db.execute(select(AssetItemName)
                       .where(AssetItemName.item_id == item.id)
                       .order_by(AssetItemName.id)).scalars().all()
    assert names[-1].effect == "history_backfill"


def test_audit_event_records_reason_and_backfill_rows(db):
    item = mk_item(db, "示例-产品A")
    add_row(db, item, "100", D)
    out = preview(db, item, "示例-产品B", reason="correction",
                  effect="history_backfill")
    execute(db, item, "示例-产品B", out["confirm_token"],
            reason="correction", effect="history_backfill")
    log = db.execute(select(AuditLog)
                     .where(AuditLog.action == "rename")).scalars().one()
    assert log.entity == "asset_item" and log.row_id == item.id
    after = json.loads(log.after_json)
    before = json.loads(log.before_json)
    assert after["display_name"] == "示例-产品B"
    assert after["reason"] == "correction"
    assert after["effect"] == "history_backfill"
    assert after["backfill_rows"] == 1
    assert before["display_name"] == "示例-产品A"


# ---------------------------------------------------------------- 撞名


def test_conflict_with_other_display_name(db):
    a = mk_item(db, "示例-产品A")
    mk_item(db, "示例-产品B")
    with pytest.raises(ApiError) as e:
        preview(db, a, "示例-产品B")
    assert err_code(e) == ErrorCode.NAME_CONFLICT
    assert e.value.status_code == 409
    assert e.value.details["matched_level"] == "display"


def test_conflict_is_normalized(db):
    a = mk_item(db, "示例-产品A")
    mk_item(db, "示例-产品B")
    with pytest.raises(ApiError) as e:
        preview(db, a, "示例·产品B")     # 分隔符 · 归一为 -，同形
    assert err_code(e) == ErrorCode.NAME_CONFLICT


def test_conflict_with_other_alias(db):
    a = mk_item(db, "示例-产品A")
    b = mk_item(db, "示例-产品B")
    db.add(AssetItemName(item_id=b.id, name="示例-旧叫法", origin="alias",
                         effective_from=None))
    db.commit()
    with pytest.raises(ApiError) as e:
        preview(db, a, "示例-旧叫法")
    assert err_code(e) == ErrorCode.NAME_CONFLICT
    assert e.value.details["matched_level"] == "alias"


def test_rename_back_to_own_old_name_is_allowed(db):
    item = mk_item(db, "示例-产品A")
    out = preview(db, item, "示例-产品B", reason="evolution", effect="current")
    execute(db, item, "示例-产品B", out["confirm_token"])
    # 自己的历史名不算撞名 → 可撤销（D10：撤销＝再改名回去，永不 DELETE）
    out2 = preview(db, item, "示例-产品A", reason="normalization",
                   effect="current")
    result = execute(db, item, "示例-产品A", out2["confirm_token"],
                     reason="normalization", effect="current")
    assert result["item"]["display_name"] == "示例-产品A"


# ---------------------------------------------------------------- 令牌


def test_token_is_single_use(db):
    item = mk_item(db, "示例-产品A")
    out = preview(db, item, "示例-产品B")
    execute(db, item, "示例-产品B", out["confirm_token"])
    with pytest.raises(ApiError) as e:
        execute(db, item, "示例-产品B", out["confirm_token"])
    assert err_code(e) == ErrorCode.RENAME_CONFIRM_INVALID


def test_token_payload_must_match(db):
    item = mk_item(db, "示例-产品A")
    out = preview(db, item, "示例-产品B", reason="evolution", effect="current")
    with pytest.raises(ApiError) as e:
        execute(db, item, "示例-产品C", out["confirm_token"],
                reason="evolution", effect="current")
    assert err_code(e) == ErrorCode.RENAME_CONFIRM_INVALID


def test_token_expires(db):
    item = mk_item(db, "示例-产品A")
    out = preview(db, item, "示例-产品B")
    token = db.execute(select(AssetItemRenameToken)
                       .where(AssetItemRenameToken.token == out["confirm_token"])
                       ).scalars().one()
    token.expires_at = datetime.now() - timedelta(minutes=1)
    db.commit()
    with pytest.raises(ApiError) as e:
        execute(db, item, "示例-产品B", out["confirm_token"])
    assert err_code(e) == ErrorCode.RENAME_CONFIRM_INVALID


def test_impact_change_invalidates_token(db):
    item = mk_item(db, "示例-产品A")
    add_row(db, item, "100", D)
    out = preview(db, item, "示例-产品B")
    add_row(db, item, "300", date(2026, 5, 31))      # 预览后又来一行
    with pytest.raises(ApiError) as e:
        execute(db, item, "示例-产品B", out["confirm_token"])
    assert err_code(e) == ErrorCode.RENAME_CONFIRM_INVALID


def test_missing_token_is_rejected(db):
    item = mk_item(db, "示例-产品A")
    with pytest.raises(ApiError) as e:
        execute(db, item, "示例-产品B", "")
    assert err_code(e) == ErrorCode.RENAME_CONFIRM_INVALID


def test_archived_item_can_be_renamed_but_flagged_in_preview(db):
    item = mk_item(db, "示例-产品A", status="archived")
    out = preview(db, item, "示例-产品B", reason="normalization",
                  effect="current")
    assert out["impact"]["archived_item"] is True