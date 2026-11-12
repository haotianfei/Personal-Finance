# P1 跨行「表头→值」列绑定验收（parser-amount-association-design §5.2 断言 1/2/3/6）
#
# fixture 来源：backend/tests/fixtures/holdings_card_blocks.json —— 合成演示数据：
# 虚构券商「示例证券」持仓卡片页的 OCR 块缓存（schema=3，59 块），机构/标的/代码/
# 账号/金额均为人工构造值，与任何真实账户无关。
# 精简内联子集（1 张代表卡 + 1 张退化卡 + 页眉 + 状态栏）沿用 test_screenshot_mapping
# 的 blk() 惯例，供可读性单测；全量合成块供 e2e 断言。
import json
import os
from collections import OrderedDict
from decimal import Decimal

import pytest

from services import screenshot_service
from services.screenshot_service import (
    ParsedItem,
    _HEADER_BIND_TOL_X,
    organize_blocks,
)

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "holdings_card_blocks.json")

# P0 配置（来源组 demo-stock 分析规则）：layout=vertical + 全词「持仓市值」。
P0_RULES = {"layout": "vertical", "amount_header": ["持仓市值"]}

# design §0/§5.2 断言 1 实测基准：4 卡中 3 卡正确；card4（示例价值ETF）为
# OCR 层值行缺失（表头双份 + 其间零块），organize 层契约 = 静默丢行、宁缺勿错。
SYN_EXPECTED_3 = [
    ParsedItem(name="示例红利ETF", amount=Decimal("7960.00")),
    ParsedItem(name="示例半导体ETF", amount=Decimal("5200.00")),
    ParsedItem(name="示例银行ETF", amount=Decimal("4321.00")),
]

# 名称行同组的证券代码（错位 bug 中被误当金额的错误值，绝不允许出现在输出）。
SECURITY_CODES = {Decimal(c) for c in
                  ("000000", "111111", "222222", "333333")}


def load_holdings_blocks():
    """全量合成块 fixture → [(y, x, text, score)]（与 _ocr_blocks 输出同构）。"""
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert data["schema"] == 3
    return [(int(y), int(x), t, float(s)) for y, x, t, s in data["blocks"]]


def blk(y, x, text, score=0.9):
    return (y, x, text, score)


# ---------------- 断言 1：全量合成块 + P0 配置 → 3 项正确 ----------------


def test_holdings_full_p0_vertical_three_items():
    items = organize_blocks(load_holdings_blocks(), P0_RULES)
    assert items == SYN_EXPECTED_3
    # 零代码错位：任何金额都不是证券代码
    assert not ({it.amount for it in items} & SECURITY_CODES)
    # OCR 层缺值行（card4）静默丢行，绝不输出错值/占位
    assert "示例价值ETF" not in {it.name for it in items}


# ---------------- 断言 2：短词「市值」陷阱（文档性测试，P1 后更新期望） ----------------


def test_holdings_short_header_traps_total_header_row():
    """amount_header:["市值"] 配置陷阱。

    P1 之前实测现状：短词命中页眉「总市值(人民币)」→ 产出垃圾项
    ('我的持仓', 50000.00)（design §1 配置陷阱 / §5.2 断言 2）。
    P1 落地了评审提醒的防御：锚点候选 cell 以「总」开头或命中
    _is_total_label → 不作锚点且整行跳过（_vertical_anchor.only_total_anchor），
    垃圾项消失，输出收敛到与全词「持仓市值」一致的 3 项。期望按防御实现更新，
    并保留本测试固化「短词不再有垃圾」这一行为。
    """
    items = organize_blocks(load_holdings_blocks(),
                            {"layout": "vertical", "amount_header": ["市值"]})
    assert items == SYN_EXPECTED_3
    assert ("我的持仓", Decimal("50000.00")) not in [
        (it.name, it.amount) for it in items]


# ---------------- 内联精简子集（1 张代表卡 + 1 张退化卡 + 页眉 + 状态栏） ----------------


def holdings_subset_blocks():
    return [
        # 状态栏行（20:30/5G → _drop_noise_rows 整行剥离）
        blk(44, 389, "20:30"), blk(45, 640, "5G"),
        # 页眉行组：我的持仓 / 脱敏账号 / 总市值(人民币)+持仓盈亏 / 50,000.00元
        blk(142, 474, "我的持仓"),
        blk(207, 492, "888****888"),
        blk(328, 932, "持仓盈亏"), blk(329, 69, "总市值(人民币)"),
        blk(395, 753, "+2,345.67"), blk(398, 71, "50,000.00元"),
        # 正常卡（card1）：名称+代码+右缘'9' / 三列表头 / 三值 / 持仓数+现价+可用数+成本
        blk(657, 1021, "9"),
        blk(668, 47, "示例红利ETF"), blk(673, 497, "000000"),
        blk(766, 417, "盈亏"), blk(768, 44, "持仓市值"), blk(768, 795, "盈亏比"),
        blk(836, 419, "+410.00"), blk(836, 795, "5.15%"), blk(837, 44, "7,960.00"),
        blk(958, 42, "持仓数 7700"), blk(959, 593, "现价 1.0300"),
        blk(1029, 42, "可用数 7700"), blk(1033, 596, "成本 0.9800"),
        # 退化卡（card4）：双份表头块 + y(2335,2530) 间零块（值行未被 OCR 捕获）
        blk(2233, 1019, "9"),
        blk(2239, 46, "示例价值ETF"), blk(2243, 480, "333333"),
        blk(2335, 415, "盈亏"), blk(2336, 416, "盈亏"),
        blk(2337, 793, "盈亏比"), blk(2338, 794, "盈亏比"),
        blk(2338, 44, "持仓市值"), blk(2339, 44, "持仓市值"),
        blk(2530, 43, "持仓数 1300"), blk(2530, 594, "现价 2.6800"),
    ]


def test_holdings_subset_vertical():
    items = organize_blocks(holdings_subset_blocks(), P0_RULES)
    assert items == [ParsedItem(name="示例红利ETF", amount=Decimal("7960.00"))]
    # 短词陷阱防御在子集上同样成立（页眉行整行跳过）
    items2 = organize_blocks(holdings_subset_blocks(),
                             {"layout": "vertical", "amount_header": ["市值"]})
    assert items2 == items
    assert "我的持仓" not in {it.name for it in items2}


# ---------------- 断言 3：列绑定行为（合成块） ----------------


def test_vertical_binding_picks_aligned_column_not_x_first():
    """列序翻转：盈亏列在左 x=44、市值列在右 x=420 → 取市值列值。

    现行 x 序首个金额逻辑会错取 +24.90（盈亏列）；P1 按锚点 x 容差绑定取 4321.00。
    """
    blocks = [
        blk(100, 40, "示例银行ETF"), blk(100, 480, "222222"),
        blk(200, 44, "盈亏"), blk(200, 420, "持仓市值"),
        blk(300, 44, "+24.90"), blk(300, 420, "4321.00"),
    ]
    items = organize_blocks(blocks, P0_RULES)
    assert items == [ParsedItem(name="示例银行ETF", amount=Decimal("4321.00"))]


def test_vertical_binding_merged_header_value_cell_same_row():
    """合并 cell '持仓市值4321.00'：表头与值同 cell → 同行锚点剥词取值命中。"""
    blocks = [
        blk(100, 40, "示例半导体ETF"), blk(100, 497, "111111"),
        blk(200, 44, "持仓市值4321.00"),
    ]
    items = organize_blocks(blocks, P0_RULES)
    assert items == [ParsedItem(name="示例半导体ETF", amount=Decimal("4321.00"))]


def test_vertical_binding_duplicated_header_blocks_single_item():
    """双份表头块（card4 OCR 实测形态）：取首个命中 cell → 输出单项不重复。"""
    blocks = [
        blk(100, 40, "示例价值ETF"), blk(100, 495, "333333"),
        blk(200, 44, "持仓市值"), blk(200, 415, "盈亏"),
        blk(201, 44, "持仓市值"), blk(201, 416, "盈亏"),
        blk(300, 44, "3456.78"), blk(300, 415, "+12.30"),
    ]
    items = organize_blocks(blocks, P0_RULES)
    assert items == [ParsedItem(name="示例价值ETF", amount=Decimal("3456.78"))]


def test_vertical_binding_failure_falls_back_to_x_first():
    """值 x 偏离锚点超容差（Δ=157 > 40）→ 绑定失败，回落 == 现行 x 序首金额。

    x=201 的 9,999.99 距锚点（x=44）超出 _HEADER_BIND_TOL_X，只可能由回落
    分支产出——输出与 P1 之前的现行逻辑逐字节一致。
    """
    assert _HEADER_BIND_TOL_X == 40
    blocks = [
        blk(100, 40, "示例红利ETF"),
        blk(200, 44, "持仓市值"), blk(200, 420, "盈亏"),
        blk(300, 201, "9,999.99"), blk(300, 420, "-70494.38"),
    ]
    items = organize_blocks(blocks, P0_RULES)
    assert items == [ParsedItem(name="示例红利ETF", amount=Decimal("9999.99"))]


# ---------------- 断言 6：e2e 缓存命中路径（规则变更追溯生效） ----------------


def test_parse_screenshot_rules_take_effect_on_cache_hit(monkeypatch, tmp_path):
    """L1 缓存命中 + monkeypatch _ocr_blocks 抛错 → parse_screenshot 仅按当前
    rules 重跑 organize，产出 3 项正确（验证「schema=3 块缓存 × P0 配置」承诺）。
    """
    blocks = load_holdings_blocks()
    img = tmp_path / "holdings_card.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def boom(*args, **kwargs):
        raise AssertionError("缓存命中路径不得触发 OCR")

    monkeypatch.setattr(screenshot_service, "_ocr_blocks", boom)
    monkeypatch.setattr(screenshot_service, "_PARSE_CACHE", OrderedDict())
    cache_key = "test-holdings-cache-hit"
    screenshot_service._PARSE_CACHE[cache_key] = screenshot_service._OcrPayload(
        blocks=list(blocks), date=None, total=None)

    parsed = screenshot_service.parse_screenshot(
        str(img), cache_key=cache_key, rules=dict(P0_RULES))
    assert parsed.items == SYN_EXPECTED_3
    assert not ({it.amount for it in parsed.items} & SECURITY_CODES)
    assert "示例价值ETF" not in {it.name for it in parsed.items}
