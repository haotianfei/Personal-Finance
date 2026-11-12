"""来源组规则的读写入口：组级解析配置（JSON 列）+ 教学规则（独立表）。

**两代存储，一套出口。**

一代（拆表前）：所有规则都是 `SourceGroup.rules` 的一份 JSON 字符串——组级解析
配置（`layout` / `amount_header` / `amount_col` / `ignore_words`）与教学规则
（`rename` / `ignore` / `exclude`）混在同一列。写入形态是「读取整份 → 内存修改 →
整份写回」，截图页 `POST .../correction-rules` 的增量追加与任何其他规则写并发
交错时，后提交者会用陈旧快照覆盖前者（丢失更新）。根治方式：组级解析键的写统一
经由 `mutate_rules()`——同一把进程内锁内完成 重读行 → 解析 → 变更 → 写回 → commit。

二代（迁移 0031 起）：教学规则搬进 `source_group_learned_rules` 表，本模块的
`add/delete/update/swap` 四族函数直接做**行级**增删改（不再是「整份替换」——
两个写者改不同条目时天然不互相覆盖），判重交给唯一索引
`(source_group_id, rule_type, match, COALESCE(replace,''))`，并顺手带上
`origin` / `sort_order` / 时间戳 / P2 的 `hit_count` 位。

**对外契约不变。** `serialize_group()` 序列化时把表里的教学规则拼回 `rules` 载荷
（见 `_serialize_rules`），所以 GET /api/source-groups 与四个 correction-rules 端点
的响应形状、键名与拆表前一致，前端「管理规则」面板无需改动。

单进程前提：部署为单 uvicorn 进程（start.sh，无 --workers 多开），threading.Lock
足以互斥全部规则写。若将来改为多进程 / 多实例部署，进程内锁失效，须换成
数据库级互斥（如 SQLite `BEGIN IMMEDIATE` 事务、共享锁表或 Redis 锁）。

另含两处共享工具：serialize_group（来源组响应序列化，routers 复用，保证规则写
端点与 GET /api/source-groups 单项同形）与历史校正条目身份定位助手
（rename=(match, replace)，ignore/exclude=match）——后者是拆表前的内存判重口径，
0031 起判重由唯一索引承担，此处保留供诊断脚本与兼容调用。
"""
import json
import threading
from datetime import datetime
from typing import Callable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from models import SourceGroup, SourceGroupLearnedRule
from services import audit_service, screenshot_service

# 规则写全链路互斥锁（单进程部署前提下即为全局写锁），见模块 docstring。
_RULES_LOCK = threading.Lock()

# 「校正规则」三型：**对外契约的取值域**（HTTP `type` 参数、`rules` 载荷的三个键）。
# 勿把 aggregate 并进来——那会同时改变四个端点接受的取值与 GET 响应的键集。
CORRECTION_RULE_TYPES = ("rename", "ignore", "exclude")

# 汇总行词表（ADR-0029 D7 / 票 03 D1）：与三型并列存储、语义互不影响（不删行、不改名、
# 不参与清零候选过滤），只喂 preview 的 `aggregate_hint`。**不进 `CORRECTION_RULE_TYPES`**。
AGGREGATE_RULE_TYPE = "aggregate"

# 表允许写入的全部 rule_type（DB CHECK 是同一集合，见 models.SourceGroupLearnedRule）
LEARNED_RULE_TYPES = CORRECTION_RULE_TYPES + (AGGREGATE_RULE_TYPE,)


class RuleNotFoundError(Exception):
    """按身份定位校正条目失败（组存在但该条不存在）→ 上层映射 404。"""


class RuleConflictError(Exception):
    """编辑后的条目与同类型其他条目身份重复 → 上层映射 409。"""


# ---------------- 序列化（共享，前后端契约：与 GET 单项同形） ----------------

def _base_rules(sg: SourceGroup, *, strict: bool) -> Optional[dict]:
    """`sg.rules` 列 → dict。两张错误策略的唯一分叉点（spec D9）。

    - `strict=True`（解析侧）：坏 JSON / 顶层非对象 → 抛
      `ApiError(SOURCE_GROUP_RULES_INVALID, 422)`，复用
      `screenshot_service._parse_group_rules`（同一实现，不是复制判断）。
    - `strict=False`（展示侧）：坏 JSON / 非对象 → 返回 `None`，由调用方**原样回显
      列内容**——展示路径绝不趁机用表内容掩盖脏数据。

    空串/None 两种策略下都视同「没有规则」，返回 `{}`。
    """
    if not sg.rules:
        return {}
    if strict:
        return dict(screenshot_service._parse_group_rules(sg) or {})
    try:
        parsed = json.loads(sg.rules)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return dict(parsed)


def _merged_rules(db: Session, sg: SourceGroup, *, strict: bool) -> tuple:
    """**「列 + 表」合并的唯一实现**（spec D9）。

    返回 `(merged_or_None, has_learned)`：

    - 第一项 `None` 表示列不可解析（仅 `strict=False` 会出现），调用方原样回显列。
    - 第二项表示表里**确有**教学规则被并了进来——展示侧据此决定「要不要重新 dumps」，
      以保住「没有教学规则时逐字节回显列」的契约（`PUT .../parse-rules` 空 body
      纯读回等场景依赖它）。

    合并口径：
    1. 先取列内容**整份**（拆表前解析侧拿到的本来就是整份列 dict；未知键对
       `organize_blocks` 是惰性的，保留它们可保证历史脏键零行为变化）；
    2. 再用表里三个类型键**覆盖**同名键，且只在表里该类型**非空**时覆盖——空类型
       与「缺键」在旧读路径里等价（0031 docstring 的归一裁决），因此空表不掩盖列里
       可能残留的历史教学键。
    """
    base = _base_rules(sg, strict=strict)
    if base is None:
        return None, False
    extra = {rtype: entries
             for rtype, entries in correction_rules_of(db, sg.id).items() if entries}
    if not extra:
        return base, False
    merged = dict(base)
    merged.update(extra)
    return merged, True


def _serialize_rules(sg: SourceGroup) -> Optional[str]:
    """对外的 `rules` 载荷 = 组级解析配置 JSON（列）+ 教学规则（独立表）。

    0031 把 `rename`/`ignore`/`exclude` 搬出 JSON 后，`sg.rules` 列里只剩组级解析
    键；但前端与既有客户端一直从 `rules` 读三键，直接原样回列会让「管理规则」面板
    突然空白。故序列化时把表内容拼回（合并口径见 `_merged_rules`，此处只决定**输出形态**）：

    - 该组没有任何教学规则 → **原样返回列内容**（一个字节都不重排，保证
      `PUT .../parse-rules` 空 body 等「纯读回」场景的逐字节契约）；
    - 列内容不是合法 JSON 对象（历史脏数据）→ 同样原样返回，**不趁机用表内容
      掩盖脏数据**（解析侧会以 422 把问题暴露出来）。

    会话不可用（对象已 detach，例如纯函数级单测直接构造的 SourceGroup）时退回
    列内容，行为与拆表前完全一致。
    """
    db = object_session(sg)
    if db is None or sg.id is None:
        return sg.rules
    merged, has_learned = _merged_rules(db, sg, strict=False)
    if merged is None or not has_learned:
        return sg.rules                     # 脏数据或纯列内容：逐字节回显
    return json.dumps(merged, ensure_ascii=False)


def serialize_group(sg: SourceGroup) -> dict:
    """SourceGroup → API 响应 dict（原 routers/source_groups._serialize_group）。

    `rules` 键由 `_serialize_rules` 组装（列 JSON + 教学规则表），与拆表前同形。
    """
    return {
        "id": sg.id,
        "slug": sg.slug,
        "label": sg.label,
        "asset_prefix": sg.asset_prefix,
        "guide": sg.guide,
        "guide_image": sg.guide_image,
        "rules": _serialize_rules(sg),
        "enabled": sg.enabled,
        "sort_order": sg.sort_order,
        "account_ids": [i.id for i in sg.institutions],   # 键名沿用前端契约，值为机构 id
        "created_at": sg.created_at.isoformat() if sg.created_at else None,
        "updated_at": sg.updated_at.isoformat() if sg.updated_at else None,
    }


# ---------------- 读-改-写核心 ----------------

def get_group(db: Session, key) -> Optional[SourceGroup]:
    """按 int id 或 slug/label 字符串解析来源组；无匹配返回 None。

    字符串键语义与 `screenshot_service.get_source_group` **同一实现**（slug 或
    label 皆可定位）——0029 把「组引用」收成外键后，全库只允许一套解析口径：
    凭证写入走 `screenshot_service.resolve_source_group`（未知即 400），本函数
    服务 correction-rules 端点（未知即 404），两侧共用同一匹配函数，避免漂移。
    """
    if key is None:
        return None
    if isinstance(key, int):
        return db.get(SourceGroup, key)
    return screenshot_service.get_source_group(db, key)


def parse_rules(sg: SourceGroup, *, lenient: bool = False) -> dict:
    """容错解析 SourceGroup.rules → dict。

    - 空 / 全空白 → {}；
    - 合法 JSON 对象 → 该 dict；
    - 坏 JSON / 非对象：lenient=True 退回 {}（历史容错行为）；否则抛中文
      ValueError，由路由层映射 400（新端点默认严格，避免静默重置用户数据）。

    0031 起本列**只承载组级解析配置**（layout / amount_header / amount_col /
    ignore_words）；教学规则三键已搬进 `source_group_learned_rules`，读它们请用
    `correction_rules_of()`，不要从这里取。
    """
    raw = sg.rules
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    parsed = None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        pass
    if isinstance(parsed, dict):
        return parsed
    if lenient:
        return {}
    raise ValueError(f"来源组 {sg.slug} 的 rules 不是合法 JSON 对象，无法安全修改")


def mutate_rules(db: Session, key, mutator: Callable[[dict], Optional[dict]],
                 *, lenient_parse: bool = False,
                 audit: Optional[Callable[[Session, SourceGroup, dict, dict],
                                          None]] = None) -> Optional[SourceGroup]:
    """锁内完成来源组 `rules` 列（组级解析配置）的读-改-写。

    0031 之后本函数**只服务组级解析键**（`PUT /api/source-groups/{id}/parse-rules`
    是唯一调用方）：教学规则的写不再经过这里，走下面的 `add/delete/update/swap`
    行级函数（表 `source_group_learned_rules`）。**刻意保留**而非删除——它是
    「整份 JSON 读-改-写」在单进程下的原子化范式，parse-rules 端点仍依赖它，
    且删除公共函数会波及既有测试与外部脚本。

    流程：加锁 → expire 会话缓存并重新读取 group 行（防止本会话持锁前读到的
    陈旧快照）→ parse_rules → mutator(raw) 就地修改或返回新 dict →
    json.dumps(ensure_ascii=False) 写回 → commit → 返回更新后的 SourceGroup。

    返回值 / 异常约定：
    - group 不存在 → 返回 None（不调用 mutator），上层映射 404「来源组不存在」；
    - mutator 返回 None 或抛 RuleNotFoundError → 定位失败，不写库，
      前者统一返回 None、后者原样上抛，上层映射 404；
    - mutator 抛的其他异常（如 RuleConflictError / ValueError / HTTPException）
      原样上抛，会话已 rollback。

    `audit`（可选）：`(db, sg, before_rules, after_rules) → None` 的留痕回调，
    在**锁内、commit 之前**调用（ADR-0027：同事务，失败即整事务回滚）。调用方
    自带 `audit_service.record`，本模块不猜 entity/action 与快照口径。
    """
    with _RULES_LOCK:
        try:
            db.expire_all()  # 丢弃锁外读到的过期 ORM 状态，保证锁内重读
            sg = get_group(db, key)
            if sg is None:
                return None
            rules = parse_rules(sg, lenient=lenient_parse)
            updated = mutator(rules)
            if updated is None:  # 定位失败：不写回
                return None
            sg.rules = json.dumps(updated, ensure_ascii=False)
            if audit is not None:
                audit(db, sg, rules, updated)      # 同事务；抛错即整笔回滚
            db.commit()
            db.refresh(sg)
            return sg
        except Exception:
            db.rollback()  # 失败路径不留半截事务（锁外连接复用更干净）
            raise


# ---------------- 教学规则（表 source_group_learned_rules） ----------------

def _now() -> datetime:
    return datetime.now()


def list_learned_rules(db: Session,
                       source_group_id: int) -> list[SourceGroupLearnedRule]:
    """该组的全部教学规则行：`sort_order` 升序 → `id` 升序。

    `sort_order` 承载 rename 的级联顺序（前一条的输出可被后一条继续改写），
    `id` 只是同序时的稳定兜底（手工造数/并发补齐都可能出现同序号）。
    """
    if source_group_id is None:
        return []
    return list(db.execute(
        select(SourceGroupLearnedRule)
        .where(SourceGroupLearnedRule.source_group_id == source_group_id)
        .order_by(SourceGroupLearnedRule.sort_order.asc(),
                  SourceGroupLearnedRule.id.asc())
    ).scalars())


def _list_by_type(db: Session, source_group_id: int,
                  rule_type: str) -> list[SourceGroupLearnedRule]:
    """该组该类型的规则行（同 `sort_order` → `id` 升序）。"""
    return [r for r in list_learned_rules(db, source_group_id)
            if r.rule_type == rule_type]


def entry_of_row(row: SourceGroupLearnedRule):
    """行 → 旧 JSON 时代的条目形态（响应与消费侧逐字段兼容）。

    - `rename`：恒为 dict `{"match", "replace"[, "note"]}`；
    - `ignore` / `exclude` / `aggregate`：无 note 时退回**裸字符串**（旧写路径就是
      这样落的，最小侵入），有 note 时是 `{"match", "note"}` dict。

    说明：迁移前手写的 `{"match": "x"}`（dict 但无 note）会被归一成 `"x"`——
    两种形态在判重与匹配上完全等价（`entry_match` / `_is_excluded` 都兼容），
    这只是拆表后「一条规则一个规范形态」的必然归一。
    """
    note = (row.note or "").strip() or None
    if row.rule_type == "rename":
        entry = {"match": row.match, "replace": row.replace}
        if note:
            entry["note"] = note
        return entry
    return {"match": row.match, "note": note} if note else row.match


def correction_rules_of(db: Session, source_group_id: int) -> dict:
    """该组教学规则 → 旧 JSON 三键形态 `{"rename": [...], "ignore": [...], "exclude": [...]}`。

    这是 `source_groups.rules` 拆表**之前**读侧拿到的形状（键名 / 条目形态完全一致），
    组级解析配置不在其中（它在 `rules` 列里，仍由 `parse_rules` 读）。
    三个键恒存在（无该类型规则时为空列表），便于调用方直接
    `for r in rules["rename"]` 而不用 `.get(...) or []`。
    """
    out: dict[str, list] = {rtype: [] for rtype in CORRECTION_RULE_TYPES}
    for row in list_learned_rules(db, source_group_id):
        if row.rule_type in out:
            out[row.rule_type].append(entry_of_row(row))
    return out


# 兼容别名：期望的读入口名字（语义同上）
get_correction_rules = correction_rules_of


def aggregate_names_of(db: Session, source_group_id: Optional[int]) -> list[str]:
    """该组的汇总行词表（`rule_type='aggregate'` 的 match，按 sort_order → id 排序）。

    只供 preview 计算 `aggregate_hint`（提示，不强制）：**不是**校正规则，不参与
    `rules_for_parsing` 的合并、不进 `correction_rules_of` 的三键、不删行也不改名。
    """
    if source_group_id is None:
        return []
    return [row.match for row in _list_by_type(db, source_group_id, AGGREGATE_RULE_TYPE)]


def remember_aggregate_names(db: Session, key, names: Optional[Sequence[str]]
                             ) -> Optional[SourceGroup]:
    """把用户勾选的名称学成该组的 `aggregate` 词（票 03 D3：**默认不学，勾了才学**）。

    单事务、单把锁内完成全部写入（同组同词已存在 → 幂等跳过，不改写既有行的 note）；
    组不存在 → 返回 None（router 决定 404/忽略）。空名/非字符串项静默丢弃（调用方
    已在请求层校验过类型，这里只做兜底）。

    Raises:
        ValueError: `names` 含超长（>200）或其他无法落库的形状——由 router 转 400。
    """
    cleaned: list[str] = []
    for name in names or []:
        if not isinstance(name, str):
            continue
        text = name.strip()
        if not text or text in cleaned:
            continue
        if len(text) > 200:
            raise ValueError(f"汇总词超长（≤200 字）：{text[:20]}…")
        cleaned.append(text)
    if not cleaned:
        return get_group(db, key)

    def work(sg: SourceGroup) -> None:
        now = _now()
        for name in cleaned:
            if _find_row(db, sg.id, AGGREGATE_RULE_TYPE, name, None) is not None:
                continue                      # 幂等：同组同词已学
            row = SourceGroupLearnedRule(
                source_group_id=sg.id,
                rule_type=AGGREGATE_RULE_TYPE,
                match=name,
                replace=None,
                note=None,
                sort_order=_next_sort_order(db, sg.id, AGGREGATE_RULE_TYPE),
                origin="manual",
                hit_count=0,
                created_at=now,
                updated_at=now,
            )
            try:
                with db.begin_nested():       # SAVEPOINT：并发下唯一索引冲突只回滚这一条
                    db.add(row)
            except IntegrityError:
                continue
            _audit_rule(db, sg.id, "create", None, _rule_snapshot(row))

    return _run_locked(db, key, work)


def rules_for_parsing(db: Session, sg: Optional[SourceGroup]) -> dict:
    """**解析侧唯一规则出口**：组级解析键（列）＋ 教学规则三键（表）合并成一份 dict。

    0031 把 `rename` / `ignore` / `exclude` 搬进 `source_group_learned_rules` 后，
    `SourceGroup.rules` 列只剩 `layout` / `amount_header` / `amount_col` /
    `ignore_words`；解析链上的三个消费点（`parse_screenshot` 的 rules、
    `apply_correction_rules` 的 rename/ignore、`compute_preview` 的 exclude）却仍按
    「一份 dict」取用。合并只在这里做一次，路由层拿到的就是这份只读快照——
    **不允许**在 `get_group_config` 或序列化路径上再长第二条合并口子。

    合并口径见 `_merged_rules`（**唯一实现**：`_serialize_rules` 与本文都走它，
    避免「界面看到的」与「解析用的」不一致）：

    1. 先取列内容**整份**（拆表前解析侧拿到的本来就是整份列 dict；未知键对
       `organize_blocks` 是惰性的，保留它们可保证历史脏键零行为变化）；
    2. 再用表里的三个类型键**覆盖**同名键——但只在表里该类型**非空**时覆盖：
       空类型与「缺键」在旧读路径里等价（见 0031 docstring 的归一裁决），因此
       空表不掩盖列里可能残留的历史教学键（与 `_serialize_rules` 完全同口径）。

    **坏 JSON 不静默吞**：复用 `screenshot_service._parse_group_rules`（同一实现，
    不是复制一份判断），因此坏 JSON / 顶层非对象抛的
    `ApiError(SOURCE_GROUP_RULES_INVALID, 422)` 与 `get_group_config` 逐字同源——
    两条路径不可能出现「一个 422、一个静默降级」的分裂行为。

    `sg` 为 None（未知组 / legacy 兜底组，库里没有行）→ `{}`：解析侧等同「无规则」，
    与拆表前 `get_group_config` 返回 `rules=None` 的语义一致。只读，不写、不加锁。
    """
    if sg is None:
        return {}
    merged, _ = _merged_rules(db, sg, strict=True)
    return merged or {}


def _run_locked(db: Session, key,
                work: Callable[[SourceGroup], None]) -> Optional[SourceGroup]:
    """锁内执行一次表写：解析组 → work(sg) → commit → refresh。

    组不存在返回 None（不调用 work）；work 抛的异常原样上抛，会话已 rollback。
    """
    with _RULES_LOCK:
        try:
            db.expire_all()          # 丢弃锁外读到的过期 ORM 状态，保证锁内重读
            sg = get_group(db, key)
            if sg is None:
                return None
            work(sg)
            db.commit()
            db.refresh(sg)
            return sg
        except Exception:
            db.rollback()
            raise


def _find_row(db: Session, source_group_id: int, rule_type: str, match: str,
              replace: Optional[str]) -> Optional[SourceGroupLearnedRule]:
    """按 (组, 型, match, replace) 定位一行；replace 为 None 时匹配 NULL。

    与唯一索引 `(source_group_id, rule_type, match, COALESCE(replace,''))` 同口径
    （只有 rename 才有非空 replace；ignore/exclude 恒为 NULL）。
    """
    stmt = select(SourceGroupLearnedRule).where(
        SourceGroupLearnedRule.source_group_id == source_group_id,
        SourceGroupLearnedRule.rule_type == rule_type,
        SourceGroupLearnedRule.match == match,
    )
    if replace is None:
        stmt = stmt.where(SourceGroupLearnedRule.replace.is_(None))
    else:
        stmt = stmt.where(SourceGroupLearnedRule.replace == replace)
    return db.execute(stmt).scalars().first()


# ---------------- 留痕（ADR-0027 ②口径桶） ----------------
#
# 教学规则（rename / ignore / exclude）直接决定同一张截图解析出**哪些行、叫什么名**，
# 属「口径」而非缓存。四条写路径都在**锁内、commit 之前**各记一行
# `entity='source_group'`（契约表冻结的取值）：
#   add → create（after＝新行关键字段全集）；delete → delete（before＝全集）；
#   update → update（**仅变更字段**）；swap → update（after 只有 `order` 序号）。
# 幂等空转（同身份已存在、编辑后零变更）不产审计行——没有写入事实就没有行。

def _rule_snapshot(row: SourceGroupLearnedRule) -> dict:
    """行的关键字段快照（含 id / sort_order / origin：取证明「这条规则哪来的」）。"""
    return {"id": row.id, "type": row.rule_type, "match": row.match,
            "replace": row.replace, "note": row.note,
            "sort_order": row.sort_order, "origin": row.origin}


def _rule_fields(row: SourceGroupLearnedRule) -> dict:
    """update 差分用的可编辑字段（id / sort_order / origin 不参与 diff）。"""
    return {"type": row.rule_type, "match": row.match,
            "replace": row.replace, "note": row.note}


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """只留值真的变了的键（update 分型：两侧保持同一组键）。"""
    keys = list(after) + [key for key in before if key not in after]
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return ({key: before.get(key) for key in changed},
            {key: after.get(key) for key in changed})


def _audit_rule(db: Session, source_group_id: int, action: str,
                before, after, *, reason: Optional[str] = None,
                reason_note: Optional[str] = None) -> None:
    """记一行 `source_group/<action>`（调用点保证：只在真有写入时调用）。

    `action='delete'` 属纠正类（spec E1）：`reason` 缺省时由
    `audit_service.record()` 422 拦下（`AUDIT_REASON_REQUIRED`）。
    """
    audit_service.record(db, audit_service.make_change(
        "source_group", source_group_id, action, before, after,
        reason=reason, reason_note=reason_note))


def _next_sort_order(db: Session, source_group_id: int, rule_type: str) -> int:
    """追加到该类型末尾：`max(sort_order) + 1`（空表从 0 起）。"""
    current = db.execute(
        select(func.max(SourceGroupLearnedRule.sort_order)).where(
            SourceGroupLearnedRule.source_group_id == source_group_id,
            SourceGroupLearnedRule.rule_type == rule_type)
    ).scalar()
    return 0 if current is None else int(current) + 1


def add_learned_rule(db: Session, key, rule_type: str, match: str,
                     replace: Optional[str] = None,
                     note: Optional[str] = None) -> Optional[SourceGroup]:
    """追加一条教学规则（`origin='manual'`，落表）。

    接受 `LEARNED_RULE_TYPES`（三型校正规则 ＋ `aggregate` 汇总词）；HTTP 层
    `_require_rule_type` 仍只放行三型——`aggregate` 的写入入口是 apply 的
    `options.remember_aggregate_names`（票 03 D3：默认不学，勾了才学）。

    **幂等**：同 (组, 型, match, replace) 已存在 → 不追加、不改写已有行的 note
    （与拆表前「内存判重、静默忽略」的外部行为一致，前端重复点保存不会长重复行）。
    并发下的兜底：唯一索引命中抛 IntegrityError 时回滚该条并同样按幂等处理
    （单进程有锁，正常走不到；多进程/脚本直写时可以兜住）。

    时间戳与 `hit_count` **显式赋值**：ORM 的 Python 端 default 只在 ORM INSERT
    生效，绕过 ORM 的 `INSERT OR IGNORE` 路径会让 NOT NULL 列拿到 NULL 并被静默吞掉
    （见迁移 0031 的同款注释），这里不依赖隐式默认。
    """
    if rule_type not in LEARNED_RULE_TYPES:
        raise ValueError(f"不支持的规则类型：{rule_type}")

    def work(sg: SourceGroup) -> None:
        if _find_row(db, sg.id, rule_type, match, replace) is not None:
            return                      # 幂等：同身份已存在，零写入
        now = _now()
        row = SourceGroupLearnedRule(
            source_group_id=sg.id,
            rule_type=rule_type,
            match=match,
            replace=replace if rule_type == "rename" else None,
            note=(note or None),
            sort_order=_next_sort_order(db, sg.id, rule_type),
            origin="manual",
            hit_count=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with db.begin_nested():     # SAVEPOINT：唯一索引冲突只回滚这一条
                db.add(row)
        except IntegrityError:          # 唯一索引命中 ＝ 已存在 → 幂等
            return
        _audit_rule(db, sg.id, "create", None, _rule_snapshot(row))

    return _run_locked(db, key, work)


def delete_learned_rule(db: Session, key, rule_type: str, match: str,
                        replace: Optional[str] = None, *,
                        reason: Optional[str] = None,
                        reason_note: Optional[str] = None
                        ) -> Optional[SourceGroup]:
    """按身份删除一条教学规则；定位不到 → `RuleNotFoundError`（上层 404）。

    只删该组的行（`source_group_id` 参与 WHERE），绝不波及同 match 的其他组。
    """
    def work(sg: SourceGroup) -> None:
        row = _find_row(db, sg.id, rule_type, match, replace)
        if row is None:
            raise RuleNotFoundError("校正规则不存在")
        before = _rule_snapshot(row)
        db.delete(row)
        db.flush()
        _audit_rule(db, sg.id, "delete", before, None, reason=reason,
                    reason_note=reason_note)

    return _run_locked(db, key, work)


def update_learned_rule(db: Session, key, rule_type: str, match: str,
                        replace: Optional[str] = None, *,
                        new_match: Optional[str] = None,
                        new_replace: Optional[str] = None,
                        has_new_replace: bool = False,
                        note_change: Optional[str] = None
                        ) -> Optional[SourceGroup]:
    """原地编辑一条教学规则（保留 `id` / `created_at` / `sort_order`）。

    参数语义（与 HTTP 契约一一对应）：

    - `match` / `replace`：**旧身份**，用于定位；
    - `new_match`：新 match，缺省（None）＝不变；
    - `has_new_replace` + `new_replace`：rename 的新 replace；`has_new_replace=False`
      时沿用旧值（"没传" 与 "传了空串" 必须区分，前者是保持，后者上层已判 400）；
    - `note_change`：三态——None＝不变，`""`＝删备注，非空＝设置。

    编辑后与同组同型其他行身份重复 → `RuleConflictError`（上层 409，自身不算重复）；
    定位不到 → `RuleNotFoundError`（上层 404）。`updated_at` 显式刷新（不依赖
    `onupdate`——同一函数也用于直连 SQL 的场景）。
    """
    def work(sg: SourceGroup) -> None:
        row = _find_row(db, sg.id, rule_type, match, replace)
        if row is None:
            raise RuleNotFoundError("校正规则不存在")

        final_match = new_match or match
        final_replace = None
        if rule_type == "rename":
            final_replace = new_replace if has_new_replace else row.replace

        clash = _find_row(db, sg.id, rule_type, final_match, final_replace)
        if clash is not None and clash.id != row.id:
            raise RuleConflictError("修改后的规则与同类型现有条目重复")

        before = _rule_fields(row)
        row.match = final_match
        if rule_type == "rename":
            row.replace = final_replace
        if note_change is not None:
            row.note = note_change or None
        row.updated_at = _now()         # 显式刷新（created_at/origin/hit_count 不动）
        db.flush()
        before_changed, after_changed = _changed_fields(before, _rule_fields(row))
        if after_changed:               # 空变更＝没有写入事实，不产审计行
            _audit_rule(db, sg.id, "update", before_changed, after_changed)

    return _run_locked(db, key, work)


def swap_learned_rules(db: Session, key, rule_type: str,
                       a: tuple[str, Optional[str]],
                       b: tuple[str, Optional[str]]) -> Optional[SourceGroup]:
    """交换同类型两条规则的位置（`(match, replace)` 身份 × 2）。

    实现：取该组该类型的全部行（按 `sort_order` → `id` 排序）→ 交换两个下标 →
    **整段重编号 0..n-1** 后落库。一个事务内完成，不存在中间态：唯一索引不含
    `sort_order`（见 models.py 的 `uq_sglr_group_type_match_replace`），所以换序
    不会撞唯一约束；重编号同时把历史遗留的重复/跳号序号一并修正，交换后顺序确定。
    """
    identity_a, identity_b = a, b

    def work(sg: SourceGroup) -> None:
        rows = _list_by_type(db, sg.id, rule_type)
        ia = ib = None
        for i, row in enumerate(rows):
            key_row = (row.match, row.replace)
            if ia is None and key_row == identity_a:
                ia = i
            if ib is None and key_row == identity_b:
                ib = i
        if ia is None or ib is None:
            raise RuleNotFoundError("校正规则不存在")
        before_order = [row.id for row in rows]
        rows[ia], rows[ib] = rows[ib], rows[ia]
        now = _now()
        for idx, row in enumerate(rows):
            if row.sort_order != idx:
                row.sort_order = idx
                row.updated_at = now
        db.flush()
        # 换序只记顺序本身（D5 update 分型 + 需求「别整段复制规则体」）：
        # 审计读者要看的是「第几条换到第几位」，规则内容由各自的 create/update 行负责
        _audit_rule(db, sg.id, "update",
                    {"order": before_order},
                    {"order": [row.id for row in rows]})

    return _run_locked(db, key, work)


# ---------------- 校正条目身份定位助手（拆表前的内存判重口径） ----------------

def entry_match(entry) -> Optional[str]:
    """取条目的 match 子串：dict 形态读 match 键，字符串形态（旧格式）即自身。

    与 routers/screenshots.py 历史 _rule_match_of 语义完全一致（note 仅展示，
    永不参与定位）。0031 起判重/定位由表与唯一索引承担，本函数保留给诊断与兼容。
    """
    return entry.get("match") if isinstance(entry, dict) else entry


def rule_identity(entry, rtype: str):
    """条目身份：rename 为 (match, replace) 且要求 dict 形态（裸字符串永不
    匹配 rename，与历史判重一致）；ignore/exclude 为 (match,)，新旧形态通用。

    0031 起同一口径落在表上：`_find_row` 的 WHERE 与唯一索引表达式
    `COALESCE(replace,'')` 表达的是同一件事（保留本函数避免诊断脚本失效）。
    """
    if rtype == "rename":
        if not isinstance(entry, dict):
            return None
        return (entry.get("match"), entry.get("replace"))
    return (entry_match(entry),)


def find_rule_index(entries, rtype: str, match: str,
                    replace: Optional[str] = None) -> Optional[int]:
    """在条目列表（非 list 视为空）中按身份定位，返回下标或 None。"""
    if not isinstance(entries, list):
        return None
    target = (match, replace) if rtype == "rename" else (match,)
    for i, entry in enumerate(entries):
        if rule_identity(entry, rtype) == target:
            return i
    return None