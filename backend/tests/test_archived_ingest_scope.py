"""录入侧归档项收口（spec `.scratch/archived-ingest-scope` §2 D1–D4 / §3 六条）。

背景（事故）：归档项会出现在复核页「未识别项」面板，却在同一时刻的「关联到已有项」
选择器里搜不到；根因是 `_group_latest_rows` 只看 `assets.asset_name` 前缀取组内最新
快照、**完全不看 `asset_items.status`**（`screenshot_service.py:1660-1677` 旧版），
于是匹配候选、未识别项、对账锚点三处一起把归档项当成在用项；apply 侧又无兜底，
钱会被写进一个已退出计量的项（as-of 聚合自 `archived_effective_from` 起剔除它）。

本文件锁住六条新口径（外加两条票内契约：补录 resolve 侧、分类补全不再吞 ITEM_ARCHIVED）：

1. `get_group_existing` 排除归档、保留 active 与 draft（draft 也算在用，D1）；
2. `compute_preview` 真值表四格（归档名 → create 行 + 「历史上已并入」提示）；
3. 未识别项（keep / zero）不含归档项（D2）；
4. `total_current_system` ＝在用项之和 ＝全量之和 − 归档项之和（D6 披露口径）；
5. apply 三条写路径（update / zero / create）命中归档项 → 行级 409 `ITEM_ARCHIVED`
   且 `assets` 行数不变（D4）；
6. `apply_supplements` / `resolve_supplements` 收到已归档名 → 判 create 而非 update。

测试数据一律合成名（公开仓库红线：真实机构名/金额/账号不入库）。所有用例现造数据，
不触碰任何现役库。
"""
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import (
    Asset, AssetClass, AssetItem, Institution, LineageLink, Member,
)
from services import screenshot_service as ss

PREFIX = "示例银行-"
LAST_DAY = date(2026, 8, 31)        # 归档项最后一条快照日
ARCHIVED_FROM = date(2026, 9, 16)   # 归档生效日
DATA_DATE = date(2026, 9, 30)


# ---------------------------------------------------------------- fixtures / 造数
@pytest.fixture(autouse=True)
def _default_source_group(db):
    """apply 走来源组配置取前缀：真库由启动种子保证，这里保持一致。"""
    ss.seed_default_source_groups(db)


def _dims(db):
    """四维字典行：`active` 项必须四维齐全（ck_asset_items_dims_required）。"""
    klass = db.execute(
        select(AssetClass).where(AssetClass.code == "A1")).scalar_one_or_none()
    if klass is None:
        klass = AssetClass(name="示例活期", code="A1", level=1, side_scope="asset")
        db.add(klass)
    inst = db.execute(
        select(Institution).where(Institution.name == "示例银行")).scalar_one_or_none()
    if inst is None:
        inst = Institution(name="示例银行", kind="bank")
        db.add(inst)
    owner = db.execute(select(Member)).scalars().first()
    if owner is None:
        owner = Member(name="示例成员", role="self")
        db.add(owner)
    db.commit()
    return klass.id, inst.id, owner.id


def _mk_item(db, name, status="draft", archived_from=None):
    """建资产项：默认 draft（四维可空）；archived 项带归档生效日。"""
    if status == "active":
        class_id, inst_id, owner_id = _dims(db)
        item = AssetItem(display_name=name, status="active",
                         asset_class_id=class_id, liquidity_days=1,
                         institution_id=inst_id, owner_id=owner_id)
    else:
        item = AssetItem(display_name=name, status=status,
                         archived_effective_from=archived_from)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _add_row(db, item, amount, day=LAST_DAY, name=None):
    row = Asset(asset_name=name or item.display_name, amount=Decimal(str(amount)),
                asset_date=day, asset_item_id=item.id)
    db.add(row)
    db.commit()
    return row


def _mk_merge(db, src_item, dst_item):
    """登记 merge 血缘（from=旧项/已归档，to=后继）——只为构造提示所需的既有事实。"""
    link = LineageLink(from_item_id=src_item.id, to_item_id=dst_item.id,
                       link_type="merge", created_at="2026-09-16T00:00:00")
    db.add(link)
    db.commit()
    return link


def _seed_env(db):
    """真值表用的最小数据集（spec §3 第二张表四格）。

    A=active 活钱A、B=draft 草稿B、C=archived 归档C（merge→父项）、D=不存在的「新名D」。
    """
    parent = _mk_item(db, PREFIX + "父项", status="draft")     # merge 目标（在用）
    a = _mk_item(db, PREFIX + "活钱A", status="active")
    b = _mk_item(db, PREFIX + "草稿B", status="draft")
    c = _mk_item(db, PREFIX + "归档C", status="archived",
                 archived_from=ARCHIVED_FROM)
    _add_row(db, a, "100.10")
    _add_row(db, b, "200.20")
    _add_row(db, c, "300.30")
    _mk_merge(db, c, parent)
    return {"parent": parent, "a": a, "b": b, "c": c}


def _png(tmp_path, name="shot.png"):
    img = tmp_path / name
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    return img


def _apply_kw(tmp_path):
    return dict(source_group="bantuou", image_path=str(_png(tmp_path)),
                evidence_root=str(tmp_path / "evidence"))


def _latest_amount_by_name(db, prefix):
    """测试侧独立实现：组内每个名称的最后一条有效行金额（**刻意不过滤 status**）。

    只看 `superseded_by IS NULL` 且取每个名称的最大日期，与生产实现同口径但独立编码，
    好让「全量之和 − 归档之和」这条等式不是拿被测函数自己给自己作证。
    """
    from models import Asset as A
    latest = {}
    rows = db.execute(
        select(A.asset_name, A.amount, A.asset_date)
        .where(A.superseded_by.is_(None))).all()
    for name, amount, day in rows:
        if not name.startswith(prefix):
            continue
        if name not in latest or day > latest[name][0]:
            latest[name] = (day, Decimal(str(amount)))
    return {n: v[1] for n, v in latest.items()}


# ---------------------------------------------------------------- 1. 读法收口
def test_group_existing_excludes_archived_keeps_active_and_draft(db):
    """验收 1：组内 {A: active, B: draft, C: archived} → 只回 A、B（draft 不算归档）。"""
    _seed_env(db)

    rows = ss.get_group_existing(db, PREFIX)
    names = [n for n, _a in rows]
    assert PREFIX + "活钱A" in names          # active 在用
    assert PREFIX + "草稿B" in names          # draft 也在用（D1：排除它会造重复项）
    assert PREFIX + "归档C" not in names      # 归档项不进录入侧
    assert len(names) == 2
    assert dict(rows)[PREFIX + "活钱A"] == Decimal("100.10")

    items = ss.get_group_existing_items(db, PREFIX)
    by_name = {e["name"]: e for e in items}
    assert PREFIX + "归档C" not in by_name
    assert {PREFIX + "活钱A", PREFIX + "草稿B"} == set(by_name)


# ---------------------------------------------------------------- 2. 真值表四格
def test_compute_preview_truth_table(db):
    """验收 2：A→update、B→update、C→create(+已并入提示)、D→create。"""
    env = _seed_env(db)
    existing = ss.get_group_existing(db, PREFIX)
    parsed = [
        ss.ParsedItem(name="活钱A", amount=Decimal("150.00")),
        ss.ParsedItem(name="草稿B", amount=Decimal("200.20")),
        ss.ParsedItem(name="归档C", amount=Decimal("333.33")),
        ss.ParsedItem(name="新名D", amount=Decimal("44.44")),
    ]
    preview = ss.compute_preview(parsed, existing, PREFIX)
    by_src = {r.source_name: r for r in preview.rows}

    # 格 1：在用 active 名 → update，目标是既有项
    assert by_src["活钱A"].action == "update"
    assert by_src["活钱A"].asset_name == PREFIX + "活钱A"
    assert by_src["活钱A"].current_amount == Decimal("100.10")
    assert by_src["活钱A"].diff == Decimal("49.90")

    # 格 2：在用 draft 名 → 同样 update（D1：draft 不许判成新增）
    assert by_src["草稿B"].action == "update"
    assert by_src["草稿B"].asset_name == PREFIX + "草稿B"

    # 格 3：已归档名 → create（不生成 update 行），带「历史上已并入 父项」提示
    assert by_src["归档C"].action == "create"
    assert by_src["归档C"].asset_name == PREFIX + "归档C"
    assert by_src["归档C"].current_amount == Decimal("0")
    assert by_src["归档C"].archived_merged_into is None   # 未 annotate 前恒 None

    # 格 4：不存在的新名 → create，无提示
    assert by_src["新名D"].action == "create"
    assert by_src["新名D"].asset_name == PREFIX + "新名D"

    annotated = ss.annotate_archived_merge_hints(db, preview)
    hinted = {r.source_name: r for r in annotated.rows}
    assert hinted["归档C"].archived_merged_into == {
        "item_id": env["parent"].id,
        "display_name": PREFIX + "父项",
        "archived_effective_from": "2026-09-16",
    }
    # 只读提示：绝不挂在 update / 其它 create 行上
    for name in ("活钱A", "草稿B", "新名D"):
        assert hinted[name].archived_merged_into is None
    # 血缘未被改动（append-only：D3 只读查询）
    assert db.execute(select(LineageLink)).scalars().all()[0].to_item_id == env["parent"].id


def test_archived_merge_hint_absent_without_lineage(db):
    """无 merge 血缘 → 提示恒 None（不发明提示）。"""
    orphan = _mk_item(db, PREFIX + "孤归档项", status="archived",
                      archived_from=ARCHIVED_FROM)
    _add_row(db, orphan, "9.99")
    preview = ss.compute_preview([ss.ParsedItem(name="孤归档项",
                                                amount=Decimal("1.00"))],
                                 ss.get_group_existing(db, PREFIX), PREFIX)
    row = preview.rows[0]
    assert row.action == "create"
    annotated = ss.annotate_archived_merge_hints(db, preview)
    assert annotated.rows[0].archived_merged_into is None


# ---------------------------------------------------------------- 3. 未识别项
def test_unrecognized_items_exclude_archived(db):
    """验收 3：只有在用项生成本次未出现的 keep / zero 行；归档项一律不出现（D2）。"""
    _seed_env(db)
    existing = ss.get_group_existing(db, PREFIX)
    only_a = [ss.ParsedItem(name="活钱A", amount=Decimal("100.10"))]

    keep_preview = ss.compute_preview(only_a, existing, PREFIX, unrecognized="keep")
    keeps = [r.asset_name for r in keep_preview.rows if r.action == "keep"]
    assert keeps == [PREFIX + "草稿B"]              # A 本图出现；C 归档不该来讨处置

    zero_preview = ss.compute_preview(only_a, existing, PREFIX, unrecognized="all")
    zeros = [r.asset_name for r in zero_preview.rows if r.action == "zero"]
    assert zeros == [PREFIX + "草稿B"]              # 'all' 也不许清零归档项


# ---------------------------------------------------------------- 4. 对账锚点
def test_total_current_system_equals_in_use_sum(db):
    """验收 4：锚点 ＝ 在用项之和 ＝ 全量之和 − 归档项之和（逐分相等，D6 披露口径）。"""
    _seed_env(db)
    preview = ss.compute_preview([], ss.get_group_existing(db, PREFIX), PREFIX)

    by_name = _latest_amount_by_name(db, PREFIX)      # 含归档项的全量
    all_sum = sum(by_name.values(), Decimal("0"))
    archived_sum = by_name[PREFIX + "归档C"]
    in_use_sum = sum((a for _n, a in ss.get_group_existing(db, PREFIX)), Decimal("0"))

    assert preview.total_current_system == in_use_sum
    assert preview.total_current_system == all_sum - archived_sum
    assert preview.total_current_system == Decimal("300.30")   # A 100.10 + B 200.20
    assert all_sum - archived_sum == Decimal("300.30")


# ---------------------------------------------------------------- 5. 写路径兜底
def test_apply_rejects_archived_item_on_update_zero_and_create(db, tmp_path):
    """验收 5：update / zero / create 三条路径命中归档项 → 409 ITEM_ARCHIVED，零写入。"""
    env = _seed_env(db)
    archived = env["c"]
    before_item_rows = db.query(Asset).filter(
        Asset.asset_item_id == archived.id).count()
    before_all_rows = db.query(Asset).count()

    update_row = ss.ScreenshotPreview(rows=[ss.PreviewRow(
        source_name="归档C", source_amount=Decimal("9.99"),
        action="update", asset_name=PREFIX + "归档C")])
    with pytest.raises(ApiError) as e_update:
        ss.apply_preview(db, update_row, data_date=DATA_DATE, **_apply_kw(tmp_path))
    assert e_update.value.code == ErrorCode.ITEM_ARCHIVED
    assert e_update.value.status_code == 409
    assert e_update.value.details["asset_item_id"] == archived.id
    db.rollback()

    zero_row = ss.ScreenshotPreview(rows=[ss.PreviewRow(
        source_name="", source_amount=Decimal("0"), action="zero",
        asset_name=PREFIX + "归档C", current_amount=Decimal("300.30"),
        diff=Decimal("-300.30"))])
    with pytest.raises(ApiError) as e_zero:
        ss.apply_preview(db, zero_row, data_date=DATA_DATE, **_apply_kw(tmp_path))
    assert e_zero.value.code == ErrorCode.ITEM_ARCHIVED
    db.rollback()

    # create：`get_or_create_item` 按名命中归档项（name_overridden 让最终名不被组前缀改写）
    create_row = ss.ScreenshotPreview(rows=[ss.PreviewRow(
        source_name=PREFIX + "归档C", source_amount=Decimal("9.99"),
        action="create", asset_name=PREFIX + "归档C", name_overridden=True)])
    with pytest.raises(ApiError) as e_create:
        ss.apply_preview(db, create_row, data_date=DATA_DATE,
                         identity_decisions={"0": {"action": "create"}},
                         **_apply_kw(tmp_path))
    assert e_create.value.code == ErrorCode.ITEM_ARCHIVED
    db.rollback()

    assert db.query(Asset).filter(Asset.asset_item_id == archived.id).count() \
        == before_item_rows
    assert db.query(Asset).count() == before_all_rows


def test_apply_archived_target_returns_409_envelope(db, tmp_path):
    """验收 5 的返回体口径：三种指向归档项的写法都得到 409 且 body 含 ITEM_ARCHIVED。

    这里用真异常处理器（`main.register_exception_handlers`）+ TestClient 验证**返回体**，
    而不是只断言异常对象：契约是前端看 envelope 的 `error.code`。

    三种写法：
      (a) update 行**按名**解析到归档项 → 本次新增的写路径兜底（旧代码会写进去）；
      (b) update 行 + 显式 `target_item_id` 指向归档项 → R-37 既有拦截（回归锁定）；
      (c) create 行 + 显式 `target_item_id` 指向归档项 → R-37 既有拦截（回归锁定）。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from main import register_exception_handlers

    env = _seed_env(db)
    archived = env["c"]
    before_item_rows = db.query(Asset).filter(
        Asset.asset_item_id == archived.id).count()
    before_all_rows = db.query(Asset).count()
    kw = _apply_kw(tmp_path)

    app = FastAPI()
    register_exception_handlers(app)

    def _row(action: str) -> ss.PreviewRow:
        if action == "update":
            return ss.PreviewRow(source_name="归档C", source_amount=Decimal("9.99"),
                                 action="update", asset_name=PREFIX + "归档C")
        return ss.PreviewRow(source_name=PREFIX + "归档C",
                             source_amount=Decimal("9.99"), action="create",
                             asset_name=PREFIX + "归档C", name_overridden=True)

    @app.post("/apply")
    def _apply(mode: str = "by_name"):
        action = "create" if mode.startswith("create") else "update"
        decisions = None
        if mode.endswith("bind"):
            decisions = {"0": {"action": "bind", "target_item_id": archived.id}}
        preview = ss.ScreenshotPreview(rows=[_row(action)])
        ss.apply_preview(db, preview, data_date=DATA_DATE,
                         identity_decisions=decisions, **kw)
        return {"ok": True}

    client = TestClient(app)
    for mode in ("by_name", "update_bind", "create_bind"):
        response = client.post(f"/apply?mode={mode}")
        assert response.status_code == 409, mode
        body = response.json()
        assert "ITEM_ARCHIVED" in json.dumps(body, ensure_ascii=False), mode
        assert body["error"]["code"] == "ITEM_ARCHIVED", mode
        if mode == "by_name":
            # 写路径兜底自带行级 details（哪个项、归档生效日）；R-37 的既有拦截
            # 只带 field，故仅在 by_name 这条上断言
            assert body["error"]["details"]["asset_item_id"] == archived.id, mode
        db.rollback()
        assert db.query(Asset).filter(
            Asset.asset_item_id == archived.id).count() == before_item_rows, mode
        assert db.query(Asset).count() == before_all_rows, mode


# ---------------------------------------------------------------- 6. 补录判 create
def test_apply_supplements_archived_name_becomes_create(db):
    """验收 6：补录收到已归档名 → create 而非 update（D3）；在用名仍 update。"""
    _seed_env(db)
    outcome = ss.apply_supplements(
        ss.ScreenshotPreview(), ss.get_group_existing(db, PREFIX), PREFIX,
        [ss.SupplementItem(name="归档C", amount=Decimal("12.00"))])
    assert outcome.written[0].action == "create"
    assert outcome.written[0].asset_name == PREFIX + "归档C"
    assert outcome.written[0].current_amount == Decimal("0")

    # 显式带 status 的三元组形态：同样判 create（防上游漏过滤）
    with_status = [(PREFIX + "活钱A", Decimal("100.10"), "active"),
                   (PREFIX + "草稿B", Decimal("200.20"), "draft"),
                   (PREFIX + "归档C", Decimal("300.30"), "archived")]
    archived_out = ss.apply_supplements(
        ss.ScreenshotPreview(), with_status, PREFIX,
        [ss.SupplementItem(name="归档C", amount=Decimal("12.00"))])
    assert archived_out.written[0].action == "create"

    # 回归：在用名（active / draft）仍是 update，别把「带 status」变成一律 create
    for name in ("活钱A", "草稿B"):
        row = ss.apply_supplements(
            ss.ScreenshotPreview(), with_status, PREFIX,
            [ss.SupplementItem(name=name, amount=Decimal("12.00"))]).written[0]
        assert row.action == "update"


def test_resolve_supplements_archived_name_becomes_create(db):
    """验收 6（resolve 侧）：预判端点同口径——归档名判 create，current 为 None。"""
    _seed_env(db)
    results = ss.resolve_supplements(
        ss.get_group_existing(db, PREFIX), PREFIX,
        [ss.SupplementItem(name="归档C", amount=Decimal("12.00"))])
    assert results[0]["action"] == "create"
    assert results[0]["current_amount"] is None
    assert results[0]["asset_name"] == PREFIX + "归档C"


# ---------------------------------------------------------------- 票内契约：分类补全不再吞 ITEM_ARCHIVED
def test_supplement_classification_does_not_swallow_item_archived(db, monkeypatch):
    """D4：`_supplement_item_classification` 不再把 ITEM_ARCHIVED 静默吞掉；
    其余分类错误照旧不阻断采纳（分类不是入账必要条件）。"""
    env = _seed_env(db)

    def _boom_archived(*_a, **_kw):
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409, "资产项已归档，不能再改分类")

    monkeypatch.setattr(ss.classification_service, "set_classification", _boom_archived)
    with pytest.raises(ApiError) as ei:
        ss._supplement_item_classification(db, env["c"], asset_name=PREFIX + "归档C")
    assert ei.value.code == ErrorCode.ITEM_ARCHIVED

    def _boom_other(*_a, **_kw):
        raise ApiError(ErrorCode.DIMS_INCOMPLETE, 422, "四个必填维度不能为空")

    monkeypatch.setattr(ss.classification_service, "set_classification", _boom_other)
    # 不抛＝照旧宽容（历史行为不被顺手改掉）
    ss._supplement_item_classification(db, env["c"], asset_name=PREFIX + "归档C")


# ---------------------------------------------------------------- 出网契约：提示字段必须真的到达 HTTP 层
def test_preview_response_passes_archived_merged_into():
    """D3 的端到端前提：`PreviewRow.archived_merged_into` 必须被 `_preview_response`
    透传进 `ScreenshotPreviewRow`（否则服务层算出来了、前端永远收不到，用户看不到提示）。

    ★ 票 01 首版漏了这层接线（`schemas.py` + `routers/screenshots.py` 在票面写域外），
    由编排层补齐后以本用例锁住；字段缺失时旧行为不变（None → JSON null）。
    """
    from types import SimpleNamespace

    from routers.screenshots import _preview_response

    hint = {"item_id": 7, "display_name": PREFIX + "父项",
            "archived_effective_from": ARCHIVED_FROM.isoformat()}
    preview = ss.ScreenshotPreview(
        total_current_system=Decimal("300.30"),
        rows=[
            ss.PreviewRow("归档C", Decimal("10.00"), "create",
                          asset_name=PREFIX + "归档C", diff=Decimal("10.00"),
                          archived_merged_into=hint),
            ss.PreviewRow("活钱A", Decimal("100.10"), "update",
                          asset_name=PREFIX + "活钱A", current_amount=Decimal("100.10"),
                          diff=Decimal("0")),
        ],
    )
    parsed = SimpleNamespace(items=[], date=None, date_text=None)
    resp = _preview_response(parsed, [], preview, PREFIX, "example.jpg")

    create_row, update_row = resp.rows

    # 1) 字段真的出网，且形状与前端 `ScreenshotPreviewRow.archived_merged_into` 逐字一致
    assert "archived_merged_into" in resp.model_dump()["rows"][0]
    assert create_row.archived_merged_into is not None
    assert create_row.archived_merged_into.item_id == 7
    assert create_row.archived_merged_into.display_name == PREFIX + "父项"
    assert create_row.archived_merged_into.archived_effective_from == ARCHIVED_FROM.isoformat()
    # 2) 只出现在 create 行（update 行不能凭空长出提示）
    assert update_row.archived_merged_into is None
    # 3) 提示不改判定：action / 金额与入参逐字相同
    assert create_row.action == "create" and create_row.source_amount == Decimal("10.00")
    assert resp.total_current_system == Decimal("300.30")