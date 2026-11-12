"""解析侧接线验收（P1 收口）：教学规则表 + 解析词表 → 解析链的端到端生效证明。

修的是两条断链（0031 拆表与 P1a 词表引擎各自落库后，解析侧都没接上）：

1. **教学规则**：`rename` / `ignore` / `exclude` 搬进 `source_group_learned_rules` 表后，
   解析链只读 `source_groups.rules` 列 → 用户在界面加的规则静默失效（回归）。
   现在由 `source_group_rules_service.rules_for_parsing`（列 + 表合并）供数。
2. **词表**：`parser_rules.load_vocabularies` 算出的生效词表从来没有调用点传下去 →
   词表页改动对解析零影响。现在管线每请求读一次并透传 `parse_screenshot`。

**验收口径**：断言一律落在**管线产物**（`_run_pipeline` 的 parsed / preview）上，
不是「函数能被调用」。每条用例都让真实的 `parse_screenshot` →
`organize_blocks` → `apply_correction_rules` → `compute_preview` 跑完，只把两跳换成
替身：OCR（约 4 分钟 CPU）→ 合成几何 blocks；磁盘缓存 → no-op（不往仓库写文件）。

数据全部合成：来源组 `demo-group` /「示例机构」，资产「示例基金A–C」/「示例基金老账户」/
「示例黄金ETF」，金额一万起。与任何真实机构、账户、金额、路径无关。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

import routers.screenshots as shots
from errors import ApiError, ErrorCode
from models import Asset, ParserVocabulary, ScreenshotEvidence
from routers.source_groups import create_source_group
from services import parser_rules_service, screenshot_service, source_group_rules_service
from services.source_group_rules_service import add_learned_rule

PREFIX = "示例-"
GROUP = "demo-group"
LABEL = "示例机构"
EXCLUDED = PREFIX + "示例基金老账户"
BASE_DATE = date(2026, 1, 1)


def blk(y, x, text_, score=0.9):
    """块格式 (abs_y, abs_x, text, score)，已按 y,x 排序（与 _ocr_blocks 输出一致）。"""
    return (y, x, text_, score)


# A 横排几何：左名右金 + 「>」闭合（自动判定即 horizontal，三行都能解析出来）
HORIZONTAL_BLOCKS = [
    blk(100, 20, "示例基金A"), blk(100, 500, "10,000.00"), blk(100, 900, ">"),
    blk(150, 20, "示例基金B"), blk(150, 500, "20,000.00"), blk(150, 900, ">"),
    blk(200, 20, "示例基金C"), blk(200, 500, "30,000.00"), blk(200, 900, ">"),
]

# B 垂直几何：名称 → 「金额」标签 → 数值下行（自动判定即 vertical，两行）
VERTICAL_BLOCKS = [
    blk(100, 20, "示例基金A"), blk(160, 20, "金额"), blk(220, 20, "10,000.00"),
    blk(300, 20, "示例基金B"), blk(360, 20, "金额"), blk(420, 20, "20,000.00"),
]


# ---------------------------------------------------------------- 沙箱助手
@pytest.fixture(autouse=True)
def _no_disk_cache():
    """清空 L1 块缓存 + 磁盘缓存 no-op（用例之间零串味，仓库不留 data/ocr_cache 文件）。

    同一用例内复用同一 sha 时仍会**命中** L1 缓存，从而走「按当前规则/词表重跑
    组织层」这条路径——那正是要验证的接线（规则改了，同图重算即生效；见 exclude 用例）。
    """
    screenshot_service._PARSE_CACHE.clear()
    original = (screenshot_service._write_disk_cache, screenshot_service._trim_disk_cache)
    screenshot_service._write_disk_cache = lambda *a, **k: None
    screenshot_service._trim_disk_cache = lambda *a, **k: None
    try:
        yield
    finally:
        screenshot_service._write_disk_cache, screenshot_service._trim_disk_cache = original
        screenshot_service._PARSE_CACHE.clear()


def make_group(db, slug=GROUP, label=LABEL, rules=None):
    return create_source_group({
        "slug": slug, "label": label, "asset_prefix": PREFIX, "rules": rules,
    }, db)


def seed_assets(db, items):
    """直插组内现有资产（get_group_existing 的读法：superseded_by IS NULL + 最新日期）。

    入参是**不带组前缀**的资产名，落库时拼 `PREFIX`——`compute_preview` 只认
    `name.startswith(group_prefix)` 的现有项，不拼前缀就整组读不到（等于没造数据）。
    """
    for name, amount in items:
        db.add(Asset(asset_name=PREFIX + name, amount=Decimal(amount),
                     asset_date=BASE_DATE))
    db.flush()


def run_pipeline(monkeypatch, db, blocks, slug=GROUP, sha="sha-wiring-0001"):
    """跑真实解析管线：OCR 换成合成 blocks，其余（含缓存重算）全真。"""
    monkeypatch.setattr(
        screenshot_service, "_ocr_blocks",
        lambda image_path, progress_cb=None: list(blocks))
    return asyncio.run(shots._run_pipeline("demo-shot.png", sha, slug, db))


def keep_names(preview):
    return [r.asset_name for r in preview.rows if r.action == "keep"]


def row_pairs(preview):
    return [(r.action, r.source_name, r.asset_name) for r in preview.rows]


def count_of(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar() or 0


# ---------------------------------------------------------------- ① 教学规则 exclude
def test_exclude_rule_lands_in_table_and_filters_preview(db, monkeypatch):
    """界面加的 exclude（落表）必须让「不录入」资产从 keep 列表消失。

    对照组：加规则之前该资产**在** keep 里——否则「消失」可能只是它本来就没被读出来。
    """
    make_group(db)
    seed_assets(db, [("示例基金A", "10000"), ("示例基金B", "20000"),
                     ("示例基金C", "30000"), ("示例基金老账户", "500")])

    _parsed, _existing, before, _prefix = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)
    assert keep_names(before) == [EXCLUDED]          # 对照组：未加规则 → keep 行在

    add_learned_rule(db, GROUP, "exclude", EXCLUDED)   # 走教学规则的正式写路径（落表）
    assert source_group_rules_service.correction_rules_of(
        db, source_group_rules_service.get_group(db, GROUP).id)["exclude"] == [EXCLUDED]

    parsed, _existing, after, _prefix = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)
    # exclude 只过滤 keep 行，不碰解析产物（红线：被排除项再次出现在截图里仍走 update）
    assert [i.name for i in parsed.items] == ["示例基金A", "示例基金B", "示例基金C"]
    assert keep_names(after) == []
    assert [p for p in row_pairs(after) if p[0] == "update"] == [
        ("update", "示例基金A", PREFIX + "示例基金A"),
        ("update", "示例基金B", PREFIX + "示例基金B"),
        ("update", "示例基金C", PREFIX + "示例基金C"),
    ]


# ---------------------------------------------------------------- ② 教学规则 rename
def test_rename_rule_lands_in_table_and_rewrites_parsed_name(db, monkeypatch):
    """表里的 rename 必须改写解析产物名称（并因此改变 preview 行的映射结果）。"""
    make_group(db)
    seed_assets(db, [("示例基金A", "10000"), ("示例基金C", "30000")])
    add_learned_rule(db, GROUP, "rename", "示例基金C", "示例黄金ETF")

    parsed, _existing, preview, _prefix = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)

    assert [i.name for i in parsed.items] == ["示例基金A", "示例基金B", "示例黄金ETF"]
    assert "示例基金C" not in [i.name for i in parsed.items]
    creates = [(r.source_name, r.asset_name) for r in preview.rows if r.action == "create"]
    assert ("示例黄金ETF", PREFIX + "示例黄金ETF") in creates
    assert ("示例基金C", PREFIX + "示例基金C") not in creates
    assert "示例基金C" not in [r.source_name for r in preview.rows]


# ---------------------------------------------------------------- ③ 词表 ignore_words
def test_vocabulary_ignore_words_drops_blocks_until_reset(db, monkeypatch):
    """全局词表（parser_vocabularies）改动必须影响解析：命中块被丢弃，reset 后恢复。"""
    make_group(db)
    seed_assets(db, [("示例基金A", "10000"), ("示例基金B", "20000")])
    db.add(ParserVocabulary(key="ignore_words",
                            add_items=json.dumps(["示例基金B"], ensure_ascii=False),
                            remove_items="[]"))
    db.commit()

    parsed, _existing, preview, _prefix = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)
    assert [i.name for i in parsed.items] == ["示例基金A", "示例基金C"]   # B 的块被丢弃
    assert PREFIX + "示例基金B" in keep_names(preview)                    # 现有项退化成 keep

    # reset（= `POST /api/parser/vocabularies/{key}/reset` 的服务层实现）→ 回退出厂词表
    assert parser_rules_service.reset_vocabulary(
        db, "ignore_words", "示例：回退出厂词表")["changed"] is True

    parsed2, _existing2, preview2, _prefix2 = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)
    assert [i.name for i in parsed2.items] == ["示例基金A", "示例基金B", "示例基金C"]
    assert PREFIX + "示例基金B" not in keep_names(preview2)
    assert ("update", "示例基金B", PREFIX + "示例基金B") in row_pairs(preview2)


# ---------------------------------------------------------------- ④ 列 + 表同时生效
def test_column_layout_and_table_exclude_both_take_effect(db, monkeypatch):
    """`layout` 在 rules 列、`exclude` 在教学规则表——一次解析里两者必须同时生效。

    （证明合并不是二选一：若只读列，exclude 失效；若只读表，layout 失效。）
    """
    make_group(db, rules=json.dumps({"layout": "horizontal"}, ensure_ascii=False))
    seed_assets(db, [("示例基金老账户", "500")])

    # 对照组：同一批垂直几何 blocks 走自动判定会解析出 2 行
    assert [i.name for i in screenshot_service.organize_blocks(VERTICAL_BLOCKS)] == \
        ["示例基金A", "示例基金B"]

    _parsed, _existing, before, _prefix = run_pipeline(
        monkeypatch, db, VERTICAL_BLOCKS, sha="sha-wiring-0004a")
    assert _parsed.items == []                      # 列里的 layout 生效：垂直几何被强制横排
    assert keep_names(before) == [EXCLUDED]         # （此时 exclude 尚未加：keep 行在）

    add_learned_rule(db, GROUP, "exclude", EXCLUDED)
    merged = source_group_rules_service.rules_for_parsing(
        db, source_group_rules_service.get_group(db, GROUP))
    assert merged["layout"] == "horizontal"        # 来自 rules 列
    assert merged["exclude"] == [EXCLUDED]         # 来自教学规则表

    parsed, _existing, after, _prefix = run_pipeline(
        monkeypatch, db, VERTICAL_BLOCKS, sha="sha-wiring-0004a")
    assert parsed.items == []                       # layout 仍生效
    assert keep_names(after) == []                  # 表里的 exclude 同时生效


# ---------------------------------------------------------------- ⑤ 预览路径零写入
def test_preview_pipeline_writes_no_evidence_and_no_assets(db, monkeypatch):
    """预览（`_run_pipeline`）只读：不产生 screenshot_evidence / assets 行。"""
    make_group(db)
    seed_assets(db, [("示例基金A", "10000")])
    add_learned_rule(db, GROUP, "rename", "示例基金B", "示例黄金ETF")  # 规则在测量窗口外先落库

    before = (count_of(db, ScreenshotEvidence), count_of(db, Asset))
    parsed, _existing, preview, _prefix = run_pipeline(monkeypatch, db, HORIZONTAL_BLOCKS)
    after = (count_of(db, ScreenshotEvidence), count_of(db, Asset))

    assert [i.name for i in parsed.items] == ["示例基金A", "示例黄金ETF", "示例基金C"]
    assert preview.rows                              # 确实产出清单（不是空跑）
    assert after == before == (0, 1)


# ---------------------------------------------------------------- ⑥ 坏 JSON：与既有路径同口径
def test_bad_json_rules_fail_loudly_and_same_as_get_group_config(db, monkeypatch):
    """坏 JSON 不静默吞：新出口与 `get_group_config` 抛**同一个** 422（不是两种行为）。"""
    assert source_group_rules_service.rules_for_parsing(db, None) == {}   # 无组行 → 无规则

    make_group(db)
    sg = source_group_rules_service.get_group(db, GROUP)
    db.execute(text("UPDATE source_groups SET rules = :raw WHERE id = :id"),
               {"raw": "{示例坏JSON", "id": sg.id})
    db.commit()
    db.expire_all()
    sg = source_group_rules_service.get_group(db, GROUP)

    with pytest.raises(ApiError) as via_parsing:
        source_group_rules_service.rules_for_parsing(db, sg)
    with pytest.raises(ApiError) as via_config:
        screenshot_service.get_group_config(db, GROUP)

    expected = (422, ErrorCode.SOURCE_GROUP_RULES_INVALID)
    assert (via_parsing.value.status_code, via_parsing.value.code) == expected
    assert (via_config.value.status_code, via_config.value.code) == expected

    # 管线不吞：仍是 422 业务错误（而不是 500 / 静默按无规则解析）
    monkeypatch.setattr(
        screenshot_service, "_ocr_blocks",
        lambda image_path, progress_cb=None: list(HORIZONTAL_BLOCKS))
    with pytest.raises(ApiError) as via_pipeline:
        asyncio.run(shots._run_pipeline("demo-shot.png", "sha-wiring-0006", GROUP, db))
    assert (via_pipeline.value.status_code, via_pipeline.value.code) == expected