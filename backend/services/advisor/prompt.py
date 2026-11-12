"""advisor prompt v1（模型只做措辞；数字只允许占位符）。

输入仅：key/kind/label/unit ＋「缩放值」（money 用 value_scaled，其余 value）；
模型看不到精确金额。输出 JSON 结构固定，数字必须写成 `{{info.<key>}}`。
"""
import json

PROMPT_VERSION = "advisor.prompt.v1"

_SYSTEM = (
    "你是家庭资产配置的解释器，不是交易执行器。"
    "你的全部职责是：在给定信息集内排序、取舍、用中文解释；"
    "禁止做算术、禁止判灯、禁止给出具体数字、禁止执行交易。"
)

_RULES = (
    "输出一个合法 JSON 对象（不要 Markdown 代码块、不要解释文字），字段："
    "title（<=40字）、summary_template（2~4句）、items（1~5项）。"
    "items 每项字段：seq(1..n)、subject_kind(attribute|goal|threshold|narrative)、"
    "subject_ref、direction(increase|decrease|hold|rebalance)、"
    "rationale_template(<=400字)、info_refs(>=1 个信息集 key)。"
    "任何数字必须写成占位符 {{info.<key>}}，禁止 %、小数、千分位数字、金额数字；"
    "允许 T+1、四位年份、第N步。"
    "不得出现：下单、转账、代扣、自动执行、委托买入/卖出。"
)

_FEWSHOT = [
    {
        "title": "权益类超配，建议分步回调",
        "summary_template": "当前权益类偏离 {{info.alloc.fund_type.3.deviation_pp}}，"
                            "已超红档，建议在下次调仓时向目标回调。",
        "items": [{
            "seq": 1, "subject_kind": "attribute",
            "subject_ref": "attr:fund_type:3", "direction": "decrease",
            "rationale_template": "偏离 {{info.alloc.fund_type.3.deviation_pp}}，"
                                  "回调金额约 {{info.alloc.fund_type.3.deviation_amount}}。",
            "info_refs": ["alloc.fund_type.3.deviation_pp",
                          "alloc.fund_type.3.deviation_amount"],
        }],
    },
    {
        "title": "目标进度需关注",
        "summary_template": "所需年化 {{info.goal.required_annualized}} 高于目标年化，"
                            "建议复核目标或投入节奏。",
        "items": [{
            "seq": 1, "subject_kind": "goal", "subject_ref": "goal:2",
            "direction": "hold",
            "rationale_template": "达成度 {{info.goal.progress}}，"
                                  "所需年化 {{info.goal.required_annualized}}。",
            "info_refs": ["goal.progress", "goal.required_annualized"],
        }],
    },
]


def _model_entry(entry: dict) -> dict:
    """发给模型的条目：money 只给缩放值；其余给原值（比率 4 位/状态枚举）。"""
    return {
        "key": entry["key"], "kind": entry["kind"], "label": entry["label"],
        "unit": entry["unit"],
        "value": (entry.get("value_scaled") if entry["kind"] == "money"
                  else entry.get("value")),
    }


def build_prompt(info_set: dict) -> str:
    """构造 prompt v1（纯函数；不含精确金额）。"""
    entries = [_model_entry(e) for e in info_set.get("entries") or []]
    return "\n".join([
        _SYSTEM,
        "",
        "【信息集】（数字已脱敏；money 单位为万元）",
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
        "",
        "【输出约束】",
        _RULES,
        "",
        "【示例】",
        json.dumps(_FEWSHOT[0], ensure_ascii=False, separators=(",", ":")),
        json.dumps(_FEWSHOT[1], ensure_ascii=False, separators=(",", ":")),
        "",
        "只输出 JSON。",
    ])