"""OCR 解析词表引擎（P1a）：出厂词表常量 + 生效计算 + 写入校验。

分层口径见 `.scratch/parser-rules-refactor/spec.md` D2（四层规则来源）：

| 层 | 存储 | 可改 | 生效顺序 |
|---|---|---|---|
| `builtin` | 本文件常量（出厂值） | 本身不可改，可被 vocabulary 的 `remove` 屏蔽 | 最低 |
| `vocabulary` | 新表 `parser_vocabularies`（全局，一条目一行，存 add/remove 增量） | 是 | 与 builtin 合并 |
| `group` | `source_groups.rules`（组级解析配置） | 是 | 覆盖 vocabulary + builtin |

生效词表 = **(builtin ∪ add) − remove**；组级 `amount_header` 是整串替换（沿用现状语义）。

本模块**只**做词表注册表、生效计算与 payload 校验，不含任何 OCR 解析算法——
判定逻辑仍在 `services/screenshot_service.py`（后续车道才改为消费本模块）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

# ---------------- 出厂词表（builtin，值自 screenshot_service 原样搬迁） ----------------
# 搬迁红线：值逐字不变、顺序不变。下游 `_detect_layout` / `_organize_vertical`
# 按词表顺序遍历（锚点选择对顺序敏感），顺序即行为。

# 金额列标题词（用于垂直/多列定位数值行）。「我的资产/预计可借」属汇总项，见 _TOTAL_LABELS。
DEFAULT_AMOUNT_HEADERS = ("金额", "金额(元)", "持有金额", "累计收益", "昨日收益", "本周收益")

# 按钮词黑名单：这些块是操作按钮/入口，不是资产行。
DEFAULT_IGNORE_WORDS = ("去诊断", "刷医保", "提余额", "查明细", "转账汇款", "充值", "提现", "立即还款",
                        "订阅", "草稿", "反馈", "评价", "最近使用", "办事记录", "购买理财", "购买存款",
                        "生活缴费", "收支明细")

# 汇总/对账标签行：作为对账锚点，不参与资产行。
_TOTAL_LABELS = ("总资产", "总负债", "投顾总金额", "我的资产", "预计可借")

# 汇总行**提示**词表（ADR-0029 D7 / 票 03 D1）：命中即给 preview 行
# `aggregate_hint=true`（前端**预勾可取消**），行照常可见、只是不落账。
#
# 与 `_TOTAL_LABELS` 的分工**不可混淆**（ADR-0029 代价一节的「两套不计入」）：
# - `_TOTAL_LABELS`：整行丢弃、**根本不进 preview**（对账锚点，用户看不到这行）；
# - 本词表：行留在清单里可见，用户可勾成「汇总行」参与跨图核对，零副作用。
# 因此两份词表刻意分开：把本表并进 total_labels 会让这些行从清单里消失（正是 D6
# 否决的做法），把 total_labels 并进本表则会造出一堆永远看不到的提示。
# 匹配口径＝**子串**（与 `_is_total_label` / `ignore` 同口径）：汇总行常带机构前缀
# 或修饰词（「示例银行-贵金属」「本月合计」），全等匹配会漏。
AGGREGATE_HINT_LABELS = (
    "合计", "小计", "总计", "总额", "汇总",
    "贵金属",            # 典型场景：一页只有「贵金属」总额，明细在另外几页
    "我的资产", "总资产", "总负债", "预计可借", "投顾总金额",
)

# 盈亏/比率区标签词：含这些词的块绝非资产名称（某券商 App「盈亏分析/今年收益/收益率/累计收益」等）。
# 注意这些词（如 累计收益/昨日收益）仍是合法的金额列头 trigger（留在 DEFAULT_AMOUNT_HEADERS），
# 只是不应作为「项目名」。安全：真实资产名均不含「收益/盈亏」。
_NAME_EXCLUDE_WORDS = ("盈亏", "收益", "收益率", "收益明细", "资产轨迹", "累计收益", "资产占比", "资产详情")

# 盈亏/比率/轨迹区（profit 区）标签行：不参与「垂直分支」触发。
# 某券商样本卡片「累计收益2026-01-15 + 345678」误触发 vertical 分支、淹没真实横排行的真因。
# 这些标签行仅用于垂直分支判定豁免，不整行剔除（某基金平台样本的多列金额列头同含这些词，须保留列）。
_PROFIT_ZONE_LABELS = ("盈亏分析", "今年收益", "收益率", "收益明细", "资产轨迹",
                       "累计收益", "资产占比", "资产详情")

# 行闭合符。**原值是含重复项的集合字面量**（"›" 写了 3 次），此处保持原样搬迁；
# 去重只发生在下方 BUILTIN_VOCABULARIES 的有序元组里（set 迭代序不稳定，不可直接引用）。
_ROW_CLOSE_TOKENS = {">", "›", "›", "≫", "›", "→"}

# 噪声 token：状态栏时间/网络速率/电池等，绝不作为资产名称。
_NOISE_TOKEN_PATTERNS = (
    r"\b\d{1,2}:\d{2}(:\d{2})?\b",  # 时间 18:59 / 19:00:38
    r"\b\d+(\.\d+)?\s*(B/s|kB/s|MB/s|B/S|kHz|MHz|GHz)\b",  # 网络/速率单位
    r"\b[245]G\b",  # 5G/4G/2G
)

# ---------------- 词表注册表 ----------------
# 合法 key 全集（顺序即前端展示序）。写入侧只接受这些 key，读侧只认这些 key。
VOCABULARY_KEYS = ("amount_headers", "ignore_words", "total_labels",
                   "name_exclude", "profit_zone", "row_close_tokens",
                   "noise_patterns")

# key → 出厂词表。全部为 tuple（不可变），模块级不暴露可变默认值。
# row_close_tokens 取自 _ROW_CLOSE_TOKENS 集合，按源码书写序去重为固定顺序
# （不可用 set 迭代序：str 的哈希随机化会让顺序每次进程不同）。
BUILTIN_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "amount_headers": tuple(DEFAULT_AMOUNT_HEADERS),
    "ignore_words": tuple(DEFAULT_IGNORE_WORDS),
    "total_labels": tuple(_TOTAL_LABELS),
    "name_exclude": tuple(_NAME_EXCLUDE_WORDS),
    "profit_zone": tuple(_PROFIT_ZONE_LABELS),
    "row_close_tokens": (">", "›", "≫", "→"),
    "noise_patterns": tuple(_NOISE_TOKEN_PATTERNS),
}


def _as_str_tuple(items: Any) -> tuple[str, ...]:
    """宽松规整为字符串元组：跳过非字符串与空白项，按序去重。"""
    if items is None:
        return ()
    if isinstance(items, str):  # 防御：单个字符串不是字符串数组
        items = (items,)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


@dataclass(frozen=True)
class EffectiveVocabularies:
    """一次解析请求内使用的生效词表。

    `overrides`：词表 key → `(add, remove)` 两个字符串元组（均为增量）。

    `get(key)` 的合成顺序（顺序即行为，不可调换）：

    1. 取 `BUILTIN_VOCABULARIES[key]` 原文顺序（key 不存在 → `KeyError`，不静默返回空）；
    2. 先剔除 `remove` 命中的出厂词（字符串精确匹配）；
    3. 再把 `add` 逐项追加到末尾（保持 add 的出现顺序；已在结果里的词不重复追加）。

    `add` 与 `remove` 同词并存时 **add 生效**：组级 `amount_header` 的「整串替换」
    正是用 `remove=出厂全量 + add=组配置列表` 编码的，若 remove 反压 add 会把
    组配置整个抹空。写入校验（`validate_vocabulary_payload`）禁止该组合，
    故仅在库表脏数据时才可能遇到，此处的取舍是「不静默丢配置」。
    """

    overrides: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        for key, pair in (self.overrides or {}).items():
            try:
                add, remove = pair
            except (TypeError, ValueError):
                raise ValueError(
                    f"词表覆盖必须为 (add, remove) 二元组：key={key}") from None
            normalized[key] = (_as_str_tuple(add), _as_str_tuple(remove))
        object.__setattr__(self, "overrides", MappingProxyType(normalized))

    def get(self, key: str) -> tuple[str, ...]:
        """生效词表 `(builtin ∪ add) − remove`；未知 key → `KeyError`。"""
        if key not in BUILTIN_VOCABULARIES:
            raise KeyError(
                f"未知词表 key：{key}（合法 key：{', '.join(VOCABULARY_KEYS)}）")
        add, remove = self.overrides.get(key, ((), ()))
        removed = set(remove)
        out: list[str] = [w for w in BUILTIN_VOCABULARIES[key] if w not in removed]
        seen = set(out)
        for w in add:
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
        return tuple(out)

    def with_group_overrides(self, group_rules: Mapping[str, Any] | None) -> "EffectiveVocabularies":
        """叠加组级解析配置（`source_groups.rules`），返回新实例（不改自身）。

        只认两个现存组级键，语义按现状对齐：

        - **`amount_header`：整串替换**。现状 `rules.get("amount_header")
          or DEFAULT_AMOUNT_HEADERS` 是「有配置就整体替换、没配置才用出厂值」，
          所以这里用 `remove=出厂全量 + add=组配置列表` 编码替换；组配置清洗后为空
          则视同未配置（回落出厂值），与 `or` 语义一致。
        - **`ignore_words`：叠加（add 语义）**。组配置的词追加到生效词表末尾。
          注：现状 parse 侧对 `ignore_words` 也是「整串替换」
          （`rules.get("ignore_words") or DEFAULT_IGNORE_WORDS`），本车道按
          「组级先只支持 add/remove 形态」实现；接入解析前若要求行为等价，
          需按 `amount_header` 的方式改为替换并同步改测试。

        其余 key 组级目前无配置来源，忽略。非法形态（非列表 / 全空白）静默跳过。
        """
        if not group_rules:
            return self
        merged: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = dict(self.overrides)

        headers = _as_str_tuple(group_rules.get("amount_header"))
        if headers:
            merged["amount_headers"] = (headers, BUILTIN_VOCABULARIES["amount_headers"])

        ignore_words = _as_str_tuple(group_rules.get("ignore_words"))
        if ignore_words:
            prev_add, prev_remove = merged.get("ignore_words", ((), ()))
            merged["ignore_words"] = (_as_str_tuple(prev_add + ignore_words), prev_remove)

        return EffectiveVocabularies(overrides=merged)


def load_vocabularies(db) -> EffectiveVocabularies:
    """从 `parser_vocabularies` 表读全局增量（未建表 / 空表 → 全 builtin）。

    行格式：`key` + `add_items` / `remove_items`（JSON 数组字符串，默认 `'[]'`）。

    容错口径（本表由另一车道落库，可能尚未建表）：
    - 表不存在（`OperationalError` / `ProgrammingError`）→ 返回空覆盖，不抛；
    - 未知 key、坏 JSON、非数组 → 跳过该行并 `logger.warning`（只记 key 名，不记值，
      值可能含用户私有文案）。
    """
    try:
        rows = db.execute(text(
            "SELECT key, add_items, remove_items FROM parser_vocabularies")).all()
    except (OperationalError, ProgrammingError):
        logger.debug("parser_vocabularies 表不可用（尚未迁移），使用全出厂词表")
        return EffectiveVocabularies()

    overrides: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for row in rows:
        key = row[0]
        if key not in VOCABULARY_KEYS:
            logger.warning("parser_vocabularies 含未知词表 key，已跳过：key=%s", key)
            continue
        try:
            add_raw = json.loads(row[1] if row[1] is not None else "[]")
            remove_raw = json.loads(row[2] if row[2] is not None else "[]")
        except (TypeError, ValueError):
            logger.warning("parser_vocabularies 行 JSON 解析失败，已跳过：key=%s", key)
            continue
        if not isinstance(add_raw, list) or not isinstance(remove_raw, list):
            logger.warning("parser_vocabularies 行的 add/remove 不是数组，已跳过：key=%s", key)
            continue
        overrides[key] = (_as_str_tuple(add_raw), _as_str_tuple(remove_raw))
    return EffectiveVocabularies(overrides=overrides)


def _reject(message: str) -> None:
    raise ApiError(ErrorCode.SOURCE_GROUP_RULES_INVALID, 422, message)


def _check_str_list(field_name: str, items: Any) -> tuple[str, ...]:
    """校验并规整字符串数组：非数组/含非字符串/含空白项 → 422。"""
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)):
        _reject(f"{field_name} 必须是字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            _reject(f"{field_name} 必须是字符串数组（含非字符串项）")
        s = item.strip()
        if not s:
            _reject(f"{field_name} 不能含空串")
        if s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def validate_vocabulary_payload(key: str, add: Sequence[str], remove: Sequence[str],
                                reason: str | None) -> None:
    """校验词表写入 payload，违反任一条 → `ApiError(SOURCE_GROUP_RULES_INVALID, 422)`。

    规则（顺序即报错优先级）：
    1. `key` 必须属于 `VOCABULARY_KEYS`（报文含合法 key 列表）；
    2. `add` / `remove` 必须是字符串数组，且不含空串（空白视同空）；
    3. `add` 与 `remove` 不得有交集（同一个词又要加又要删是自相矛盾）；
    4. `remove` 里的词必须已在生效词表中（builtin 或本次 add），否则是无用项；
    5. `remove` 非空时 `reason` 必须非空（全局删词留痕，spec D2.2）。
    """
    if key not in VOCABULARY_KEYS:
        _reject(f"未知词表 key：{key}（合法 key：{', '.join(VOCABULARY_KEYS)}）")

    add_items = _check_str_list("add", add)
    remove_items = _check_str_list("remove", remove)
    set_remove = set(remove_items)

    conflict = [w for w in add_items if w in set_remove]
    if conflict:
        _reject(f"add 与 remove 存在冲突词（同一词不能既加又删）：{', '.join(conflict)}")

    effective = set(BUILTIN_VOCABULARIES[key]) | set(add_items)
    unknown = [w for w in remove_items if w not in effective]
    if unknown:
        _reject("remove 含不在生效词表中的词："
                f"{', '.join(unknown)}（该词既不在出厂词表也不在 add 中）")

    if remove_items and not (reason or "").strip():
        _reject("删除出厂词必须填写 reason（全局生效，需留痕）")