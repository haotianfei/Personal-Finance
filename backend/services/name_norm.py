"""名称归一与模糊匹配真源（单点）。

出处：从 `services/screenshot_service.py` 原样搬迁（`_normalize_name` /
`_cmp_norm` / `_period_of` / `_digits` / `_skeleton` / `fuzzy_match_name` /
`match_existing`），逻辑零改写，仅位置调整——避免「录入侧一套、改名侧另一套」
各自演进造成同一名字两处不同判定（票 04：名称 → 身份解析收口）。

为什么单独成模块：`screenshot_service` 与 `name_resolution_service` 都要用，
放进任一侧都会形成循环 import。
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

_PERIOD_RE = re.compile(r"(\d{6,8})\s*期$")
_NAME_SEP_RE = re.compile(r"[|｜·。]+")
_CMP_DROP = frozenset("-()（）")


def _period_of(name: str) -> Optional[str]:
    """OCR 名尾部期号（'20260101期'）→ 期号串；无期号返回 None。

    （_cmp_norm 保留内部空格不剥，"示例盈 20260101期" 尾部能匹配上。）
    """
    norm = _cmp_norm(name)
    m = _PERIOD_RE.search(norm)
    return m.group(1) + "期" if m else None


def _normalize_name(name: str) -> str:
    """OCR 名归一（结构保留版：除分隔符统一外尽量保持原名）。

    - 分类/分段分隔符 | ｜ ·（含连续）统一为 "-"："基金|示例XX" → "基金-示例XX"，
      保留截图里「分类|产品名」结构（分类一般为 基金/投顾/定期 等）。
    - 名称中间的"。"多为"·"的 OCR 误读（如"示例盈。甲"实际是"示例盈·甲"，
      点在中间而非句尾），同样按分隔符归 "-"；句尾噪声"。"归一后被 strip("-") 消除。
    - 半角 "." 位于名称中间（如"示例盈.甲"）原样保留。
    - 产品名括号 ()（） 原样保留（如 "xx(LOF)A"、"xx（A类份额）"）。
    - 纯装饰括号与标点剥离：「」【】[]、，
    - 连续 "-" 合并；首尾分隔符与空白去除；内部空格保留（期号识别依赖）。
    """
    name = _NAME_SEP_RE.sub("-", name)
    for ch in "「」【】[]、，":
        name = name.replace(ch, "")
    name = re.sub(r"-{2,}", "-", name)
    return name.strip("-").strip()


def _cmp_norm(name: str) -> str:
    """模糊比对键：_normalize_name 后再剥结构噪声 "-" 与括号。

    让新规范名（"基金-示例光伏"）与存量粘连名（"基金示例光伏"）、
    "xx(原)" 与旧名 "xx原" 命中同一资产行，不因命名规则升级造成重复创建。
    """
    return "".join(ch for ch in _normalize_name(name) if ch not in _CMP_DROP)


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _skeleton(s: str) -> str:
    return "".join(ch for ch in s if not ch.isdigit())


def head_tail(name: str) -> tuple[str, str]:
    """资产全名 → (机构头, 产品尾)。

    全名约定为「机构-产品」（`asset_name = prefix + 识别名`）；无分隔符时整体算尾。
    """
    parts = (name or "").split("-", 1)
    return (parts[0], parts[1]) if len(parts) > 1 else ("", name or "")


def fuzzy_match_name(name: str, known_names: list[str]) -> Optional[str]:
    """OCR 项目名 → 已知名单最匹配项；无匹配返回 None。

    策略（归一化后）：1) 精确；2) 已知名为 OCR 名子串且余部为纯期号
    （长匹配优先，如 "示例盈" ⊂ "示例盈20260101期"）——类别词（基金/投顾/
    定期等）余部非期号不折叠，避免粗桶吞并细粒度产品；3) 非数字骨架相同且
    已知数字串为 OCR 数字串后缀（"1180" 含 "180"）；4) difflib 相似度 >= 0.82。

    注：期号折叠（如 示例盈 → 示例盈20260101期）在 compute_preview 里需再过
    期号规则——带期号识别项不允许折叠进无期号/异期号旧桶，否则按未命中 create。
    """
    norm = _cmp_norm(name)
    knowns = [(_cmp_norm(k), k) for k in known_names]
    for k, raw in knowns:
        if k == norm:
            return raw
    # 期号锚定折叠：已知名是 OCR 名前缀，且余部为纯期号（如 "20260101期"）。
    # 其余子串关系一律不折叠，回落到骨架/difflib，防止 "基金" ⊂ "基金华夏…" 吞并。
    subs = []
    for k, raw in knowns:
        if k and norm.startswith(k):
            rest = norm[len(k):]
            if re.fullmatch(r"\s*\d{6,8}\s*期?\s*", rest):
                subs.append((len(k), raw))
    if subs:
        return max(subs, key=lambda x: x[0])[1]
    sk, dg = _skeleton(norm), _digits(norm)
    for k, raw in knowns:
        kdg = _digits(k)
        if sk and dg and kdg and _skeleton(k) == sk and dg.endswith(kdg):
            return raw
    best = difflib.get_close_matches(norm, [k for k, _ in knowns], n=1, cutoff=0.82)
    if best:
        for k, raw in knowns:
            if k == best[0]:
                return raw
    return None


def match_existing(name: str, known_names: list[str]) -> Optional[str]:
    """项目名 → 已知名单（不带前缀）命中项；未命中返回 None。

    fuzzy_match_name + 期号规则的完整判定：带期号识别项仅同期号资产才命中，
    无期号/异期号一律视为未命中（上层按 create 处理）。compute_preview 与
    补录（apply_supplements / resolve_supplements）共用的唯一匹配真源。
    """
    m = fuzzy_match_name(name, known_names)
    if m is not None:
        ip, mp = _period_of(name), _period_of(m)
        if ip is not None and ip != mp:
            return None   # 期号不匹配旧桶/异期 → 独立成新资产
    return m