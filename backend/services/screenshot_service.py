"""截图录入服务（T2 解析+映射 → T3 写入+凭证）。

术语见 CONTEXT.md：来源组 / 截图凭证 / 待录入清单 / 对账锚点。
T2 = 解析层（PaddleOCR PP-OCRv6）+ 映射引擎（接缝 A，纯逻辑），不写库。
T3 = 写入与凭证（接缝 B）：确认清单 → 写/更新/清零当天记录 + 凭证落盘存证。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date as _date, datetime as _datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Optional, Sequence

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (
    Asset, AssetItem, AssetItemName, Channel, Institution, LineageLink,
    ScreenshotEvidence, SourceGroup,
)
from services.asset_service import get_or_create_item, resolve_item_id, _change
from services.principal_service import resolve_cost_basis
from services import audit_service, classification_service, lineage_service
# 词表出厂值已搬到 services/parser_rules.py（词表引擎）；此处按原名重导出，
# 保持本模块级名字可用（下游判定函数与 scripts/screenshot_spike.py 继续照旧引用）。
from services.parser_rules import (
    AGGREGATE_HINT_LABELS, DEFAULT_AMOUNT_HEADERS, DEFAULT_IGNORE_WORDS,
    EffectiveVocabularies, _NAME_EXCLUDE_WORDS, _NOISE_TOKEN_PATTERNS,
    _PROFIT_ZONE_LABELS, _ROW_CLOSE_TOKENS, _TOTAL_LABELS,
)
# 名称归一与匹配真源（单点）：本模块按原名重导出，保持模块级符号可用。
from services.name_norm import (  # noqa: F401  (re-export)
    _cmp_norm, _digits, _normalize_name, _period_of, _skeleton,
    fuzzy_match_name, match_existing,
)

logger = structlog.get_logger(__name__)

# ---------------- 来源组 ----------------
# 兼容旧硬编码 dict：迁移后首选库表 SourceGroup，未落库时退回此常量（防御）。
SOURCE_GROUPS = {
    "bantuou": {
        "label": "支付宝",
        "asset_prefix": "支付宝-",  # 覆盖整个支付宝 App 截图（基金/黄金/余额宝/帮你投…）
    },
}

# App 包名 → 来源组显示名，用于文件名自动推荐来源组（可被用户改选）。
APP_PACKAGE_SOURCE_GROUPS = {
    "com.icbc": "工商银行",
    "cmb.pb": "招商银行",
    "com.cmschina.stock": "招商证券",
    "com.mybank.android.phone": "网商银行",
    "com.tencent.mm": "微信理财通",
    "com.eg.android.AlipayGphone": "支付宝",
}


# ---------------- 解析规则版本（ADR-0023 D7） ----------------
PARSER_VERSION = "2026.11-1"
"""服务端解析规则版本号，写进每条识别快照的 `recognized.parser_version`。

**凡词表 / 规则 / 算法变更必须手工递增**（`2026.11-1` → `2026.11-2` → `2026.11-3`…）。
没有它，「同一张图的识别率变差了」无法归因是规则改了还是图换了——这正是识别快照
（`screenshot_evidence.recognition_json`）存在的主要理由（ADR-0023 D7）。

修改本常量**不需要**迁移：它只影响此后写入的快照，历史快照保留当时的版本号
（那正是要保留的信息，不要回填）。
"""


# ---------------- 来源组 DB 读取/落库 ----------------
def seed_default_source_groups(db: Session) -> None:
    """把默认 bantuou 组落入 source_groups 表（幂等）。

    迁移机制：无 Alembic，建表靠 create_all；本函数在应用启动（migrate_database）
    与测试中调用，保证旧键 'bantuou' 能被解析到库表实体。

    幂等口径：slug="bantuou" **或** label="支付宝" 任一已存在即视为已种——
    用户改过该组 slug（或另建同名组）后，启动种子不再按原 slug 键重复插入
    撞 label 唯一约束（曾致启动日志 UNIQUE constraint failed: source_groups.label）。

    旧定义（「支付宝-帮你投」子产品）已废弃：现在 bantuou 组代表整个支付宝
    App 截图来源。仅对按 slug 命中的旧定义行就地更新，且目标 label 未被
    其它行占用时才改（防迁移 UPDATE 撞唯一约束）；用户改过的定义不覆盖。
    """
    row = db.execute(
        select(SourceGroup).where(SourceGroup.slug == "bantuou")
    ).scalar_one_or_none()
    if row is None:
        if db.execute(
            select(SourceGroup.id).where(SourceGroup.label == "支付宝")
        ).first() is not None:
            return  # label 已存在（原行改过 slug、或用户自建同名组）：跳过补种
        db.add(SourceGroup(
            slug="bantuou", label="支付宝", asset_prefix="支付宝-",
            guide="打开支付宝 App，进入资产总览/基金理财持仓页，截图包含总资产与各产品金额",
            sort_order=0,
        ))
        db.commit()
        return
    if row.label == "支付宝-帮你投" or (row.asset_prefix or "").startswith("支付宝-帮你投-"):
        taken = db.execute(
            select(SourceGroup.id).where(SourceGroup.label == "支付宝",
                                         SourceGroup.id != row.id)
        ).first()
        if taken is None:
            row.label = "支付宝"
            row.asset_prefix = "支付宝-"
            if row.guide and "帮你投" in row.guide:
                row.guide = "打开支付宝 App，进入资产总览/基金理财持仓页，截图包含总资产与各产品金额"
            db.commit()


def get_source_group(db: Session, key) -> Optional[SourceGroup]:
    """按稳定标识（slug 或 label）解析来源组；无匹配返回 None。"""
    if key is None:
        return None
    return db.execute(
        select(SourceGroup).where(or_(SourceGroup.slug == str(key), SourceGroup.label == str(key)))
    ).scalar_one_or_none()


def resolve_source_group(db: Session, value) -> SourceGroup:
    """凭证写入前把入参解析成来源组行；未知组 → 400 `SOURCE_GROUP_UNKNOWN`。

    0029 起「这张凭证属于哪个组」只存 `screenshot_evidence.source_group_id`
    外键，写入前必须先有行可引用。解析口径与 `get_source_group` 一致
    （slug 或 label 均可，前端历来两种都传）。

    **解析不到必须报错**，两条捷径都被明确否决：自动建组会污染用户可选字典，
    静默置 NULL 会让 NOT NULL 外键失效、把悬空重新放回库里。特别注意
    `get_group_config` 的 legacy 兜底（硬编码 `SOURCE_GROUPS`）只服务只读解析
    （asset_prefix / rules），**不能**当作「组存在」的凭据——外键需要真实行。
    """
    sg = get_source_group(db, value)
    if sg is None:
        raise ApiError(
            ErrorCode.SOURCE_GROUP_UNKNOWN, 400, f"未知来源组: {value}",
            {"source_group": None if value is None else str(value)})
    return sg


def _parse_group_rules(sg: SourceGroup) -> Optional[dict]:
    """`source_groups.rules` 文本 → dict（fail-fast，spec D4 / 现状隐患 H1）。

    旧实现解析失败或顶层非对象时**静默降级 `rules=None`**（无日志）：写坏一次 JSON，
    该组全部教学规则与解析规则就此失效，脏资产开始进账本而界面无异常。现在一律
    抛 `ApiError(SOURCE_GROUP_RULES_INVALID, 422)`。

    报文**不回显 rules 原文**（可能含真实机构名/账户文案/金额），只带来源组 id 与
    出错位置（JSON 解析错误给出行/列/偏移，非对象给出实际 JSON 类型）。

    空串/None 视同「没有规则」，仍返回 `None`（未配置 ≠ 配错）。
    """
    raw = sg.rules
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        pos = getattr(exc, "pos", None)
        if pos is None:
            where = "位置不可定位"
        else:
            where = (f"第 {getattr(exc, 'lineno', '?')} 行第 "
                     f"{getattr(exc, 'colno', '?')} 列（偏移 {pos}）")
        raise ApiError(
            ErrorCode.SOURCE_GROUP_RULES_INVALID, 422,
            f"来源组解析规则不是合法 JSON：source_group_id={sg.id}，{where}",
            {"source_group_id": sg.id, "position": pos},
        ) from None
    if not isinstance(parsed, dict):
        raise ApiError(
            ErrorCode.SOURCE_GROUP_RULES_INVALID, 422,
            "来源组解析规则必须是 JSON 对象："
            f"source_group_id={sg.id}，实际为 {type(parsed).__name__}",
            {"source_group_id": sg.id, "actual_type": type(parsed).__name__},
        )
    return parsed


def get_group_config(db: Session, key: str) -> Optional[dict]:
    """来源组解析配置：asset_prefix + 关联账户 id 列表 + 可选解析 rules。

    优先库表 SourceGroup（读其关联账户与 rules JSON）；未落库退回 legacy
    SOURCE_GROUPS（账户为空、无 rules）。返回 None 表示未知来源组。

    rules 坏 JSON / 顶层非对象 → **抛 422**（不再静默降级为 None，见 `_parse_group_rules`）。
    调用方如需宽容解析（例如只读诊断脚本），不要在此加开关，直接在脚本侧自行读列。

    **`rules` 是「列 + 表」的合并结果**（spec D9）：0031 把教学规则搬到
    `source_group_learned_rules` 后，只读列会让调用方静默丢掉表内规则。合并实现
    唯一（`source_group_rules_service._merged_rules`），此处只做转发；因该模块
    `import screenshot_service`，反向依赖用**函数内延迟 import**（先例：
    `classification_service` → `asset_owner_service.append_change`），
    **不得为此再长第三处合并**。空配置仍返回 `None`，与旧契约一致。
    """
    sg = get_source_group(db, key)
    if sg:
        # 延迟 import：source_group_rules_service 顶层已 import 本模块，顶部反向 import 会成环
        from services.source_group_rules_service import rules_for_parsing
        return {"asset_prefix": sg.asset_prefix, "account_ids": [i.id for i in sg.institutions],
                "rules": rules_for_parsing(db, sg) or None}
    legacy = SOURCE_GROUPS.get(key)
    if legacy:
        return {"asset_prefix": legacy["asset_prefix"], "account_ids": [], "rules": None}
    return None


def recommend_source_group(db: Session, filename: str) -> Optional[str]:
    """文件名内嵌 App 包名 → 来源组显示名；无匹配返回 None。

    用于文件名自动推荐来源组（spec 用户故事 7）。包名命中后按 label 反查
    库表；若组未落库仍返回 label，供上层引导创建。
    """
    if not filename:
        return None
    for pkg, label in APP_PACKAGE_SOURCE_GROUPS.items():
        if pkg in filename:
            sg = get_source_group(db, label)
            return (sg.slug or label) if sg else label
    return None


# ---------------- 数据结构 ----------------
@dataclass
class ParsedItem:
    name: str
    amount: Decimal


@dataclass
class ParsedScreenshot:
    items: list[ParsedItem] = field(default_factory=list)
    total: Optional[Decimal] = None       # 投顾总金额（对账锚点）
    date: Optional[str] = None            # 数据日期 YYYY-MM-DD
    raw_blocks: list = field(default_factory=list)
    # 命中日期那条行的拼接原文（_row_join 形态），供 preview 展示「原文：…」
    # 让用户判断是否误报；与 date 同源派生（缓存命中/miss 两路一致）。
    date_text: Optional[str] = None


def apply_correction_rules(items: list[ParsedItem], rules: Optional[dict]) -> list[ParsedItem]:
    """校正层（Seam D）：把来源组 rules 的 rename[] / ignore[] 套到解析产物上。

    - ignore：item.name 含任一 match 子串 → 丢弃该行。条目兼容两种形态：
      "子串"（旧）或 {"match": "子串", "note": "可选备注"}（新）。note 仅供
      人看，绝不参与匹配；两种形态的空 match 照旧跳过（防 '' in name 恒真）。
    - rename（[{match, replace, note?}] 按序）：item.name 含 match →
      name.replace(match, replace)。note 为可选备注，只读 match/replace，
      天然不参与。
    - rules 为 None / 缺这两键 → 原样返回（幂等）。
    纯逻辑、无 IO。只做名称层校正，不动 organize_blocks 核心组织算法。
    """
    if not isinstance(rules, dict):
        return items
    ignore = rules.get("ignore") or []
    rename = rules.get("rename") or []
    if not ignore and not rename:
        return items
    result: list[ParsedItem] = []
    for it in items:
        name = it.name
        # ignore：子串命中 → 丢弃该行（忽略空 match，避免 '' in name 恒真；
        # dict 形态只读 match 键，note 绝不参与匹配）
        dropped = False
        for rule in ignore:
            sub = rule.get("match") if isinstance(rule, dict) else rule
            if sub and sub in name:
                dropped = True
                break
        if dropped:
            continue
        # rename：按序替换（前一条输出的结果可被后续规则继续改写）
        for rule in rename:
            if not isinstance(rule, dict):
                continue
            match = rule.get("match")
            repl = rule.get("replace")
            if match and repl is not None and match in name:
                name = name.replace(match, repl)
        if name == it.name:
            result.append(it)
        else:
            result.append(replace(it, name=name))
    return result


@dataclass
class PreviewRow:
    source_name: str
    source_amount: Decimal
    action: str                           # update | create | zero | keep
    asset_name: Optional[str] = None
    current_amount: Optional[Decimal] = None
    diff: Optional[Decimal] = None
    selected: bool = True
    name_overridden: bool = False         # 用户显式覆盖名称（最终 asset_name 直接采用，不再拼 prefix）
    side: Optional[str] = None            # 补录行显式指定 asset/liability；OCR 行恒 None
                                          # （写入时 create 走历史继承、update 不动存量列）
    index: Optional[int] = None           # 稳定行号（compute_preview 落定；采纳时
                                          # identity_decisions 的键，见 apply_preview）
    identity_guess: Optional[dict] = None  # 身份裁决提示（D4，见 _identity_candidates）；
                                          # 仅 create 行可能非空，绝不自动改写任何决策
    item_id: Optional[int] = None         # 命中既有名称时所属资产项（attach_item_ids 回填）；
                                          # create 行恒 None。前端据此做「同一项只计一次」去重
    aggregate_hint: bool = False          # 疑似「汇总行」（ADR-0029 D7）：命中内置汇总词表或
                                          # 该组 aggregate 规则 → 前端**预勾可取消**。纯提示，
                                          # 后端绝不据此自动跳过任何行（动作仍由人给）
    archived_merged_into: Optional[dict] = None
    # 归档项提示（spec `.scratch/archived-ingest-scope` D3）：**仅 create 行**可能非空。
    # 形状 {"item_id": int, "display_name": str, "archived_effective_from": str|None}
    # ＝ OCR 名命中了一个已归档项、且该项在 `lineage_links` 上有 merge 去向时，
    # 这里给「历史上已并入 <display_name>」（item_id/display_name 取 **merge 目标项**；
    # archived_effective_from 取被命中**归档项**的归档生效日，无则 None）。
    # 由 `annotate_archived_merge_hints` 只读填充，绝不改写任何决策、绝不写血缘。


@dataclass
class ScreenshotPreview:
    date: Optional[str] = None
    total_screenshot: Optional[Decimal] = None
    total_current_system: Optional[Decimal] = None
    rows: list[PreviewRow] = field(default_factory=list)
    # 来源组现有资产项的 item 级现值（ADDITIVE）：前端算「绑定行的差额基线」与
    # 「系统合计去重」用；由 attach_item_ids 填充，apply 路径不用。
    existing_items: list[dict] = field(default_factory=list)


# ---------------- 工具 ----------------
def _to_decimal(s: str) -> Optional[Decimal]:
    t = s.replace(",", "").strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


_STRICT_AMOUNT = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)\.\d{2}$")


# 名称归一 / 期号判定 / 模糊匹配真源已搬到 services/name_norm.py（单点收口）。
# 本模块顶部 import 按原名重导出，既有引用（测试与 scripts）零改动。
def _amount_from_cell(text: str) -> Optional[Decimal]:
    """单元格文本若为严格金额（千分位+两位小数）则返回 Decimal，否则 None。"""
    t = text.strip()
    if _STRICT_AMOUNT.match(t):
        return _to_decimal(t)
    return None


def _is_excluded(exclude: Optional[list], name: str) -> bool:
    """「不录入」判定：exclude 条目兼容 "资产全名"（旧式字符串）与
    {"match": "资产全名", "note": 可选备注} 两形态；note 绝不参与匹配；空 match 跳过。

    命中口径（spec `.scratch/multicolumn-signature` D6）：
    - 全名精确相等；或
    - name 以 match 开头，且 match 之后紧跟的字符**不是字母/数字**（含 CJK）——
      即 match 在 name 里是一个完整段。判据用 Unicode 字符类而非硬编码分隔符集合，
      因此不需要新增配置面，也不会漏掉历史库里各种分隔写法。
      覆盖尾号/期号变体（`示例银行-示例数字钱包` 命中 `示例银行-示例数字钱包-尾号0000`、
      `示例银行-存款` 命中 `示例银行-存款 3年期`），同时保证连写的更长名不命中
      （`示例银行-沪深300` 不误伤 `示例银行-沪深300ETF`、`-存款` 不误伤 `-存款证明`）。
      文中名称均为合成示例（公开仓库红线：真实持仓与真实数值不入库）。
    """
    for e in exclude or []:
        m = e.get("match") if isinstance(e, dict) else e
        if not m:
            continue
        if m == name:
            return True
        if name.startswith(m) and not name[len(m)].isalnum():
            return True
    return False


def compute_preview(parsed_items: list[ParsedItem],
                     existing_items: list[tuple[str, Decimal]],
                     group_prefix: str,
                     unrecognized: str = "keep",
                     exclude: Optional[list] = None,
                     ) -> ScreenshotPreview:
    """解析行 + 来源组现有资产项 → 待录入清单（动作 update/create/zero/keep）。

    规则：带期号识别项（尾部如 '20260819期'）独立成各自资产——仅同期号资产
    才 update，无期号/异期号一律 create（资产名 = 前缀 + OCR 名），无期号普通项
    沿用模糊同名规则 update；同一资产多条同名重复行仍取和写一条；截图有系统
    无→新增；组前缀不含的（汇总项）不参与。

    unrecognized：未识别项（系统有、本张截图无）策略三态：
      - "keep"（默认，安全）：不生成 zero，只生成 action="keep" 行
        （保持原值；前端可展示并可单独勾选经 apply_user_edits 转 zero）
      - "all"：全部清零，生成 action="zero" 行
    exclude（来源组 rules["exclude"]，「不录入」资产全名列表，两种形态见
    _is_excluded）：仅从 keep 行生成中过滤命中项（"all" 策略的 zero 行不受
    影响）。红线：绝不参与 known_names 候选集——被排除项若再次出现在截图
    里，仍正常命中为 update 行而非 create 重复行。total_current_system 锚点
    维持含排除项的原口径（一次性写 0 后金额为 0，锚点自然收敛，无需特殊
    处理；改锚点口径会破坏既有对账语义）。

    归档项（spec `.scratch/archived-ingest-scope` D2/D3/D6）：`existing_items` 由
    `get_group_existing` 给出、已剔除 `status='archived'` 的项，故 `known_names`
    （→ update 行）、未识别项 keep/zero 行与 `total_current_system` 锚点三处同源收敛，
    本函数**不做**第二次 status 判定（那是两处口径分叉的开始）。OCR 命中已归档名因此
    走 create 行；「历史上已并入 X」由 `annotate_archived_merge_hints` 另挂只读提示。
    """
    group_existing = {n: a for n, a in existing_items if n.startswith(group_prefix)}
    known_names = [n[len(group_prefix):] for n in group_existing]

    # 每解析行 → 命中的系统资产项（None=新增）；判定搬迁至 match_existing（零变化）
    matched: list[tuple[ParsedItem, Optional[str]]] = []
    used: set[str] = set()
    for item in parsed_items:
        m = match_existing(item.name, known_names)
        full = group_prefix + m if m else None
        if full:
            used.add(full)
        matched.append((item, full))

    # 多期命中同一项 → 取和
    sums: dict[str, Decimal] = {}
    for item, full in matched:
        if full:
            sums[full] = sums.get(full, Decimal("0")) + item.amount

    rows: list[PreviewRow] = []
    seen: set[str] = set()
    for item, full in matched:
        if full and full not in seen:
            seen.add(full)
            cur = group_existing.get(full, Decimal("0"))
            amt = sums[full]
            rows.append(PreviewRow(source_name=item.name, source_amount=amt,
                                   action="update", asset_name=full,
                                   current_amount=cur, diff=amt - cur))
        elif not full:
            # create：新增资产无既有金额，current=0，diff = 截图金额（与前端一致）；
            # asset_name 预填最终落库名（前缀 + OCR 名），与 apply 写入时一致。
            rows.append(PreviewRow(source_name=item.name, source_amount=item.amount,
                                   action="create", asset_name=group_prefix + item.name,
                                   current_amount=Decimal("0"), diff=item.amount))

    # 未识别项（组内未被任何解析行命中）：按策略生成 keep（保持原值）或 zero（清零）
    for name, amt in sorted(group_existing.items()):
        if name not in used:
            if unrecognized == "all":
                rows.append(PreviewRow(source_name="", source_amount=Decimal("0"),
                                       action="zero", asset_name=name,
                                       current_amount=amt, diff=Decimal("0") - amt))
            else:
                if _is_excluded(exclude, name):
                    continue  # 「不录入」资产：不再出现在 keep 列表（仅此处过滤，
                              # 上方 known_names/update/create/zero 路径均不受影响）
                rows.append(PreviewRow(source_name="", source_amount=Decimal("0"),
                                       action="keep", asset_name=name,
                                       current_amount=amt, diff=Decimal("0")))

    total_current = sum(group_existing.values(), Decimal("0"))
    for i, r in enumerate(rows):
        r.index = i          # 稳定行号：预览响应 index / 采纳 identity_decisions 的键
    return ScreenshotPreview(total_current_system=total_current, rows=rows)


# ---------------- 身份裁决提示（item-identity-drift D4：新 item 首现须人表态） ----------------
# 定位：`_upsert_asset` 走 `get_or_create_item`，名字未命中即静默新建资产项；若这实际是
# 一次改名/拆分，旧项不会归零 → as-of 沿用与新项双算一期（2026-09-16 事故）。本节的
# 函数只产出**提示**，真正裁决由人在采纳时给出（`identity_decisions`），登记血缘走
# 既有的 `lineage_service.register_lineage`（归档 + 口径承接 + 审计能力齐备）。
IDENTITY_GUESS_REASON = "same_account_new_name"


def _org_prefix(name: str) -> str:
    """机构前缀＝第一个 '-' 之前的文本（'示例银行-贵金属-黄金' → '示例银行'）。"""
    return (name or "").split("-", 1)[0].strip()


def _name_suffix(name: str) -> str:
    """第一个 '-' 之后的文本；无 '-' → ''。"""
    _, _, tail = (name or "").partition("-")
    return tail.strip()


def _institution_id_by_name(db: Session, name: str) -> Optional[int]:
    """机构名 → institutions.id；无同名机构返回 None。"""
    if not name:
        return None
    return db.execute(
        select(Institution.id).where(Institution.name == name)
    ).scalar_one_or_none()


def _channel_id_by_name(db: Session, name: str) -> Optional[int]:
    if not name:
        return None
    return db.execute(
        select(Channel.id).where(Channel.name == name)
    ).scalar_one_or_none()


def _active_items(db: Session, institution_id: Optional[int] = None) -> list[tuple]:
    """机构维度 active 资产项的「最后一条快照」视图。

    返回 [(item_id, display_name, amount, last_date, side)]。ADR-0019 把账户维度
    收敛为 `asset_items.institution_id`，所以「同账户」＝同机构的项；机构未定
    （draft 或机构名未进字典）时退回全量，再由调用侧的机构前缀过滤收窄。
    """
    latest = (
        select(Asset.asset_item_id, func.max(Asset.asset_date).label("max_date"))
        .where(Asset.superseded_by.is_(None), Asset.asset_item_id.isnot(None))
        .group_by(Asset.asset_item_id)
        .subquery()
    )
    q = (
        select(AssetItem.id, AssetItem.display_name, Asset.amount,
               Asset.asset_date, Asset.side)
        .join(latest, latest.c.asset_item_id == AssetItem.id)
        .join(Asset, and_(Asset.asset_item_id == AssetItem.id,
                          Asset.asset_date == latest.c.max_date,
                          Asset.superseded_by.is_(None)))
        .where(AssetItem.status != "archived")
    )
    if institution_id is not None:
        q = q.where(AssetItem.institution_id == institution_id)
    return [tuple(r) for r in db.execute(q).all()]


def _identity_candidates(db: Session, name: str,
                         account_ids: Optional[Sequence[int]]) -> Optional[dict]:
    """新名 name 的同账户「疑似同源旧项」提示；无提示 → None（**hint，绝不自动裁决**）。

    命中条件（全部成立）：
      (a) name 解析不到任何既有资产项（`resolve_item_id`，含 legacy/历史名）——
          即采纳会真的新建一项；
      (b) 机构维度内至少一项 **active** 资产项与其**同机构前缀**
          （第一个 '-' 之前的文本相同）。

    在 (b) 之上两条择一：
      1. 名称后缀互相包含（'贵金属-黄金' vs '黄金账户' 不成立，故需兜底）；
      2. **兜底**：同机构 + 同 side（OCR 行 side 恒 None → 按写入缺省 'asset'）+
          该候选是机构内同侧的最大持仓 —— 覆盖「改名后字面已无交集」的真实改名。

    返回 ``{"reason": "same_account_new_name", "candidates": [...]}``；候选按金额降序
    （大额优先让人先看），金额/日期取候选最后一条快照。account_ids 为空（来源组未挂
    账户／未绑机构）→ None：无账户维度可比，宁可不猜。
    """
    if not name or not account_ids:
        return None
    if resolve_item_id(db, name) is not None:
        return None
    head = _org_prefix(name)
    if not head:
        return None
    same_org = [r for r in _active_items(db, _institution_id_by_name(db, head))
                if r[1] and _org_prefix(r[1]) == head and r[1] != name]
    if not same_org:
        return None

    tail = _name_suffix(name)
    hits = [r for r in same_org
            if tail and _name_suffix(r[1])
            and (tail in _name_suffix(r[1]) or _name_suffix(r[1]) in tail)]
    if not hits:
        same_side = [r for r in same_org if (r[4] or "asset") == "asset"]
        if same_side:
            hits = [max(same_side, key=lambda r: (Decimal(str(r[2])), -r[0]))]
    if not hits:
        return None

    hits.sort(key=lambda r: (-Decimal(str(r[2])), r[0]))
    return {
        "reason": IDENTITY_GUESS_REASON,
        "candidates": [{"item_id": r[0], "name": r[1],
                        "amount": str(Decimal(str(r[2])).quantize(Decimal("0.01"))),
                        "last_date": r[3].isoformat() if r[3] else None}
                       for r in hits],
    }


def annotate_identity_guesses(db: Session, preview: ScreenshotPreview,
                              account_ids: Optional[Sequence[int]]) -> ScreenshotPreview:
    """给「会新建资产项」的预览行挂 `identity_guess`（只读 + 行替换，不改任何决策）。

    只标注 action="create" 的行：update/zero/keep 已解析到既有项，不存在身份歧义。
    无候选的行保持 None（不发明歧义）。account_ids 只作「来源组挂过账户/机构」的门槛
    （ADR-0019：账户维度＝机构，同源口径由 `asset_items.institution_id` 承担）。
    """
    if not account_ids:
        return preview
    rows: list[PreviewRow] = []
    for r in preview.rows:
        guess = (_identity_candidates(db, r.asset_name, account_ids)
                 if r.action == "create" and r.asset_name else None)
        rows.append(replace(r, identity_guess=guess) if guess is not None else r)
    return replace(preview, rows=rows)


# ---------------- 归档项提示（spec `.scratch/archived-ingest-scope` D3） ----------------
def _archived_merge_target(db: Session, name: Optional[str]) -> Optional[dict]:
    """name 命中的**已归档**项在血缘上的 merge 去向 → 提示 dict；无则 None。

    命中条件（全部成立）：
      (a) `name` 解析到既有资产项（`resolve_item_id`，含 legacy/历史名与别名）；
      (b) 该项 `status == 'archived'`（在用项没有「已并入」可言）；
      (c) `lineage_links` 上存在 `link_type='merge'` 且 `from_item_id` 为本项的行
          （`from`＝旧项、`to`＝后继，见 `models.LineageLink`）。只认 merge：
          split/revision 不是「并入」，给出去会误导用户。

    返回 `{"item_id": merge 目标项 id, "display_name": 目标项现名,
    "archived_effective_from": 被命中归档项的归档生效日 ISO 串或 None}`。
    **只读**：本函数及调用方不发任何写语句——血缘 append-only（拍板 8），
    这里只是把既有事实读给用户看（D3：让用户知道这笔该落到哪）。
    """
    if not name:
        return None
    item_id = resolve_item_id(db, name)
    if item_id is None:
        return None
    item = db.get(AssetItem, item_id)
    if item is None or item.status != "archived":
        return None
    to_item_id = db.execute(
        select(LineageLink.to_item_id)
        .where(LineageLink.from_item_id == item.id,
               LineageLink.link_type == "merge")
        .order_by(LineageLink.id)
        .limit(1)
    ).scalars().first()
    if to_item_id is None:
        return None
    target = db.get(AssetItem, to_item_id)
    if target is None:
        return None
    return {
        "item_id": target.id,
        "display_name": target.display_name,
        "archived_effective_from": (item.archived_effective_from.isoformat()
                                    if item.archived_effective_from else None),
    }


def annotate_archived_merge_hints(db: Session,
                                  preview: ScreenshotPreview) -> ScreenshotPreview:
    """给「命中已归档项」的 create 行挂 `archived_merged_into`（只读，不改任何决策）。

    只标注 `action="create"` 的行：update/zero/keep 已解析到在用项，不存在「历史已并入」
    的歧义。没有 merge 血缘、或名字压根没命中归档项 → 保持 None（不发明提示）。

    典型来源：OCR 再次读到已归档项的名字 → `known_names` 已剔除该项（D3）→ 该行是
    create；此处补一条「历史上已并入 X」让人自己决定这笔落到哪。
    """
    rows: list[PreviewRow] = []
    for r in preview.rows:
        hint = (_archived_merge_target(db, r.asset_name)
                if r.action == "create" and r.asset_name else None)
        rows.append(replace(r, archived_merged_into=hint) if hint is not None else r)
    return replace(preview, rows=rows)


def apply_user_edits(preview: ScreenshotPreview, skipped: Optional[list[int]] = None,
                     amounts: Optional[dict] = None,
                     zero_names: Optional[list[str]] = None,
                     names: Optional[dict[int, str]] = None,
                     reconciles: Optional[list[int]] = None) -> ScreenshotPreview:
    """应用前端用户编辑：按行号跳过（勾选）/ 覆盖金额（改数）/ 未识别项单独清零 / 覆盖名称。

    未识别项策略：zero_names 列出的资产项由 keep（保持原值）翻转成 zero（清零），
    实现「单独清0」；不列出的 keep 项保持原值。金额覆盖后同步重算 diff；
    names 按行号直接覆盖最终 asset_name（create 行写 source_name 并标记
    name_overridden，apply_preview 落库时不再拼 prefix；update/zero 行写
    asset_name）；行号越界仅跳过。保持 ScreenshotPreview 其余字段原样。

    `reconciles`（ADR-0029 D5/D6，**additive**）：行级「汇总行」标记，与 `skipped`
    **同形**（预览行号，0 起）。本函数只做**互斥校验**（同一行既跳过又标汇总 →
    400 `RECONCILE_CONFLICT`：跳过＝这次不处理，汇总＝参与核对但不落账，语义不能
    并存，票 03 D6），**不改任何行**——真正生效点在 `apply_preview` 的写入循环之前
    （命中行零写入、从清零候选剔除）。行号越界与 skipped 同口径：忽略。
    """
    skipped_set = set(skipped or [])
    reconcile_set = set(reconciles or [])
    conflict = sorted(skipped_set & reconcile_set)
    if conflict:
        raise ApiError(
            ErrorCode.RECONCILE_CONFLICT, 400,
            "同一行不能既跳过又标为汇总行（跳过＝这次不处理，汇总＝参与核对但不落账）",
            {"rows": conflict,
             "message": "请二选一：取消勾选，或去掉「汇总行」标记"})
    amounts = {int(k): v for k, v in (amounts or {}).items()}
    zero_set = set(zero_names or [])
    names = {int(k): v for k, v in (names or {}).items()}
    rows: list[PreviewRow] = []
    for i, r in enumerate(preview.rows):
        if i in skipped_set:
            continue
        if r.action == "keep" and r.asset_name in zero_set:
            r = replace(r, action="zero", diff=Decimal("0") - (r.current_amount or Decimal("0")))
        elif i in amounts:
            d = _to_decimal(str(amounts[i]))
            if d is None:
                raise ValueError(f"行 {i + 1} 金额无效: {amounts[i]!r}")
            r = replace(r, source_amount=d, diff=d - (r.current_amount or Decimal("0")))
        if i in names:
            if r.action == "create":
                r = replace(r, source_name=names[i], name_overridden=True)
            else:
                r = replace(r, asset_name=names[i], name_overridden=True)
        rows.append(r)
    return replace(preview, rows=rows)


def count_corrected_rows(before_rows, after_rows) -> int:
    """「人改过的行数」——识别快照 `adopted.corrected_rows` 的唯一口径（ADR-0023 D7）。

    识别率 = `recognized`（机器给的）与 `adopted`（人落账的）之差，所以「改过」
    按**行**数而不是按编辑类型计（同一行既改金额又改名仍算 1 行）：

    - `before_rows`：管线原始预览行（`compute_preview` + 身份提示之后、**未经**
      `apply_user_edits` / `apply_supplements`）；
    - `after_rows`：最终行（`apply_user_edits` + `apply_supplements` 之后，即
      `apply_preview` 收到的 `preview.rows`）。

    计入三类（互不重叠，故不会双计）：
      1. 被改的行：`(action, source_name, source_amount, asset_name)` 任一变化
         （改数 / 改名 / 单独清0 —— 清0 是 keep→zero 的动作翻转）；
      2. 被删的行：在 `before_rows` 里、`after_rows` 里没有（前端取消勾选）；
      3. 补录新增的行：`index is None` 且不在 `before_rows`（人手工加的行）。

    `keep` 行原样不动 → 不计。取行号（`PreviewRow.index`）对位：`apply_user_edits`
    用 `replace` 逐行保号，`apply_supplements` 追加的行 `index` 为 None，天然
    「未配对」= 新增。纯函数、无 IO，两侧可为任意可迭代的 `PreviewRow`。
    """
    before = {r.index: r for r in before_rows or [] if r.index is not None}
    seen: set = set()
    changed = 0
    for r in after_rows or []:
        prev = before.get(r.index) if r.index is not None else None
        if r.index is not None:
            seen.add(r.index)
        if prev is None:
            changed += 1        # 补录新增（无行号或行号不在原始清单里）
        elif (prev.action, prev.source_name, prev.source_amount,
              prev.asset_name) != (r.action, r.source_name, r.source_amount,
                                   r.asset_name):
            changed += 1        # 改数 / 改名 / keep→zero
    changed += sum(1 for i in before if i not in seen)   # 被取消勾选的行
    return changed


# ---------------- 补录（supplements）：apply_user_edits 之后的合成通道 ----------------
# 刻意去「AI」化命名：写入路径不识别「AI」概念，补录来源（verify 的 ai_only
# 单边项等）只存在于前端草稿；后端只见 name/amount/side。


@dataclass
class SupplementItem:
    """一条已校验的补录项（parse_supplements 产物）。side 默认 "asset"。"""
    name: str
    amount: Decimal
    side: str = "asset"


@dataclass
class SupplementDrop:
    """被丢弃的补录项。reason ∈ conflict_update|conflict_create|conflict_zero|
    duplicate_target|invalid（invalid 仅在 parse 层抛 ValueError，不进 drop）。"""
    name: str
    reason: str
    asset_name: Optional[str] = None


@dataclass
class SupplementOutcome:
    """apply_supplements 结果：合成后的预览 + 写入行 / 丢弃行（均按 payload 顺序）。"""
    preview: ScreenshotPreview
    written: list[PreviewRow] = field(default_factory=list)
    dropped: list[SupplementDrop] = field(default_factory=list)


_SUPPLEMENT_MAX = 200
_SUPPLEMENT_NAME_MAX = 100
_SUPPLEMENT_AMOUNT_MAX = Decimal("1e13")   # Numeric(15,2) 上限护栏
_SUPPLEMENT_SIDES = ("asset", "liability")


def parse_supplements(raw) -> list[SupplementItem]:
    """options["supplements"] 原始 JSON → 校验后的 SupplementItem 列表。

    任何非法 → ValueError（router 转 400，中文消息带序号/名字）；缺省/None/[]
    → []（与今天逐字段等价）。name strip 后非空且 ≤100 字符；amount 复用
    _to_decimal（与 amounts 编辑通道同源），必须是有限数（拒 NaN/sNaN/±Infinity
    ——Decimal 比较对 NaN 静默 False，不设防会漏过范围校验）、>0 且 <1e13
    （≤0 的消息指路「未识别项 → 单独清0」）；side 可选，缺省 "asset"，
    白名单 {asset, liability}；条目为 dict、未知键忽略；总条数 ≤200。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("supplements 必须为数组")
    if len(raw) > _SUPPLEMENT_MAX:
        raise ValueError(f"补录项数量超限（最多 {_SUPPLEMENT_MAX} 条）")
    out: list[SupplementItem] = []
    for i, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {i} 条补录项必须为对象")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {i} 条补录项名称不能为空")
        name = name.strip()
        if len(name) > _SUPPLEMENT_NAME_MAX:
            raise ValueError(f"第 {i} 条补录项「{name[:20]}…」名称超长"
                             f"（最多 {_SUPPLEMENT_NAME_MAX} 字符）")
        amount = _to_decimal(str(entry.get("amount")))
        if amount is None or not amount.is_finite():
            # 非有限数必须先挡：NaN 与 0/1e13 的比较均为 False（不抛错），
            # sNaN 参与比较还会抛 InvalidOperation 逃逸成 500
            raise ValueError(f"第 {i} 条补录项「{name}」金额无效")
        if amount <= 0:
            raise ValueError(f"第 {i} 条补录项「{name}」金额必须大于 0"
                             "（清零请用：未识别项 → 单独清0）")
        if amount >= _SUPPLEMENT_AMOUNT_MAX:
            raise ValueError(f"第 {i} 条补录项「{name}」金额超出上限")
        side = entry.get("side")
        if side is None:
            side = "asset"
        elif side not in _SUPPLEMENT_SIDES:
            raise ValueError(f"第 {i} 条补录项「{name}」side 必须为 asset 或 liability")
        out.append(SupplementItem(name=name, amount=amount, side=side))
    return out


def _in_use_existing(existing_items) -> dict[str, Decimal]:
    """`existing_items` → {名称: 金额}，**剔除归档项**（spec D1/D3 的单一判据）。

    条目兼容两种形状：
      - `(name, amount)`：历史调用方/`get_group_existing` 产物。没有 status 可判，
        按**在用**处理（`get_group_existing` 已在 `_group_latest_rows` 处剔过归档项，
        这里再判一次反而是第二处口径）。
      - `(name, amount, status)`：带状态的调用方。`status == 'archived'` 的条目被剔除
        ——归档名因此不进 `known_names`（→ 补录命中它只能判 create，D3），也不做
        update 的既有金额基线（否则钱写进归档项、在合计里消失）。

    其余 status（`active` / `draft` / None 等）一律算在用：`draft` 是可计入净值的
    在册项（ADR-0019/0020），排除它会造出重复项。
    """
    out: dict[str, Decimal] = {}
    for entry in existing_items or ():
        if not entry:
            continue
        name, amount = entry[0], entry[1]
        status = entry[2] if len(entry) > 2 else None
        if status == "archived":
            continue
        out[name] = amount
    return out


def _supplement_target(sup: SupplementItem, group_prefix: str,
                       known_names: list[str]) -> tuple[str, Optional[str]]:
    """补录项 → (final_name, 命中的不带前缀已知名或 None)。

    name 已带组前缀则先剥掉（防双拼；known_names 不带前缀），再走
    match_existing（与 compute_preview 同一真源，含期号规则）。
    """
    bare = sup.name
    if bare.startswith(group_prefix):
        bare = bare[len(group_prefix):]
    target = match_existing(bare, known_names) if known_names else None
    return group_prefix + (target or bare), target


def apply_supplements(preview: ScreenshotPreview,
                      existing_items: list[tuple[str, Decimal]],
                      group_prefix: str,
                      supplements: Optional[list[SupplementItem]],
                      ) -> SupplementOutcome:
    """补录项合成进（已应用用户编辑的）预览清单——纯函数、无 IO、不抛业务异常。

    原则：可替换 keep（非动作）行，绝不与「已决定要写」的行（update/create/zero）
    并存 → drop + 报告；永不取和、永不覆盖。逐项：
      - 目标已被本批更早补录占用 → drop(duplicate_target)（first-wins，不取和）
      - 命中 preview 中 update/create/zero 行 → drop(conflict_*)
      - 命中 keep 行 → 原地 replace 转 update（行数/位置不变）
      - 无 → 追加末尾（命中已知名 → update，current 取 existing；否则 create，
        current=0、diff=amount）
    supplements 为 None/[] → preview 逐字段原样返回（向后兼容硬要求）。
    written/dropped 顺序 == payload 顺序。

    归档项（spec D1/D3）：`existing_items` 里 `status='archived'` 的条目被剔除
    （见 `_in_use_existing`）→ 补录命中归档名判 **create** 而非 update，与
    `compute_preview` 同口径。命中 create 后写入侧仍由 `_upsert_asset` 兜底拒绝
    （D4：预览与 apply 之间库会变）。
    """
    if not supplements:
        return SupplementOutcome(preview=preview)
    group_existing = {n: a for n, a in _in_use_existing(existing_items).items()
                      if n.startswith(group_prefix)}
    known_names = [n[len(group_prefix):] for n in group_existing]
    rows = list(preview.rows)
    written: list[PreviewRow] = []
    dropped: list[SupplementDrop] = []
    claimed: set[str] = set()   # 本批更早补录已占用的 final_name
    for sup in supplements:
        final_name, target = _supplement_target(sup, group_prefix, known_names)
        if final_name in claimed:
            dropped.append(SupplementDrop(name=sup.name,
                                          reason="duplicate_target",
                                          asset_name=final_name))
            continue
        hit_idx = next((i for i, r in enumerate(rows)
                        if r.asset_name == final_name), None)
        if hit_idx is not None:
            act = rows[hit_idx].action
            if act == "keep":
                cur = rows[hit_idx].current_amount or Decimal("0")
                new_row = replace(
                    rows[hit_idx], source_name=sup.name, source_amount=sup.amount,
                    action="update", diff=sup.amount - cur, side=sup.side,
                    selected=True)
                rows[hit_idx] = new_row
                written.append(new_row)
                claimed.add(final_name)
                continue
            # update/create/zero：均已决定要写，并存即双计/互相覆盖 → drop 报告。
            # （zero 在末尾，若允许追加 update 会被 last-write-wins 静默吞掉，
            #   故必须显式 drop。）update/create 同理防 _upsert_asset 同日互覆。
            dropped.append(SupplementDrop(name=sup.name,
                                          reason=f"conflict_{act}",
                                          asset_name=final_name))
            continue
        cur = group_existing.get(final_name, Decimal("0"))
        bare = sup.name[len(group_prefix):] if sup.name.startswith(group_prefix) \
            else sup.name
        if target is not None:
            new_row = PreviewRow(source_name=target, source_amount=sup.amount,
                                 action="update", asset_name=final_name,
                                 current_amount=cur, diff=sup.amount - cur,
                                 side=sup.side)
        else:
            # create：与 compute_preview 的 create 行同构（asset_name 预填最终名，
            # apply_preview 写入时 prefix + source_name = final_name）
            new_row = PreviewRow(source_name=bare, source_amount=sup.amount,
                                 action="create", asset_name=final_name,
                                 current_amount=Decimal("0"), diff=sup.amount,
                                 side=sup.side)
        rows.append(new_row)
        written.append(new_row)
        claimed.add(final_name)
    return SupplementOutcome(preview=replace(preview, rows=rows),
                             written=written, dropped=dropped)


def resolve_supplements(existing_items: list[tuple[str, Decimal]],
                        group_prefix: str,
                        supplements: list[SupplementItem]) -> list[dict]:
    """补录草稿 → 逐项权威预判（resolve 端点用；只读、不跑 OCR、不落库）。

    与 apply_supplements 同一目标判定真源（_supplement_target → match_existing）；
    无 OCR 预览可对照，冲突仅可能来自批内互撞（duplicate_target）。
    金额以字符串回传（与全项目 JSON 金额口径一致，避免 float 精度）。

    归档项（spec D1/D3）：同 `apply_supplements`——`existing_items` 中
    `status='archived'` 的条目被剔除（`_in_use_existing`），命中归档名判 create。
    """
    amounts = _in_use_existing(existing_items)
    known_names = [n[len(group_prefix):] for n in amounts
                   if n.startswith(group_prefix)]
    results: list[dict] = []
    claimed: set[str] = set()
    for sup in supplements:
        final_name, target = _supplement_target(sup, group_prefix, known_names)
        base = {"name": sup.name, "side": sup.side}
        if final_name in claimed:
            results.append({**base, "final_name": final_name, "action": "conflict",
                            "asset_name": final_name, "current_amount": None,
                            "conflict_reason": "duplicate_target"})
        elif target is not None:
            results.append({**base, "final_name": final_name, "action": "update",
                            "asset_name": final_name,
                            "current_amount": str(amounts[final_name]),
                            "conflict_reason": None})
        else:
            results.append({**base, "final_name": final_name, "action": "create",
                            "asset_name": final_name,
                            "current_amount": None, "conflict_reason": None})
        claimed.add(final_name)
    return results


# ---------------- OCR 解析层（引擎实现见 services/ocr_engine.py） ----------------
def _get_engine():
    """旧 PaddleOCR pipeline 引擎（兼容历史调用/测试 monkeypatch；实现在 ocr_engine）。"""
    from services import ocr_engine
    return ocr_engine.get_pipeline_engine()


def _ocr_blocks(image_path: str, chunk: int = 1500, overlap: int = 100,
                progress_cb=None) -> list[tuple[int, int, str, float]]:
    """分块 OCR → [(abs_y, abs_x, text, score)]，已按 y/x 排序。

    薄委托 ocr_engine.run_ocr（OCR_ENGINE 选 lowlevel/pipeline 双路线；
    pipeline 路线行为与本函数历史实现一致）。CLI 脚本
    （scripts/screenshot_dump_blocks.py / screenshot_spike.py）继续直调。
    """
    from services import ocr_engine
    return ocr_engine.run_ocr(image_path, chunk=chunk, overlap=overlap,
                              progress_cb=progress_cb)


def _rows_from_blocks(blocks, gap: int = 40) -> list[list[tuple[int, int, str, float]]]:
    rows: list[list[tuple[int, int, str, float]]] = []
    for y, x, text, score in blocks:
        if rows and abs(y - rows[-1][0][0]) <= gap:
            rows[-1].append((y, x, text, score))
        else:
            rows.append([(y, x, text, score)])
    return rows


def raw_blocks_to_json(blocks) -> list[dict]:
    """OCR 原始块 → 可 JSON 序列化的块列表（ADR-0023 D7，`raw_blocks` 首次序列化）。

    形态 `[{"text": "…", "x": 0, "y": 0}]`——**文本 + 坐标**就是换规则离线重解析
    所需的全部原料（单张截图重跑 OCR 约 4 分钟，块留着才不必重跑）。

    `score` 刻意不入库：组织层判定（`_rows_from_blocks` 及各 `_organize_*`）只搬
    不读分数，存了纯放大体积（1 MiB 上限下这是实打实的代价），对「换规则重解析」
    也没有用处。

    容错：单个块形状/类型异常 → 跳过该块（识别快照是旁证，绝不因脏块阻断存证）。
    纯函数、无 IO；`blocks` 为空 / None → `[]`。
    """
    out: list[dict] = []
    for b in blocks or []:
        try:
            y, x, text = b[0], b[1], b[2]
        except (TypeError, IndexError):
            continue
        if not isinstance(text, str):
            continue
        try:
            out.append({"text": text, "x": int(x), "y": int(y)})
        except (TypeError, ValueError):
            continue
    return out


# ---------------- 通用解析器（Seam C：纯逻辑，进单测） ----------------
# 组织 OCR 文本块 → 结构化行 [{name, amount}]，不依赖单一来源锚点。
# 三分支：A 横排（左名右金 + 「>」闭合） / B 垂直层级（名称→标签→数值下行） / C 三列表头。

# 出厂词表（金额列标题/按钮词黑名单/汇总标签/名称排除/profit 区/行闭合符/噪声正则）
# 已搬到 services/parser_rules.py 的 BUILTIN_VOCABULARIES，供词表引擎做「出厂 ∪ 全局增量
# − 全局删除」的生效计算。本模块只保留上面的同名引用（见顶部 import）。
#
# 判定层一律从 `_ParserVocab` 快照取词，**不再直接读模块级常量**：
#   organize_blocks(blocks, rules, vocabularies) → _resolve_parser_vocab() → _ParserVocab
#   → 四个 _organize_* 分支 + 判定函数（_is_name_token / _is_total_label /
#     _is_profit_row / _is_noise_row / _amount_token / _cell_amount）。
# 每个判定函数的 `vocab` 参数默认 None → 回落 `_DEFAULT_VOCAB`（= 出厂常量），所以
# **不经 organize_blocks 的直调（老测试、脚本、spike）行为与重构前逐字节一致**。


@dataclass(frozen=True)
class _ParserVocab:
    """一次解析请求内的生效词表快照（organize_blocks 入口算一次，逐判定复用）。

    字段与 `parser_rules.BUILTIN_VOCABULARIES` 的 7 个 key 一一对应；默认值即出厂
    常量本身，因此 `_ParserVocab()` 与「重构前直接读模块常量」等价。
    """

    amount_headers: Sequence[str] = DEFAULT_AMOUNT_HEADERS
    ignore_words: Sequence[str] = DEFAULT_IGNORE_WORDS
    total_labels: Sequence[str] = _TOTAL_LABELS
    name_exclude: Sequence[str] = _NAME_EXCLUDE_WORDS
    profit_zone: Sequence[str] = _PROFIT_ZONE_LABELS
    # 原 `_ROW_CLOSE_TOKENS` 是含重复项的 set（"›" 写 3 次），此处用去重后的有序元组；
    # 判定只做 `in` 成员测试，行为一致。
    row_close_tokens: Sequence[str] = (">", "›", "≫", "→")
    noise_patterns: Sequence[str] = _NOISE_TOKEN_PATTERNS


_DEFAULT_VOCAB = _ParserVocab()


def _v(vocab: Optional[_ParserVocab]) -> _ParserVocab:
    """`vocab=None` → 出厂词表快照（向后兼容的唯一入口）。"""
    return _DEFAULT_VOCAB if vocab is None else vocab


def _as_word_list(value) -> tuple[str, ...]:
    """组级词表值的宽松归一：`str → (s,)`、序列去空去重保序、其它 → `()`。

    `source_groups.rules.amount_header` 在真实库里两种形态都有（组配置可能是单个
    字符串或字符串数组）；词表引擎的 `with_group_overrides` 只认序列，故在传参前
    归一。非字符串项直接跳过（脏配置，不是合法词）。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def _resolve_parser_vocab(vocabularies, rules) -> _ParserVocab:
    """（全局/调用方词表 + 组级 rules）→ 生效词表快照。

    组级覆盖是**替换**语义（spec D2.3）：
    - `amount_header` 交给 `with_group_overrides` 的替换编码（`remove=出厂全量 +
      add=组配置`），与现状 `rules.get("amount_header") or 出厂` 等价；
    - `ignore_words` 现状同为整串替换（`rules.get("ignore_words") or 出厂`），而
      `with_group_overrides` 目前对它是 add 语义，故在此本地替换（组里配了就只用语
      组里的词），守住 D7 行为等价；待 parser_rules 车道支持替换语义后改为透传。
    """
    base = EffectiveVocabularies() if vocabularies is None else vocabularies
    group_rules = rules or {}

    amount_header = _as_word_list(group_rules.get("amount_header"))
    eff = base.with_group_overrides(
        {"amount_header": amount_header} if amount_header else None)

    group_ignore_words = _as_word_list(group_rules.get("ignore_words"))
    return _ParserVocab(
        amount_headers=eff.get("amount_headers"),
        ignore_words=group_ignore_words or eff.get("ignore_words"),
        total_labels=eff.get("total_labels"),
        name_exclude=eff.get("name_exclude"),
        profit_zone=eff.get("profit_zone"),
        row_close_tokens=eff.get("row_close_tokens"),
        noise_patterns=eff.get("noise_patterns"),
    )


def _is_profit_row(row, vocab: Optional[_ParserVocab] = None) -> bool:
    """判断整行是否属 profit 区（盈亏/收益/轨迹标签行），不作为垂直分支触发依据。"""
    joined = _row_join(row)
    return any(lb in joined for lb in _v(vocab).profit_zone)


def _is_total_label(text: str, vocab: Optional[_ParserVocab] = None) -> bool:
    return any(lb in text for lb in _v(vocab).total_labels)


def _is_ignored(text: str, ignore_words: Sequence[str]) -> bool:
    return any(w in text for w in ignore_words)


@lru_cache(maxsize=8)
def _close_token_strip_re(tokens: tuple[str, ...]) -> "re.Pattern[str]":
    """尾部「行闭合符 + 空白」剥离正则（按当前词表的 row_close_tokens 生成）。

    出厂词 `(">", "›", "≫", "→")` 生成 `(?:\\s|>|›|≫|→)+$`，与原硬编码字符类
    `[\\s>›≫›→]+$` 等价（单字符，交替序不影响结果）。按词表生成后，组/全局词表
    新增的闭合符才会真的被剥离；空词表退化为纯空白剥离。
    """
    alts = [re.escape(t) for t in tokens if t]
    pattern = (r"(?:\s|" + "|".join(alts) + r")+$") if alts else r"\s+$"
    return re.compile(pattern)


def _amount_token(text: str, vocab: Optional[_ParserVocab] = None) -> str:
    """剥离金额常见前后缀（「元」/人民币符号/行闭合符/尾部空白）再交给严格金额判定。

    真实 OCR 常把行闭合符（`> `）拼到金额块尾部，如 `4,321.09 >`；若不剥掉，
    `_STRICT_AMOUNT` 整串匹配会失败，导致整个资产行被跳过（某银行 App 漏项真因）。
    """
    t = text.strip()
    t = t.rstrip("元￥¥").lstrip("￥¥")
    # 剥尾部行闭合符及其后空白：> › ≫ → 等（取值见词表 row_close_tokens）
    tokens = tuple(_v(vocab).row_close_tokens)
    if tokens:
        t = _close_token_strip_re(tokens).sub("", t).strip()
    return t


# 整数金额（无小数）：千分位或纯数字。真实截图卡片常无「.XX」，如 567,890 / 234560 / 567890。
# 必须接受「无逗号的 4+ 位纯数字」（567890）——旧正则 ^\d{1,3}(,\d{3})*$ 会拒绝它，
# 导致整数卡片金额被当作名称（某支付平台社保卡片「567890元」错配根因）。
_INT_AMOUNT = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)$")


def _cell_amount(text: str, vocab: Optional[_ParserVocab] = None) -> Optional[Decimal]:
    """单元格金额：剥前后缀后走严格金额正则；退回整数金额（无 .XX）识别。"""
    t = _amount_token(text, vocab)
    a = _amount_from_cell(t)
    if a is not None:
        return a
    if _INT_AMOUNT.match(t):
        return _to_decimal(t)
    return None


@lru_cache(maxsize=8)
def _compile_noise_matcher(patterns: tuple[str, ...]) -> "re.Pattern[str]":
    """噪声 token 正则（按词表 noise_patterns 编译；同一词表只编译一次）。"""
    return re.compile("|".join(patterns))


def _noise_token_matcher(vocab: Optional[_ParserVocab] = None):
    return _compile_noise_matcher(tuple(_v(vocab).noise_patterns))


def _is_noise_row(row, vocab: Optional[_ParserVocab] = None) -> bool:
    """判断整行是否为状态栏/时间戳/网络速率噪声行（跑在布局判定前）。"""
    joined = _row_join(row)
    return bool(_noise_token_matcher(vocab).search(joined))


def _drop_noise_rows(rows, vocab: Optional[_ParserVocab] = None) -> list:
    return [r for r in rows if not _is_noise_row(r, vocab)]


def _is_noise_token(text: str, vocab: Optional[_ParserVocab] = None) -> bool:
    return bool(_noise_token_matcher(vocab).search(text))


def _is_name_token(text: str, vocab: Optional[_ParserVocab] = None) -> bool:
    """判断文本块是否可能为项目名。

    收紧为「必须含至少一个汉字」：真实截图所有合法资产名都含汉字，而
    状态栏（18:59/SI/B/s/5G/6+/电量）、网络速率、纯数字串等被一键剔除。
    """
    v = _v(vocab)
    t = text.strip()
    if not t or _cell_amount(t, v) is not None:
        return False
    if t in v.row_close_tokens or t.isdigit():
        return False
    if "%" in t:
        return False  # 占比/收益率行，不作名称
    if any(w in t for w in v.name_exclude):
        return False
    if _is_total_label(t, v):
        return False
    if _is_noise_token(t, v):
        return False
    if not re.search(r"[\u4e00-\u9fff]", t):
        return False  # 必须含汉字
    return True


def _row_join(row) -> str:
    return " ".join(c[2] for c in row)


def _row_amount(row, vocab: Optional[_ParserVocab] = None) -> Optional[Decimal]:
    """行内首个严格金额（千分位+两位小数，剥前后缀）。"""
    for c in sorted(row, key=lambda x: x[1]):
        a = _cell_amount(c[2], vocab)
        if a is not None:
            return a
    return None


def _row_name(row, vocab: Optional[_ParserVocab] = None) -> Optional[str]:
    """行内最左侧的项目名块。"""
    for c in sorted(row, key=lambda x: x[1]):
        if _is_name_token(c[2], vocab):
            return c[2]
    return None


def _detect_layout(rows, amount_headers, vocab: Optional[_ParserVocab] = None) -> str:
    """自动判定版式分支：多列金额 → 三列；标签→数值下行 → 垂直；卡片列 → 卡片；否则横排。

    多列签名（spec `.scratch/multicolumn-signature` D1/D2）：
    - 金额互异按 `_cell_amount` 解析后的 Decimal 比，不比原文——OCR 重复行常带不同
      尾部标记（「10,000.00 >」/「10,000.00>」），原文不等而金额相等，按原文比会把
      重复行当成「两列不同金额」并绑架整图。
    - 需**≥2 行**支持才算多列（真三列表每行都是多列）；单行命中不足以代表整图版式，
      否则 `_organize_multicolumn` 会把其余单金额行全部丢弃。
    """
    multi_col_rows = 0
    for row in rows:
        names = [c[2] for c in row if _is_name_token(c[2], vocab)]
        amts = [a for a in (_cell_amount(c[2], vocab) for c in row) if a is not None]
        if names and len(amts) >= 2 and len(set(amts)) >= 2:
            multi_col_rows += 1
            if multi_col_rows >= 2:
                return "multi_column"
    for i, row in enumerate(rows):
        joined = _row_join(row)
        if _is_total_label(joined, vocab):
            continue  # 汇总/对账行不作分支判定依据
        # profit 区豁免：只跳过「独占 profit 标签行」（<2 个金额表头词，如某券商样本
        # 「累计收益2026-01-15」）；真多列表头行（≥2 表头词，如某基金平台样本
        # 「持有金额|累计收益|昨日收益」）必须放行走 vertical，否则其资产丢失。
        profit_headers = [h for h in amount_headers if h in joined]
        if _is_profit_row(row, vocab) and len(profit_headers) < 2:
            continue
        if any(h in joined for h in amount_headers):
            if _row_amount(row, vocab) is not None or (
                    i + 1 < len(rows) and _row_amount(rows[i + 1], vocab) is not None):
                return "vertical"
    # 卡片列：名称行(上,≥2 列名且无金额) + 金额行(下,≥2 金额且无名称)，多列卡片签名
    # （如某支付平台「医保/公积金/社保」三列卡片）。必须 ≥2 列避免单列横排被误判为卡片导致行丢失。
    for i in range(len(rows) - 1):
        top, bot = rows[i], rows[i + 1]
        top_names = [c[2] for c in top if _is_name_token(c[2], vocab)]
        bot_amts = [c[2] for c in bot if _cell_amount(c[2], vocab) is not None]
        if len(top_names) >= 2 and not any(_cell_amount(c[2], vocab) is not None for c in top):
            if len(bot_amts) >= 2 and not any(_is_name_token(c[2], vocab) for c in bot):
                return "card"
    return "horizontal"


def _organize_card(rows, vocab: Optional[_ParserVocab] = None) -> list["ParsedItem"]:
    """卡片列：名称行(上) + 金额行(下)，按 x 最近列配对。返回 [{name, amount}]。"""
    items: list["ParsedItem"] = []
    i = 0
    while i < len(rows) - 1:
        top, bot = rows[i], rows[i + 1]
        top_names = [(c[1], c[2]) for c in top if _is_name_token(c[2], vocab)]
        bot_amts = [(c[1], _cell_amount(c[2], vocab)) for c in bot
                    if _cell_amount(c[2], vocab) is not None]
        # 必须符合卡片签名：上≥2 列名且无金额 + 下≥2 金额且无名称（与 _detect_layout 一致），
        # 否则会把底部导航「目|同|回」等误配成本行下一行的 0.00。
        if (len(top_names) >= 2 and not any(_cell_amount(c[2], vocab) is not None for c in top)
                and len(bot_amts) >= 2 and not any(_is_name_token(c[2], vocab) for c in bot)):
            # 为每个名称找 x 最接近的金额
            for nx, ntext in top_names:
                best = min(bot_amts, key=lambda a: abs(a[0] - nx))
                if best is not None and best[1] is not None:
                    items.append(ParsedItem(name=_normalize_name(ntext), amount=best[1]))
            i += 2  # 消费两行
        else:
            i += 1
    return items


def _organize_horizontal(rows, vocab: Optional[_ParserVocab] = None) -> list["ParsedItem"]:
    """A 主骨架（横排）：同 Y 基线左名右金，「>」闭合；千分位=金额高置信。"""
    items: list["ParsedItem"] = []
    for row in rows:
        names = [c[2] for c in row if _is_name_token(c[2], vocab)]
        amts = sorted([c for c in row if _cell_amount(c[2], vocab) is not None], key=lambda x: x[1])
        if not names or not amts:
            continue
        amt = _cell_amount(amts[0][2], vocab)
        if amt is None:
            continue
        items.append(ParsedItem(name=_normalize_name(names[0]), amount=amt))
    return items


# P1「表头→值」列绑定水平容差（px）。某券商卡片样本实测同列表头/值 x 漂移 Δ≤3；取 40
# 既容忍 OCR 块抖动，又排除相邻列误绑（如市值列 x44 与 盈亏列 x≈415、拆分后
# 「持仓数」数字块 x≈90+ 均远超容差）。
_HEADER_BIND_TOL_X = 40


def _vertical_anchor(row, amount_headers, vocab: Optional[_ParserVocab] = None):
    """触发行内按 amount_headers 列表序找首个锚点表头 cell（P1 列绑定）。

    返回 (anchor_x, merged_amt, only_total_anchor)：
    - anchor_x：锚点 cell 的 x；None = 行内无可作锚点的表头 cell（调用侧回落
      现行「x 序首个严格金额」逻辑，零回归）。
    - merged_amt：同 cell 合并形态（'持仓市值3555.55'）剥离表头词后的行内值；
      纯表头 cell（'持仓市值'）为 None，值按 x 容差在下探窗口内另找。
    - only_total_anchor：True = 行内所有含表头词的 cell 均为「总市值」类汇总页眉
      （以「总」开头或命中 _is_total_label）。防御短词配置陷阱：amount_header
      配「市值」会命中页眉「总市值(人民币)」，实测产出垃圾项 ('我的股票',
      123456.78)——此类行整行跳过，绝不产出汇总页眉 garbage。
    """
    only_total = False
    for h in amount_headers:
        for c in sorted(row, key=lambda x: x[1]):
            if h not in c[2]:
                continue
            t = c[2].strip()
            if t.startswith("总") or _is_total_label(t, vocab):
                only_total = True
                continue  # 汇总页眉不作锚点
            return c[1], _cell_amount(t.replace(h, "", 1), vocab), False
    return None, None, only_total


def _organize_vertical(rows, amount_headers,
                       vocab: Optional[_ParserVocab] = None) -> list["ParsedItem"]:
    """B 垂直层级：名称 → 「金额/持有金额/持仓市值」标签 → 数值下行（P1 列绑定）。

    触发行内定位锚点表头 cell（_vertical_anchor，列表序=列优先序），在同行→
    下 ≤2 行窗口内取与锚点 x 水平容差内（|x_v − anchor_x| ≤ _HEADER_BIND_TOL_X）
    的首个金额；合并形态（'持仓市值3555.55'）由同行锚点自身命中。锚点缺失或
    容差内无金额 → 原样回落现行「x 序首个严格金额」逻辑（逐字节等价，零回归）；
    表头词仅命中汇总页眉 → 跳过该行（短词陷阱防御）。名称绑定（上 ≤3 行）不动。
    """
    items: list["ParsedItem"] = []
    for i, row in enumerate(rows):
        joined = _row_join(row)
        if not any(h in joined for h in amount_headers):
            continue
        if _is_total_label(joined, vocab):
            continue  # 汇总/对账行作锚点，不生成资产项
        anchor_x, merged_amt, only_total_anchor = _vertical_anchor(row, amount_headers, vocab)
        if anchor_x is None and only_total_anchor:
            continue
        amt = merged_amt
        if amt is None and anchor_x is not None:
            for j in range(i, min(i + 3, len(rows))):
                for c in sorted(rows[j], key=lambda x: x[1]):
                    if abs(c[1] - anchor_x) > _HEADER_BIND_TOL_X:
                        continue
                    a = _cell_amount(c[2], vocab)
                    if a is not None:
                        amt = a
                        break
                if amt is not None:
                    break
        if amt is None:
            # 回落：现行「同行 → 下 ≤2 行，x 序首个严格金额」逻辑原样执行
            amt = _row_amount(row, vocab)
            if amt is None:
                for j in range(i + 1, min(i + 3, len(rows))):
                    a = _row_amount(rows[j], vocab)
                    if a is not None:
                        amt = a
                        break
        if amt is None:
            continue
        name = None
        for j in range(i - 1, max(i - 3, -1), -1):
            n = _row_name(rows[j], vocab)
            if n is not None:
                name = n
                break
        if name is None:
            continue
        items.append(ParsedItem(name=_normalize_name(name), amount=amt))
    return items


def _organize_multicolumn(rows, amount_col: int,
                          vocab: Optional[_ParserVocab] = None) -> list["ParsedItem"]:
    """C 三列表头：名称左 + 多列金额（持有金额/累计收益/昨日收益），取第 amount_col 列。"""
    items: list["ParsedItem"] = []
    for row in rows:
        names = [c[2] for c in row if _is_name_token(c[2], vocab)]
        amts = sorted([c for c in row if _cell_amount(c[2], vocab) is not None], key=lambda x: x[1])
        if not names or len(amts) < 2:
            continue
        idx = min(amount_col, len(amts) - 1)
        amt = _cell_amount(amts[idx][2], vocab)
        if amt is None:
            continue
        items.append(ParsedItem(name=_normalize_name(names[0]), amount=amt))
    return items


def organize_blocks(blocks, rules: Optional[dict] = None,
                    vocabularies=None) -> list["ParsedItem"]:
    """OCR 文本块 → 结构化行 [{name, amount}]（纯函数，无 IO，进单测）。

    通用锚点：标签括号剥离（_normalize_name）、「元」不强制（_amount_token 剥后缀）、
    千分位/正负号=金额高置信（_STRICT_AMOUNT）、子行轴不定（按行内多列/下行情形统一处理）、
    按钮词黑名单（_is_ignored）、汇总行作对账锚点（total_labels 排除）。
    可选 rules：layout(horizontal|vertical|multi_column)、amount_header、ignore_words、amount_col。
    `vocabularies`：生效词表（`parser_rules.EffectiveVocabularies`，全局 builtin ∪ add − remove）。
    `None` → 纯出厂词表（**与词表引擎接入前的行为逐项相同**）。组级 rules 再在其上做替换覆盖。

    **行为等价红线（spec D7）**：本函数与四个 _organize_* 分支的算法一行未改，只把
    词表取值来源从模块常量换成 `_ParserVocab` 快照；对照测试见
    `tests/test_parser_equivalence.py`（旧实现经 `git show HEAD:` 加载为独立模块）。
    """
    rules = rules or {}
    vocab = _resolve_parser_vocab(vocabularies, rules)
    layout = rules.get("layout")
    amount_headers = vocab.amount_headers
    ignore_words = vocab.ignore_words
    amount_col = int(rules.get("amount_col", 0))

    filtered = [b for b in blocks if not _is_ignored(b[2], ignore_words)]
    rows = _rows_from_blocks(filtered)
    rows = _drop_noise_rows(rows, vocab)  # 剥离状态栏/时间戳/网络速率噪声行

    if layout == "horizontal":
        return _organize_horizontal(rows, vocab)
    if layout == "vertical":
        return _organize_vertical(rows, amount_headers, vocab)
    if layout == "multi_column":
        return _organize_multicolumn(rows, amount_col, vocab)
    if layout == "card":
        return _organize_card(rows, vocab)

    detected = _detect_layout(rows, amount_headers, vocab)
    if detected == "multi_column":
        return _organize_multicolumn(rows, amount_col, vocab)
    if detected == "vertical":
        return _organize_vertical(rows, amount_headers, vocab)
    if detected == "card":
        return _organize_card(rows, vocab)
    return _organize_horizontal(rows, vocab)


def _extract_header_date(text: str) -> Optional[str]:
    """从表头文本提取数据日期 → 'YYYY-MM-DD' 或 None。

    优先完整 'YYYY[-/]MM[-/]DD'；退回 'MM[-/]DD'（用今年补齐）。
    仅取 1<=月<=12、1<=日<=31 的合法值，避免金额等数字串误判。

    线上事故加固（不许再靠金额行误提取）：分隔符两侧**不允许空格**
    （'-0.10 70.07 -3.93' 的 '07␣-3' 曾被认成 7月3日）；数字前后边界排除
    数字与小数点（'70.07-3.93' 粘连形态同样拒收）；短式 lookbehind 另排除
    中英文逗号（千分位列表）。
    """
    m = re.search(r"(?<![\d.])(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?![\d.])", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(?<![\d.，,])(\d{1,2})[-/](\d{1,2})(?![\d.])", text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_date.today().year:04d}-{mo:02d}-{d:02d}"
    return None


# 解析结果内存缓存（L1）：key = 文件 sha256（+解析参数），避免前端
# preview/apply 传同一文件时重复跑 4~5 分钟的 CPU OCR。单用户本机部署足够。
_PARSE_CACHE: "OrderedDict[str, ParsedScreenshot]" = OrderedDict()
_PARSE_CACHE_MAX = 32  # 一批最多 12 图 preview+apply 双通道，32 保证同批不互踩

# 解析结果磁盘缓存（L2）：跨服务重启生效（重启后 L1 清空仍能秒回）。
# 目录 = 项目根/data/ocr_cache（与 routers 里 evidence_root 的三级 dirname 算法一致）。
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                          "data", "ocr_cache")
_CACHE_MAX_FILES = 200  # 每图一份 blocks JSON（几十~几百 KB），200 张≈几十 MB，
                        # 覆盖长期重传需求；超出仍按 mtime 淘汰最旧

# 缓存语义版本 3：缓存从「结果缓存」改为「OCR 块缓存」。schema≤2 磁盘文件存的是
# items（依赖 rules 的组织产物），配了 rules 的组只能「有 rules 不走缓存」，apply
# 在 HTTP 请求内重跑 4~5 分钟 OCR 触发代理超时。v3 存 rules 无关的原料
# blocks+date+total，命中后组织层按当前 rules 重跑 organize（毫秒级）。
# 旧 schema（≤2）文件一律视为未命中并重跑一次 OCR（预期行为，之后以 v3 落盘）。
_PARSE_SCHEMA = 3


@dataclass
class _OcrPayload:
    """块缓存载荷（与 rules 无关的原料）：OCR blocks + date/total。

    注意：事故后读侧（parse_screenshot 命中路径）不再消费 date/total——一律
    从 blocks 现推（_derive_total_date 自愈语义）；两字段仅保留写入以维持
    v3 文件格式兼容，旧文件里的坏值被自然忽略。
    """
    blocks: list[tuple[int, int, str, float]]
    date: Optional[str]
    total: Optional[Decimal]


def _payload_from_disk(data: dict) -> Optional[_OcrPayload]:
    """磁盘缓存 JSON(schema=3) → _OcrPayload。

    健壮容错：schema 版本不符/字段缺失/块结构异常/解析失败 → None（当作未命中，
    不抛异常），外层据此再走完整 OCR。blocks JSON 形态 [[y,x,text,score],...]。
    """
    try:
        if data.get("schema") != _PARSE_SCHEMA:
            return None
        date = data.get("date")
        if date is not None and not isinstance(date, str):
            return None
        total: Optional[Decimal] = None
        if data.get("total") is not None:
            total = Decimal(str(data["total"]))
        raw = data.get("blocks")
        if not isinstance(raw, list):
            return None
        blocks: list[tuple[int, int, str, float]] = []
        for b in raw:
            if (not isinstance(b, (list, tuple)) or len(b) != 4
                    or not isinstance(b[2], str)):
                return None
            blocks.append((int(b[0]), int(b[1]), b[2], float(b[3])))
        return _OcrPayload(blocks=blocks, date=date, total=total)
    except Exception:
        return None


def _write_disk_cache(cache_key: str, payload: _OcrPayload) -> None:
    """OCR 块载荷写磁盘缓存（data/ocr_cache/<sha>.json，schema=3 存 blocks）。

    先写临时文件再 os.replace 原子替换，避免进程中途退出留下半截 JSON。
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    disk_payload = {
        "schema": _PARSE_SCHEMA,
        "date": payload.date,
        "total": str(payload.total) if payload.total is not None else None,
        "blocks": [[b[0], b[1], b[2], b[3]] for b in payload.blocks],
    }
    dst = os.path.join(_CACHE_DIR, f"{cache_key}.json")
    tmp = os.path.join(_CACHE_DIR, f"{cache_key}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk_payload, f, ensure_ascii=False)
    os.replace(tmp, dst)


def _trim_disk_cache() -> None:
    """磁盘缓存裁剪：*.json 超 _CACHE_MAX_FILES 个时按 mtime 从旧到新删除。"""
    if not os.path.isdir(_CACHE_DIR):
        return
    files = sorted(
        (f for f in os.listdir(_CACHE_DIR) if f.endswith(".json")),
        key=lambda f: os.path.getmtime(os.path.join(_CACHE_DIR, f)),
    )
    for f in files[: max(0, len(files) - _CACHE_MAX_FILES)]:
        try:
            os.remove(os.path.join(_CACHE_DIR, f))
        except OSError:
            pass


def parse_screenshot(image_path: str, cache_key: Optional[str] = None,
                     rules: Optional[dict] = None,
                     progress_cb=None, force_ocr: bool = False,
                     vocabularies=None) -> ParsedScreenshot:
    """截图 → 解析结果（项目清单 + 投顾总金额 + 数据日期）。

    两级缓存（schema=3 为 rules 无关的 OCR 块缓存）：L1 进程内存
    （_PARSE_CACHE，存 _OcrPayload）→ L2 磁盘（data/ocr_cache/<sha>.json）。
    cache_key：文件 sha256。**无论 rules 有无**都查缓存；命中（任一级）→ 复用
    blocks，仅按当前 rules 重跑组织层 organize_blocks（毫秒级）→ 来源组 rules
    变更后同图重传不再挂 4~5 分钟 OCR，items 随新 rules 生效。
    **自愈**：命中时 total/date/date_text 一律从 blocks 现推（_derive_total_date_text，
    毫秒级），不信任盘上/内存 payload 里的旧值——事故版把 MM-DD 兜底误提取的
    日期固化进了 v3 文件，blocks 本身是对的，重推即修复，无需升 schema、无需
    重跑 OCR。未命中 → 完整 OCR → 写 L1/L2（存 blocks+现推 date/total，date/
    total 键保留写入仅为格式兼容，读侧忽略）→ 组织 → 返回。旧 schema（≤2）
    磁盘文件视为未命中，重跑一次 OCR 并以 v3 落盘（预期）。
    progress_cb：命中先发 ('cache',) 再照发 ('organize',)（组织层确实重跑）；
    未命中 OCR 期间纯透传 ('chunk',i,n)/('line',done,total,text,score)
    （本函数不解释不重组），OCR 完成进入组织层前发 ('organize',)。
    force_ocr=True：本次忽略 L1/L2 缓存直接重跑 OCR（不发 ('cache',) 事件），
    新结果照常写回刷新缓存（后续普通解析命中新块）。
    vocabularies：生效词表（`parser_rules.EffectiveVocabularies`，全局
    builtin ∪ add − remove），**原样透传**给 `organize_blocks`；None → 纯出厂词表
    （= 词表引擎接入前的行为）。本函数只搬运不解释，判定逻辑仍全在组织层。
    """
    cached: Optional[_OcrPayload] = None
    if cache_key and not force_ocr:
        cached = _PARSE_CACHE.get(cache_key)
        if cached is None:
            disk_path = os.path.join(_CACHE_DIR, f"{cache_key}.json")
            if os.path.exists(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    cached = _payload_from_disk(data)
            if cached is not None:  # 磁盘命中 → 回填 L1
                _PARSE_CACHE[cache_key] = cached
                _PARSE_CACHE.move_to_end(cache_key)
                while len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
                    _PARSE_CACHE.popitem(last=False)
    if cached is not None:
        if progress_cb:
            progress_cb(("cache",))
        # 浅拷贝：cache 里的 blocks 不与返回值共享可变引用（消费方均只读，
        # 双保险）；内层元组不可变。
        blocks = list(cached.blocks)
        # 自愈：不信任 payload 的 date/total（可能是坏 regex 时代固化的错值），
        # 从 blocks 现推（毫秒级，派生逻辑与派生自同一数据源，结果自洽）。
        # date_text（命中行原文）与 date 同源现推，miss/hit 两路结果一致。
        total, date, date_text = _derive_total_date_text(blocks)
    else:
        # 未命中 / 无 cache_key / force_ocr：完整 OCR；有 cache_key 就写回
        # （force_ocr 时即"刷新"旧缓存）
        blocks, total, date, date_text = _fresh_ocr(image_path, progress_cb)
        if cache_key:
            payload = _OcrPayload(blocks=list(blocks), date=date, total=total)
            _PARSE_CACHE[cache_key] = payload
            _PARSE_CACHE.move_to_end(cache_key)
            while len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
                _PARSE_CACHE.popitem(last=False)
            _write_disk_cache(cache_key, payload)
            _trim_disk_cache()

    if progress_cb:
        progress_cb(("organize",))
    # 通用解析器：组织文本块 → 结构化行。rules 非空时按来源组规则覆盖通用
    # 启发式；为空走纯通用。缓存命中时 items 每次重新 organize，天然无共享。
    # vocabularies 为生效词表（None = 纯出厂），组级 rules 在组织层再叠加覆盖。
    items = organize_blocks(blocks, rules, vocabularies=vocabularies)
    return ParsedScreenshot(items=items, total=total, date=date,
                            raw_blocks=blocks, date_text=date_text)


def _derive_total_date(blocks) -> tuple[Optional[Decimal], Optional[str]]:
    """纯派生（blocks → total, date），毫秒级、无 IO。兼容旧两元组签名；
    需要命中行原文（疑似日期展示）时用 _derive_total_date_text。"""
    total, date, _text = _derive_total_date_text(blocks)
    return total, date


def _derive_date_with_text(rows) -> tuple[Optional[str], Optional[str]]:
    """行序列（_rows_from_blocks 产物）→ (date, date_text)。

    date：首个能 _extract_header_date 命中的行（收紧后金额行不会再误触发）；
    date_text：该命中行的 _row_join 拼接原文，供前端展示判断误报。
    未命中 → (None, None)。
    """
    for r in rows:
        text = _row_join(r)
        d = _extract_header_date(text)
        if d:
            return d, text
    return None, None


def _derive_total_date_text(blocks) -> tuple[Optional[Decimal], Optional[str], Optional[str]]:
    """纯派生（blocks → total, date, date_text），毫秒级、无 IO。

    total：「投顾总金额」锚点行之后 1~2 行内首个可解析金额；
    date/date_text：见 _derive_date_with_text。与 rules 无关，故缓存命中时
    可直接对缓存 blocks 重推（自愈坏 date），且 miss/hit 两路同源同结果。
    """
    rows = _rows_from_blocks(blocks)

    total: Optional[Decimal] = None
    for i, r in enumerate(rows):
        if "投顾总金额" in " ".join(c[2] for c in r):
            for r2 in rows[i + 1:i + 3]:
                for c in sorted(r2, key=lambda x: x[1]):
                    a = _amount_from_cell(c[2])
                    if a is not None:
                        total = a
                        break
                if total:
                    break
        if total:
            break

    date, date_text = _derive_date_with_text(rows)
    return total, date, date_text


def _fresh_ocr(image_path: str, progress_cb=None):
    """完整 OCR → (blocks, total, date, date_text)。派生自 blocks，随缓存复用。"""
    blocks = _ocr_blocks(image_path, progress_cb=progress_cb)
    total, date, date_text = _derive_total_date_text(blocks)
    return blocks, total, date, date_text


# ---------------- DB 读取（T2 只读） ----------------
def _group_latest_rows(db: Session, prefix: str) -> list[tuple[str, Decimal, int | None, _date]]:
    """来源组**在用**项的「每个名称的最后一笔有效行」：名称 / 金额 / 所属资产项 / 日期。

    `assets.asset_name` 是**写入时的名称快照**：改名未回写时同一资产项可能以多个
    名称出现，因此这里按名称分组（保留历史口径），再由调用方决定按名称还是按项归并。

    在用判据（spec `.scratch/archived-ingest-scope` D1）：`asset_items.status != 'archived'`
    —— **`draft` 也算在用**（ADR-0019/0020：draft 是计入净值的在册项，排除会令 OCR 读到
    draft 项时判成新增 → 造出重复项）；`archived` 项自 `archived_effective_from` 起已退出
    计量，不该再出现在录入侧的匹配候选、未识别项与对账锚点里（D2/D3/D6）。归档过滤只做在
    本函数这一处，`get_group_existing` / `get_group_existing_items` / `compute_preview`
    因此自动收敛，不需要各自再加 status 判断（也**不加** status 参数，签名与返回结构不变）。

    历史行未挂资产项（`asset_item_id IS NULL`）沿用旧口径保留：它们是迁移前的无主行，
    status 无从判断，剔掉等于让用户看不到自己账上的旧金额。
    """
    archived_item_ids = select(AssetItem.id).where(AssetItem.status == "archived")
    subq = (
        select(Asset.asset_name, func.max(Asset.asset_date).label("max_date"))
        .where(Asset.asset_name.like(prefix + "%"), Asset.superseded_by.is_(None))
        .group_by(Asset.asset_name)
    ).subquery()
    rows = db.execute(
        select(Asset.asset_name, Asset.amount, Asset.asset_item_id, Asset.asset_date)
        .join(subq, and_(Asset.asset_name == subq.c.asset_name,
                         Asset.asset_date == subq.c.max_date,
                         Asset.superseded_by.is_(None)))
        .where(or_(Asset.asset_item_id.is_(None),
                   Asset.asset_item_id.notin_(archived_item_ids)))
    ).all()
    return [(r[0], Decimal(str(r[1])), r[2], r[3]) for r in rows]


def get_group_existing(db: Session, prefix: str) -> list[tuple[str, Decimal]]:
    """（按名口径）来源组现有资产项：每个名称最新金额。

    既有调用方（`compute_preview` 的 `known_names`、对账锚点）依赖「名字 → 金额」，
    故保持不变；需要资产项身份请用 `get_group_existing_items`。
    """
    return [(name, amount) for name, amount, _iid, _d in _group_latest_rows(db, prefix)]


def get_group_existing_items(db: Session, prefix: str) -> list[dict]:
    """来源组现有资产项的 **item 级现值**（ADDITIVE，名称→身份的桥）。

    用途：录入界面把「识别行关联到已有项」后，行内差额与顶部「系统合计」需要目标项
    的现值做基线；只给名称（`get_group_existing`）无法区分同名的历史/别名行，也无法
    去重。故这里按 `asset_item_id` 归并：同一项若因改名未回写而存在多个名称，取日期
    最新的那行作为代表，保证前端「同一项只计一次」。

    `item_id` 可能为 `None`（历史行未挂资产项）：前端按名称兜底。
    """
    best: dict[object, tuple[_date, str, Decimal, int | None]] = {}
    for name, amount, item_id, day in _group_latest_rows(db, prefix):
        key: object = item_id if item_id is not None else f"name:{name}"
        prev = best.get(key)
        if prev is None or (day, name) > (prev[0], prev[1]):
            best[key] = (day, name, amount, item_id)
    return [
        {"item_id": item_id, "name": name, "amount": str(amount),
         "as_of": day.isoformat() if day else None}
        for day, name, amount, item_id in sorted(
            best.values(), key=lambda v: (v[1],))
    ]


def attach_item_ids(preview: "ScreenshotPreview",
                    existing_items: list[dict]) -> "ScreenshotPreview":
    """把名称解析到的资产项 id 回填到预览行（`row.item_id`）。

    只有命中既有名称的行（update / zero / keep）才有 item_id；create 行为 None。
    前端据此对「同一项」去重，避免同项多行把系统合计算重。
    """
    by_name = {e["name"]: e.get("item_id") for e in existing_items}
    for row in preview.rows:
        if row.asset_name and row.action != "create":
            row.item_id = by_name.get(row.asset_name)
    preview.existing_items = existing_items
    return preview


# ---------------- 汇总行提示（ADR-0029 D7 / 票 03 D1） ----------------
def is_aggregate_name(name: Optional[str],
                      learned: Optional[Sequence[str]] = None) -> bool:
    """该名称是否像「汇总行」（内置汇总词表 ∪ 该组的 `aggregate` 规则，**子串**匹配）。

    只回答「值不值得预勾」，绝不决定动作：动作由人给（`options.reconciles`），后端
    `apply_preview` 只认载荷不认提示。词表口径见 `parser_rules.AGGREGATE_HINT_LABELS`
    的注释（与整行丢弃的 `total_labels` 刻意分开）。
    """
    if not name:
        return False
    return (any(w in name for w in AGGREGATE_HINT_LABELS)
            or any(w and w in name for w in (learned or ())))


def annotate_aggregate_hints(preview: ScreenshotPreview,
                             learned: Optional[Sequence[str]] = None
                             ) -> ScreenshotPreview:
    """给疑似汇总行打 `aggregate_hint=True`（纯函数：只 `replace` 行，不改其它字段）。

    `learned` 来自 `source_group_rules_service.aggregate_names_of`（该组学过的词），
    与内置词表并集生效；空/None → 只用内置词表。已为 True 的行不重复替换。
    """
    rows: list[PreviewRow] = []
    for r in preview.rows:
        if r.aggregate_hint:
            rows.append(r)
            continue
        hinted = is_aggregate_name(r.source_name, learned)
        rows.append(replace(r, aggregate_hint=True) if hinted else r)
    return replace(preview, rows=rows)


# ---------------- T3：确认 → 写入 + 凭证 ----------------
@dataclass
class ApplyResult:
    evidence_id: int
    data_date: _date
    asset_date: str            # YYYY-MM-DD（= data_date，资产记录统一日期）
    rows_written: int
    rows: list[PreviewRow]     # 写入后回填（create 行带上最终资产名）
    # v5 写语义：action=create 撞上同日 active 行 → 不覆盖，显式上报
    # （additive 字段，缺省空清单，旧调用方零影响）
    conflicts: list[dict] = field(default_factory=list)
    # 账本即变更日志：同日同项 (amount, side) 完全一致 → 不覆盖、不写审计，
    # 只在此回报（形状与 asset_service/import_service 的同类字段对齐）
    skipped_unchanged: list[dict] = field(default_factory=list)
    # 绑定回执（ADR-0026）：本批 identify_decisions 中 action=bind 的行实际写到了哪
    # （additive 字段，旧调用方零影响）
    bindings: list[dict] = field(default_factory=list)
    # 绑定优先于未识别项清零：目标项本来会被 zero 掉，因本批已绑到它就跳过清零
    zero_skipped_due_to_bind: list[dict] = field(default_factory=list)
    # 汇总行回执（ADR-0029 D5/D6，additive）：本批被标为汇总行、**零写入**的行
    # 形状：[{preview_index, asset_name, amount, asset_item_id}]
    reconciled: list[dict] = field(default_factory=list)
    # 汇总行优先于未识别项清零（与 zero_skipped_due_to_bind 同构）：目标项本来会被
    # zero 掉，因本批的汇总行指向它就跳过清零（否则「既没落账又被清零」，净值错）
    zero_skipped_due_to_reconcile: list[dict] = field(default_factory=list)


def _save_evidence_file(image_path: str, data_date: _date,
                        evidence_root: str) -> tuple[str, str]:
    """（legacy，仅 test_evidence_file_unique_on_collision 直测文件名冲突语义）
    v5 步骤 6 起生产路径改走 `get_or_create_evidence`（上传即存证，按上传年月落位）。
    """
    """截图原图永久落盘到 <root>/<年>/<月>/，返回 (相对路径, sha256)。

    文件名带 sha256 前 12 位保证唯一；同名不同内容时退化为全 hash，绝不覆盖。
    """
    sha = hashlib.sha256()
    with open(image_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    stem, ext = os.path.splitext(os.path.basename(image_path))
    rel_dir = os.path.join(str(data_date.year), f"{data_date.month:02d}")
    fname = f"{stem}_{digest[:12]}{ext or '.png'}"
    rel = os.path.join(rel_dir, fname)
    dest = os.path.join(evidence_root, rel)
    if os.path.exists(dest):
        with open(dest, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != digest:
                rel = os.path.join(rel_dir, f"{stem}_{digest}{ext or '.png'}")
                dest = os.path.join(evidence_root, rel)
                shutil.copyfile(image_path, dest)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(image_path, dest)
    return rel.replace(os.sep, "/"), digest


# ---------------- 凭证文件命名与原图信息（ADR-0022 D1/D2/D3/D6） ----------------
# 扩展名白名单：与 `evidence_service._MEDIA_TYPES`、spec §3 的验收正则
# `^\d{8}-\d{6}_sg\d+_[0-9a-f]{12}\.(jpe?g|png|webp)$` 同源。
EVIDENCE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_EVIDENCE_EXT_FALLBACK = ".png"

# 历史脏名口径（迁移 0032 的 UPDATE 与本函数同一条规则）：`shot_upload_*` 是服务端
# `tempfile.mkstemp` 的临时名，没有信息量，一律不进 `original_filename`（存 NULL）。
DIRTY_ORIGINAL_PREFIX = "shot_upload_"

# EXIF 白名单（ADR-0022 D2）：只认这五个 tag。GPS（0x8825 / GPSInfo 子 IFD）
# **有意不取**——位置信息无业务价值，且不该在库里多留一份。
_EXIF_WHITELIST = {
    0x9003: "DateTimeOriginal",      # Exif 子 IFD（拍摄时间；与文件名时间戳互不替代）
    0x010F: "Make",
    0x0110: "Model",
    0x0131: "Software",
    0x0112: "Orientation",
}
_EXIF_IFD_TAG = 0x8769


def evidence_ext(original_filename: Optional[str]) -> str:
    """客户端扩展名 → 白名单内**小写**扩展名；白名单外（含无扩展名、奇形）→ `.png`。

    白名单化是验收正则的前提（`jpe?g|png|webp`）：客户端传来 `.gif`/`.bmp`/
    `.JPG` 时不能原样落进路径——那会造出解析不出、也发不出正确 content-type 的名字。
    """
    base = os.path.basename(str(original_filename or "")).strip()
    ext = os.path.splitext(base)[1].lower()
    return ext if ext in EVIDENCE_EXTS else _EVIDENCE_EXT_FALLBACK


def evidence_file_name(created_at: _datetime, source_group_id: Optional[int],
                       file_sha256: str, ext: str) -> str:
    """D1 命名规则的**唯一实现**：`<YYYYMMDD-HHMMSS>_sg<组id>_<sha 前 12><ext>`。

    - 时间戳＝**上传时刻**（本地时区，精确到秒），不是拍摄时间、也不是 data_date：
      截图常无 EXIF，上传时刻必然存在、永不变动，且按名即可排序；拍摄时间不丢——
      它在 `image_meta.exif.DateTimeOriginal` 单独留存，两者不互相替代。
    - 命名基座只用不可变字段：上传时刻 + `source_group_id`（自增主键，永不改动）+
      内容哈希。**不用 slug**（会变）、不用显示名（几乎全中文，ASCII 消毒后信息量
      为零）。可读性由页面承担：文件名只回答「什么时候传的、哪个组、哪张图」。
    - `source_group_id` 缺失（异常数据）→ `sg0`，格式仍合法可解析。
    """
    ts = created_at.strftime("%Y%m%d-%H%M%S")
    gid = 0 if source_group_id is None else int(source_group_id)
    return f"{ts}_sg{gid}_{file_sha256[:12]}{ext}"


def clean_original_filename(original_filename: Optional[str]) -> Optional[str]:
    """客户端原名 → `original_filename` 列值（D3）：原样保留，脏名与空值 → NULL。

    「原样」指只取 basename（防把路径写进列），不推断、不伪造：`shot_upload_*`
    本来就是临时名（迁移 0032 已把历史 23 条置 NULL），新上传同规则处理，
    不留第二种脏名口径。
    """
    base = os.path.basename(str(original_filename or "").strip())
    if not base or base.startswith(DIRTY_ORIGINAL_PREFIX):
        return None
    return base


def _file_sha256(path: str) -> str:
    """文件 sha256（流式，大图不整块读进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _exif_scalar(value):
    """EXIF 值 → JSON 安全标量；表达不了 → None（宁缺勿造）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        value = value.replace("\x00", "").strip()
        return value or None
    return None


def _exif_whitelist(img) -> dict:
    """EXIF → 白名单子集；无 EXIF / 读不出 → `{}`（D2：**不存 GPS**）。

    Pillow 把 IFD0（Make/Model/Software/Orientation）与 Exif 子 IFD
    （DateTimeOriginal）分开放，故两处都看：IFD0 优先，子 IFD 补齐缺口。
    只按 tag 白名单取值，GPS 子 IFD（0x8825）从头到尾不访问。
    """
    try:
        exif = img.getexif()
    except Exception:                       # 坏 EXIF 段不该让整条存证失败
        return {}
    if not exif:
        return {}
    try:
        sub = exif.get_ifd(_EXIF_IFD_TAG)
    except Exception:
        sub = None
    sources = [dict(exif), sub if isinstance(sub, dict) else {}]
    found: dict = {}
    for source in sources:
        for tag, name in _EXIF_WHITELIST.items():
            if name in found or tag not in source:
                continue
            value = _exif_scalar(source[tag])
            if value is not None:
                found[name] = value
    return found


def build_image_meta(path: str) -> Optional[dict]:
    """原图体检表（ADR-0022 D2）：白名单 JSON；读不出 → None，**不阻断存证**。

    白名单形状：`{width, height, bytes, format, exif{...}}`，`exif` 见
    `_EXIF_WHITELIST`（无 EXIF → `{}`）。Pillow 未安装、图片打不开、EXIF 段损坏
    都只记一条 warning 并返回 None——「可信 > 完备」：存证是硬需求，体检表是旁证。
    日志只记 basename，不把绝对路径写进日志（公开安全红线）。
    """
    name = os.path.basename(path)
    try:
        from PIL import Image
    except Exception as exc:                # 依赖缺失（未装 Pillow）
        logger.warning("evidence_image_meta_unavailable", file=name, error=str(exc))
        return None
    try:
        with Image.open(path) as img:
            return {
                "width": img.width,
                "height": img.height,
                "bytes": os.path.getsize(path),
                "format": img.format,
                "exif": _exif_whitelist(img),
            }
    except Exception as exc:                # 坏图 / 截断 / 非图片
        logger.warning("evidence_image_meta_unreadable", file=name, error=str(exc))
        return None


def get_or_create_evidence(db: Session, file_sha256: str, tmp_path: str,
                           source_group: str, original_filename: str,
                           evidence_root: str) -> int:
    """上传即存证（v5 步骤 6，spec §2）：按**（sha, 来源组）**去重，无则落盘＋建行，有则复用。

    - 查重范围＝**同一来源组内的同一张图**（ADR-0024 D1）：命中即返回该组自己的行，
      不再全局按 sha 复用。全局化会把「B 组的图」记成 A 组的凭证（`source_group_id`
      与文件名里的 `sg<id>` 双双说谎）；同一张图属于两个组时，两行各带自己的
      `sg<id>` 名、各自一份文件——归属才与事实一致。存储层同口径的复合唯一索引
      `ux_screenshot_evidence_sha_group(file_sha256, source_group_id)` 由迁移 0033 建；
    - 新行按**上传年月** `data/evidence/<年>/<月>/` 落位且不再移动
      （data_date 是 DB 属性非目录语义；file_path 列为真源）；
    - 文件名由服务端计算（ADR-0022 D1）：`<上传时刻>_sg<组id>_<sha 前 12><ext>`，
      **客户端原名不再进入文件路径**（23/28 历史脏名的成因就是路径继承客户端名）；
      目标名已被占用且内容不同 → 抛错停止，绝不覆盖、也不静默加后缀；
    - 原图信息写 `image_meta`（白名单 JSON，ADR-0022 D2）：落盘并校 sha 成功后读文件；
      读不出（Pillow 缺失/坏图）→ NULL 并记 warning，**不阻断存证**（可信 > 完备）；
      同组同 sha 复用既有行 → 元数据一字不改（同组一 sha 一行一文件）；
    - 会话属性（data_date/supplement_rows/engine）此刻未知 → NULL，
      apply 采纳时富化（`apply_preview`）。
    - 落盘/建行失败 → 上抛（调用方映射 500 EVIDENCE_WRITE_FAILED，阻断识别）；
      建行走独立 commit（凭证是持久事实，后续识别失败不回滚——孤儿凭证永久保留）。
    - 源组先解析成行（`resolve_source_group`）：0029 起凭证只存 `source_group_id`
      外键，未知组 → 400 SOURCE_GROUP_UNKNOWN，**落盘与建行都不发生**。
    返回 evidence.id（新建或复用，复用取同组最早行）。
    """
    sg = resolve_source_group(db, source_group)   # 未知组 → 400，先于落盘与建行
    existing = db.execute(
        select(ScreenshotEvidence)
        .where(ScreenshotEvidence.file_sha256 == file_sha256,
               ScreenshotEvidence.source_group_id == sg.id)
        .order_by(ScreenshotEvidence.id)
    ).scalars().first()
    if existing is not None:
        return existing.id

    now = _datetime.now()                        # 上传时刻（本地时区，容器 TZ=Asia/Shanghai）
    ext = evidence_ext(original_filename)
    rel = f"{now.year}/{now.month:02d}/{evidence_file_name(now, sg.id, file_sha256, ext)}"
    dest = os.path.join(evidence_root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        # D1 冲突即停：同 sha 的复用分支在上方已返回，能走到这里说明「同名字、
        # 不同内容」（时间戳到秒 + 组 + sha 前 12 位撞车）。宁可停下看，也不覆盖。
        if _file_sha256(dest) != file_sha256:
            raise IOError(
                f"存证文件名冲突：{rel} 已存在且内容不同（不覆盖、不改名，需人工处理）")
    else:
        shutil.copyfile(tmp_path, dest)
        # 写后校 sha：磁盘静默损坏宁抛不存（可信 > 便利）
        if _file_sha256(dest) != file_sha256:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise IOError("存证文件写后校验不一致")

    meta = build_image_meta(dest)                # 读不出 → None，不阻断存证
    ev = ScreenshotEvidence(
        source_group_id=sg.id, file_path=rel, file_sha256=file_sha256,
        original_filename=clean_original_filename(original_filename),
        data_date=None, supplement_rows=None, engine=None,
        image_meta=None if meta is None else json.dumps(meta, ensure_ascii=False),
    )
    db.add(ev)
    db.flush()
    db.commit()
    return ev.id


def _inherit_side(db: Session, asset_name: str, asset_date: _date) -> Optional[str]:
    """新资产行 side 的历史继承：同 asset_name 在 asset_date ≤ 目标日的最近一行
    side；无历史 → None。负债条目在新日期补行时保留负债侧，防「liability 资产
    的新日期被硬编码 asset」翻转 analysis/proportion 口径。side 缺失的历史脏值
    由调用方按 "asset" 兜底。
    """
    return db.execute(
        select(Asset.side)
        .where(Asset.asset_name == asset_name, Asset.asset_date <= asset_date,
               Asset.superseded_by.is_(None))
        .order_by(Asset.asset_date.desc())
        .limit(1)
    ).scalars().first()


def _supplement_item_classification(db: Session, item: AssetItem, *,
                                    asset_name: str,
                                    source_group: Optional[str] = None) -> None:
    """截图采纳：把**能解析出来的**维度补到资产项的空维度上（ADR-0019）。

    截图解析层只产出「名称 + 金额」，机构/渠道可以推：
      - 机构 ← 资产名第一个 '-' 之前的前缀（'示例银行-黄金' → 示例银行）命中
        `institutions.name`；退一步用来源组 label（多数组就是一个 App/机构）；
      - 渠道 ← 来源组 label（'支付宝' → `channels.name`）。
    类别 / 流动性 / 归属 / 风险无从解析 → 留空：`get_or_create_item` 建出的是 draft，
    draft 允许缺维（D9）。**已存在的项只补空维度，绝不覆盖已有分类。**

    分类失败不阻断采纳（分类不是入账必要条件），**但归档项例外**：分类服务给出的
    `ITEM_ARCHIVED` 会原样上抛，成为行级错误（spec `.scratch/archived-ingest-scope` D4）。
    """
    payload: dict = {}
    if item.institution_id is None:
        inst = (_institution_id_by_name(db, _org_prefix(asset_name))
                or _institution_id_by_name(db, _group_label(db, source_group)))
        if inst is not None:
            payload["institution_id"] = inst
    if item.channel_id is None:
        channel = _channel_id_by_name(db, _group_label(db, source_group))
        if channel is not None:
            payload["channel_id"] = channel
    if not payload:
        return
    try:
        classification_service.set_classification(
            db, item.id, payload, reason="截图采纳", actor="screenshot")
    except ApiError as exc:
        if exc.code == ErrorCode.ITEM_ARCHIVED:
            # 归档是硬拦（spec D4）：这一条过去被静默吞掉，等于「已归档却仍在补分类」
            # 被当成「分类不是必要条件」放过。与 `_upsert_asset` 的拦截统一为行级错误，
            # 让 apply 整行失败而不是留一条「分类没补上但金额写进去了」的糊涂账。
            raise
        # 其余分类错误照旧不阻断采纳：分类不是入账的必要条件，宁可留空（draft）
        pass


def _group_label(db: Session, source_group: Optional[str]) -> str:
    """来源组 → 显示名（未落库/未知时回退原文，便于按同名字典项匹配）。"""
    if not source_group:
        return ""
    sg = get_source_group(db, source_group)
    return (sg.label if sg else "") or str(source_group)


def _upsert_asset(db: Session, asset_name: str, amount: Decimal, asset_date: _date,
                  source_evidence_id: int,
                  template_attribute_ids: Optional[list[int]] = None,
                  account_ids: Optional[list[int]] = None,
                  side: Optional[str] = None,
                  allow_update: bool = True,
                  source_group: Optional[str] = None,
                  item_id: Optional[int] = None) -> tuple[str, Optional[Asset]]:
    """v5 写语义（spec §4）：按名（含历史名）解析资产项 → (状态, 行)。

    - 分类（ADR-0019）：**项级**，只补空维度（机构/渠道可解析，其余留空 → draft）。
      记录行不承载任何分类，故 `template_attribute_ids` / `account_ids` 两个旧 EAV
      参数已废弃为零影响：签名保留只为现有路由调用不变（旧 EAV 表已随 0027 删除）。
    - side（优先级 payload.side > 同名历史行 side > "asset"）：仅 create 分支
      生效；update 分支不动存量行的 side 列。
    - **归档项一律拒绝**（spec `.scratch/archived-ingest-scope` D1/D4）：解析到的资产项
      `status == 'archived'` → 抛 `ApiError(ITEM_ARCHIVED, 409)`，且**不落任何 `assets`
      行**（拦截点在任何写入之前，create/update/zero 三条路径共用）。create 路径同样
      覆盖：按名 `get_or_create_item` 会命中归档项，不拦就等于把钱写进已退出计量的项。
    - allow_update=True（人裁决 action=update/zero）：同日同项 active 行覆盖
      amount/凭证（F1 待录入清单语义不变，冻结契约不破）；但**值没变就不算变化**
      （账本即变更日志，见下条）。
    -     allow_update=False（action=create 却撞上同日 active 行）：意外冲突——
      **绝不静默覆盖**，返回 ("conflict", 既有行, None) 由调用方显式上报。
    - 账本即变更日志（与 `asset_service.set_current_value` /
      `import_service` 同一条约定）：update 分支命中时先
      比对 (amount, side)，完全一致 → ("unchanged", 既有行, None)：
      **一个字段都不改、不产生 before、调用方也不写审计**（"没变化"不是一条变化）。
      side 的比对口径：update 分支不动存量 side 列，故以 `row.side` 为「将写入的
      侧」；调用方显式给了 side 且与存量不同 → 不是同值 → 仍返回 ("updated")。

    Returns:
        ("created"|"updated"|"unchanged"|"conflict", Asset, before|None)
        before＝update 分支覆盖前的旧值 {amount, side}（步骤 4 审计用）；
        created / unchanged / conflict 均为 None。
    """
    item = None
    if item_id is not None:
        # 绑定语义（ADR-0026）：目标项由人裁决选定，**不再按名解析**——否则别名一多、
        # 旧名与别名交错时，同一行会漂到别的项上。此处禁止回落到 get_or_create_item：
        # 绑定失败只允许报错，绝不允许「悄悄新建一个项」。
        item = db.get(AssetItem, item_id)
        if item is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                           details={"field": "identity_decisions.target_item_id"})
    if item is None:
        item = get_or_create_item(db, asset_name)   # name→item（含显示名史，拍板 10）
    if item.status == "archived":
        # 写路径兜底（spec D1/D4）：归档项**不参与录入侧任何一条写路径**。这里是
        # apply 的唯一写入口，故 create / update / zero 三条路径都在此一次拦住——
        # create 也走这里：`get_or_create_item` 按名（含历史名/别名）会命中归档项
        # （asset_service.py:126-153），不拦就会把钱写进一个已退出计量的项，
        # as-of 聚合自 `archived_effective_from` 起剔除它 → 用户看到「已录入」但
        # 总额不变（比报错更难诊断）。抛行级 ApiError（code=ITEM_ARCHIVED，409），
        # 调用方零写入：本函数在写入（assets 行）之前抛出。
        archived_from = (item.archived_effective_from.isoformat()
                         if item.archived_effective_from else None)
        raise ApiError(
            ErrorCode.ITEM_ARCHIVED, 409,
            f"资产项已归档，不可写入录入金额：{item.display_name}"
            f"（该名已退出计量：请点「关联到已有项」改录到它在用的目标项，"
            f"或把本行改名为新名称）",
            details={"asset_item_id": item.id,
                     "asset_name": item.display_name,
                     "archived_effective_from": archived_from,
                     "field": "asset_name"})
    _supplement_item_classification(db, item, asset_name=asset_name,
                                     source_group=source_group)
    row = db.execute(
        select(Asset).where(Asset.asset_item_id == item.id,
                            Asset.asset_date == asset_date,
                            Asset.superseded_by.is_(None))
    ).scalar_one_or_none()
    if row:
        if not allow_update:
            return "conflict", row, None
        # 账本即变更日志：同 (amount, side) → 不是变化。不覆盖、不留 before、
        # 不写审计（与 set_current_value / import 同约定）。
        # side 口径：update 分支不动 side 列，故存量 row.side 就是「将写入的侧」；
        # 调用方显式传了不同的 side 才算不同值（仍走 updated）。历史脏值 NULL
        # 按既有约定归一为 "asset"（与 asset_service 的同值/侧别归一约定同口径）。
        if row.amount == amount and (side is None or (row.side or "asset") == side):
            return "unchanged", row, None
        before = {"amount": str(row.amount), "side": row.side}  # 覆盖前读旧值（审计 before）
        row.amount = amount
        row.source_evidence_id = source_evidence_id
        return "updated", row, before
    row = Asset(asset_name=item.display_name, asset_item_id=item.id,
                amount=amount, asset_date=asset_date,
                side=side or _inherit_side(db, asset_name, asset_date) or "asset",
                source_evidence_id=source_evidence_id,
                cost_basis=resolve_cost_basis(db, item.id, asset_date))  # 2.1 建行冗余
    db.add(row)
    return "created", row, None


def _record_alias(db: Session, item_id: int,
                  name: Optional[str]) -> Optional[int]:
    """把识别名沉淀为目标项的别名（`asset_item_names.origin='alias'`）。

    别名与历史名共用一张表、同一条解析链（ADR-0026 D6）：它们都是「这个名字指向
    这个项」的证据，区别只在于别名不占时间区间（`effective_from` 为 NULL）。
    同项同名重复绑定幂等返回既有行 id，不新增行（append-only 表不能靠唯一索引去重，
    否则同一别名被两个项占用时会互相顶掉——那是撞名，该在裁决时拦，不该在存储层炸）。
    """
    name = (name or "").strip()
    if not name:
        return None
    existing = db.execute(
        select(AssetItemName.id).where(AssetItemName.item_id == item_id,
                                       AssetItemName.name == name)
    ).scalars().first()
    if existing is not None:
        return existing
    row = AssetItemName(item_id=item_id, name=name, origin="alias",
                        effective_from=None)
    db.add(row)
    db.flush()
    return row.id


def _row_final_name(r: PreviewRow, prefix: str) -> Optional[str]:
    """行 → 最终落库全名（校验与写入**同源**，防两处口径分叉）。

    create 行：用户覆盖过名称则直接采用 source_name（不再拼前缀），否则 prefix+source_name；
    其余行用 asset_name（update/zero 的映射目标）。
    """
    if r.action == "create":
        return r.source_name if r.name_overridden else (prefix + (r.source_name or ""))
    return r.asset_name


# 身份裁决策略（ADR-0026 D1–D4）：
#   merge  ＝登记血缘（旧项 → 本行新名），id 会前移
#   bind   ＝本行识别结果其实就是某个**已有项**（OCR 名字与历史名不同）→ 写进目标项、
#            不动 display_name、不建血缘、id 不变，并把本行识别名沉淀为该项目的别名
#   create ＝确认新增（与前缀拼名建新项）
# 撤销该行＝采纳时 skipped，行根本不进 preview.rows，故不需要第四种 action。
_DECISION_ACTIONS = ("merge", "create", "bind")


def _decision_key(r: PreviewRow, pos: int) -> int:
    """行身份：优先 compute_preview 落定的稳定行号（r.index），手搓预览回退位置。

    采用 r.index 而非「当前行号」是因为 apply_user_edits 会按 skipped 删行，
    位置会前移而 frontend 的 identity_decisions 键取自预览响应的 index。
    """
    return r.index if r.index is not None else pos


def _parse_effective_date(raw, default: _date) -> _date:
    if raw is None or raw == "":
        return default
    if isinstance(raw, _date):
        return raw
    try:
        return _date.fromisoformat(str(raw))
    except ValueError:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"effective_date 非法（YYYY-MM-DD）: {raw!r}",
                       details={"field": "identity_decisions.effective_date"})


def _validate_decision(db: Session, raw, row: PreviewRow, prefix: str,
                       data_date: _date, seen_names: set[str]) -> dict:
    """单行身份裁决 → 归一化 dict；任何非法 → ApiError（400/404/409，零写入）。"""
    if not isinstance(raw, dict):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "身份裁决必须为对象",
                       details={"field": "identity_decisions"})
    action = raw.get("action")
    if action not in _DECISION_ACTIONS:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"身份裁决 action 仅支持 {'|'.join(_DECISION_ACTIONS)}",
                       details={"field": "identity_decisions.action"})
    name = _row_final_name(row, prefix)
    out = {"action": action, "name": name}
    if action == "create":
        return out   # 确认新增：与今天完全一样，仅留一条裁决审计

    # ---- bind：本行识别结果＝已有项（名称不一致），写进目标项且 id 不变。
    # 别名只沉淀 create 行的识别名（D5）；update/zero 行的重绑只是换写入目标，
    # 它原本就解析到了另一个项，把旧名塞成目标项别名反而是污染（D3）。
    if action == "bind":
        try:
            target_id = int(raw.get("target_item_id"))
        except (TypeError, ValueError):
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           "bind 必须携带合法 target_item_id",
                           details={"field": "identity_decisions.target_item_id"})
        target = db.get(AssetItem, target_id)
        if target is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {target_id}",
                           details={"field": "identity_decisions.target_item_id"})
        if target.status == "archived":
            raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                           f"已归档资产项不可作为绑定目标（{target.display_name}，R-37）")
        out.update(target_item_id=target_id,
                   alias_name=(name or None) if row.action == "create" else None)
        return out

    # ---- merge：登记血缘（旧项 → 本行新名），随后本行值即后继项首条测量
    try:
        target_id = int(raw.get("target_item_id"))
    except (TypeError, ValueError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "merge 必须携带合法 target_item_id",
                       details={"field": "identity_decisions.target_item_id"})
    target = db.get(AssetItem, target_id)
    if target is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {target_id}",
                       details={"field": "identity_decisions.target_item_id"})
    if target.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                       f"已归档资产项不可作为血缘前身（{target.display_name}，R-37）")
    if not name:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "血缘目标名不能为空",
                       details={"field": "identity_decisions"})
    if resolve_item_id(db, name) is not None:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"本行名称已解析到既有资产项（{name}），无需登记血缘",
                       details={"field": "identity_decisions"})
    if name in seen_names:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"同一批次的多个裁决不能指向同一新名: {name}",
                       details={"field": "identity_decisions"})
    seen_names.add(name)
    link_type = raw.get("link_type") or "merge"
    if link_type not in lineage_service.LINK_TYPES:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"link_type 仅支持 {'|'.join(lineage_service.LINK_TYPES)}",
                       details={"field": "identity_decisions.link_type"})
    split_ratio = raw.get("split_ratio")
    if link_type != "split":
        if split_ratio not in (None, ""):
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           f"{link_type} 不应携带 split_ratio（仅 split）",
                           details={"field": "identity_decisions.split_ratio"})
        split_ratio = None
    reason = raw.get("reason")
    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "血缘承接必须附理由（R-9：承接值是人确认的裁决）")
    principal = raw.get("principal")
    if principal in (None, ""):
        principal = row.source_amount      # 缺省＝本行金额（后继首条测量值）
    try:
        principal = Decimal(str(principal)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"承接口径非法: {principal!r}",
                       details={"field": "identity_decisions.principal"})
    out.update(target_item_id=target_id, link_type=link_type,
               split_ratio=split_ratio, principal=principal,
               reason=str(reason).strip(),
               effective_date=_parse_effective_date(raw.get("effective_date"), data_date))
    return out


def _resolve_identity_decisions(db: Session, preview: ScreenshotPreview, prefix: str,
                                identity_decisions: Optional[dict],
                                data_date: _date,
                                skip_positions: Optional[set[int]] = None,
                                ) -> dict[int, dict]:
    """校验并归一化整批身份裁决（**全量前置，任何缺口 → 400 且零写入**）。

    键＝预览行号（`ScreenshotPreviewRow.index` / `PreviewRow.index`，JSON 里是字符串）。

    D4：**每一行 create 都必须表态**（`create` 或 `bind`），有 `identity_guess`
    的行同样必须表态——`create` 不再有「不表态就默认新建」的向后兼容通道，
    因为那正是静默新建的来源。其余动作（update/zero）零影响。

    `skip_positions`（additive）：本批**不会落账**的行（汇总行）——它们既不该要求
    表态，也不该被校验；传进来就整行跳过（ADR-0029 D6「零副作用」，票 03 D5）。
    """
    raw = identity_decisions or {}
    if not isinstance(raw, dict):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "identity_decisions 必须为对象",
                       details={"field": "identity_decisions"})
    by_key = {str(k): v for k, v in raw.items()}
    plans: dict[int, dict] = {}
    missing: list[dict] = []
    seen_names: set[str] = set()
    bind_targets: dict[int, int] = {}      # target_item_id → 先占用的行号
    skip = skip_positions or set()
    for pos, r in enumerate(preview.rows):
        if pos in skip:
            continue                       # 汇总行不落账：免表态、免校验
        # D4：create 行必须显式表态（create 或 bind）。不表态就采纳，等于让 OCR
        # 名称自己决定「这是新资产」——正是本次要修的静默新建。
        needs_decision = r.action == "create" or bool(r.identity_guess)
        key = _decision_key(r, pos)
        decision = by_key.get(str(key))
        if decision is None:
            if needs_decision:
                missing.append({
                    "key": key,
                    "name": _row_final_name(r, prefix) or r.source_name,
                    "candidates": list((r.identity_guess or {}).get("candidates") or []),
                })
            continue
        plan = _validate_decision(db, decision, r, prefix, data_date, seen_names)
        if plan["action"] == "bind":
            target_id = plan["target_item_id"]
            if target_id in bind_targets:
                raise ApiError(
                    ErrorCode.DUPLICATE_BIND_TARGET_DATE, 409,
                    f"同一批次有两行绑定到同一个资产项（#{target_id}）："
                    f"同一天同一项只能有一条测量值（D7）",
                    details={"target_item_id": target_id,
                             "rows": [bind_targets[target_id], key]})
            bind_targets[target_id] = key
        plans[key] = plan
    if missing:
        raise ApiError(
            ErrorCode.IDENTITY_DECISION_REQUIRED, 400,
            "存在待表态的行（新增行 / 疑似改名）：请逐行确认新增或关联到已有项后再采纳",
            details={"rows": missing})
    return plans


def apply_preview(db: Session, preview: ScreenshotPreview, *, source_group: str,
                  data_date: Optional[_date],
                  evidence_id: Optional[int] = None,
                  image_path: Optional[str] = None,
                  evidence_root: str = "data/evidence",
                  supplement_log: Optional[dict] = None,
                  engine: Optional[str] = None,
                  identity_decisions: Optional[dict] = None,
                  reconciles: Optional[list[int]] = None,
                  batch_id: Optional[int] = None) -> ApplyResult:
    """确认一份待录入清单 → 全量执行写入 + 凭证（v5 步骤 6：采纳＝对复用行富化）。

    两条凭证路径：
    - `evidence_id`（生产路径，router persist_upload 已建行）：加载复用行并
      **富化会话属性**（data_date/supplement_rows/engine 随最近采纳覆盖；
      文件/sha/created_at/source_group_id 永久不可变；采纳留痕由 audit apply_row 每次承载）。
    - `image_path`（legacy 直调/旧调用方）：apply 时**按 (sha, 来源组) find-or-create**
      凭证行——同图同组二次 apply 复用同一行（返回既有 evidence_id，不新建；
      迁移 0033 的复合唯一索引即此口径的存储层兜底），只富化会话属性。
      该分支必须把 `source_group` 解析成来源组行（0029 起凭证只存外键）：
      未知组 → 400 `SOURCE_GROUP_UNKNOWN`，且先于落盘与任何资产写入。

    - update/create/zero 全部按 (资产项, data_date) 覆盖语义写入，挂 source_evidence_id
    - create 行的属性继承自同组现有任一资产项
    - 记录日期 = 截图内数据日期（无则今天）
    - **账本即变更日志**：update/zero 行撞上同日 active 行且 (amount, side) 完全一致
      → 不覆盖、不写 apply_row 审计、不计入 rows_written，只进 `skipped_unchanged`
      （形状：asset_item_id / asset_name / asset_date / amount / side / existing_id，
      与 asset_service、import_service 的同类字段同形）。金额或 side 变了才写。
    - supplement_log（可选，审计）：本次补录结构，JSON 文本存 supplement_rows
    - engine（可选，审计）：识别引擎骨架来源（"ocr"/"ai"）；NULL 语义 = "ocr"

    身份裁决（item-identity-drift D4，additive）：
    - `identity_decisions`（可选）以**预览行号**（`PreviewRow.index`，JSON 键为字符串）
      为键：`{"3": {"action": "merge", "target_item_id": 44, "link_type": "revision",
      "principal": "1200000.00", "effective_date": "2026-09-16", "reason": "改名"},
      "5": {"action": "create"}}`。
    - `merge` ＝先把本行新名登记为 target 的后继（`lineage_service.register_lineage`，
      内部归档旧项自 `effective_date` 起终止计量 + 口径承接 + 审计），再按后继名写本行
      （本行值即后继项首条测量）。`link_type` 缺省 merge；`principal` 缺省＝本行金额；
      `effective_date` 缺省＝本次 data_date。
    - `create` ＝与历史行为完全一致（不建链），仅留一条裁决审计。
    - 带 `identity_guess` 却无裁决的行 → 400 `IDENTITY_DECISION_REQUIRED`
      （details.rows），**全量前置校验，任何缺口＝零写入**（凭证也不建）。
    - 每笔裁决一条审计（entity="screenshot_preview_row", action="identity_decide"）。

    汇总行（ADR-0029 D5/D6，additive）：
    - `reconciles`＝本图被标为「汇总行」的预览行号（与 `skipped` 同形，0 起）；
      命中行**零副作用**：不写 `assets`、不改 `display_name`、不沉淀别名、不动血缘、
      不进 `rows_written`，也免身份表态；回执进 `ApplyResult.reconciled`
      （行号 / 名称 / 金额）与 `zero_skipped_due_to_reconcile`（该行指向的项本来会被
      `zero_names`/`unrecognized='all'` 清零 → 跳过清零，机制照抄 bind）。
      行号越界忽略（与 skipped 同口径）。
    - 汇总行「归谁」不在本函数判定：跨图父子关系与金额核对由
      `POST /api/screenshot-batches/{id}/finalize` 统一建立（票 04 D4：唯一校验点）。

    批次（ADR-0029 D5，additive）：
    - `batch_id` 非空 → **先**校验批次存在且未定案（404 / 409
      `BATCH_ALREADY_FINALIZED`），**先于**任何写入；写入成功后登记成员行
      `screenshot_batch_members(status='applied')` 并把 `open` 推进到 `partial`。
      成员登记发生在采纳提交之后（自己的事务）：登记失败不会回滚已落账的测量值
      ——`partial` 的语义就是「允许部分成功、永不跨图回滚」（票 04 D5）。
    - `batch_id` 为空 → 行为与本期之前**逐字段一致**（旧调用方零影响）。
    """
    data_date = data_date or _date.today()
    config = get_group_config(db, source_group)
    if config is None:
        raise ValueError(f"未知来源组: {source_group}")
    prefix = config["asset_prefix"]
    supplement_json = (json.dumps(supplement_log, ensure_ascii=False)
                       if supplement_log is not None else None)
    # 汇总行（ADR-0029 D5/D6）：行号＝预览行位置（与 apply_user_edits 的 skipped 同口径）
    reconcile_positions = {int(i) for i in (reconciles or [])}
    # 汇总行的命中项：解析既有资产项 id（create 行解析不到 → None，不影响任何写入）。
    # 不做「按名字新建」：汇总行零副作用，绝不因为标了汇总就凭空造资产项。
    reconcile_targets: set[int] = set()
    reconcile_receipts: list[dict] = []
    for pos, r in enumerate(preview.rows):
        if pos not in reconcile_positions:
            continue
        full_name = _row_final_name(r, prefix)
        resolved = resolve_item_id(db, full_name) if full_name else None
        if resolved is not None:
            reconcile_targets.add(resolved)
        reconcile_receipts.append({
            "preview_index": r.index,
            "asset_name": full_name,
            "amount": str(Decimal(str(r.source_amount)).quantize(Decimal("0.01"))),
            "asset_item_id": resolved,
        })
    # 批次前置校验（additive）：批次非法时**零写入**（凭证与资产行都不动）
    if batch_id is not None:
        from services import batch_service      # 延迟 import：batch_service 依赖本模块
        batch_service.require_open(db, batch_id)
    # 身份裁决全量前置校验：先于凭证/行写入，缺口时库里一行不多（含凭证）。
    # 汇总行不落账 → 免表态（否则「标了汇总」反而被迫为它做新增/关联裁决）。
    identity_plan = _resolve_identity_decisions(
        db, preview, prefix, identity_decisions, data_date,
        skip_positions=reconcile_positions)

    if evidence_id is not None:
        ev = db.get(ScreenshotEvidence, evidence_id)
        if ev is None:
            raise ValueError(f"凭证不存在: {evidence_id}")
        # 采纳富化：会话属性随最近一次采纳覆盖（文件本体不可变）
        ev.data_date = data_date
        ev.supplement_rows = supplement_json
        ev.engine = engine
        db.flush()
    else:
        group = resolve_source_group(db, source_group)  # 未知组 → 400，先于落盘
        # find-or-create（与 `get_or_create_evidence` 同一口径：同组同 sha 一行，
        # 存储层由 ux_screenshot_evidence_sha_group(file_sha256, source_group_id) 兜底）。
        # 但**不复用 `get_or_create_evidence`**：legacy 传进来的图已经在盘上
        # （不经过 persist_upload），那个函数会 copyfile 到规范路径并改名；这里
        # 只按「查 → 无则建行」处理，落盘沿用 `_save_evidence_file`（同内容幂等，
        # 复用既有文件不覆盖）。
        sha = _file_sha256(image_path)
        ev = db.execute(
            select(ScreenshotEvidence)
            .where(ScreenshotEvidence.file_sha256 == sha,
                   ScreenshotEvidence.source_group_id == group.id)
            .order_by(ScreenshotEvidence.id)
        ).scalars().first()
        if ev is None:
            rel_path, _ = _save_evidence_file(image_path, data_date, evidence_root)
            ev = ScreenshotEvidence(
                source_group_id=group.id, file_path=rel_path, file_sha256=sha,
                original_filename=clean_original_filename(os.path.basename(image_path)),
                data_date=data_date,
                supplement_rows=supplement_json, engine=engine,
            )
            db.add(ev)
            db.flush()  # 取得凭证 id
        else:
            # 复用既有行：文件/sha/created_at/source_group_id 永久不可变（与
            # `evidence_id` 分支同口径），只富化会话属性（最近一次采纳覆盖）
            ev.data_date = data_date
            ev.supplement_rows = supplement_json
            ev.engine = engine
            db.flush()


    # 新增行分类（ADR-0019）：不再从同组模板行继承记录级属性——分类在资产项上，
    # 只补截图中可解析的机构/渠道，其余留空（draft 允许，D9）。
    written: list[PreviewRow] = []
    conflicts: list[dict] = []
    skipped_unchanged: list[dict] = []  # 账本即变更日志：值没变 → 不写入、不写审计
    changes: list[dict] = []  # 步骤 4 审计：每 written 行一条（commit 前统一 record）
    bindings: list[dict] = []           # 绑定回执（additive）
    zero_skipped: list[dict] = []       # 因绑定而跳过的清零（D10）
    # 绑定优先于未识别项清零（D10）：目标项在本批「未识别项」里本来要被 zero，
    # 但同一个 item 已经收到本批一条测量值，再清零就自相矛盾——先剔除清零。
    bound_targets = {plan["target_item_id"] for plan in identity_plan.values()
                     if plan["action"] == "bind"}
    zero_skipped_reconcile: list[dict] = []   # 因汇总行而跳过的清零（D6，additive）
    for pos, r in enumerate(preview.rows):
        if pos in reconcile_positions:
            # 汇总行：零副作用（不写 assets / 不改 display_name / 不留别名 / 不动血缘），
            # 也不进 rows_written；只参与 finalize 的跨图核对与留痕（票 03 D5）
            continue
        if r.action == "keep":
            continue  # 未识别项保持原值：不写入
        full_name = _row_final_name(r, prefix)   # create: 覆盖名直用 / 否则拼前缀
        amount = r.source_amount if r.action in ("create", "update") else Decimal("0")
        if full_name is None:  # 防御：update/zero 行必有映射目标
            continue
        key = _decision_key(r, pos)
        decision = identity_plan.get(key)
        if r.action == "zero" and bound_targets:
            zero_target = resolve_item_id(db, r.asset_name) if r.asset_name else None
            if zero_target in bound_targets:
                zero_skipped.append({"asset_name": r.asset_name,
                                     "asset_item_id": zero_target,
                                     "reason": "zero_skipped_due_to_bind"})
                continue
        if r.action == "zero" and reconcile_targets:
            # 汇总行优先于未识别项清零（票 03 D4，机制同 bind）：目标项已被本批的汇总行
            # 认领 → 它这次不落账，但也不能被清零（否则「既没记又被抹掉」）。
            zero_target = resolve_item_id(db, r.asset_name) if r.asset_name else None
            if zero_target in reconcile_targets:
                zero_skipped_reconcile.append({
                    "asset_name": r.asset_name,
                    "asset_item_id": zero_target,
                    "reason": "zero_skipped_due_to_reconcile"})
                continue
        bind_target: Optional[int] = None
        alias_id: Optional[int] = None
        if decision and decision["action"] == "merge":
            # 先登记血缘：旧项自 effective_date 起归档（退出 as-of 计量）+ 口径承接
            # + 链/承接/归档三条既有审计；随后按后继名写本行＝后继首条测量。
            # 注意 register_lineage 内部自带 commit（失败安全序），故此处之后不再回滚。
            lineage_service.register_lineage(
                db, [decision["target_item_id"]], full_name,
                decision["link_type"], decision["split_ratio"], decision["principal"],
                decision["reason"], decision["effective_date"])
        elif decision and decision["action"] == "bind":
            # 绑定：本行识别结果就是目标项（OCR 名与历史名不同）。不建血缘、不改
            # display_name、id 不变；识别名沉淀为别名，下次 OCR 再看到同样的字就能
            # 自己解析回这一项（D5）。
            bind_target = decision["target_item_id"]
            alias_id = _record_alias(db, bind_target, decision.get("alias_name"))
        # 人裁决 action=update/zero 允许覆盖同日 active 行；create 撞上既有行 → 显式冲突。
        # 绑定行等价于「update 那条目标项的当日值」（D9），故 allow_update=True。
        status, row, before = _upsert_asset(
            db, full_name, amount, data_date, ev.id, side=r.side,
            allow_update=(True if bind_target is not None
                          else r.action in ("update", "zero")),
            source_group=source_group, item_id=bind_target)
        if bind_target is not None:
            # 回执让人能核对「这一行到底写进了谁」（additive，旧调用方零影响）
            bindings.append({
                "key": key,
                "preview_index": r.index,
                "target_item_id": bind_target,
                "asset_id": row.id if row else None,
                "status": status,
                "alias_added": alias_id is not None,
            })
        if status == "conflict":
            conflicts.append({
                "source_name": r.source_name,
                "asset_name": full_name,
                "asset_date": data_date.isoformat(),
                "existing_id": row.id if row else None,
                "reason": "create_on_active_same_day",
            })
            continue  # 冲突行不留审计（未发生写入）
        if status == "unchanged":
            # 账本即变更日志：同 (amount, side) 不是一条变化 → 不覆盖、不留 before、
            # 不写 apply_row 审计，只回报（与 set_current_value / import 同约定）。不进 written，故也不计入 rows_written。
            # 身份裁决只挂在 create 行（create 撞同日行必为 conflict），故此处不丢留痕。
            skipped_unchanged.append({
                "asset_item_id": row.asset_item_id if row else None,
                "asset_name": row.asset_name if row else full_name,
                "asset_date": data_date.isoformat(),
                "amount": str(Decimal(str(amount)).quantize(Decimal("0.01"))),
                "side": ((row.side or "asset") if row else (r.side or "asset")),
                "existing_id": row.id if row else None,
            })
            continue
        written.append(replace(r, asset_name=row.asset_name if row else full_name))
        # 步骤 4 审计：每 written 行一条 apply_row（凭证 ev.id；update 带覆盖前旧值）
        db.flush()  # 取 row.id
        changes.append(_change(
            "asset_snapshot", row.id if row else None, "apply_row",
            before=before,
            after={"amount": str(Decimal(str(amount)).quantize(Decimal("0.01"))),
                   "side": (row.side if row else "asset")},
            evidence_ref=ev.id))
        if decision:
            # 身份裁决留痕（人决策本身可追溯）：before＝当时的提示，after＝实际执行
            decided = {"action": decision["action"],
                       "asset_item_id": row.asset_item_id if row else None,
                       "amount": str(Decimal(str(amount)).quantize(Decimal("0.01")))}
            if decision["action"] == "merge":
                decided.update(target_item_id=decision["target_item_id"],
                               link_type=decision["link_type"],
                               effective_date=decision["effective_date"].isoformat(),
                               principal=str(decision["principal"]))
            elif decision["action"] == "bind":
                # 绑定留痕：写进了哪一项、有没有新增别名（display_name 不变、id 不变）
                decided.update(target_item_id=bind_target,
                               alias_added=alias_id is not None,
                               display_name_unchanged=True)
            changes.append(_change(
                "screenshot_preview_row", key, "identity_decide",
                before={"identity_guess": r.identity_guess}, after=decided,
                evidence_ref=ev.id))

    for change in changes:
        audit_service.record(db, change)  # 同事务：失败阻断采纳
    db.commit()
    if batch_id is not None:
        from services import batch_service      # 延迟 import（同上）
        # 采纳已提交 → 再登记成员（自己的事务）。登记失败不回滚测量值：批次语义是
        # 「允许部分成功、永不跨图回滚」（票 04 D5），前端重试会重复登记（幂等刷新）。
        batch_service.register_member(db, batch_id, ev.id)
    return ApplyResult(
        evidence_id=ev.id, data_date=data_date,
        asset_date=data_date.isoformat(), rows_written=len(written),
        rows=written, conflicts=conflicts,
        skipped_unchanged=skipped_unchanged,
        bindings=bindings, zero_skipped_due_to_bind=zero_skipped,
        reconciled=reconcile_receipts,
        zero_skipped_due_to_reconcile=zero_skipped_reconcile,
    )
