"""截图批次服务：批次壳 / 成员登记 / finalize（**唯一校验点**）。

规格：ADR-0029 D5/D6/D9/D10；票 04（跨图核对与批次记录）。

三件事，各管一段：

1. **批次壳**（`screenshot_batches`）：`POST /api/screenshot-batches` 显式先建，拿
   `batch_id`；各图 apply 载荷 additive 带 `batch_id`。状态推进**单向**：
   `open`（建壳，零成员）→ `partial`（已登记成员、未定案）→ `finalized`（finalize
   通过）。`abandoned` 是旁路终态（本期不提供端点，见 spec §4「批次删除」不做）。
   **没有回退路径**：成员写失败只让批次停在 `partial`，重试由前端逐图发起。
2. **成员登记**（`screenshot_batch_members`）：apply 成功后由
   `screenshot_service.apply_preview` 调用 `register_member`；`UNIQUE(batch_id,
   evidence_id)` ⇒ 同图重复 apply 是原地刷新，不新增行。
3. **finalize**：整批唯一的校验点。校验顺序**固定**（先便宜后昂贵、先结构后金额）：
   批次存在 → 未定案 → 成员齐 → 每条父项/子项可解析 → 算差额 → 差额为 0 或已显式
   接受 → **单事务**写核对记录 + 建/校验构成边 + 置 finalized + 一条审计。

为什么「父项必须已能解析到既有资产项」是硬前置（P2）：finalize 是**核对**，不是
**建账**。若允许在这里按名字新建资产项，一次手误就会凭空造出一笔资产并立刻进入
计量口径——那是「零副作用」原则的反面。解析不到就 409 `RECONCILE_PARENT_NOT_FOUND`，
让用户回去把那张图先 apply 成正常的 update/create 行。

为什么差额**零容差**（D10）：金额全是两位小数，容忍带只会掩盖错抓；「静默吸收差额」
在会计上是禁止动作。三个合法出口：改子项 / 补子项 / 显式接受（理由必填）。

审计口径（ADR-0027）：只记一条 `entity='screenshot_batch', action='finalize'`
（不属纠正类 → 不强制 reason；接受差额的理由写在 `after.accept_reason`）。action 刻意
用 `finalize` 而非 `delete`：`delete` 在 `REASON_REQUIRED_ACTIONS` 里，会被 422 卡住。
"""

from datetime import date as _date, datetime
from decimal import Decimal, InvalidOperation
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import (
    AssetItem, ItemComposition, ScreenshotBatch, ScreenshotBatchMember,
    ScreenshotReconciliation, ScreenshotReconciliationChild, SourceGroup,
)
from services import audit_service, composition_service
from services.audit_service import make_change as _change

logger = logging.getLogger(__name__)

ENTITY = "screenshot_batch"
NOTE_MAX = 200
REASON_MAX = 200
_DEFAULT_EDGE_NOTE = "批次核对建立"
CENT = Decimal("0.01")
ZERO = Decimal("0")


# ---------------------------------------------------------------- 读取
def get_batch(db: Session, batch_id: int) -> Optional[ScreenshotBatch]:
    """按 id 取批次行；不存在返回 None（调用方决定 404 文案）。"""
    if batch_id is None:
        return None
    return db.get(ScreenshotBatch, int(batch_id))


def get_batch_or_404(db: Session, batch_id: int) -> ScreenshotBatch:
    batch = get_batch(db, batch_id)
    if batch is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"批次不存在: {batch_id}",
                       {"batch_id": batch_id})
    return batch


def members_of(db: Session, batch_id: int) -> list[ScreenshotBatchMember]:
    """该批次成员行（按登记先后）。"""
    return list(db.execute(
        select(ScreenshotBatchMember)
        .where(ScreenshotBatchMember.batch_id == batch_id)
        .order_by(ScreenshotBatchMember.id)
    ).scalars())


# ---------------------------------------------------------------- 写：批次壳
def _check_expected_members(value) -> Optional[int]:
    """预计成员数：None / 正整数；其余（0、负数、非整数、字符串）→ 400。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            "expected_members 必须为正整数（预计成员数：几张图构成这一批）",
            {"fields": [{"field": "expected_members", "message": "必须为正整数"}]})
    if value < 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            "expected_members 必须 ≥ 1（没有成员的批次无法定案）",
            {"fields": [{"field": "expected_members", "message": "必须 ≥ 1"}]})
    return value


def _resolve_group_id(db: Session, source_group) -> Optional[int]:
    """来源组入参（slug / label / id）→ source_group_id；未传 → None。

    未知组名 → 400 `SOURCE_GROUP_UNKNOWN`（与 apply 同码同文案，绝不静默置 NULL：
    NULL 在本列的含义是「这批没声明来源组」，不是「名字打错了」）。
    """
    if source_group is None or source_group == "":
        return None
    from services import screenshot_service      # 延迟 import：本模块被它反向引用
    if isinstance(source_group, int):
        row = db.get(SourceGroup, source_group)
    else:
        row = screenshot_service.get_source_group(db, source_group)
    if row is None:
        raise ApiError(ErrorCode.SOURCE_GROUP_UNKNOWN, 400,
                       f"未知来源组: {source_group}",
                       {"source_group": str(source_group)})
    return row.id


def create_batch(db: Session, *, data_date: Optional[_date] = None,
                 source_group=None, expected_members=None,
                 note: Optional[str] = None) -> ScreenshotBatch:
    """建批次壳（`status='open'`）并返回行。

    幂等性由调用方（幂等键）承担：同一请求重放走首次响应，不建第二个壳。
    """
    expected = _check_expected_members(expected_members)
    group_id = _resolve_group_id(db, source_group)
    note_text = (note or "").strip() or None if isinstance(note, str) else None
    if note_text is not None and len(note_text) > NOTE_MAX:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400, f"批次备注超长（≤{NOTE_MAX} 字）",
            {"fields": [{"field": "note", "message": f"最多 {NOTE_MAX} 字"}]})

    batch = ScreenshotBatch(
        created_at=datetime.now(),
        data_date=data_date,
        source_group_id=group_id,
        status="open",
        finalized_at=None,
        note=note_text,
        expected_members=expected,
    )
    db.add(batch)
    db.flush()                              # 取 batch.id（审计行要引用）
    # 留痕（ADR-0027 D9：写端点必须可追溯）：建壳本身不改账本，但它是「这一批要
    # 核对什么」的起点，事后追查跨图核对时先要找到它。action 用 create（P4：一律
    # create/remove/finalize，刻意不用 delete——delete 在 REASON_REQUIRED_ACTIONS 里）
    audit_service.record(db, _change(
        ENTITY, batch.id, "create", before=None,
        after={"data_date": data_date.isoformat() if data_date else None,
               "source_group_id": group_id,
               "expected_members": expected,
               "note": note_text}))
    db.commit()
    db.refresh(batch)
    return batch


def require_open(db: Session, batch_id: int) -> ScreenshotBatch:
    """写入前置校验：批次存在且仍可写。

    Raises:
        ApiError: 不存在（404 `NOT_FOUND`）；已定案（409 `BATCH_ALREADY_FINALIZED`）；
            已弃置（409 `BATCH_ABANDONED`）。
    """
    batch = get_batch_or_404(db, batch_id)
    if batch.status == "finalized":
        raise ApiError(
            ErrorCode.BATCH_ALREADY_FINALIZED, 409,
            f"批次已定案（finalized_at={batch.finalized_at}），不能再写入或重复定案",
            {"batch_id": batch.id, "status": batch.status,
             "finalized_at": (batch.finalized_at.isoformat()
                              if batch.finalized_at else None)})
    if batch.status == "abandoned":
        raise ApiError(
            ErrorCode.BATCH_ABANDONED, 409, "批次已弃置（abandoned），不能再写入",
            {"batch_id": batch.id, "status": batch.status})
    return batch


def register_member(db: Session, batch_id: int, evidence_id: int,
                    status: str = "applied") -> ScreenshotBatchMember:
    """登记一张图的成员行（apply 成功后调用），并把 `open` 推进到 `partial`。

    `UNIQUE(batch_id, evidence_id)` ⇒ 同图重复 apply **原地刷新**（`applied_at`
    更新为最近一次），不新增行——重试路径因此天然幂等。

    状态推进规则写在这里（唯一实现）：批次当前是 `open` 且有成员落下 → `partial`；
    已是 `partial` 保持不变；`finalized`/`abandoned` 由 `require_open` 先拦（本函数
    只做防御性跳过，不静默改终态）。「成员全到齐」也是 `partial`：`partial` 的语义是
    **未定案**，不是「有失败」——失败单独记在成员行的 `status` 上（票 04 D5）。
    """
    if status not in ("pending", "applied", "failed"):
        raise ValueError(f"非法成员状态: {status}")
    batch = get_batch_or_404(db, batch_id)
    now = datetime.now()
    member = db.execute(
        select(ScreenshotBatchMember).where(
            ScreenshotBatchMember.batch_id == batch.id,
            ScreenshotBatchMember.evidence_id == evidence_id)
    ).scalar_one_or_none()
    if member is None:
        member = ScreenshotBatchMember(batch_id=batch.id, evidence_id=evidence_id,
                                       applied_at=now, status=status)
        db.add(member)
    else:
        member.applied_at = now
        member.status = status
    if batch.status == "open":
        batch.status = "partial"        # 单向推进：open → partial（不回退、不跳阶）
    db.commit()
    db.refresh(member)
    return member


# ---------------------------------------------------------------- finalize：入参解析
# 校验层一律「null 容忍」：契约补丁（前端实测）明确 `parent_evidence_id` 与
# `parent_row_index` 可为 null（手选既有资产项当汇总行时没有截图行），子项
# `item_id` 同理。**不要**为这些字段加必填校验——那会把合法请求打成 422。
def _optional_int(value) -> Optional[int]:
    """宽容整数读取：bool / None / 非整数 → None。

    `parent_evidence_id` / `parent_row_index` / 子项 `row_index` 是**展示与留痕**字段，
    前端可能只给 `item_id`（契约第 2 条），因此不做强校验——缺了只记日志。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _entry_name(entry: dict) -> str:
    """核对条目的展示名（`name` 优先；兼容 `parent_name` / `asset_name` 别名）。"""
    for key in ("name", "parent_name", "asset_name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _require_amount(value, field: str) -> Decimal:
    """金额 → `Decimal`（两位小数）。非数值 / NaN / 无穷 → 400。

    全仓禁止浮点：入参一律 `str(value)` 再 `Decimal`，避免 0.1+0.2 这类误差进比对。
    """
    if value is None or isinstance(value, bool):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400,
                       f"{field} 必须为金额",
                       {"fields": [{"field": field, "message": "必须为金额"}]})
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(CENT)
    except (InvalidOperation, ValueError):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            f"{field} 不是合法金额: {value!r}",
            {"fields": [{"field": field, "message": "金额无效"}]}) from None


def _require_reason(accept_mismatch) -> Optional[str]:
    """`accept_mismatch` → 已 trim 的理由（None＝未接受）。

    传了对象但理由为空/空白 → 400 `RECONCILE_REASON_REQUIRED`（D10：差额必须有人
    负责），details 固定为空对象（冻结契约：前端不看键，只看码）；理由超长 → 400。
    非对象 → 400。未传 → None（差额≠0 时会被 409 拦下）。
    """
    if accept_mismatch is None:
        return None
    if not isinstance(accept_mismatch, dict):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            "accept_mismatch 必须为对象（{reason: 为什么接受这个差额}）",
            {"fields": [{"field": "accept_mismatch", "message": "必须为对象"}]})
    reason = accept_mismatch.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise ApiError(
            ErrorCode.RECONCILE_REASON_REQUIRED, 400,
            "接受非零差额必须填写理由（为什么这个差额是合理的）",
            {})
    if len(reason) > REASON_MAX:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400, f"接受差额的理由超长（≤{REASON_MAX} 字）",
            {"fields": [{"field": "accept_mismatch.reason",
                         "message": f"最多 {REASON_MAX} 字"}]})
    return reason


def _resolve_parent(db: Session, entry: dict, label: str) -> AssetItem:
    """汇总行 → 既有资产项（**只解析、绝不新建**，P2）。

    解析顺序：`parent_item_id`（契约规范字段）→ `name`/`parent_name` 走名称单点
    （`asset_service.resolve_item_id`，含别名与历史名）。都解析不到 →
    409 `RECONCILE_PARENT_NOT_FOUND`，details **冻结为**
    `{"parent_row_index": int, "name": str}`（前端按这两个键取值，别改名）。
    """
    pid = _optional_int(entry.get("parent_item_id"))
    if pid is None:
        logger.warning("reconcile_parent_item_id_missing entry=%s", label)
    else:
        item = db.get(AssetItem, pid)
        if item is not None:
            return item

    name = _entry_name(entry)
    if name:
        from services.asset_service import resolve_item_id   # 本地导入：避免顶层环
        resolved = resolve_item_id(db, name)
        if resolved is not None:
            item = db.get(AssetItem, resolved)
            if item is not None:
                return item

    row_index = _optional_int(entry.get("parent_row_index"))
    where = f"第 {row_index + 1} 行" if row_index is not None else "手选父项"
    raise ApiError(
        ErrorCode.RECONCILE_PARENT_NOT_FOUND, 409,
        f"汇总行解析不到既有资产项（{where}"
        f"「{name or '未命名'}」）：请先把它落成正常资产项，再回来做批次核对",
        {"parent_row_index": row_index,      # 冻结键；手选父项时为 null
         "name": name})


def _parse_entry(db: Session, entry, pos: int) -> dict:
    """一条核对（汇总行 ← 若干明细行）→ 已校验的内部结构。

    契约形状（编排冻结，前端已按它对接）：

    ```
    {"parent_item_id": 123, "parent_evidence_id": 45, "parent_row_index": 0,
     "aggregate_amount": "1234.56",
     "children": [{"evidence_id": 45, "row_index": 2, "item_id": 88,
                   "amount": "600.00"}]}
    ```

    - `parent_evidence_id` / `parent_row_index` **可空、不强校验**（前端手选父项时
      两者都是 null）：`parent_item_id` 是唯一锚，解析不到才 409。`children` 可省
      （默认 `[]`），空数组合法（合计 0，差额由顶层 `accept_mismatch` 决定去留）。
    - 子项 `item_id` **允许为 null** → 按名称（`name` / `child_name`）解析；解析不到
      **不算失败**，记进 `unresolved_children` 回执，只是不建构成边（金额照常计入
      合计——核对的是「这几个数加起来等不等于汇总数」）。
    - 核对口径＝**直接子项之和**（不穿透孙层）：只累加本次载荷里的 children 金额，
      不递归读取 `item_compositions` 的孙层（契约第 4 条，与前端一致）。
    """
    label = f"reconciliations[{pos}]"
    if not isinstance(entry, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400,
                       f"{label} 必须为对象",
                       {"fields": [{"field": label, "message": "必须为对象"}]})

    parent = _resolve_parent(db, entry, label)
    parent_evidence_id = _optional_int(entry.get("parent_evidence_id"))
    parent_row_index = _optional_int(entry.get("parent_row_index"))
    # 父项来源的唯一判据：有凭证 id＝来自某张图的预览行；没有＝用户手选的既有资产项
    # （那个项在本批截图里没有行，两个字段只能带 null）。审计里按这个值区分。
    parent_source = "preview_row" if parent_evidence_id is not None else "manual"
    if parent_source == "manual":
        logger.info("reconcile_parent_manual entry=%s item_id=%s",
                    label, parent.id)
    aggregate_amount = _require_amount(entry.get("aggregate_amount"),
                                       f"{label}.aggregate_amount")

    raw_children = entry.get("children")
    if raw_children is None:
        raw_children = []                # 契约：children 可省（默认 []）
    if not isinstance(raw_children, list):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            f"{label}.children 必须为数组（构成这个汇总数的明细行）",
            {"fields": [{"field": f"{label}.children",
                         "message": "必须为数组"}]})

    children: list[dict] = []
    unresolved: list[dict] = []
    for ci, child in enumerate(raw_children):
        clabel = f"{label}.children[{ci}]"
        if not isinstance(child, dict):
            raise ApiError(ErrorCode.VALIDATION_ERROR, 400,
                           f"{clabel} 必须为对象",
                           {"fields": [{"field": clabel, "message": "必须为对象"}]})
        amount = _require_amount(child.get("amount"), f"{clabel}.amount")
        child_evidence_id = _optional_int(child.get("evidence_id"))
        child_row_index = _optional_int(child.get("row_index"))
        child_name = (child.get("name") or child.get("child_name")
                      or child.get("asset_name") or "")
        child_name = child_name.strip() if isinstance(child_name, str) else ""

        item = None
        child_item_id = _optional_int(child.get("item_id"))
        if child_item_id is not None:
            item = db.get(AssetItem, child_item_id)
        if item is None and child_name:
            from services.asset_service import resolve_item_id   # 本地导入：避免顶层环
            resolved = resolve_item_id(db, child_name)
            if resolved is not None:
                item = db.get(AssetItem, resolved)
        if item is None:
            # 解析不到**不是失败**（契约第 2 条）：只记回执，金额照常进合计
            unresolved.append({"parent_item_id": parent.id,
                               "row_index": child_row_index,
                               "name": child_name or None,
                               "amount": str(amount)})
            children.append({"child_item_id": None,
                             "child_evidence_id": child_evidence_id,
                             "child_row_index": child_row_index,
                             "amount": amount,
                             "child_name": child_name or None})
            continue

        children.append({
            "child_item_id": item.id,
            "child_evidence_id": child_evidence_id,
            "child_row_index": child_row_index,
            "amount": amount,
            "child_name": item.display_name,
        })

    child_amount_sum = sum((c["amount"] for c in children), ZERO)
    diff = aggregate_amount - child_amount_sum
    return {
        "parent_item_id": parent.id,
        "parent_item_name": parent.display_name,
        "parent_evidence_id": parent_evidence_id,   # 可空（手选父项）
        "parent_row_index": parent_row_index,       # 可空（手选父项）
        "parent_source": parent_source,             # manual | preview_row
        "aggregate_amount": aggregate_amount,
        "child_amount_sum": child_amount_sum.quantize(CENT),
        "diff": diff.quantize(CENT),
        "children": children,
        "unresolved_children": unresolved,
        "reason": None,               # 由 finalize 填（顶层 accept_mismatch）
    }


def _ensure_complete(batch: ScreenshotBatch,
                     members: list[ScreenshotBatchMember]) -> None:
    """成员齐不齐（票 04 D4 第 1 条）→ 不齐就 409 `BATCH_NOT_COMPLETE`。

    判定（`expected_members` 已知时）＝已 `applied` 的成员数必须**恰好等于**预计数
    （多了也报：说明这批图不是当初声明的那几张，定案会把别的东西算进来）。
    未声明预计数时只能要求「至少一条成员、且全部 applied」。

    **details 冻结为 `{"missing_evidence_ids": [...]}`**（前端按这个键取缺失的图）：
    只装**已知但没成功采纳**的成员 evidence_id（`status != 'applied'`）。从未上传的
    图没有成员行、其 evidence_id 无从得知——这类缺口写进中文 message（「还差 N 张」），
    不编造 id。
    """
    expected = batch.expected_members
    applied = [m for m in members if m.status == "applied"]
    not_applied = [m for m in members if m.status != "applied"]
    missing_count = (max(expected - len(applied), 0) if expected is not None else None)
    incomplete = (
        bool(not_applied)
        or (expected is not None and len(applied) != expected)
        or (expected is None and not applied)
    )
    if not incomplete:
        return

    if expected is None:
        hint = "该批次未声明 expected_members，但一条成员都没有"
    elif missing_count:
        hint = f"还差 {missing_count} 张图没采纳（预计 {expected} 张）"
    else:
        hint = f"成员数与预计不符（预计 {expected} 张，已登记 {len(members)} 张）"
    logger.warning("batch_not_complete batch_id=%s expected=%s applied=%s",
                   batch.id, expected, len(applied))
    raise ApiError(
        ErrorCode.BATCH_NOT_COMPLETE, 409,
        f"批次成员不齐，不能定案：{hint}",
        {"missing_evidence_ids": [m.evidence_id for m in not_applied]})


def _ensure_edges(db: Session, entry: dict) -> list[dict]:
    """按核对关系建立/校验构成边（一子一父、禁环 → 既有 409）。

    - **解析不到的子项跳过**（`child_item_id is None`）：没有项可挂，构成边无从建立
      （金额仍参与核对合计，回执里进 `unresolved_children`）；
    - 「已有一条指向同一父项」＝合法状态（finalize 可重放同一份核对，只要批次还没
      定案），直接跳过；指向别的父项 → 交给 `composition_service.create_edge` 抛
      409 `COMPOSITION_PARENT_CONFLICT`（错误码与语义只有那一处定义）；
    - 口径是**直接子项**（不穿透孙层）：本函数只连「本条的父 ← 本条的子」这一层，
      不去展开子项自己的子项（契约第 4 条）。
    """
    done: list[dict] = []
    for child in entry["children"]:
        child_id = child["child_item_id"]
        if child_id is None:
            continue                       # 解析不到：不建边（不是失败）
        existing = db.execute(
            select(ItemComposition)
            .where(ItemComposition.child_item_id == child_id)
        ).scalar_one_or_none()
        if existing is not None and existing.parent_item_id == entry["parent_item_id"]:
            done.append({"child_item_id": child_id,
                         "edge_id": existing.id, "created": False})
            continue
        edge = composition_service.create_edge(
            db, entry["parent_item_id"], child_id,
            note=_DEFAULT_EDGE_NOTE, commit=False)
        done.append({"child_item_id": child_id,
                     "edge_id": edge.id, "created": True})
    return done


# ---------------------------------------------------------------- finalize
def finalize(db: Session, batch_id: int, reconciliations,
             accept_mismatch=None) -> dict:
    """定案：校验 → 单事务写核对记录 + 建构成边 + 置 finalized + 一条审计。

    校验顺序（**不要重排**，报错优先级即用户修数据的顺序）：
    1. 批次存在（404）、未定案（409 `BATCH_ALREADY_FINALIZED`）；
    2. 成员齐（409 `BATCH_NOT_COMPLETE`，details 冻结 `{missing_evidence_ids}`）；
    3. 每条汇总行可解析（409 `RECONCILE_PARENT_NOT_FOUND`，details 冻结
       `{parent_row_index, name}`）；子项解析不到**不算失败**（只进 `unresolved_children`）；
    4. `Decimal` 算差额：`diff != 0` 且该条没有生效理由 → 409 `RECONCILE_MISMATCH`
       （details 冻结 `{reconciliations:[{parent_item_id, aggregate_amount,
       children_sum, diff}], accepted: false}`）；理由传了但为空 → 400
       `RECONCILE_REASON_REQUIRED`（details `{}`）；
    5. 通过 → 单事务：核对记录 + 子行明细 + 构成边 + 批次置 finalized + 审计。

    接受口径（前端已确认）：`accept_mismatch` **只在顶层出现**，作用于所有差额条目；
    条目内不带（带了也不读，避免两套语义各说各话）。

    父项锚（前端实测契约补丁）：`parent_item_id` 是唯一锚；`parent_evidence_id` 与
    `parent_row_index` 都可能为 null——用户手选既有资产项当汇总行时，该项在本批截图
    里没有行，`(evidence_id, row_index)` 无处可取。审计与回执用
    `parent_source: "manual" | "preview_row"` 区分这两条路径。

    核对口径（契约第 4 条）：**直接子项之和**，不穿透孙层——只累加本次载荷里的
    children 金额；某个子项自己也是汇总项时，它的快照金额原样计入，不去展开它的子项。

    Returns:
        dict：`{batch_id, status, finalized_at, member_count, members,
        reconciliation_count, mismatch_accepted, accepted_mismatch,
        unresolved_children, reconciliations}`。
    """
    batch = require_open(db, batch_id)
    members = members_of(db, batch.id)
    _ensure_complete(batch, members)

    if reconciliations is None:
        reconciliations = []
    if not isinstance(reconciliations, list):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, 400,
            "reconciliations 必须为数组（每条＝一个汇总行 ← 若干明细行）",
            {"fields": [{"field": "reconciliations", "message": "必须为数组"}]})

    # accept_mismatch **只在顶层**（前端已确认；条目内不带，带了也不读——不留第二套语义）
    reason = _require_reason(accept_mismatch)   # 空串 → 400（先于差额判定）
    entries = [_parse_entry(db, raw, pos) for pos, raw in enumerate(reconciliations)]
    for entry in entries:
        entry["reason"] = reason

    # 只有「差额≠0 且没有理由」才是拦路虎
    blocking = [e for e in entries if e["diff"] != ZERO and not e["reason"]]
    if blocking:
        raise ApiError(
            ErrorCode.RECONCILE_MISMATCH, 409,
            "汇总金额与明细合计不相等，未定案：请改子项 / 补子项，"
            "或显式接受差额（accept_mismatch.reason）",
            {
                # 冻结字段名（前端按这些键取值）：children_sum / accepted
                "reconciliations": [{
                    "parent_item_id": e["parent_item_id"],
                    "aggregate_amount": str(e["aggregate_amount"]),
                    "children_sum": str(e["child_amount_sum"]),
                    "diff": str(e["diff"]),
                } for e in blocking],
                "accepted": False,
            })

    unresolved_all = [u for e in entries for u in e["unresolved_children"]]
    if unresolved_all:
        logger.warning("reconcile_unresolved_children batch_id=%s count=%s",
                       batch.id, len(unresolved_all))

    now = datetime.now()
    try:
        created: list[dict] = []
        for e in entries:
            accepted_entry = e["diff"] != ZERO   # 能走到这里 ⇒ 必有理由
            status = "accepted_mismatch" if accepted_entry else "matched"
            rec = ScreenshotReconciliation(
                batch_id=batch.id,
                parent_item_id=e["parent_item_id"],
                # 手选父项时两者都是 null（父项在本批截图里没有行，只能靠 item_id 锚定）
                parent_evidence_id=e["parent_evidence_id"],
                parent_row_index=e["parent_row_index"],
                aggregate_amount=e["aggregate_amount"],
                child_amount_sum=e["child_amount_sum"],
                diff=e["diff"],
                status=status,
                accepted_reason=(e["reason"] if accepted_entry else None),
                accepted_at=now if accepted_entry else None,
            )
            db.add(rec)
            db.flush()                          # 取 rec.id
            for c in e["children"]:
                db.add(ScreenshotReconciliationChild(
                    reconciliation_id=rec.id,
                    child_item_id=c["child_item_id"],       # 可空（解析不到只留痕）
                    child_evidence_id=c["child_evidence_id"],
                    child_row_index=c["child_row_index"],
                    amount=c["amount"],
                ))
            edges = _ensure_edges(db, e)
            created.append({
                "reconciliation_id": rec.id,
                "parent_item_id": e["parent_item_id"],
                "parent_row_index": e["parent_row_index"],
                "parent_source": e["parent_source"],   # manual | preview_row
                "status": status,
                "diff": str(e["diff"]),
                "edges": edges,
                "unresolved_children": e["unresolved_children"],
            })

        batch.status = "finalized"
        batch.finalized_at = now
        audit_service.record(db, _change(
            ENTITY, batch.id, "finalize", before=None,
            after={
                "batch_id": batch.id,
                "data_date": (batch.data_date.isoformat()
                              if batch.data_date else None),
                "member_count": len(members),
                "members": [m.evidence_id for m in members],
                "reconciliation_count": len(created),
                "mismatch_accepted": sum(1 for c in created
                                         if c["status"] == "accepted_mismatch"),
                # 理由摘要：接受差额的**责任人证据**，全文限长（DB 侧 200 字）
                "accept_reason": reason,
                "parent_sources": {c["parent_item_id"]: c["parent_source"]
                                   for c in created},
                "unresolved_children_count": len(unresolved_all),
                "reconciliations": [{k: v for k, v in c.items()
                                     if k != "unresolved_children"}
                                    for c in created],
            }))
        db.commit()
    except Exception:
        db.rollback()                       # 单事务：核对记录/构成边/状态一起回滚
        raise

    return {
        "batch_id": batch.id,
        "status": batch.status,
        "finalized_at": now.isoformat(),
        "member_count": len(members),
        "members": [m.evidence_id for m in members],
        "reconciliation_count": len(created),
        "mismatch_accepted": sum(1 for c in created
                                 if c["status"] == "accepted_mismatch"),
        "accepted_mismatch": any(c["status"] == "accepted_mismatch" for c in created),
        "unresolved_children": unresolved_all,
        "reconciliations": created,
    }
