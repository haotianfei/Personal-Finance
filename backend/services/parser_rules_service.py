"""规则/词表**写入路径**的校验收口（P1b）。

口径来源：`.scratch/parser-rules-refactor/spec.md` D2.2（全局可删但留痕）与 D4
（未知键 fail-fast、写侧统一收口）。本模块是「写」的单一入口：

- `validate_group_rules()`：`source_groups.rules` 的唯一校验/归一函数。四条写路径
  里本期接入两条（`PUT /api/source-groups/{id}` 整串替换、
  `PUT /api/source-groups/{id}/parse-rules` 定向 merge）；教学规则三端点属另一车道。
- `list_vocabularies()` / `update_vocabulary()` / `reset_vocabulary()`：全局词表
  （`parser_vocabularies` 表）的读写。生效计算本身仍在 `services/parser_rules.py`
  （`load_vocabularies` + `EffectiveVocabularies`），本模块只负责**校验 → 落库 → 留痕**。

**本层不做语义处理**：「组里配了就替换出厂词」是 `parser_rules.with_group_overrides`
的职责（D2.3），这里只保证存进列里的值是合法形状。

关于组级合法键集合（`GROUP_RULE_KEYS`）——本期取**解析引擎当前真正消费的四个键**：

| 键 | 消费点 | 形态 |
|---|---|---|
| `layout` | `screenshot_service.organize_blocks` 强制布局 | `horizontal/vertical/multi_column/card` 或 null |
| `amount_header` | 同上（金额列标题 trigger） | `str` 或 `list[str]`（真实库两种形态并存，统一归一为 list） |
| `amount_col` | `organize_blocks` → `_organize_multicolumn(amount_col)` | 非负整数 |
| `ignore_words` | `organize_blocks` 块级丢弃 + `parser_rules.with_group_overrides` | `list[str]` |

除此四键外的**任何**顶层键一律 422（`SOURCE_GROUP_RULES_INVALID`），包括：
① 已搬进 `source_group_learned_rules` 表的 `rename`/`ignore`/`exclude`（0031）；
② 0031 清掉的历史死键 `name_col`/`total_header`/`amount_suffix`/`anchor_summary`；
③ 拼错的键（如 `nmae_col`）——D4 的初衷就是让这类静默失效暴露出来。

审计留痕的形状（`audit_log` 无 reason 列，列集合自 0018 冻结，故 reason 并入
before/after 的 `reason` 键，与 D2.2「含 reason 与 before/after 两个列表」等价）：

- 更新：`action="parser_vocabulary_update"`，`before={"add":[...],"remove":[...]}`
  （首次落库时 NULL＝创建），`after={"add":[...],"remove":[...],"reason":"..."}`
- 重置：`action="parser_vocabulary_reset"`，`before={"add":[...],"remove":[...],"reason":"..."}`，
  `after=NULL`（NULL＝删除，沿用审计表既有约定）
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import ParserVocabulary, SourceGroup
from services import audit_service
from services.parser_rules import (
    BUILTIN_VOCABULARIES,
    VOCABULARY_KEYS,
    load_vocabularies,
    validate_vocabulary_payload,
)

# ---------------- 组级 rules（source_groups.rules） ----------------

# 合法顶层键（顺序即报文里的展示序）。见模块 docstring 的消费点表格。
GROUP_RULE_KEYS = ("layout", "amount_header", "amount_col", "ignore_words")
# 强制布局枚举（与 screenshot_service.organize_blocks 的四个分支一一对应）。
LAYOUT_VALUES = ("horizontal", "vertical", "multi_column", "card")


def _reject(message: str, *, details: dict[str, Any] | None = None) -> None:
    raise ApiError(ErrorCode.SOURCE_GROUP_RULES_INVALID, 422, message, details)


def _type_name(value: Any) -> str:
    return type(value).__name__


def _normalize_str_list(field: str, value: Any) -> list[str]:
    """字符串数组归一：逐项 trim、丢空、按序去重。非数组/含非字符串 → 422。"""
    if not isinstance(value, (list, tuple)):
        _reject(f"{field} 必须是字符串数组（收到 {_type_name(value)}）")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            _reject(f"{field} 必须是字符串数组（含非字符串项：{_type_name(item)}）")
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _normalize_amount_header(value: Any) -> list[str]:
    """`amount_header` 两种历史形态都收：`str` → `[str]`，`list[str]` 原序去重。

    真实库里两种写法并存（一个组是 list，也可能有组写 str），解析侧
    `rules.get("amount_header") or DEFAULT_AMOUNT_HEADERS` 对 str 会**逐字符**
    迭代——这是个隐患，故写入侧统一归一成 list，读侧无需再分支。
    """
    if isinstance(value, str):
        value = [value]
    return _normalize_str_list("amount_header", value)


def validate_group_rules(payload: Any) -> dict:
    """校验并归一 `source_groups.rules`，返回**归一后**的 dict（恒不为 None）。

    入参形态（三者都收，因为存量契约不统一）：

    - `None` / 空串 / `{}` → `{}`（无配置；`rules` 列 NULL 与 `'{}'` 等价，写侧统一成 NULL）
    - JSON 字符串（`create_source_group` / `PUT` 的既有契约就是字符串）→ 先 `json.loads`
    - dict → 直接校验

    逐键规则（`layout` / `amount_header` / `amount_col` / `ignore_words` 之外一律 422）：

    - `layout`：`LAYOUT_VALUES` 之一；null/空串 → 删键（回落自动检测）
    - `amount_header`：`str` 或 `list[str]`；清洗后为空 → 删键（回落出厂词表）
    - `amount_col`：非负整数；null → 删键
    - `ignore_words`：`list[str]`；清洗后为空 → 删键

    未知键 → `ApiError(SOURCE_GROUP_RULES_INVALID, 422)`，报文含未知键名与合法键集合
    （键名是字段名不是数据，可以回显；**值一律不进报文**，里面可能有真实机构名）。
    """
    raw = payload
    if raw is None:
        return {}
    if isinstance(raw, str):
        text_value = raw.strip()
        if not text_value:
            return {}
        try:
            raw = json.loads(text_value)
        except (TypeError, ValueError):
            _reject("rules 不是合法 JSON，无法保存（坏 JSON 会让该组的规则整体静默失效）")
    if not isinstance(raw, dict):
        _reject(f"rules 必须是 JSON 对象（收到 {_type_name(raw)}）")

    unknown = [k for k in raw if k not in GROUP_RULE_KEYS]
    if unknown:
        _reject(
            "rules 含未识别的顶层键：" + "、".join(str(k) for k in unknown)
            + "（合法键：" + ", ".join(GROUP_RULE_KEYS) + "）",
            details={"unknown_keys": [str(k) for k in unknown],
                     "allowed_keys": list(GROUP_RULE_KEYS)},
        )

    out: dict[str, Any] = {}

    if "layout" in raw:
        value = raw["layout"]
        if value is None or (isinstance(value, str) and not value.strip()):
            pass                                   # 删键：回落自动检测
        elif not isinstance(value, str):
            _reject("layout 必须是字符串（" + " / ".join(LAYOUT_VALUES)
                    + f"）或 null，收到 {_type_name(value)}")
        elif value.strip() not in LAYOUT_VALUES:
            _reject(f"layout 必须为 {' / '.join(LAYOUT_VALUES)} 之一"
                    "（或 null 回落自动检测）")
        else:
            out["layout"] = value.strip()

    if "amount_header" in raw:
        headers = _normalize_amount_header(raw["amount_header"])
        if headers:
            out["amount_header"] = headers

    if "amount_col" in raw:
        value = raw["amount_col"]
        if value is None:
            pass
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _reject(f"amount_col 必须为非负整数或 null（收到 {_type_name(value)}）")
        else:
            out["amount_col"] = value

    if "ignore_words" in raw:
        words = _normalize_str_list("ignore_words", raw["ignore_words"])
        if words:
            out["ignore_words"] = words

    return out


def serialize_group_rules(rules: dict) -> str | None:
    """归一后的 dict → 存列形态（空 dict 写 NULL，与 0031 的归一约定一致）。"""
    if not rules:
        return None
    return json.dumps(rules, ensure_ascii=False)


# ---------------- 全局词表（parser_vocabularies） ----------------

_ENTITY = "parser_vocabulary"
_ACTION_UPDATE = "parser_vocabulary_update"
_ACTION_RESET = "parser_vocabulary_reset"


def _require_key(key: str) -> str:
    """词表 key 必须属于 `VOCABULARY_KEYS`，否则 404。

    选 404 而不是 422 的理由：七个 key 是**固定的资源集合**（`GET /api/parser/vocabularies`
    会列出来），`{key}` 是路径里的资源标识——路径段指向不存在的资源即 404 NOT_FOUND。
    422 留给**请求体**不合法（列表形态、remove 无用项、缺 reason 等），两类错误不混淆。
    """
    if key not in VOCABULARY_KEYS:
        raise ApiError(
            ErrorCode.NOT_FOUND, 404,
            f"未知词表 key：{key}（合法 key：{', '.join(VOCABULARY_KEYS)}）",
            details={"key": key, "allowed_keys": list(VOCABULARY_KEYS)},
        )
    return key


def _require_reason(reason: Any) -> str:
    """词表写入的 reason 必填（D2.2：全局改动必须留痕），空白视同缺失。"""
    text_value = reason.strip() if isinstance(reason, str) else ""
    if not text_value:
        _reject("词表写入必须填写 reason（全局生效，需留痕）")
    return text_value


def _parse_items(raw: Any) -> list[str]:
    """库列（JSON 数组字符串）→ 字符串列表；坏值一律按空处理（读不炸写）。"""
    try:
        parsed = json.loads(raw if raw is not None else "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [w for w in parsed if isinstance(w, str)]


def _row_of(db: Session, key: str) -> ParserVocabulary | None:
    return db.execute(
        select(ParserVocabulary).where(ParserVocabulary.key == key)
    ).scalar_one_or_none()


def _affected_groups(db: Session) -> int:
    """当前**启用**的来源组数（前端提示「将在 N 个来源组生效」的 N）。"""
    try:
        return int(db.execute(
            select(func.count()).select_from(SourceGroup)
            .where(SourceGroup.enabled.is_(True))
        ).scalar() or 0)
    except (OperationalError, ProgrammingError):   # 桩库没有 source_groups 表
        return 0


def _vocabulary_view(db: Session, key: str, *, changed: bool) -> dict:
    """单个词表的完整视图（builtin / add / remove / effective + 影响面）。"""
    row = _row_of(db, key)
    add = _parse_items(row.add_items) if row is not None else []
    remove = _parse_items(row.remove_items) if row is not None else []
    effective = list(load_vocabularies(db).get(key))
    return {
        "key": key,
        "builtin": list(BUILTIN_VOCABULARIES[key]),
        "add": add,
        "remove": remove,
        "effective": effective,
        "affected_groups": _affected_groups(db),
        "changed": changed,
        "updated_at": (row.updated_at.isoformat()
                       if row is not None and row.updated_at else None),
    }


def list_vocabularies(db: Session) -> list[dict]:
    """七个词表的全量视图（顺序即 `VOCABULARY_KEYS`，前端直接照序渲染）。

    每项含 `builtin`（出厂，只读）/ `add` / `remove`（全局增量）/ `effective`
    （`(builtin ∪ add) − remove`，与解析侧同一个 `load_vocabularies` 口径，不另算一份）。
    """
    return [_vocabulary_view(db, key, changed=False) for key in VOCABULARY_KEYS]


def update_vocabulary(db: Session, key: str, add, remove, reason,
                      actor: str = "owner") -> dict:
    """整份覆盖某个词表的 add/remove 增量，并写一条 `parser_vocabulary_update` 审计。

    **PUT 语义：整份覆盖**——未提供的列表按空处理（`add` 不传＝清空新增项），
    要回出厂值用 `reset_vocabulary`（语义更明确，且审计 action 不同）。

    幂等：add/remove 与现状完全一致 → **不写库、不写审计**，返回 `changed=False`
    （否则前端「保存」按钮点两次就多两条噪音审计）。

    校验顺序（失败即 422，无任何写入）：
    未知 key → 404（见 `_require_key`）→ reason 必填 → `validate_vocabulary_payload`
    （列表形态 / add∩remove 冲突 / remove 无用项 / 删出厂词需 reason）。

    返回：`{key, builtin, add, remove, effective, affected_groups, changed, updated_at}`；
    `affected_groups` ＝ 当前启用的来源组数，供前端提示「将在 N 个来源组生效」。
    """
    _require_key(key)
    reason_text = _require_reason(reason)
    validate_vocabulary_payload(key, add, remove, reason_text)

    add_items = _normalize_str_list("add", add) if add is not None else []
    remove_items = _normalize_str_list("remove", remove) if remove is not None else []

    row = _row_of(db, key)
    current_add = _parse_items(row.add_items) if row is not None else []
    current_remove = _parse_items(row.remove_items) if row is not None else []

    if row is not None and add_items == current_add and remove_items == current_remove:
        return _vocabulary_view(db, key, changed=False)      # 零写入、零审计

    before = {"add": current_add, "remove": current_remove} if row is not None else None
    after = {"add": add_items, "remove": remove_items, "reason": reason_text}

    if row is None:
        db.add(ParserVocabulary(key=key,
                                add_items=json.dumps(add_items, ensure_ascii=False),
                                remove_items=json.dumps(remove_items, ensure_ascii=False)))
    else:
        row.add_items = json.dumps(add_items, ensure_ascii=False)
        row.remove_items = json.dumps(remove_items, ensure_ascii=False)
    db.flush()

    audit_service.record(db, audit_service.make_change(
        _ENTITY, None, _ACTION_UPDATE, before=before, after=after), actor=actor)
    db.commit()

    return _vocabulary_view(db, key, changed=True)


def reset_vocabulary(db: Session, key: str, reason, actor: str = "owner") -> dict:
    """删掉某个词表的增量行 ＝ 完全回退出厂值，并写一条 `parser_vocabulary_reset` 审计。

    幂等：本来就没有增量行 → 不写审计，返回 `changed=False`（重置一个未改过的词表
    不是一次「变更」）。`reason` 同样必填（删的是全局配置，留痕口径同 update）。
    """
    _require_key(key)
    reason_text = _require_reason(reason)

    row = _row_of(db, key)
    if row is None:
        return _vocabulary_view(db, key, changed=False)

    before = {"add": _parse_items(row.add_items),
              "remove": _parse_items(row.remove_items),
              "reason": reason_text}
    db.delete(row)
    db.flush()
    audit_service.record(db, audit_service.make_change(
        _ENTITY, None, _ACTION_RESET, before=before, after=None), actor=actor)
    db.commit()

    return _vocabulary_view(db, key, changed=True)