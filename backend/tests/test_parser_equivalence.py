"""解析引擎重构的**等价对照**（spec D7 硬验收，`.scratch/parser-rules-refactor/spec.md`）。

口径：`organize_blocks` 从「直接读模块级出厂常量」改为「消费 `EffectiveVocabularies`
生效词表」后，**同一批 blocks 的输出必须与重构前逐项相同**（顺序 + name + amount）。

对照片的取法：`git show <固定提交>:backend/services/screenshot_service.py` ——
**基准必须是钉死的提交，不能用 `HEAD`**：HEAD 会随本特性自身的提交而前进，届时
「旧实现」就变成了新实现，本文件的对照会静默退化成「自己跟自己比」而仍然全绿。
所选 `bfaeff2` 是 P1 落地前的最后一次提交，该版本自带 7 组出厂常量、不依赖
`services/parser_rules.py`，可直接整文件加载为独立模块当基准实现（不需要 `ast` 抠常量）。

加载要点：
- 以**唯一模块名** `_legacy_screenshot_service_head` 注册进 `sys.modules`——
  `services.screenshot_service` 本体不受影响（`dataclass` 需要 `cls.__module__` 能在
  `sys.modules` 里查到，否则 `@dataclass` 直接抛 AttributeError）。
- 旧模块里 `ParsedItem` 是**另一个类**（不同模块），dataclass 的 `__eq__` 要求同类，
  故对照一律比 `[(name, amount), ...]` 归一形态，而不是对象相等。

本文件的数据全部为合成演示值（虚构产品名/金额），与任何真实账户无关。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
from decimal import Decimal

import pytest

from services.parser_rules import EffectiveVocabularies
from services.screenshot_service import organize_blocks

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"

_LEGACY_MODULE_NAME = "_legacy_screenshot_service_head"
#: 对照基准提交（P1 落地前）。**不要改成 HEAD**——见模块 docstring 的理由。
_LEGACY_REF = "bfaeff2"
_LEGACY = None


def _load_legacy():
    """基准提交版 `screenshot_service.py` → 独立模块（模块级缓存，只取一次）。"""
    src = subprocess.run(
        ["git", "-c", "safe.directory=*", "show",
         f"{_LEGACY_REF}:backend/services/screenshot_service.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True).stdout
    assert src.strip(), f"git show 未取到 {_LEGACY_REF} 版 services/screenshot_service.py"

    tmp_dir = tempfile.mkdtemp(prefix="legacy_parser_")
    path = pathlib.Path(tmp_dir) / "legacy_screenshot_service.py"
    path.write_text(src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(_LEGACY_MODULE_NAME, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_LEGACY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def legacy():
    global _LEGACY
    if _LEGACY is None:
        _LEGACY = _load_legacy()
    return _LEGACY


def _rows(module, blocks, rules=None):
    """模块无关的归一形态：[(name, amount), ...]（保序）。"""
    return [(i.name, i.amount) for i in module.organize_blocks(blocks, rules)]


def _new(blocks, rules=None, vocabularies=None):
    return [(i.name, i.amount) for i in organize_blocks(blocks, rules, vocabularies)]


def blk(y, x, text, score=0.9):
    """块格式 (abs_y, abs_x, text, score)，已按 y,x 排序（与 _ocr_blocks 输出一致）。"""
    return (y, x, text, score)


def _holdings_fixture_blocks():
    """真实几何 fixture（合成数据：虚构券商持仓卡片页，59 块，schema=3）。"""
    data = json.loads((FIXTURE_DIR / "holdings_card_blocks.json").read_text(encoding="utf-8"))
    assert data["schema"] == 3
    return [tuple(b) for b in data["blocks"]]


# ---------------- fixture 形状（对齐 test_screenshot_mapping.py 的几何约定） ----------------
FIXTURES = {
    # A 横排：同 Y 基线左名右金 + 「>」闭合
    "horizontal_basic": [
        blk(100, 20, "示例银行"), blk(100, 500, "1,876,543.21"), blk(100, 900, ">"),
        blk(150, 20, "示例活期"), blk(150, 500, "12,345.67"), blk(150, 900, ">"),
        blk(200, 20, "示例定期"), blk(200, 500, "50,000.00"), blk(200, 900, ">"),
    ],
    # B 垂直层级：名称 → 「金额」标签 → 数值下行
    "vertical_basic": [
        blk(100, 20, "示例养老账户"), blk(160, 20, "金额"), blk(220, 20, "43,210.98"),
        blk(300, 20, "示例公积金"), blk(360, 20, "金额"), blk(420, 20, "8,900.00"),
    ],
    # C 三列：名称左 + 三列金额
    "multicolumn_three": [
        blk(100, 20, "示例混合基金"), blk(100, 400, "10,000.00"),
        blk(100, 700, "50.00"), blk(100, 1000, "2.50"),
        blk(150, 20, "示例零钱通"), blk(150, 400, "3,221.45"),
        blk(150, 700, "17.22"), blk(150, 1000, "1.10"),
    ],
    # 卡片列：名称行(上) + 金额行(下)
    "card_three_columns": [
        blk(100, 20, "示例医保"), blk(100, 400, "示例公积金"), blk(100, 800, "示例社保"),
        blk(150, 20, "54,321.09元"), blk(150, 400, "567,890元"), blk(150, 800, "234,560元"),
    ],
    # 卡片列 + 无汉字噪声列(SI) + 货币符号列 + 按钮词行
    "card_with_si_and_buttons": [
        blk(100, 218, "示例医保"), blk(100, 500, "¥"), blk(100, 700, "示例公积金"),
        blk(100, 1100, "SI"), blk(100, 1300, "示例社保"),
        blk(150, 135, "54,321.09元"), blk(150, 700, "567,890元"), blk(150, 1300, "234,560元"),
        blk(200, 200, "刷医保"), blk(200, 700, "提余额"), blk(200, 1300, "查明细"),
    ],
    # 汇总/对账标签（总资产/我的资产/总负债）与资产行混排
    "total_labels_mixed": [
        blk(100, 20, "我的资产"),
        blk(200, 20, "总资产（元)"), blk(200, 500, "432,198.76"),
        blk(300, 20, "示例贵金属"), blk(300, 500, "298,765.43 >"),
        blk(400, 20, "示例理财"), blk(400, 500, "15,432.10 >"),
        blk(500, 20, "总负债(元)"), blk(500, 500, "0.00"),
    ],
    # profit 区标签行（盈亏/收益/轨迹）与资产行混排
    "profit_zone": [
        blk(100, 20, "示例股票"), blk(100, 500, "112,233.44 >"),
        blk(200, 20, "盈亏分析"), blk(200, 400, "今年收益"), blk(200, 800, "6,543.21"),
        blk(230, 20, "今年收益率"), blk(230, 800, "5.43%"),
        blk(300, 20, "累计收益2026-01-15"), blk(300, 800, "345678"),
    ],
    # 状态栏噪声（时间/速率/5G/电量）
    "status_bar_noise": [
        blk(44, 393, "18:59"), blk(44, 470, "四节四复必"), blk(44, 600, "381"),
        blk(44, 700, "B/s"), blk(44, 880, "35"), blk(44, 900, "5G"),
        blk(150, 20, "示例活钱"), blk(150, 500, "23,456.78 >"),
        blk(200, 20, "示例盈"), blk(200, 500, "13,579.24 >"),
    ],
    # 行闭合符变体（> › ≫ →，含尾部空白）
    "close_token_variants": [
        blk(100, 20, "示例甲"), blk(100, 500, "4,321.09 >"),
        blk(150, 20, "示例乙"), blk(150, 500, "10,987.65 ›"),
        blk(200, 20, "示例丙"), blk(200, 500, "1,543,210.98 ≫"),
        blk(250, 20, "示例丁"), blk(250, 500, "65,432.10 →"),
    ],
    # 占比 % 子行
    "percentage_subrows": [
        blk(100, 20, "示例贵金属"), blk(100, 500, "298,765.43 >"),
        blk(130, 20, "90.68%"),
        blk(180, 20, "示例养老金"), blk(180, 500, "17,654.32 >"),
    ],
    # 重复行金额完全相同 → 不得被 multi_column 绑架
    "duplicated_row": [
        blk(100, 20, "示例活期"), blk(100, 600, "4,321.09 >"),
        blk(150, 20, "示例零钱"), blk(150, 600, "10,555.55 >"),
        blk(150, 900, "示例零钱"), blk(150, 1200, "10,555.55 >"),
        blk(200, 20, "示例特色"), blk(200, 600, "6,000.00 >"),
    ],
    # 整数金额卡片（无 .XX）
    "int_amount_card": [
        blk(100, 20, "示例医保"), blk(100, 300, "示例公积金"), blk(100, 600, "示例社保"),
        blk(150, 20, "54321.09元"), blk(150, 300, "567890元"), blk(150, 600, "234560元"),
    ],
    # 表头行 + 数值行（含 profit 词但 ≥2 金额表头 → 必须放行 vertical）
    "header_plus_values": [
        blk(90, 20, "我的资产(元)"),
        blk(150, 20, "累计收益+543.21>"), blk(150, 300, "昨日收益"), blk(150, 500, "0.00>"),
        blk(210, 20, "示例活钱"), 
        blk(270, 20, "持有金额"), blk(270, 300, "累计收益"), blk(270, 600, "昨日收益"),
        blk(330, 20, "0.06"), blk(330, 300, "+45.67"), blk(330, 600, "0.00"),
        blk(400, 20, "示例混合基金C"), blk(400, 300, "连续多年正收益"),
        blk(460, 20, "+6.78%"), blk(460, 300, "4%~6%"),
    ],
    # 空输入 / 只有名 / 只有金额
    "empty": [],
    "name_only": [blk(100, 20, "示例未知项")],
    "amount_only": [blk(100, 500, "10,000.00")],
}

FIXTURE_IDS = sorted(FIXTURES)
HOLDINGS_ID = "holdings_card_fixture"


def _all_fixtures():
    """(id, blocks) 列表：合成 fixture + 真实几何 fixture。"""
    return [(name, FIXTURES[name]) for name in FIXTURE_IDS] + [
        (HOLDINGS_ID, _holdings_fixture_blocks())]


ALL_FIXTURES = _all_fixtures()
ALL_IDS = [name for name, _ in ALL_FIXTURES]

# 组级 rules 矩阵：强制四类 layout、amount_col、amount_header（list 形态）、ignore_words。
LAYOUT_RULES = [
    {"layout": "horizontal"},
    {"layout": "vertical"},
    {"layout": "multi_column"},
    {"layout": "card"},
]
RULES_MATRIX = [
    ("no_rules", None),
    ("horizontal", {"layout": "horizontal"}),
    ("vertical", {"layout": "vertical"}),
    ("multi_column", {"layout": "multi_column"}),
    ("card", {"layout": "card"}),
    ("multi_column_col1", {"layout": "multi_column", "amount_col": 1}),
    ("multi_column_col2", {"layout": "multi_column", "amount_col": 2}),
    ("vertical_amount_header_list", {"layout": "vertical", "amount_header": ["持仓市值"]}),
    ("vertical_amount_header_multi",
     {"layout": "vertical", "amount_header": ["金额", "持有金额", "持仓市值"]}),
    ("ignore_words_list", {"ignore_words": ["示例甲", "示例零钱"]}),
    ("amount_header_plus_ignore_words",
     {"amount_header": ["金额"], "ignore_words": ["示例养老账户"]}),
]


# =====================================================================
# 1. 逐 fixture × 逐组级 rules 矩阵：新旧输出必须完全相等
# =====================================================================
@pytest.mark.parametrize("rules_id,rules", RULES_MATRIX, ids=[r[0] for r in RULES_MATRIX])
@pytest.mark.parametrize("fixture_id,blocks", ALL_FIXTURES, ids=ALL_IDS)
def test_organize_blocks_equivalence(legacy, fixture_id, blocks, rules_id, rules):
    """同一批 blocks + 同一组 rules：新实现 == HEAD 旧实现（顺序/name/amount 全等）。"""
    old_items = _rows(legacy, blocks, rules)
    new_items = _new(blocks, rules)
    assert new_items == old_items, (
        f"fixture={fixture_id} rules={rules_id} 新旧实现输出不一致")


# =====================================================================
# 2. 自动检测路径显式断言（四类 layout 都真的被走到）
# =====================================================================
def test_auto_detection_covers_all_layouts(legacy):
    """自动检测路径下四类分支都被真实触发，且每类的新旧输出相等。"""
    cases = {
        "horizontal": FIXTURES["horizontal_basic"],
        "vertical": FIXTURES["vertical_basic"],
        "multi_column": FIXTURES["multicolumn_three"],
        "card": FIXTURES["card_three_columns"],
    }
    for layout, blocks in cases.items():
        detected = legacy._detect_layout(
            legacy._drop_noise_rows(legacy._rows_from_blocks(blocks)),
            legacy.DEFAULT_AMOUNT_HEADERS)
        assert detected == layout, f"{layout} fixture 的旧实现检测结果变为 {detected}"
        assert _new(blocks) == _rows(legacy, blocks)
        # 非空结果，防止「两边都空」式的假等价
        assert _new(blocks), f"{layout} fixture 产出为空，等价断言无意义"


#: 默认路径下**本就应当为空**的退化 fixture（等同 test_screenshot_mapping 的
#: 「只有名称 / 只有金额 / 空输入不生成行」三条）。其余 fixture 必须有产出，否则
#: 「两边都空」会让等价断言变成假绿。
DEGENERATE_FIXTURES = {"empty", "name_only", "amount_only"}


def test_fixtures_are_not_vacuously_empty(legacy):
    """除退化 fixture 外，每个 fixture 在默认路径下都要有产出（防空对照假绿）。"""
    empty_ids = {fid for fid, blocks in ALL_FIXTURES if not _new(blocks)}
    assert empty_ids == DEGENERATE_FIXTURES, f"意外为空的 fixture：{empty_ids}"
    for fid, blocks in ALL_FIXTURES:
        if fid in DEGENERATE_FIXTURES:
            continue
        assert _rows(legacy, blocks), f"{fid} 旧实现产出为空，对照无意义"


# =====================================================================
# 3. 真实几何 fixture（holdings_card_blocks.json）
# =====================================================================
def test_holdings_fixture_default_rules(legacy):
    blocks = _holdings_fixture_blocks()
    assert _new(blocks) == _rows(legacy, blocks)


def test_holdings_fixture_p0_rules(legacy):
    """来源组 P0 配置（vertical + 全词「持仓市值」）：新旧相等且产出 3 项。"""
    blocks = _holdings_fixture_blocks()
    rules = {"layout": "vertical", "amount_header": ["持仓市值"]}
    new_items = _new(blocks, rules)
    assert new_items == _rows(legacy, blocks, rules)
    assert [n for n, _ in new_items] == ["示例红利ETF", "示例半导体ETF", "示例银行ETF"]


def test_holdings_fixture_with_builtin_vocabularies_object(legacy):
    """传入「纯出厂」EffectiveVocabularies() 必须与不传（vocabularies=None）逐项相同。"""
    blocks = _holdings_fixture_blocks()
    assert _new(blocks, None, EffectiveVocabularies()) == _new(blocks)
    assert _new(blocks, None, EffectiveVocabularies()) == _rows(legacy, blocks)


# =====================================================================
# 4. 判定函数级别的等价（单点证明，不依赖 organize 的合成路径）
# =====================================================================
@pytest.mark.parametrize("text", [
    "4,321.09 >", "10,987.65 ›", "1,543,210.98 ≫", "65,432.10 →", "4,321.09   >   ",
    "1,876,543.21", "0.07", "23.45 元", "￥1,234.56", "abc", "", "   ",
    "累计收益+543.21>", "+6.78%", "90.68%", "2026-01-15",
])
def test_amount_token_equivalence(legacy, text):
    """`_amount_token` 由硬编码字符类改为「按 row_close_tokens 生成」后逐串等价。"""
    import services.screenshot_service as ss

    assert ss._amount_token(text) == legacy._amount_token(text)


@pytest.mark.parametrize("text", [
    "18:59", "19:00:38", "B/s", "5G", "4G", "381", "示例活钱", "1,234.56", ">>", "›",
    "90.68%", "累计收益", "总资产", "示例养老账户", "2026-01-15 18:59",
])
def test_name_token_equivalence(legacy, text):
    """`_is_name_token`（name_exclude / row_close_tokens / total_labels / noise）等价。"""
    import services.screenshot_service as ss

    assert ss._is_name_token(text) == legacy._is_name_token(text)


@pytest.mark.parametrize("row", [
    [(44, 393, "18:59", 0.9)],
    [(44, 700, "B/s", 0.9)],
    [(44, 900, "5G", 0.9)],
    [(150, 20, "示例活钱", 0.9), (150, 500, "23,456.78", 0.9)],
    [(200, 20, "累计收益2026-01-15", 0.9)],
    [(200, 20, "示例资产", 0.9), (200, 500, "盈亏", 0.9)],
])
def test_row_judgement_equivalence(legacy, row):
    """整行级判定（噪声行 / profit 区 / 汇总标签）等价。"""
    import services.screenshot_service as ss

    assert ss._is_noise_row(row) == legacy._is_noise_row(row)
    assert ss._is_profit_row(row) == legacy._is_profit_row(row)
    assert ss._is_total_label(ss._row_join(row)) == legacy._is_total_label(legacy._row_join(row))


# =====================================================================
# 5. 词表引擎真的被消费（不是「传了参数但没人读」的假接线）
# =====================================================================
def test_vocabularies_drive_name_exclude():
    blocks = [blk(100, 20, "示例活钱"), blk(100, 500, "10,000.00")]
    assert _new(blocks) == [("示例活钱", Decimal("10000.00"))]
    eff = EffectiveVocabularies(overrides={"name_exclude": (("示例活钱",), ())})
    assert _new(blocks, None, eff) == []  # 该词不再作名称 → 整行无名称，不出项


def test_vocabularies_drive_ignore_words():
    blocks = [blk(100, 20, "示例活钱"), blk(100, 500, "10,000.00")]
    eff = EffectiveVocabularies(overrides={"ignore_words": (("示例活钱",), ())})
    assert _new(blocks, None, eff) == []  # 块级丢弃（分行前）


def test_vocabularies_drive_total_labels():
    blocks = [blk(100, 20, "示例活钱"), blk(100, 500, "10,000.00")]
    eff = EffectiveVocabularies(overrides={"total_labels": (("示例活钱",), ())})
    assert _new(blocks, None, eff) == []


def test_vocabularies_drive_profit_zone():
    blocks = [
        blk(100, 20, "示例资产A"), blk(100, 300, "1,000.00"),
        blk(200, 20, "累计收益2026-01-15"), blk(200, 300, "345678"),
    ]
    baseline = _new(blocks)
    assert [n for n, _ in baseline] == ["示例资产A"]  # profit 区行不触发 vertical
    eff = EffectiveVocabularies(overrides={"profit_zone": ((), ("累计收益",))})
    changed = _new(blocks, None, eff)
    assert changed != baseline, "profit_zone 未被消费：移除「累计收益」应改变分支判定"


def test_vocabularies_drive_row_close_tokens():
    blocks = [blk(100, 20, "示例活钱"), blk(100, 500, "10,000.00》")]
    assert _new(blocks) == []  # 出厂词表不含「》」→ 金额不成立
    eff = EffectiveVocabularies(overrides={"row_close_tokens": (("》",), ())})
    assert _new(blocks, None, eff) == [("示例活钱", Decimal("10000.00"))]


def test_vocabularies_drive_noise_patterns():
    blocks = [
        blk(100, 20, "示例活钱"), blk(100, 300, "18:59"), blk(100, 500, "10,000.00"),
    ]
    assert _new(blocks) == []  # 行内命中时间噪声 → 整行剥离
    eff = EffectiveVocabularies(overrides={"noise_patterns": (
        (), (r"\b\d{1,2}:\d{2}(:\d{2})?\b",))})
    assert _new(blocks, None, eff) == [("示例活钱", Decimal("10000.00"))]


def test_vocabularies_drive_amount_headers():
    blocks = [
        blk(100, 20, "示例活钱"), blk(160, 20, "市值"), blk(220, 20, "10,000.00"),
    ]
    baseline = _new(blocks)
    eff = EffectiveVocabularies(overrides={"amount_headers": (("市值",), ())})
    assert _new(blocks, None, eff) != baseline


# =====================================================================
# 6. 组级覆盖语义：replace（不是 add），与现状一致
# =====================================================================
def test_group_ignore_words_is_replace_not_add(legacy):
    """组里配了 ignore_words 就只用组里的词（出厂的「去诊断」不再被丢）。

    现状 `rules.get("ignore_words") or DEFAULT_IGNORE_WORDS` 是整串替换（spec D2.3），
    接入词表引擎后必须保持——否则出厂黑名单会与组配置叠加，行为漂移。
    """
    blocks = [
        blk(100, 20, "去诊断"), blk(100, 500, "0.00"),
        blk(150, 20, "示例屏蔽词"), blk(150, 500, "1,111.11"),
        blk(200, 20, "示例活钱"), blk(200, 500, "10,000.00"),
    ]
    rules = {"ignore_words": ["示例屏蔽词"]}
    new_items = _new(blocks, rules)
    assert new_items == _rows(legacy, blocks, rules)
    names = [n for n, _ in new_items]
    assert "去诊断" in names, "组级 ignore_words 被当成增量（add）叠加了出厂词"
    assert "示例屏蔽词" not in names


@pytest.mark.parametrize("amount_header", [
    ["持仓市值"], ["金额", "持有金额"], ["持仓市值", "盈亏"],
])
def test_group_amount_header_list_form(legacy, amount_header):
    """list 形态（真实库主形态）：新旧相等。"""
    blocks = _holdings_fixture_blocks()
    rules = {"layout": "vertical", "amount_header": amount_header}
    assert _new(blocks, rules) == _rows(legacy, blocks, rules)


def test_group_amount_header_list_order_is_behavior():
    """amount_header 是「锚点优先序」：顺序即行为，接入词表后不得被重排/去重打乱。"""
    blocks = [
        blk(100, 20, "示例资产"), blk(100, 300, "金额"), blk(100, 700, "持有金额"),
        blk(160, 20, "1,000.00"), blk(160, 300, "22,000.00"), blk(160, 700, "3,300.00"),
    ]
    rules = {"layout": "vertical", "amount_header": ["持有金额", "金额"]}
    first = _new(blocks, rules)
    assert [a for _, a in first] == [Decimal("22000.00")] or first == []


# =====================================================================
# 7. 已裁决的**已知差异**（str 形态 amount_header，交业主裁决，非静默修复）
# =====================================================================
def test_group_amount_header_str_known_difference(legacy):
    """`amount_header` 存成**单个字符串**时，新旧实现不等（已知差异，待裁决）。

    旧实现 `rules.get("amount_header") or DEFAULT` 直接拿到 str，随后
    `any(h in joined for h in amount_headers)` 会**逐字符迭代**（"持仓市值" → 持/仓/市/值），
    等于「命中任一汉字即算表头」，实测产出垃圾行（如把证券代码块配成金额）。
    本车道按 spec 归一为 `[s]`（整词），旧实现无此归一。

    断言口径：新实现 str 与 [str] 等价；旧实现两种形态不等（差异被记录，不隐藏）。
    """
    blocks = _holdings_fixture_blocks()
    str_rules = {"layout": "vertical", "amount_header": "持仓市值"}
    list_rules = {"layout": "vertical", "amount_header": ["持仓市值"]}

    assert _new(blocks, str_rules) == _new(blocks, list_rules)

    old_str = _rows(legacy, blocks, str_rules)
    old_list = _rows(legacy, blocks, list_rules)
    assert old_str != old_list, "旧实现已能归一 str，本差异记录应随之删除"
    assert len(old_str) > len(old_list)  # 逐字符匹配 → 多出垃圾行
    assert _new(blocks, str_rules) == old_list  # 新实现 == 旧的「正确形态」


# =====================================================================
# 8. 组配置读取 fail-fast（spec D4 / 现状隐患 H1）
# =====================================================================
#: 坏 JSON 里故意塞一段「像真实机构名」的合成文案，用来断言报文**不回显原文**。
_BAD_RULES_WITH_PRIVATE_TEXT = '{"机构名称": "示例不可外泄机构", '


def _make_group(db, rules, slug="demo-equivalence", label="示例来源组"):
    from models import SourceGroup

    sg = SourceGroup(slug=slug, label=label, asset_prefix=f"{label}-", rules=rules)
    db.add(sg)
    db.commit()
    return sg


def test_get_group_config_rejects_bad_json(db):
    """坏 JSON → 422 SOURCE_GROUP_RULES_INVALID（不再静默降级 None）。"""
    from errors import ApiError, ErrorCode
    from services.screenshot_service import get_group_config

    sg = _make_group(db, _BAD_RULES_WITH_PRIVATE_TEXT)
    with pytest.raises(ApiError) as ei:
        get_group_config(db, sg.slug)
    assert ei.value.status_code == 422
    assert ei.value.code == ErrorCode.SOURCE_GROUP_RULES_INVALID
    assert str(sg.id) in ei.value.message  # 报文带来源组 id
    assert "示例不可外泄机构" not in ei.value.message  # 不回显 rules 原文
    assert ei.value.details and ei.value.details["source_group_id"] == sg.id


def test_get_group_config_rejects_non_object_json(db):
    """顶层不是 JSON 对象（数组/字符串/数字）→ 422，报文带实际类型。"""
    from errors import ApiError, ErrorCode
    from services.screenshot_service import get_group_config

    sg = _make_group(db, '["layout", "vertical"]', slug="demo-array", label="示例来源组二")
    with pytest.raises(ApiError) as ei:
        get_group_config(db, sg.slug)
    assert ei.value.status_code == 422
    assert ei.value.code == ErrorCode.SOURCE_GROUP_RULES_INVALID
    assert "list" in ei.value.message


def test_get_group_config_empty_rules_is_unconfigured_not_error(db):
    """`rules` 为空串 → None（未配置 ≠ 配错，仍走纯通用），不抛。"""
    from services.screenshot_service import get_group_config

    sg = _make_group(db, "", slug="demo-empty", label="示例来源组三")
    cfg = get_group_config(db, sg.slug)
    assert cfg["rules"] is None
    assert cfg["asset_prefix"] == "示例来源组三-"


def test_get_group_config_valid_rules_roundtrip(db):
    """合法 JSON 对象原样交出（不增删键）。"""
    from services.screenshot_service import get_group_config

    sg = _make_group(db, '{"layout": "vertical", "amount_col": 1}',
                     slug="demo-ok", label="示例来源组四")
    assert get_group_config(db, sg.slug)["rules"] == {"layout": "vertical", "amount_col": 1}