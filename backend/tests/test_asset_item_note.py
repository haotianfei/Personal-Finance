"""项级备注端点 `PUT /api/asset-items/{item_id}/note`（ADR-0031 D2/D3/D4；票 09）。

锁六面（对应票 09「验收」）：

1. **200 回读**：`"  hello \\n world  "` → trim 掉首尾空白、内侧换行/空格原样入列；
   响应形状 `{item_id, note, updated_at}`；
2. **空串/纯空白 → 落 NULL**（清空），不是空字符串；
3. **长度上限 500**（trim 后 Unicode 码点）：500 通过、501 → 422 `NOTE_TOO_LONG`
   且库里不动（校验失败不写半条审计行）；
4. **项不存在** → 404 `ASSET_ITEM_NOT_FOUND`，零审计行；
5. **归档项可改**：不拦 `status`；审计 `asset_item/note_update`，`before`/`after`
   各含旧/新 `note`，并带上 `reason`/`reason_note`；
6. **备注不进 `asset_item_attr_history`**（D4）；同值写入＝口径未变 → 零审计行。
7. **列表读回**（票 14）：`GET /api/asset-items` 每项回传 `note`/`updated_at`，
   写后立即读回一致、`null` 与空串同语义（`''` 绝不出现），且**一条查询**取全量
   备注（不因项数增长而多查一次＝无 N+1）。
8. **当前值读模型回传 note**（票 `current-value-note`）：`GET /api/assets/current`
   每行带 `note`（同源同归一，trim 后空串→`null`）、`include_archived=true` 时归档项
   同样回传、项数 1 → 5 读 note 的语句数不变（无 N+1）。

沙箱：内存库 fixture（同 `conftest.db`），直接调路由函数（同步函数，`db` 就是依赖
注入的那个参数），不起 TestClient、不触真实 `data/`。
"""
import json

import pytest
from sqlalchemy import event, select

from errors import ApiError
from models import AssetItem, AssetItemAttrHistory, AuditLog
from routers.asset_items import (ItemNoteRequest, list_asset_items,
                                 set_item_note)
from services.asset_service import get_current_values
from services.audit_contract import rule_for

NOTE = "note_update"


@pytest.fixture()
def seed(db):
    """一个 draft ＋ 一个 archived 资产项（名称一律合成，示例- 前缀）。

    draft 而非 active：`ck_asset_items_dims_required` 要求 active 项四维齐全，
    而本端点**不看 status**（备注不参与净值），draft 足以覆盖「非归档项」这一面。
    """
    item = AssetItem(display_name="示例-项一", status="draft")
    archived = AssetItem(display_name="示例-项二", status="archived")
    db.add_all([item, archived])
    db.commit()
    return {"item": item, "archived": archived}


# ---------------------------------------------------------------- 助手

def _put(db, item_id: int, note, **body):
    """按端点的入参形状调用（body 里的 reason/reason_note 走请求体）。"""
    return set_item_note(item_id, ItemNoteRequest(note=note, **body), db)


def _audit(db, action: str = NOTE) -> list[AuditLog]:
    return db.execute(
        select(AuditLog).where(AuditLog.action == action)
        .order_by(AuditLog.id)).scalars().all()


def _payload(row: AuditLog, which: str):
    """审计行的 before_json / after_json（SQL NULL → None）。"""
    text = getattr(row, f"{which}_json")
    return None if text is None else json.loads(text)


def _stored_note(db, item_id: int):
    """直接读列：`None` 证明落的是 SQL NULL（空串会回 `''`，两者可区分）。"""
    return db.execute(
        select(AssetItem.note).where(AssetItem.id == item_id)).scalar()


# ---------------------------------------------------------------- 1. 200 回读

def test_write_and_readback_trims_outer_whitespace(db, seed):
    item = seed["item"]

    body = _put(db, item.id, "  hello \n world  ")

    assert body["item_id"] == item.id
    assert body["note"] == "hello \n world"      # 首尾 trim，内部换行/空格保留
    assert isinstance(body["updated_at"], str) and body["updated_at"]
    assert set(body) == {"item_id", "note", "updated_at"}
    assert _stored_note(db, item.id) == "hello \n world"


def test_write_overwrites_whole_note_not_append(db, seed):
    """整条替换（不是追加）：第二次写短文本，旧内容不得残留。"""
    item = seed["item"]
    _put(db, item.id, "很长的一段旧备注，含多行\n第二行")
    assert _put(db, item.id, "新")["note"] == "新"
    assert _stored_note(db, item.id) == "新"


# ---------------------------------------------------------------- 2. 空 → NULL

def test_empty_and_whitespace_clear_to_null(db, seed):
    item = seed["item"]
    _put(db, item.id, "先写一条备注")

    assert _put(db, item.id, "")["note"] is None          # 空串＝清空
    assert _stored_note(db, item.id) is None              # SQL NULL，不是 ''

    _put(db, item.id, "再写一条")
    assert _put(db, item.id, "  \n\t  ")["note"] is None   # 纯空白同样清空
    assert _stored_note(db, item.id) is None


# ---------------------------------------------------------------- 3. 长度上限

def test_length_limit_counts_trimmed_code_points(db, seed):
    item = seed["item"]

    assert _put(db, item.id, "字" * 499)["note"] == "字" * 499
    # 上限按 trim 后的码点算：首尾空白不计入（501 字符的原文 trim 后是 500 → 通过）
    assert _put(db, item.id, "  " + "字" * 500 + "  ")["note"] == "字" * 500

    with pytest.raises(ApiError) as exc:
        _put(db, item.id, "字" * 501)
    assert (exc.value.code, exc.value.status_code) == ("NOTE_TOO_LONG", 422)
    assert exc.value.details == {"max_length": 500, "length": 501}

    assert _stored_note(db, item.id) == "字" * 500        # 库内不变
    assert len(_audit(db)) == 2                           # 失败那次零审计行


# ---------------------------------------------------------------- 4. 404

def test_missing_item_returns_asset_item_not_found(db):
    with pytest.raises(ApiError) as exc:
        _put(db, 999999, "示例备注")

    assert (exc.value.code, exc.value.status_code) == ("ASSET_ITEM_NOT_FOUND", 404)
    assert exc.value.details == {"item_id": 999999}
    assert _audit(db) == []


# ---------------------------------------------------------------- 5. 归档项可改 + 审计

def test_archived_item_is_writable_and_audited(db, seed):
    archived = seed["archived"]
    _put(db, archived.id, "旧备注")

    body = _put(db, archived.id, "归档后仍可改",
                reason="correction", reason_note="补记续期条款")

    assert body["note"] == "归档后仍可改"
    db.expire_all()
    assert db.get(AssetItem, archived.id).status == "archived"   # 状态未被顺手改动

    rows = _audit(db)
    assert len(rows) == 2
    last = rows[-1]
    assert (last.entity, last.action, last.row_id) == ("asset_item", NOTE, archived.id)
    assert last.actor == "owner"
    assert _payload(last, "before") == {"note": "旧备注"}
    assert _payload(last, "after") == {"note": "归档后仍可改"}
    assert (last.reason, last.reason_note) == ("correction", "补记续期条款")


def test_clearing_note_is_audited_with_null_after(db, seed):
    """清空也改了口径（有 → 无），审计必须留行且 after.note 为 null。"""
    item = seed["item"]
    _put(db, item.id, "待清空")
    _put(db, item.id, "")

    last = _audit(db)[-1]
    assert _payload(last, "before") == {"note": "待清空"}
    assert _payload(last, "after") == {"note": None}


# ---------------------------------------------------------------- 6. D4 + 同值零行

def test_note_does_not_enter_attr_history_and_same_value_writes_nothing(db, seed):
    item = seed["item"]
    _put(db, item.id, "第一条")
    _put(db, item.id, "第二条")
    _put(db, item.id, "第二条")            # 同值＝口径未变

    assert db.execute(
        select(AssetItemAttrHistory)
        .where(AssetItemAttrHistory.item_id == item.id)).scalars().all() == []
    assert len(_audit(db)) == 2


# ---------------------------------------------------------------- 7. 列表读回（票 14）

def _note_queries(db):
    """抓「读 `asset_items.note`」的 SQL 语句（用来锁「一条查询取全量」）。

    只认 select 列表里带该列的那条：`list_items` 自己的项/名查询不算，
    否则会把服务层既有的 `aliases` 逐项查询误记为备注 N+1。
    """
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if "asset_items.note" in statement:
            seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _listener)
    return seen, lambda: event.remove(engine, "before_cursor_execute", _listener)


def test_list_returns_note_with_null_semantics(db):
    """① 写后立即读回一致（trim 口径与写口相同）；② 没写/清空都是 `null`，绝无 `''`。"""
    written = AssetItem(display_name="示例-列表一", status="draft")
    cleared = AssetItem(display_name="示例-列表二", status="draft")
    never = AssetItem(display_name="示例-列表三", status="draft")
    db.add_all([written, cleared, never])
    db.commit()

    _put(db, written.id, "  hello \n world  ")      # 写口 trim 首尾，内部换行保留
    _put(db, cleared.id, "先写后被清空")
    _put(db, cleared.id, "")

    body = list_asset_items(include_archived=True, db=db)
    by_id = {it["id"]: it for it in body["items"]}

    assert body["total"] == len(body["items"]) == 3
    assert by_id[written.id]["note"] == "hello \n world"   # 与写口回读逐字一致
    assert by_id[cleared.id]["note"] is None              # 清空＝NULL
    assert by_id[never.id]["note"] is None                # 从未写＝NULL
    # 键恒在（不因没备注而缺键），且空串绝不外泄
    assert all("note" in it and "updated_at" in it for it in body["items"])
    assert all(it["note"] != "" for it in body["items"])
    assert all(isinstance(it["updated_at"], str) and it["updated_at"]
               for it in body["items"])


def test_list_note_is_independent_of_archived_filter(db, seed):
    """归档过滤只筛项，不改备注读法（两种过滤下同一项回同一个值）。"""
    _put(db, seed["item"].id, "在册项备注")
    _put(db, seed["archived"].id, "归档项备注")

    active_only = {it["id"]: it["note"] for it in
                   list_asset_items(include_archived=False, db=db)["items"]}
    assert active_only == {seed["item"].id: "在册项备注"}

    everything = {it["id"]: it["note"] for it in
                  list_asset_items(include_archived=True, db=db)["items"]}
    assert everything == {seed["item"].id: "在册项备注",
                          seed["archived"].id: "归档项备注"}


def test_list_empty_db_stays_well_formed(db):
    """零项时不得因「IN ()」之类的空集查询炸掉（返回空列表＋total=0）。"""
    assert list_asset_items(include_archived=True, db=db) == {"items": [], "total": 0}


def test_list_reads_all_notes_in_constant_query_count(db):
    """不引 N+1：项数 1 → 5，读 note 的语句条数不变（逐项读会 1 → 5）。

    只按「条数随项数增长与否」断言，不钉死绝对值：`list_items` 自身那条全列查询
    也会带出 `asset_items.note`（ORM select-all），钉绝对值会把服务层实现绑进本票。
    """
    def _list_notes():
        seen, stop = _note_queries(db)
        try:
            body = list_asset_items(include_archived=True, db=db)
        finally:
            stop()
        return seen, {it["id"]: it["note"] for it in body["items"]}

    first = AssetItem(display_name="示例-批量0", status="draft")
    db.add(first)
    db.commit()
    _put(db, first.id, f"备注-{first.id}")

    seen_one, notes_one = _list_notes()
    assert notes_one == {first.id: f"备注-{first.id}"}

    more = [AssetItem(display_name=f"示例-批量{i}", status="draft") for i in range(1, 5)]
    db.add_all(more)
    db.commit()
    for it in more:
        _put(db, it.id, f"备注-{it.id}")

    seen_five, notes_five = _list_notes()
    assert notes_five == {it.id: f"备注-{it.id}" for it in [first, *more]}
    assert len(seen_five) == len(seen_one)       # 项数×5，语句数不变＝无 N+1


# ---------------------------------------------------------------- 8. 当前值读模型回传 note

def test_current_values_return_note_with_null_semantics(db):
    """`GET /api/assets/current` 每行带 `note`（`.scratch/current-value-note/spec.md` §3.1）。

    ① 有备注＝非空文本，且与写口同归一（trim 首尾、内部换行保留）；
    ② 清空 / 从未写 / 库里只存空白 → `null`（历史脏数据不得点亮前端便签）；
    ③ 键恒在，**全库不出现空串**。
    """
    written = AssetItem(display_name="示例-当前值一", status="draft")
    cleared = AssetItem(display_name="示例-当前值二", status="draft")
    never = AssetItem(display_name="示例-当前值三", status="draft")
    # 存量脏数据形态：绕过写口直接落纯空白（写口会把它归一成 NULL）
    blank = AssetItem(display_name="示例-当前值四", status="draft", note="  \n\t ")
    db.add_all([written, cleared, never, blank])
    db.commit()

    _put(db, written.id, "  hello \n world  ")      # 写口 trim 首尾，内部换行保留
    _put(db, cleared.id, "先写后被清空")
    _put(db, cleared.id, "")

    body = get_current_values(db, include_archived=True)
    by_id = {row["item_id"]: row for row in body["items"]}

    assert set(by_id) == {written.id, cleared.id, never.id, blank.id}
    assert all("note" in row for row in body["items"])      # 无记录行也带备注键
    assert by_id[written.id]["note"] == "hello \n world"    # 与写口回读逐字一致
    assert by_id[cleared.id]["note"] is None               # 清空＝NULL
    assert by_id[never.id]["note"] is None                 # 从未写＝NULL
    assert by_id[blank.id]["note"] is None                 # 纯空白＝没写（trim 后为空）
    assert all(row["note"] != "" for row in body["items"])  # 空串绝不外泄


def test_current_values_note_covers_archived_items(db, seed):
    """§3.2：`include_archived=true` 时归档项同样带备注（过滤只筛项，不改备注读法）。"""
    _put(db, seed["item"].id, "在册项备注")
    _put(db, seed["archived"].id, "归档项备注")

    active_only = {row["item_id"]: row["note"]
                   for row in get_current_values(db)["items"]}
    assert active_only == {seed["item"].id: "在册项备注"}

    everything = {row["item_id"]: row["note"]
                  for row in get_current_values(db, include_archived=True)["items"]}
    assert everything == {seed["item"].id: "在册项备注",
                          seed["archived"].id: "归档项备注"}


def test_current_values_read_all_notes_in_constant_query_count(db):
    """§3.3：不引 N+1——项数 1 → 5，读 `asset_items.note` 的语句条数不变。

    只断言「条数随项数增长与否」，不钉绝对值：`get_current_values` 自己那条
    `select(AssetItem)`（ORM 全列）也会带出 note 列，钉绝对值会把实现绑进本票。
    """
    def _current_notes():
        seen, stop = _note_queries(db)
        try:
            body = get_current_values(db, include_archived=True)
        finally:
            stop()
        return seen, {row["item_id"]: row["note"] for row in body["items"]}

    first = AssetItem(display_name="示例-当前值批0", status="draft")
    db.add(first)
    db.commit()
    _put(db, first.id, f"备注-{first.id}")

    seen_one, notes_one = _current_notes()
    assert notes_one == {first.id: f"备注-{first.id}"}

    more = [AssetItem(display_name=f"示例-当前值批{i}", status="draft")
            for i in range(1, 5)]
    db.add_all(more)
    db.commit()
    for it in more:
        _put(db, it.id, f"备注-{it.id}")

    seen_five, notes_five = _current_notes()
    assert notes_five == {it.id: f"备注-{it.id}" for it in [first, *more]}
    assert len(seen_five) == len(seen_one)       # 项数×5，语句数不变＝无 N+1


# ---------------------------------------------------------------- 契约表对齐

def test_contract_row_is_registered_and_route_takes_it_from_table():
    """D9：entity/action 以契约为唯一真源，路由不另抄一份（改表即改留痕）。"""
    rule = rule_for("PUT", "/api/asset-items/{item_id}/note")
    assert rule is not None
    assert (rule.bucket, rule.entity, rule.action, rule.status) == (
        "口径", "asset_item", NOTE, "traced")

    from routers.asset_items import _note_rule
    assert (_note_rule().entity, _note_rule().action) == ("asset_item", NOTE)