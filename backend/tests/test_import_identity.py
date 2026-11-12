"""导入身份裁决（item-identity-drift §2 D4 / issue 05）。

缺陷形状（2026-09-16 事故）：`_upsert_asset` 经 `get_or_create_item` 在名字未命中时
静默新建资产项；当这实际是一次改名/拆分时，旧项不会归零，as-of 沿用与新项**双算一期**。
本票把「新项首现」变成流程上的强表态点：

- 预览：新名 + 同机构（`asset_items.institution_id`，ADR-0019 后账户维度＝机构）
  已有 active 旧项且名称相似 → 行打 `identity_guess`（提示，绝不自动裁决）；
- 采纳：未表态 → 400 `identity_decision_required`（零写入）；`merge` → 走既有
  `lineage_service.register_lineage`（旧项自生效日归档 + 口径承接 + 审计）；
  `create` → 与历史行为一致。

分类在资产项上（ADR-0019 D2）：fixture 用 `classification_service` 给项补四维，
记录行只写金额/side（`create_asset` 传 `attribute_ids` 已 422）。
数据全部合成（虚构机构、示例条目、示例金额），不含真实财务信息。
"""
from datetime import date
from decimal import Decimal
from typing import NamedTuple

import pytest
from sqlalchemy import func, select

from errors import ApiError, ErrorCode
from models import (
    Asset, AssetClass, AssetItem, AssetItemName, AuditLog, Institution,
    LineageLink, Member, SourceGroup,
)
from schemas import ScreenshotPreviewRow
from services import analysis_service, asset_service, classification_service, screenshot_service
from services.screenshot_service import (
    ParsedItem,
    ScreenshotPreview,
    _identity_candidates,
    annotate_identity_guesses,
    apply_preview,
    compute_preview,
    get_group_existing,
)

PREFIX = "示例银行-"
OLD = PREFIX + "贵金属-黄金"          # 旧命名（改名前的资产项）
NEW = PREFIX + "黄金账户"             # 截图里的新命名（不同 -> 会新建）
GROUP = "demo-bank"

D_PRE = date(2026, 8, 31)             # 旧项最后一次有值
D_MERGE = date(2026, 9, 16)           # 新命名首现（= 改名生效日）
OLD_AMOUNT = Decimal("1200000.00")
NEW_AMOUNT = Decimal("1180000.00")


# ---------------------------------------------------------------- fixture 助手


# 来源组 ↔ 账户仍是过渡机制（`source_group_accounts` 外键指 `attributes`，0027 才迁）；
# 身份提示里 `account_ids` 只作「该组挂过账户/机构」的**非空门槛**，不参与任何比较
# （见 `screenshot_service.annotate_identity_guesses`）。因此用哨兵表达门槛，避免本
# 文件依赖一张将被删除的旧表。
ACCOUNT_GATE: tuple[int, ...] = (1,)


class Env(NamedTuple):
    """一次建齐分类字典三件套（类别叶子 / 机构 / 成员）+ 来源组账户门槛。"""
    accounts: tuple[int, ...]
    institution: Institution
    leaf: AssetClass
    member: Member


def _env(db, slug=GROUP, label="示例银行", prefix=PREFIX) -> Env:
    root = AssetClass(code="A", name="现金与等价物", level=1, side_scope="any")
    db.add(root)
    db.flush()
    leaf = AssetClass(code="A1", name="活期存款", parent_id=root.id, level=2,
                      side_scope="any")
    inst = Institution(name=label, kind="bank")
    member = Member(name="成员甲", role="self")
    db.add_all([leaf, inst, member])
    db.flush()
    sg = SourceGroup(slug=slug, label=label, asset_prefix=prefix)
    db.add(sg)
    db.commit()
    return Env(accounts=ACCOUNT_GATE, institution=inst, leaf=leaf, member=member)


def _classify(db, item_id, env: Env):
    """补齐四维（draft → active）——与人工在设置页补分类等效。"""
    classification_service.set_classification(db, item_id, {
        "asset_class_id": env.leaf.id, "liquidity_days": 0,
        "institution_id": env.institution.id, "owner_id": env.member.id})


def _hold(db, env: Env, name, amount, on_date, side="asset"):
    """一条历史快照（经 ledger 单点建 item，形态与真库一致）。"""
    asset = asset_service.create_asset(db, name, Decimal(str(amount)), on_date,
                                       side=side)
    _classify(db, asset.asset_item_id, env)
    return asset


def _make_image(tmp_path, name="shot.png"):
    img = tmp_path / name
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    return img


def _annotated_preview(db, accounts, source_name="黄金账户", amount=NEW_AMOUNT):
    """截图解析行 → 映射清单 → 身份提示（与 /preview 管线同序）。"""
    p = compute_preview(
        [ParsedItem(name=source_name, amount=Decimal(str(amount)))],
        get_group_existing(db, PREFIX), PREFIX)
    return annotate_identity_guesses(db, p, accounts)


def _kw(tmp_path):
    return dict(source_group=GROUP, image_path=str(_make_image(tmp_path)),
                evidence_root=str(tmp_path / "evidence"))


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar()


# ---------------------------------------------------------------- 1. 提示（preview）


def test_guess_populated_with_same_account_candidate(db, tmp_path):
    """新名 + 同账户 + 同机构前缀 → 打 identity_guess（含候选 id/金额/最后日期）。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)

    p = _annotated_preview(db, env.accounts)
    row = p.rows[0]
    assert row.action == "create"                 # 名字未命中 → 会新建（条件 a）
    guess = row.identity_guess
    assert guess["reason"] == "same_account_new_name"
    cand = guess["candidates"][0]
    # 后缀 'after-str' 无字面包含（'黄金账户' 不含 '贵金属-黄金'）→ 由「同账户最大持仓」兜底命中
    assert cand["item_id"] == old.asset_item_id
    assert cand["name"] == OLD
    assert cand["amount"] == "1200000.00"
    assert cand["last_date"] == D_PRE.isoformat()
    # 提示绝不自动建链、也不改行动作
    assert _count(db, LineageLink) == 0
    assert row.asset_name == NEW


def test_guess_suffix_containment_rule(db, tmp_path):
    """后缀互相包含（任一方向）也算同源候选。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)

    guess = _identity_candidates(db, PREFIX + "贵金属-黄金ETF", env.accounts)
    assert [c["item_id"] for c in guess["candidates"]] == [old.asset_item_id]

    guess2 = _identity_candidates(db, PREFIX + "黄金", env.accounts)
    assert [c["item_id"] for c in guess2["candidates"]] == [old.asset_item_id]


def test_guess_null_without_candidate(db, tmp_path):
    """无同账户同机构候选（或来源组未挂账户/名字已解析）→ identity_guess 为空。"""
    env = _env(db)
    _hold(db, env, "示例信托-集合计划", "50000", D_PRE)   # 不同机构前缀 → 不是候选

    p = _annotated_preview(db, env.accounts)
    assert p.rows[0].action == "create"
    assert p.rows[0].identity_guess is None
    assert _identity_candidates(db, NEW, []) is None                 # 无账户维度
    assert _identity_candidates(db, NEW, None) is None
    assert _identity_candidates(db, OLD, env.accounts) is None           # 名字已解析 → 不新建


def test_guess_ignores_archived_candidates(db, tmp_path):
    """归档项不再作为候选（身份已被人裁决终止）。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    asset_service.set_item_status(db, old.asset_item_id, "archived", effective_from=D_PRE,
                                  reason="normalization")

    assert _identity_candidates(db, NEW, env.accounts) is None


def test_preview_row_schema_carries_guess():
    """契约字段 additive：ScreenshotPreviewRow.identity_guess 逐字段可序列化。"""
    row = ScreenshotPreviewRow(
        index=3, action="create", source_amount=Decimal("100"), diff=Decimal("100"),
        identity_guess={"reason": "same_account_new_name", "candidates": [
            {"item_id": 44, "name": OLD, "amount": "1200000.00",
             "last_date": "2026-08-31"}]})
    payload = row.model_dump(mode="json")
    assert payload["identity_guess"] == {
        "reason": "same_account_new_name",
        "candidates": [{"item_id": 44, "name": OLD, "amount": "1200000.00",
                        "last_date": "2026-08-31"}]}
    assert ScreenshotPreviewRow(
        index=0, action="create", source_amount=Decimal("1"),
        diff=Decimal("1")).identity_guess is None


# ---------------------------------------------------------------- 2. 未表态 ⇒ 阻断


def test_apply_without_decision_is_400_and_writes_nothing(db, tmp_path):
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    p = _annotated_preview(db, env.accounts)
    assert p.rows[0].identity_guess is not None

    assets_before, items_before = _count(db, Asset), _count(db, AssetItem)
    with pytest.raises(ApiError) as ei:
        apply_preview(db, p, data_date=D_MERGE, **_kw(tmp_path))
    err = ei.value
    assert err.status_code == 400
    assert err.code == ErrorCode.IDENTITY_DECISION_REQUIRED
    assert err.code == "identity_decision_required"
    assert err.details["rows"] == [{
        "key": 0, "name": NEW,
        "candidates": [{"item_id": old.asset_item_id, "name": OLD,
                        "amount": "1200000.00", "last_date": D_PRE.isoformat()}]}]
    # 零写入：连凭证都没建（校验发生在凭证/行写入之前）
    assert _count(db, Asset) == assets_before
    assert _count(db, AssetItem) == items_before
    assert db.execute(select(Asset).where(Asset.asset_date == D_MERGE)).first() is None
    assert _count(db, AssetItemName) == 0
    assert db.get(AssetItem, old.asset_item_id).status != "archived"


def test_apply_decision_keyed_by_row_index_after_skip(db, tmp_path):
    """skipped 删行后行号前移：裁决键仍取预览行号（r.index），不会串行。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    p = compute_preview(
        [ParsedItem(name="无关新项A", amount=Decimal("1")),
         ParsedItem(name="黄金账户", amount=NEW_AMOUNT)],
        get_group_existing(db, PREFIX), PREFIX)
    # 行 0 无候选（不同机构前... 同机构但它是最大持仓候选）→ 需表态；此处两行都要表态
    p = annotate_identity_guesses(db, p, env.accounts)
    p = screenshot_service.apply_user_edits(p, skipped=[0])   # 撤销行 0
    # 行 1（create+guess）前移，但 r.index 仍是预览行号 1；行 2 是 keep（无 guess）
    assert [r.index for r in p.rows] == [1, 2]
    with pytest.raises(ApiError) as ei:
        apply_preview(db, p, data_date=D_MERGE, **_kw(tmp_path))
    assert [r["key"] for r in ei.value.details["rows"]] == [1]

    res = apply_preview(db, p, data_date=D_MERGE,
                        identity_decisions={"1": {"action": "merge",
                                                 "target_item_id": old.asset_item_id,
                                                 "reason": "示例改名"}},
                        **_kw(tmp_path))
    assert res.rows_written == 1
    assert db.get(AssetItem, old.asset_item_id).status == "archived"


# ---------------------------------------------------------------- 3. merge 采纳


def test_apply_merge_successor_first_measure_no_double_count(db, tmp_path):
    """merge：后继首测 = 行金额；旧项自生效日归档；当日无双算、此前无塌陷。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    p = _annotated_preview(db, env.accounts)

    res = apply_preview(
        db, p, data_date=D_MERGE,
        identity_decisions={"0": {"action": "merge",
                                  "target_item_id": old.asset_item_id,
                                  "link_type": "revision",
                                  "principal": "1200000.00",
                                  "effective_date": D_MERGE.isoformat(),
                                  "reason": "示例改名"}},
        **_kw(tmp_path))
    assert res.rows_written == 1

    db.expire_all()
    old_item = db.get(AssetItem, old.asset_item_id)
    assert old_item.status == "archived"
    assert old_item.archived_effective_from == D_MERGE   # 生效日 = 人声明日，不是 today

    new_row = db.execute(select(Asset).where(Asset.asset_name == NEW,
                                             Asset.asset_date == D_MERGE)).scalar_one()
    assert new_row.amount == NEW_AMOUNT
    assert new_row.asset_item_id != old.asset_item_id
    # 行值即后继首条测量（仅此一条）
    assert db.execute(
        select(func.count()).select_from(Asset)
        .where(Asset.asset_item_id == new_row.asset_item_id)).scalar() == 1
    # 血缘链：旧 → 新，link_type 取自裁决
    link = db.execute(select(LineageLink)).scalars().one()
    assert (link.from_item_id, link.to_item_id, link.link_type) == \
        (old.asset_item_id, new_row.asset_item_id, "revision")
    # 口径承接（origin='lineage'，生效日同一天）由 register_lineage 既有能力承载

    # as-of：生效日前保留旧项真实历史（不塌陷）、生效日起只计后继（无双算）
    trend = {pt.period: pt.total_amount
             for pt in analysis_service.get_total_trend(db, "day")}
    assert trend[D_PRE.isoformat()] == OLD_AMOUNT
    assert trend[D_MERGE.isoformat()] == NEW_AMOUNT            # 旧口径：2380000.00（双算）
    # 裁决留痕：一条 identity_decide（before=提示 / after=实际执行）
    audit = db.execute(select(AuditLog)
                       .where(AuditLog.action == "identity_decide")).scalars().all()
    assert len(audit) == 1
    assert audit[0].entity == "screenshot_preview_row"
    assert audit[0].row_id == 0
    assert "贵金属-黄金" in (audit[0].before_json or "")
    assert '"revision"' in (audit[0].after_json or "")
    # apply_row 仍逐行留痕（既有行为不退化）
    assert db.execute(select(AuditLog)
                      .where(AuditLog.action == "apply_row")).scalars().all()


# ---------------------------------------------------------------- 4. create 采纳


def test_apply_create_decision_keeps_legacy_behaviour(db, tmp_path):
    """create：与历史行为一致（新项照建、旧项不动、不建链），仅多一条裁决审计。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    p = _annotated_preview(db, env.accounts)

    res = apply_preview(db, p, data_date=D_MERGE,
                        identity_decisions={"0": {"action": "create"}},
                        **_kw(tmp_path))
    assert res.rows_written == 1
    db.expire_all()
    assert _count(db, LineageLink) == 0
    assert db.get(AssetItem, old.asset_item_id).status != "archived"
    assert db.get(AssetItem, old.asset_item_id).archived_effective_from is None
    new_row = db.execute(select(Asset).where(Asset.asset_name == NEW,
                                             Asset.asset_date == D_MERGE)).scalar_one()
    assert new_row.amount == NEW_AMOUNT
    assert new_row.source_evidence_id == res.evidence_id
    # 旧口径沿用 + 新项 = 双算（正是本票要人表态规避的情形；选 create 即明确接受）
    trend = {pt.period: pt.total_amount
             for pt in analysis_service.get_total_trend(db, "day")}
    assert trend[D_MERGE.isoformat()] == OLD_AMOUNT + NEW_AMOUNT
    audit = db.execute(select(AuditLog)
                       .where(AuditLog.action == "identity_decide")).scalars().all()
    assert len(audit) == 1 and '"create"' in (audit[0].after_json or "")


def test_apply_without_guess_still_needs_decision(db, tmp_path):
    """D4：即使没有 identity_guess，create 行不表态也 400 且零写入。

    旧契约「没有 identity_guess 就不需要表态」已被 D4 推翻——「不表态就默认新建」
    正是本次要修的静默新建通道；同一个无候选场景现在同样必须先请人表态。
    """
    env = _env(db)
    _hold(db, env, "示例信托-集合计划", "50000", D_PRE)
    p = _annotated_preview(db, env.accounts)
    assert p.rows[0].action == "create" and p.rows[0].identity_guess is None

    assets_before, items_before = _count(db, Asset), _count(db, AssetItem)
    with pytest.raises(ApiError) as ei:
        apply_preview(db, p, data_date=D_MERGE, **_kw(tmp_path))
    err = ei.value
    assert err.status_code == 400
    assert err.code == ErrorCode.IDENTITY_DECISION_REQUIRED
    assert err.details["rows"] == [{"key": 0, "name": NEW, "candidates": []}]
    # 零写入：连凭证都没建（校验发生在凭证/行写入之前）
    assert _count(db, Asset) == assets_before
    assert _count(db, AssetItem) == items_before
    assert db.execute(select(Asset).where(Asset.asset_date == D_MERGE)).first() is None
    assert db.execute(select(AuditLog)
                      .where(AuditLog.action == "identity_decide")).first() is None


def test_merge_decision_validation_errors(db, tmp_path):
    """非法裁决在**任何写入之前**被拒（零写入）。"""
    env = _env(db)
    old = _hold(db, env, OLD, OLD_AMOUNT, D_PRE)
    p = _annotated_preview(db, env.accounts)
    assets_before = _count(db, Asset)

    cases = [
        ({"0": {"action": "rename"}}, ErrorCode.BAD_REQUEST),
        ({"0": {"action": "merge", "reason": "r"}}, ErrorCode.BAD_REQUEST),   # 缺 target
        ({"0": {"action": "merge", "target_item_id": 999999,
                "reason": "r"}}, ErrorCode.NOT_FOUND),
        ({"0": {"action": "merge", "target_item_id": old.asset_item_id}},
         ErrorCode.PRINCIPAL_REASON_REQUIRED),                               # 缺理由（R-9）
        ({"0": {"action": "merge", "target_item_id": old.asset_item_id,
                "reason": "r", "link_type": "split"}}, ErrorCode.BAD_REQUEST),  # split 缺 ratio
        ({"0": {"action": "merge", "target_item_id": old.asset_item_id,
                "reason": "r", "effective_date": "2026/09/16"}}, ErrorCode.BAD_REQUEST),
    ]
    for decisions, code in cases:
        with pytest.raises(ApiError) as ei:
            apply_preview(db, p, data_date=D_MERGE,
                          identity_decisions=decisions, **_kw(tmp_path))
        assert ei.value.code == code, decisions
    assert _count(db, Asset) == assets_before
    assert db.get(AssetItem, old.asset_item_id).status != "archived"