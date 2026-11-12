import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session
from sqlalchemy import select, func, asc, desc, or_

from models import (Asset, AssetItem, AssetItemName,
                    AssetItemTag, Channel, ImportBatch, Member, Tag)
from services import audit_service
from services import asset_owner_service, classification_service
from services.audit_service import make_change
from errors import ApiError, ErrorCode
from services.period_service import get_period_start_end, get_previous_period_end
from services.principal_service import resolve_cost_basis


class WriteOnceConflict(ValueError):
    """write-once 账本事实（amount/side/asset_date/asset_name）直改请求。

    router 映射为 409：改金额走 correct（更正＝追加），改名走 asset-items rename。
    """


# ==================== 分类上移后的记录侧守卫（ADR-0019 D2） ====================
#
# 记录（assets）是账本事实，分类（类别/流动性/机构/渠道/归属/标签）是资产项的固有
# 属性。记录级分类写入已于 0026 迁移整体取消：这里对仍然传旧字段的调用方**显式报错**，
# 绝不静默忽略——静默忽略会让调用方以为分类存上了（正是 ADR-0019 要根治的双真源）。

CLASSIFICATION_MOVED_MESSAGE = (
    "资产分类已上移到资产项（ADR-0019），记录侧只读：请改为在资产项上设置——"
    "类别/流动性/机构/渠道/归属走 PUT /api/asset-items/{item_id}/classification，"
    "标签走 PUT /api/asset-items/{item_id}/tags")

# 旧 EAV 字段 → 新归属（错误信息里给出替换建议，便于前端一次改到位）
LEGACY_FIELD_REPLACEMENTS: dict[str, str] = {
    "attribute_ids": "asset_class_id / liquidity_days / institution_id / channel_id / "
                     "owner_id / risk_level / tag_ids",
    "fund_type_id": "asset_class_id",
    "account_id": "institution_id / channel_id",
    "liquidity_rating_id": "liquidity_days",
    "tag_id": "tag_ids",
    "owner_id": "owner_id（资产项级，值域为 members）",
    "tag_ids": "tag_ids（资产项级）",
    "asset_class_id": "asset_class_id（资产项级）",
    "liquidity_days": "liquidity_days（资产项级）",
    "institution_id": "institution_id（资产项级）",
    "channel_id": "channel_id（资产项级）",
    "risk_level": "risk_level（资产项级）",
}


def _reject_legacy_classification(fields, where: str = "记录") -> None:
    """传了已废弃的分类字段 → 422（带 fields 明细，指明新入口）。"""
    present = sorted({f for f in fields if f})
    if not present:
        return
    fields_out = [{
        "field": name,
        "message": f"{name} 属于资产项级分类，不在记录上；请改用 "
                   f"{LEGACY_FIELD_REPLACEMENTS.get(name, '资产项分类端点')}",
    } for name in present]
    raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                   f"{where}写入路径不接受分类字段：{present}。" + CLASSIFICATION_MOVED_MESSAGE,
                   {"fields": fields_out})


class InvalidSideError(ValueError):
    """side 取值非法（仅 asset/liability）。

    专用异常：router 精确捕获映射 400 INVALID_SIDE（其余 ValueError 不再被
    批量更新路由误标 400）；子类化 ValueError 保持既有宽捕获测试兼容。
    """


# ==================== 分类展示上下文（ADR-0019） ====================

def classification_context(db: Session, item_ids=None) -> dict:
    """一次取齐分类展示所需的批量数据（列表/当前值/响应组装共用，避免 N+1）。

    `by_item` 走 `classification_service.classifications_for_items`（4 条以内查询
    覆盖整页）；`class_path`/`channel_name`/`member` 各一条字典全量查询。逐项查库
    是本重构要根治的问题（旧 EAV 列表页每行都要回查 asset_attributes）。

    注：渠道名直接读 `channels` 表，不走 `classification_service.list_channels`——
    后者当前对本表执行 `{"kind": r.kind}` 取值，而 `Channel` 没有 `kind` 列，
    一调即 AttributeError（`_list_simple` 的通用分支只适用于 institutions）。
    该缺陷在其归属车道修好后本函数无需改动（同一份数据）。
    """
    ids = [int(i) for i in (item_ids or []) if i is not None]
    channel_rows = db.execute(select(Channel).order_by(Channel.name)).scalars().all()
    return {
        "by_item": classification_service.classifications_for_items(db, ids),
        "class_path": {row["id"]: row["path"]
                       for row in classification_service.list_asset_classes(db)},
        "channel_name": {row.id: row.name for row in channel_rows},
        "member": {row["id"]: row
                   for row in classification_service.list_members(db)},
    }


def class_path_of(context: dict, class_id) -> str | None:
    """类别 id → 全路径（复用 `list_asset_classes` 的 path，不另写一份）。"""
    if class_id is None:
        return None
    return (context or {}).get("class_path", {}).get(class_id)


def channel_name_of(context: dict, channel_id) -> str | None:
    if channel_id is None:
        return None
    return (context or {}).get("channel_name", {}).get(channel_id)


def owner_kind_of(context: dict, owner_id) -> str | None:
    """归属类型＝成员角色（self/spouse/joint/child/nominee；ADR-0019 D6）。"""
    if owner_id is None:
        return None
    member = (context or {}).get("member", {}).get(owner_id)
    return member.get("role") if member else None


# ==================== v5 身份层：ledger 写函数单点 ====================

def get_or_create_item(db: Session, name: str) -> AssetItem:
    """name（现名/legacy 锚名/历史名）→ 资产项；解析不到则新建（双轨窗口单点）。

    解析顺序：display_name 精确（active 优先）→ legacy_name 精确 → 显示名史精确
    （校正规则兼容来源旧名，拍板 10）。一个 name 命中多项时取 id 最小（确定性）。
    """
    if not name:
        raise ValueError("资产项解析需要非空名称")
    item = db.execute(
        select(AssetItem)
        .where(or_(AssetItem.display_name == name, AssetItem.legacy_name == name))
        .order_by(AssetItem.status != "active", AssetItem.id)  # active 优先（MySQL 兼容写法在 sqlite 亦可）
    ).scalars().first()
    if item is None:
        hit = db.execute(
            select(AssetItem)
            .join(AssetItemName, AssetItemName.item_id == AssetItem.id)
            .where(AssetItemName.name == name)
            .order_by(AssetItem.status != "active", AssetItem.id)
        ).scalars().first()
        item = hit
    if item is None:
        # ADR-0019 D9：新建项一律 draft —— 此时六维分类未知，active 要求分类齐全
        # （ck_asset_items_dims_required）。补齐分类后由分类编辑路径转 active。
        item = AssetItem(display_name=name, status="draft")
        db.add(item)
        db.flush()
    return item


def resolve_item_id(db: Session, name: str) -> int | None:
    """只解析不建：name → item_id；无则 None。"""
    if not name:
        return None
    item = db.execute(
        select(AssetItem)
        .where(or_(AssetItem.display_name == name, AssetItem.legacy_name == name))
        .order_by(AssetItem.status != "active", AssetItem.id)
    ).scalars().first()
    if item is None:
        item = db.execute(
            select(AssetItem)
            .join(AssetItemName, AssetItemName.item_id == AssetItem.id)
            .where(AssetItemName.name == name)
            .order_by(AssetItem.status != "active", AssetItem.id)
        ).scalars().first()
    return item.id if item else None


# 变更 dict 单点构造已上收 audit_service.make_change（步骤 5 期跨服务共用无环）；
# 保留 `_change` 别名供本模块与 screenshot_service 既有引用不变。
_change = make_change


# ==================== 批量写入留痕（ADR-0027 D3/D4：①值桶 + ⑤整库桶） ====================
#
# 一次批量操作写「1 条头行 + N 条明细行」，全部与该批业务写入**同一事务**：
# 业务回滚 → 审计不留；审计失败 → 异常上抛阻断业务（ADR-0007 语义）。
# 头行回答「这是哪次操作」（op/count/ids_hash/scope），明细行回答「我这一行怎么没的」，
# 取证要能一行一行往回查。截图采纳（screenshot_service.apply_preview）是逐行一条的
# 先例；批量端点在此之上补头行。

#: 单次批量上限（ADR-0027 D4）：超限走「先导出再清理」的显式流程，不做无上限硬删。
#: 常量落本模块而非 audit_contract.py——后者是纯契约数据（枚举/脱敏词表），
#: 而 500 这个数是**批量写入口的业务护栏**，D4 明确把它挂在批量操作上。
BATCH_WRITE_LIMIT = 500


def ids_fingerprint(ids) -> str:
    """批次指纹：排序去重后 id 清单的 sha256 前 16 位。

    头行不落明文 id 清单（几百个 int 会把头行撑成噪声），但同一批可复现：
    拿候选 id 清单重算即可比对「是不是同一次操作」。
    """
    payload = ",".join(str(i) for i in sorted({int(i) for i in ids}))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def asset_key_fields(asset: Asset) -> dict:
    """审计明细行的「关键字段」（ADR-0027 D6 create/delete 口径）。

    契约列 `category` / `owner_id` 在记录侧**不存在**（ADR-0019：分类与归属都在
    资产项上）→ 落 `item_id`；`evidence_id` ← `source_evidence_id`。金额一律
    `str()`（Decimal 直出保精度，绝不经 float）。
    """
    return {
        "id": asset.id,
        "date": asset.asset_date.isoformat(),
        "amount": str(asset.amount),
        "side": asset.side,
        "item_id": asset.asset_item_id,
        "evidence_id": asset.source_evidence_id,
    }


def batch_head_change(op: str, count: int, ids, scope: dict | None = None,
                      reason: str | None = None,
                      reason_note: str | None = None) -> dict:
    """批次头行：`entity='batch'` / `action='execute'` / `after={op,count,ids_hash,scope}`。

    批次理由（R5）挂在头行：头行就是「这次操作」的载体。注：spec E1 之后明细行
    **也**带同一条 reason（`actor='owner'` 的无理由 delete 行会被 422 拦下），
    头行＋明细同值，不再有「只有头行可解释」的缺口。
    """
    return _change("batch", None, "execute", after={
        "op": op,
        "count": count,
        "ids_hash": ids_fingerprint(ids),
        "scope": scope or {},
    }, reason=reason, reason_note=reason_note)


def _batch_limit_error(count: int) -> ApiError:
    return ApiError(
        ErrorCode.BATCH_LIMIT_EXCEEDED, 400,
        f"超过单次批量上限 {BATCH_WRITE_LIMIT} 条（本次 {count} 条），请先导出再清理",
        details={"limit": BATCH_WRITE_LIMIT, "requested": count})


def _ensure_under_batch_limit(count: int) -> None:
    if count > BATCH_WRITE_LIMIT:
        raise _batch_limit_error(count)


def _active_row(db: Session, asset_item_id: int, asset_date: date) -> Asset | None:
    """同日同项的 active（未被 supersede）快照行。"""
    return db.execute(
        select(Asset).where(Asset.asset_item_id == asset_item_id,
                            Asset.asset_date == asset_date,
                            Asset.superseded_by.is_(None))
    ).scalar_one_or_none()


def correct_asset(db: Session, asset_id: int, amount, side: str | None = None,
                  source_evidence_id: int | None = None,
                  reason: str | None = None,
                  reason_note: str | None = None,
                  commit: bool = True) -> tuple[Asset, dict] | None:
    """更正＝追加（R-4）：同日追加新行＋旧行打 superseded_by；金额本体 write-once。

    三步翻转避开部分唯一索引的语句级检查：
      1) 新行插入时先指向旧行（新行暂不入 active 集）；
      2) 旧行指向新行（旧行退出 active 集）；
      3) 新行落位 active。同一事务内完成。
    分类在资产项上（ADR-0019 D2）：更正不改分类，也不复制任何记录级属性
    （旧 EAV 两表已随 0027 删除）。

    `reason`/`reason_note`（ADR-0027 D7）：可选，只进审计行；缺省＝NULL（不记录原因）——
    运行时绝不因缺 reason 报错。
    """
    old = db.get(Asset, asset_id)
    if old is None:
        return None
    if old.superseded_by is not None:
        raise ValueError("该行已被更正（历史行不可再更正），请对当前有效行操作")
    new_side, amt = _normalize_side_amount(amount, side or old.side or "asset")
    item = db.get(AssetItem, old.asset_item_id) if old.asset_item_id else None
    new = Asset(
        asset_name=item.display_name if item else old.asset_name,  # name 快照单点＝当前显示名
        amount=amt, side=new_side, asset_date=old.asset_date,
        asset_item_id=old.asset_item_id,
        source_evidence_id=source_evidence_id or old.source_evidence_id,
        superseded_by=old.id,  # 步骤 1：暂不入 active
        # 2.1：沿用当日生效口径（此刻旧行仍 active→resolve 读其 cost_basis，NULL 回退史）
        cost_basis=(resolve_cost_basis(db, old.asset_item_id, old.asset_date)
                    if old.asset_item_id is not None else None),
    )
    db.add(new)
    db.flush()
    old.superseded_by = new.id          # 步骤 2：旧行退出 active
    db.flush()
    new.superseded_by = None            # 步骤 3：新行落位 active
    # ADR-0019：分类在资产项上（0027 起记录级 EAV 表已删除），更正不复制任何属性
    change = _change("asset_snapshot", new.id, "correct",
                     before={"amount": str(old.amount), "side": old.side},
                     after={"amount": str(amt), "side": new_side},
                     evidence_ref=new.source_evidence_id,
                     reason=reason, reason_note=reason_note)
    audit_service.record(db, change)  # 同事务：业务回滚则审计不留
    if commit:
        db.commit()
        db.refresh(new)
    # commit=False：批量单事务场景，由调用方统一 commit（不 refresh）
    return new, change


def rename_item(db: Session, item_id: int, new_name: str) -> tuple[AssetItem, dict] | None:
    """资产项改名（拍板 10）：显示名史追加＋display_name 更新；历史行不回写。

    旧名确保入史（运行期新建项的首名不靠 0008 回填）：若旧名尚无记录，补一行，
    保证「历史名可解析回 item」（校正规则/导入兼容来源旧名）。
    """
    item = db.get(AssetItem, item_id)
    if item is None:
        return None
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("显示名不可为空")
    before = item.display_name
    if new_name == before:
        return item, _change("asset_item", item.id, "rename",
                             before=before, after=before)
    recorded = set(db.execute(
        select(AssetItemName.name).where(AssetItemName.item_id == item.id)
    ).scalars().all())
    if before and before not in recorded:
        eff = item.created_at.date() if isinstance(item.created_at, datetime) \
            else date.today()
        db.add(AssetItemName(item_id=item.id, name=before,
                             effective_from=eff, origin="rename"))
    db.add(AssetItemName(item_id=item.id, name=new_name,
                         effective_from=date.today(), origin="rename"))
    item.display_name = new_name
    change = _change("asset_item", item.id, "rename",
                     before=before, after=new_name)
    audit_service.record(db, change)
    db.commit()
    db.refresh(item)
    return item, change


def _dims_complete(item: AssetItem) -> bool:
    """ADR-0019：active 态要求四个必填维度齐全（与 ck_asset_items_dims_required 一致）。"""
    return all(getattr(item, f) is not None for f in
               ("asset_class_id", "liquidity_days", "institution_id", "owner_id"))


def set_item_status(db: Session, item_id: int, status: str,
                    effective_from: date | None = None,
                    reason: str | None = None,
                    reason_note: str | None = None,
                    actor: str = "owner") -> tuple[AssetItem, dict] | None:
    """资产项状态机（R-37）：active ↔ archived；归档＝人裁决动作。

    `effective_from`（ADR-0017 D2 修订／0025）：归档**自该日起**终止计量，此前
    的历史测量值保留。缺省＝今天（人裁决「现在起不再持有」）；血缘合并传入人声明的
    `effective_date`，使归档与承接口径在同一天生效。解除归档把该日期清回 NULL
    （重新进入计量）。审计 before/after 一并记下该日期，便于回溯「哪天起不算」。

    归档日未知（NULL 且 status='archived'）只对 D2 落地前的存量形态成立：对一切
    as-of 日期保守剔除。
    """
    if status not in ("active", "archived"):
        raise ValueError(f"非法状态: {status}")
    item = db.get(AssetItem, item_id)
    if item is None:
        return None
    before = {"status": item.status,
              "archived_effective_from": item.archived_effective_from}
    item.status = status
    if status == "archived":
        item.archived_effective_from = effective_from or date.today()
    else:
        # ADR-0019：active 要求六维分类齐全（ck_asset_items_dims_required）。
        # 重新启用一个尚未分类的项时退回 draft，而不是让整条 unarchive 失败。
        if not _dims_complete(item):
            item.status = "draft"
        item.archived_effective_from = None
    after = {"status": item.status,
             "archived_effective_from": item.archived_effective_from}
    change = _change("asset_item", item.id,
                     "archive" if status == "archived" else "unarchive",
                     before=before, after=after,
                     reason=reason, reason_note=reason_note)
    # `actor='system'` 只给内部流程（血缘合并/拆分/挂接归档前身）用：归档是那次
    # 人裁决的**派生物**，不是第二次裁决（spec E2 口径），缺 reason 不拦。
    audit_service.record(db, change, actor=actor)
    db.commit()
    db.refresh(item)
    return item, change


def list_items(db: Session, include_archived: bool = True) -> list[dict]:
    """资产项列表（含 aliases＝历史名）。"""
    q = select(AssetItem).order_by(AssetItem.id)
    if not include_archived:
        # draft 是「分类未补齐」，不是一个隐藏状态：仍然出现在列表与当前值里，
        # 只由 ck_asset_items_dims_required 约束其不能缺分类地转 active。
        q = q.where(AssetItem.status != "archived")
    items = db.execute(q).scalars().all()
    out = []
    for it in items:
        aliases = db.execute(
            select(AssetItemName.name).where(AssetItemName.item_id == it.id)
        ).scalars().all()
        out.append({
            "id": it.id,
            "display_name": it.display_name,
            "channel": it.channel,
            "status": it.status,
            "legacy_name": it.legacy_name,
            "aliases": sorted(set(aliases)),
        })
    return out


# ==================== 快照/列表读函数（active 行口径） ====================

def _normalize_side_amount(amount, side="asset"):
    """归一化金额与 side：负债以正数存储，负数金额视为负债。

    金额统一 quantize 到分（与 NUMERIC(15,2) 存储一致）——审计
    before/after 的 amount 字符串由此单点保证两位小数格式。

    Returns:
        (side, amount)：side ∈ {"asset", "liability"}，amount 恒 >= 0（两位小数）
    """
    amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    if side == "liability" or amt < 0:
        return "liability", abs(amt)
    return side or "asset", amt


def expand_asset_class_ids(db: Session, class_ids) -> list[int]:
    """类别过滤＝该类别**及其所有后代**（沿用旧 fund_type 的树展开语义）。

    旧实现展开 `attributes.parent_id` 树，新实现展开 `asset_classes.parent_id`；
    只选父节点（如「B 固定收益」）必须能命中其全部叶子下的资产项，否则是退化。
    """
    roots = [int(c) for c in (class_ids or [])]
    if not roots:
        return []
    children: dict[int | None, list[int]] = {}
    for row in classification_service.list_asset_classes(db):
        children.setdefault(row["parent_id"], []).append(row["id"])
    out: list[int] = []
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        class_id = stack.pop()
        if class_id in seen:
            continue
        seen.add(class_id)
        out.append(class_id)
        stack.extend(children.get(class_id, []))
    return out


def _item_ids_matching(*conditions):
    """`AssetItem` 上满足条件的项 id 子查询（记录侧过滤用，避免 join 放大行数）。"""
    return select(AssetItem.id).where(*conditions)


def list_assets(
    db: Session,
    asset_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    asset_class_ids: list[int] | None = None,
    institution_ids: list[int] | None = None,
    channel_ids: list[int] | None = None,
    owner_ids: list[int] | None = None,
    liquidity_days: int | None = None,
    asset_name: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    sort_field: str | None = None,
    sort_order: str | None = "desc",
    page: int = 1,
    page_size: int = 100,
    item_id: int | None = None,
    include_superseded: bool = False,
    owner_kind: str | None = None,
    tag_ids: list[int] | None = None,
    source_evidence_id: int | None = None,
) -> tuple[list[Asset], int]:
    """列出资产记录（active 行口径）。

    分类维度（类别/流动性/机构/渠道/归属/标签）已上移到资产项（ADR-0019），
    过滤器因此全部落到 `asset_items` / `asset_item_tags` 上，通过 item id 子查询
    施加——记录侧不再持有分类。

    Args:
        asset_class_ids: 资产类别（**含其所有后代**，树展开语义不退化）
        institution_ids: 机构 id
        channel_ids: 渠道 id
        owner_ids: 归属成员 id（`members`，不再是旧 owner 属性）
        liquidity_days: 流动性档位天数（精确匹配；8 档白名单）
        owner_kind: 成员角色（self/spouse/joint/child/nominee）
        tag_ids: 标签 id 多值，**AND 语义**（每一项都要有这些标签）
        include_superseded: 含被更正历史行（默认 False＝仅 active 行）
        source_evidence_id: **读**参数：按来源凭证反查（`assets.source_evidence_id`
            == 给定值）。与写路径的同名参数职责不同，见下方注释，勿合并。

    Returns:
        (资产列表, 总数)
    """
    q = select(Asset).distinct()

    if not include_superseded:
        q = q.where(Asset.superseded_by.is_(None))

    if asset_date:
        q = q.where(Asset.asset_date == asset_date)
    if date_from:
        q = q.where(Asset.asset_date >= date_from)
    if date_to:
        q = q.where(Asset.asset_date <= date_to)

    # ---- 分类过滤（项级） ----
    class_scope = expand_asset_class_ids(db, asset_class_ids)
    if class_scope:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(AssetItem.asset_class_id.in_(class_scope))))

    if institution_ids:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(AssetItem.institution_id.in_(institution_ids))))

    if channel_ids:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(AssetItem.channel_id.in_(channel_ids))))

    if owner_ids:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(AssetItem.owner_id.in_(owner_ids))))

    if owner_kind:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(
                AssetItem.id.in_(
                    select(AssetItem.id).join(Member, Member.id == AssetItem.owner_id)
                    .where(Member.role == owner_kind)))))

    if liquidity_days is not None:
        q = q.where(Asset.asset_item_id.in_(
            _item_ids_matching(AssetItem.liquidity_days == liquidity_days)))

    # 标签：多值 AND（每个 tag 一份 exists，同一项可被多行记录引用故用子查询）
    for tag_id in tag_ids or []:
        q = q.where(Asset.asset_item_id.in_(
            select(AssetItemTag.item_id).where(AssetItemTag.tag_id == int(tag_id))))

    if asset_name:
        q = q.where(Asset.asset_name.contains(asset_name))

    if item_id:
        q = q.where(Asset.asset_item_id == item_id)

    # 凭证反查（spec D8）：这里是与写路径**同名但不同职责**的只读筛选参数。
    # 写路径的两个同名参数存在别处——`correct_asset(..., source_evidence_id=)`
    # （本模块 :189 附近，更正时给新行记来源）与 `screenshot_service` 采纳落库
    # （`row.source_evidence_id = ...`）。三者共享列名，但本参数只做 `WHERE`，
    # 不写库、不参与写入语义，**不可与写路径参数合并**。
    if source_evidence_id is not None:
        q = q.where(Asset.source_evidence_id == source_evidence_id)

    # 处理金额区间筛选
    if amount_min is not None:
        q = q.where(Asset.amount >= amount_min)
    if amount_max is not None:
        q = q.where(Asset.amount <= amount_max)

    count_q = select(func.count()).select_from(q.subquery())
    total = db.execute(count_q).scalar() or 0

    # 排序
    if sort_field:
        sort_column = getattr(Asset, sort_field, None)
        if sort_column:
            # 主键排序 + **固定** `id DESC` 兜底（spec `records-updated-at` D1/D2）。
            # 平局是常态：批量 UPDATE 会把上千行刷成同一微秒，无兜底键时 OFFSET
            # 翻页的顺序不定 → 重复/漏行。兜底方向固定 desc，**不随** `sort_order`
            # 翻转（同刻内「后录入的在前」两种方向下都可预测）。
            # 未传 `sort_field` 的默认分支另有 `asset_name` 次键，本次不动。
            tiebreaker = desc(Asset.id)
            if sort_order and sort_order.lower() == "asc":
                q = q.order_by(asc(sort_column), tiebreaker)
            else:
                q = q.order_by(desc(sort_column), tiebreaker)
    else:
        q = q.order_by(desc(Asset.asset_date), Asset.asset_name)

    q = q.offset((page - 1) * page_size).limit(page_size)

    assets = db.execute(q).scalars().all()
    return assets, total


def get_asset(db: Session, asset_id: int) -> Asset | None:
    """获取资产详情（新的多对多模型）

    Args:
        db: 数据库会话
        asset_id: 资产ID

    Returns:
        资产对象
    """
    return db.get(Asset, asset_id)


def create_asset(
    db: Session,
    asset_name: str,
    amount: float | Decimal,
    asset_date: date,
    attribute_ids: list[int] | None = None,
    side: str = "asset",
    item_id: int | None = None,
) -> Asset:
    """创建资产记录（账本事实）。

    Args:
        db: 数据库会话
        asset_name: 资产名称（item_id 缺省时用于解析/新建资产项）
        amount: 金额（负数将按负债归一）
        asset_date: 资产日期
        attribute_ids: **已废弃**（ADR-0019 D2：分类上移到资产项）。传非空 → 422；
            保留参数位只为把旧调用方的静默失效变成显式报错。
        side: 资产/负债标识 "asset" | "liability"
        item_id: 已知资产项（当前值写入口用）：跳过名字解析，直接挂该项

    Returns:
        创建的资产对象
    """
    _reject_legacy_classification(
        ["attribute_ids"] if attribute_ids else [], where="资产记录")
    side, amt = _normalize_side_amount(amount, side)
    item = (db.get(AssetItem, item_id) if item_id is not None
            else get_or_create_item(db, asset_name))
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, "资产项不存在",
                       details={"item_id": item_id})
    asset = Asset(
        asset_name=item.display_name,   # name 快照＝写入时显示名（单点生成）
        asset_item_id=item.id,
        amount=amt,
        asset_date=asset_date,
        side=side,
        # 2.1：建行时冗余「当日生效口径」（NULL＝该日无口径；读端回退史，等价）
        cost_basis=resolve_cost_basis(db, item.id, asset_date),
    )
    db.add(asset)
    db.flush()
    audit_service.record(db, _change(
        "asset_snapshot", asset.id, "create",
        before=None,
        after={"amount": str(asset.amount), "side": asset.side,
               "asset_date": asset.asset_date.isoformat()},
        evidence_ref=asset.source_evidence_id))
    db.commit()
    db.refresh(asset)
    return asset


def update_asset(
    db: Session,
    asset_id: int,
    asset_name: str | None = None,
    amount: float | Decimal | None = None,
    asset_date: date | None = None,
    attribute_ids: list[int] | None = None,
    side: str | None = None,
) -> Asset | None:
    """更新资产——v5 写语义收敛（spec §4）＋ ADR-0019 分类上移。

    write-once：amount / side / asset_date 属账本事实，请求修改 → WriteOnceConflict
    （router 409，提示走 correct）；asset_name 不可直改（走 asset-items rename）。
    attribute_ids **已废弃**：分类只存在于资产项，传非空 → 422（不再写记录级绑定）。
    """
    asset = db.get(Asset, asset_id)
    if not asset:
        return None

    _reject_legacy_classification(
        ["attribute_ids"] if attribute_ids else [], where="资产记录")

    forbidden = [k for k, v in (
        ("asset_name", asset_name), ("amount", amount),
        ("asset_date", asset_date), ("side", side),
    ) if v is not None]
    # 幂等豁免：值与现值完全一致的 amount 传参（如前端回填整行）不算改写请求
    if "amount" in forbidden and amount is not None and side is None:
        _, want = _normalize_side_amount(amount, asset.side or "asset")
        if want == Decimal(str(asset.amount)):
            forbidden.remove("amount")
    if forbidden:
        raise WriteOnceConflict(
            f"账本事实字段不可直改：{forbidden}；改金额走 correct，改名走 asset-items rename")

    db.commit()
    db.refresh(asset)
    return asset


def _require_tags(db: Session, tag_ids) -> list[int]:
    """校验 tag_ids 均存在于 `tags` 字典（去重保序）；非法 → 422（带 fields）。"""
    unique: list[int] = []
    for tag_id in tag_ids or []:
        try:
            value = int(tag_id)
        except (TypeError, ValueError):
            raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                           f"标签 id 须为整数: {tag_id!r}",
                           details={"fields": [{"field": "tag_ids",
                                                "message": f"非法标签 id: {tag_id!r}"}]})
        if value not in unique:
            unique.append(value)
    if unique:
        found = {t.id for t in db.execute(select(Tag).where(Tag.id.in_(unique))).scalars().all()}
        missing = sorted(set(unique) - found)
        if missing:
            raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                           f"标签不存在: {missing}",
                           details={"fields": [{"field": "tag_ids",
                                                "message": f"非法标签 id: {missing}"}]})
    return unique


def _item_id_of(db: Session, asset: Asset) -> int:
    """记录 → 资产项 id（存量无 item 行按名补解析，不做读时写回填）。"""
    if asset.asset_item_id is not None:
        return asset.asset_item_id
    return get_or_create_item(db, asset.asset_name).id


def _item_tag_ids(db: Session, item_id: int) -> list[int]:
    item = db.get(AssetItem, item_id)
    return sorted(t.id for t in item.tags) if item is not None else []


def replace_asset_tags(db: Session, asset_id: int, tag_ids) -> tuple[Asset, bool] | None:
    """替换该记录所属**资产项**的标签（标签已上移到项，ADR-0019 D2/D7）。

    入参仍是记录 id（端点不变），但作用对象是资产项：标签挂项不挂记录，
    一次标注覆盖全历史。返回 (asset, changed)；记录不存在 → None。
    归档项 409 ITEM_ARCHIVED、超 5 个 422 TAG_LIMIT_EXCEEDED 由分类服务统一给。
    """
    asset = db.get(Asset, asset_id)
    if asset is None:
        return None
    item_id = _item_id_of(db, asset)
    wanted = _require_tags(db, tag_ids)
    before = _item_tag_ids(db, item_id)
    after = classification_service.set_tags(db, item_id, wanted)
    db.refresh(asset)
    return asset, sorted(t["id"] for t in after["tags"]) != before


def batch_tag_records(db: Session, record_ids, add_tag_ids=None,
                      remove_tag_ids=None) -> dict:
    """批量加/减**资产项**标签（先加后减，remove 胜出）。

    标签上移到项之后按 `asset_item_id` 分组：同一项被多行引用时只写一次，
    但计数仍按记录条数回报（端点入参是记录 id，响应语义保持按记录统计）。
    逐项独立成败，失败项返回 code（NOT_FOUND/SUPERSEDED/ITEM_ARCHIVED/…）。

    留痕（ADR-0027 D3 口径桶）：1 条批次头行（`batch/execute`，
    `after={op:'update', count, ids_hash, scope}`）＋ 逐项明细
    （`classification/update`，`row_id`＝资产项 id，快照只有 `tags` 一个键）。
    - 头行 `count` ＝ **实际生效的明细行数**（＝真被改动的资产项数，不是记录条数：
      同一项的多条记录只产生一条明细）；记录条数落在 `scope.record_count` 里。
    - 整批**一次提交**：逐项用 SAVEPOINT 隔离失败，头行与成功项明细同一事务落库；
      全批无改动/全失败 → 零审计行（什么都没变就没有可留痕的事实）。
    """
    ids: list[int] = []
    for record_id in record_ids or []:
        try:
            value = int(record_id)
        except (TypeError, ValueError):
            raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "record_ids 含非法 id",
                           details={"fields": [{"field": "record_ids",
                                                "message": "id 须为整数"}]})
        if value not in ids:
            ids.append(value)
    if not ids or len(ids) > 200:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                       "record_ids 须为 1..200 个去重 id",
                       details={"fields": [{"field": "record_ids",
                                            "message": "1..200 个 id"}]})
    add_ids = set(_require_tags(db, add_tag_ids))
    remove_ids = set(_require_tags(db, remove_tag_ids))

    # 记录 → 资产项分组；记录级不可写的情形（不存在/已更正）先判、逐条报错
    groups: dict[int, list[int]] = {}
    failed: list[dict] = []
    for record_id in ids:
        asset = db.get(Asset, record_id)
        if asset is None:
            failed.append({"id": record_id, "code": "NOT_FOUND"})
            continue
        if asset.superseded_by is not None:
            failed.append({"id": record_id, "code": "SUPERSEDED"})
            continue
        groups.setdefault(_item_id_of(db, asset), []).append(record_id)

    updated = skipped = 0
    changed_items: list[int] = []
    for item_id, record_id_list in groups.items():
        current = set(_item_tag_ids(db, item_id))
        target = (current | add_ids) - remove_ids
        if target == current:
            skipped += len(record_id_list)
            continue
        nested = db.begin_nested()                   # SAVEPOINT：逐项独立成败
        try:
            classification_service.set_tags(db, item_id, sorted(target), commit=False)
        except ApiError as exc:
            nested.rollback()                        # 连带回滚该项自己的审计行
            failed.extend({"id": rid, "code": str(exc.code)} for rid in record_id_list)
            continue
        nested.commit()
        updated += len(record_id_list)
        changed_items.append(int(item_id))

    if changed_items:
        audit_service.record(db, batch_head_change(
            "update", len(changed_items), changed_items,
            scope={"via": "assets/tags/batch", "field": "tags",
                   "record_count": len(ids),
                   "add_tag_ids": sorted(add_ids),
                   "remove_tag_ids": sorted(remove_ids)}))
    db.commit()
    return {"updated": updated, "skipped": skipped, "failed": failed}


def delete_asset(db: Session, asset_id: int, reason: str | None = None,
                 reason_note: str | None = None) -> bool:
    """删除资产（误录撤销；v5：delete 审计落库，审计痕迹步骤 4 语义）。

    `reason`/`reason_note`（ADR-0027 D7）只进审计行；缺省＝NULL（不记录原因）。

    Returns:
        是否删除成功
    """
    asset = db.get(Asset, asset_id)
    if not asset:
        return False

    audit_service.record(db, _change(
        "asset_snapshot", asset.id, "delete",
        before={"amount": str(asset.amount), "side": asset.side,
                "asset_date": asset.asset_date.isoformat()},
        after=None,
        reason=reason, reason_note=reason_note))
    db.delete(asset)
    db.commit()
    return True


def get_snapshot_dates(db: Session) -> list[date]:
    """获取所有快照日期

    active 行口径（与 `analysis_service._get_all_snapshot_dates` 同形）：只有
    superseded（被更正）行的日期不进日期选择器，否则会被当成一个空的账期。

    Args:
        db: 数据库会话

    Returns:
        日期列表
    """
    result = db.execute(
        select(func.distinct(Asset.asset_date))
        .where(Asset.superseded_by.is_(None))
        .order_by(desc(Asset.asset_date))
    ).scalars().all()
    # 确保返回的是 date 对象
    return [d if isinstance(d, date) else datetime.strptime(str(d), '%Y-%m-%d').date() for d in result]


def get_snapshot_assets(db: Session, snapshot_date: date) -> list[Asset]:
    """获取指定日期的快照资产（active 行口径，superseded_by IS NULL）。"""
    return db.execute(
        select(Asset)
        .where(Asset.asset_date == snapshot_date, Asset.superseded_by.is_(None))
        .order_by(Asset.asset_name)
    ).scalars().all()


def find_last_snapshot_date(db: Session, before_date: date, period_type: str) -> date | None:
    if period_type == "day":
        target_before = before_date
    else:
        prev_end = get_previous_period_end(before_date, period_type)
        prev_start, _ = get_period_start_end(prev_end, period_type)
        target_before = prev_end + __import__("datetime").timedelta(days=1)

    result = db.execute(
        select(Asset.asset_date)
        .where(Asset.asset_date < target_before)
        .order_by(Asset.asset_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return result


# ==================== 当前值读模型 / 就地写（ADR-0016，spec §4） ====================

#: 估值日距今阈值：**只用于前端打标**（`valuation_stale`），后端只给天数。
#: 90 天（旧 `CURRENT_FRESH_DAYS = 30` 的三态枚举已删，ADR-0019 D12）。
VALUATION_STALE_DAYS = 90


def _parse_current_amount(amount) -> Decimal:
    """当前值写入口的金额解析：必填、有限、>= 0，quantize 到分（422 带 fields）。"""
    try:
        amt = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "金额格式非法",
                       details={"fields": [{"field": "amount",
                                            "message": "须为十进制数字符串"}]})
    if not amt.is_finite():
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "金额格式非法",
                       details={"fields": [{"field": "amount",
                                            "message": "须为有限小数"}]})
    amt = amt.quantize(Decimal("0.01"))
    if amt < 0:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422, "金额不可为负",
                       details={"fields": [{"field": "amount",
                                            "message": "须 >= 0"}]})
    return amt


def get_current_values(db: Session, as_of: date | None = None,
                       include_archived: bool = False) -> dict:
    """当前值读模型（spec §4.1）：每个资产项一行＝截至 as_of 的最新有效记录。

    取数复用 analysis_service._get_latest_records_as_of（按 item 分组、active 行
    口径、跨期 carry），本函数只做「行→资产项」挂接与**估值日距今**派生，不另写 SQL。
    从未录入的项也返回（record_id/asset_date/amount 与估值字段均为 null）；排序由
    前端负责，服务端按 item id 稳定输出。

    陈旧度口径（ADR-0019 D12 / `.scratch/db-rebuild/spec.md` D4）：旧的三态
    `freshness: fresh|stale|missing` 已删除——`missing`（没有记录）本来就是 null，
    不是一个状态。现在只给事实与一个阈值布尔：

    - `valuation_date`：该值来自哪一天（ISO 日期；无记录 → null）；
    - `valuation_age_days`：该日距参考日的天数（无记录 → null）；
    - `valuation_stale`：`valuation_age_days > VALUATION_STALE_DAYS`（90 天），
      仅供前端打标/排序；无记录 → null（不把「没录过」混同为「不陈旧」）。

    分类（ADR-0019）：每行的类别/流动性/机构/渠道/归属/标签/风险一律取自**资产项**，
    与「有没有记录」无关——所以无记录行也带完整分类与 `classification_missing`。
    过滤口径保持 `status != 'archived'`：`draft`（分类未补齐）是正常状态，照常显示。

    备注（ADR-0031 D2，`.scratch/current-value-note/spec.md` D1）：每行带项级 `note`，
    与明细视图同一归一（trim 后空串→`null`，绝不回空串）；备注挂在项上，所以无记录行
    同样带它，`include_archived=True` 时归档项照常回传。

    `include_archived=True` 取数：陈列（每项一行＋其最后记录）不是汇总，归档项
    也应显示自己的最后值（ADR-0017 D2 只约束求和口径）；是否列出该行仍由下面的
    `include_archived` 参数过滤决定，两者职责分离。
    """
    from services.analysis_service import _get_latest_records_as_of  # 局部导入破环

    ref = as_of or date.today()
    # 陈列口径：归档项也取最后记录（列出与否由下面的 item 状态过滤决定）
    latest_rows = _get_latest_records_as_of(db, ref, include_archived=True)
    record_ids = [row[2] for row in latest_rows]
    latest_by_item: dict[int, Asset] = {}
    if record_ids:
        for rec in db.execute(
            select(Asset).where(Asset.id.in_(record_ids))
        ).scalars().all():
            # 双轨窗口内无 item 的存量行无法归属到资产项（不做读时写回填）
            if rec.asset_item_id is not None:
                latest_by_item.setdefault(rec.asset_item_id, rec)

    q = select(AssetItem).order_by(AssetItem.id)
    if not include_archived:
        # draft 是「分类未补齐」，不是一个隐藏状态：仍然出现在列表与当前值里，
        # 只由 ck_asset_items_dims_required 约束其不能缺分类地转 active。
        q = q.where(AssetItem.status != "archived")

    item_rows = db.execute(q).scalars().all()
    context = classification_context(db, [i.id for i in item_rows])

    # 项级备注（ADR-0031 D2，`.scratch/current-value-note/spec.md` D1/D2）：与明细视图
    # 同源同归一——trim 后空串＝没写（`None`），与写入侧 `PUT /{item_id}/note` 一致。
    # **一次 IN 查询取全量**（票 14 同一写法）：备注只有一份、挂在项上，逐行读会让
    # 当前值表的行数变成 SQL 条数（N+1）。空集守卫：零项时 `IN ()` 是无效 SQL。
    note_by_item: dict[int, str | None] = {}
    item_ids = [i.id for i in item_rows]
    if item_ids:
        note_by_item = {
            item_id: (note or "").strip() or None
            for item_id, note in db.execute(
                select(AssetItem.id, AssetItem.note)
                .where(AssetItem.id.in_(item_ids))).all()
        }

    items = []
    for item in item_rows:
        cls = context["by_item"].get(item.id) or {}
        base = {
            "item_id": item.id,
            "display_name": item.display_name,
            "item_status": item.status,
            # 分类只读回显（项级唯一真源）
            "asset_class_id": cls.get("asset_class_id"),
            "asset_class_name": cls.get("asset_class_name"),
            "asset_class_code": cls.get("asset_class_code"),
            "asset_class_path": class_path_of(context, cls.get("asset_class_id")),
            "liquidity_days": cls.get("liquidity_days"),
            "liquidity_label": cls.get("liquidity_label"),
            "institution_id": cls.get("institution_id"),
            "institution_name": cls.get("institution_name"),
            "channel_id": cls.get("channel_id"),
            "channel_name": channel_name_of(context, cls.get("channel_id")),
            "owner_id": cls.get("owner_id"),
            "owner_name": cls.get("owner_name"),
            "owner_kind": owner_kind_of(context, cls.get("owner_id")),
            "risk_level": cls.get("risk_level"),
            "tags": cls.get("tags") or [],
            "classification_missing": cls.get("missing_dims") or [],
            # 项级备注（项级只读回显）：有备注为非空文本、没写为 None，**绝不回空串**。
            # 放在 base 里：无记录行也带备注（备注挂在项上，与「有没有记录」无关）。
            "note": note_by_item.get(item.id),
        }
        rec = latest_by_item.get(item.id)
        if rec is None:
            items.append({
                **base,
                "side": "asset",
                "record_id": None,
                "asset_date": None,
                "amount": None,
                # 无记录：估值日与天数都是 null（不是第三种「状态」）
                "valuation_date": None,
                "valuation_age_days": None,
                "valuation_stale": None,
            })
            continue
        age_days = (ref - rec.asset_date).days
        items.append({
            **base,
            "side": rec.side or "asset",
            "record_id": rec.id,
            "asset_date": rec.asset_date.isoformat(),
            "amount": str(rec.amount),   # 与 build_asset_response 同格式（字符串小数）
            "valuation_date": rec.asset_date.isoformat(),
            "valuation_age_days": age_days,
            "valuation_stale": age_days > VALUATION_STALE_DAYS,
        })
    return {"as_of": ref.isoformat(), "is_latest": ref == date.today(),
            "items": items}


def set_current_value(db: Session, item_id: int, amount,
                      as_of: date | None = None, mode: str = "auto",
                      side: str | None = None,
                      reason: str | None = None,
                      reason_note: str | None = None) -> dict:
    """当前值就地写（spec §4.2）：唯一分支点，路由只做参数绑定。

    参考日 D = as_of ?? today；D 日已有有效行 → correct_asset（数改、日期不变）；
    否则 mode=auto 时以 D 前最近有效行为模板在 D 新建（无任何记录 → 用 body 的
    side，缺省 asset，asset_name＝item 当前显示名），mode=correct_only → 409。
    金额与现值相同 → unchanged（不建行、不写审计；沿用 update_asset 既有约定）。
    写入一律经 create_asset / correct_asset（不改账本存储语义）。
    """
    if mode not in ("auto", "correct_only"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 422,
                       "mode 必须为 auto 或 correct_only",
                       details={"fields": [{"field": "mode",
                                            "message": "auto|correct_only"}]})
    item = db.get(AssetItem, item_id)
    if item is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, "资产项不存在",
                       details={"item_id": item_id})
    amt = _parse_current_amount(amount)
    ref = as_of or date.today()

    row = _active_row(db, item_id, ref)
    if row is not None:
        prev_amount = str(row.amount)
        if amt == Decimal(prev_amount):
            return {"kind": "unchanged", "record_id": row.id,
                    "asset_date": row.asset_date.isoformat(),
                    "amount": prev_amount,
                    "previous_amount": None, "previous_record_id": None}
        # 「当前值就地写」走到更正分支＝纠正类写入（spec E1）：reason 透传给
        # correct_asset 的审计行；缺则 422（新建分支是 create，不在此列）。
        new, _ = correct_asset(db, row.id, amount=amt, reason=reason,
                               reason_note=reason_note)
        return {"kind": "corrected", "record_id": new.id,
                "asset_date": new.asset_date.isoformat(),
                "amount": str(new.amount),
                "previous_amount": prev_amount,
                "previous_record_id": row.id}

    if mode == "correct_only":
        raise ApiError("no_record_for_date", 409,
                       f"{ref.isoformat()} 无该资产项的有效记录",
                       details={"item_id": item_id,
                                "asset_date": ref.isoformat()})

    template = db.execute(
        select(Asset)
        .where(Asset.asset_item_id == item_id,
               Asset.asset_date < ref,
               Asset.superseded_by.is_(None))
        .order_by(desc(Asset.asset_date), desc(Asset.id))
        .limit(1)
    ).scalars().first()

    if template is not None:
        new_side = template.side or "asset"
    else:
        new_side = side or "asset"
    if new_side not in ("asset", "liability"):
        raise ApiError(ErrorCode.INVALID_SIDE, 400, f"无效的侧别：{new_side!r}",
                       details={"side": new_side})

    # 分类不再随行复制（ADR-0019：分类在资产项上，新行天然继承该项分类）
    created = create_asset(db, item.display_name, amt, ref,
                           side=new_side, item_id=item_id)
    return {"kind": "created", "record_id": created.id,
            "asset_date": created.asset_date.isoformat(),
            "amount": str(created.amount),
            "previous_amount": None, "previous_record_id": None}


def batch_update_records(db: Session, record_ids: list[int], updates: dict,
                         reason: str | None = None,
                         reason_note: str | None = None) -> int:
    """批量更新资产记录——v5 写语义收敛（spec §4）＋ ADR-0019 分类上移。

    amount/asset_date/asset_name 属账本事实，请求携带 → WriteOnceConflict（409）。
    side 为记录级账本事实，唯一合法变更语义是 append 更正（冻结契约）：
    请求携带合法 side（"asset"/"liability"）时，对每个 active 行（superseded_by
    IS NULL）——side 相同跳过、已 superseded 跳过、不同则按 correct 语义追加新行
    （金额不变、同日、旧行 supersede、审计 action="correct"，同事务 commit=False）。
    非法 side → InvalidSideError（router 映射 400）。

    分类字段（attribute_ids / fund_type_id / account_id / liquidity_rating_id /
    owner_id / tag_id …）**一律 422**：分类只存在于资产项，记录侧不可写（ADR-0019
    D2）。静默忽略会让前端以为改上了，正是本重构要根治的双真源问题。

    留痕（ADR-0027 ①值桶）：1 条批次头行（`entity='batch', action='execute'`，
    `after={op:'update', count, ids_hash, scope}`）落在逐行更正**之前**；逐行明细
    由 `correct_asset` 的 `asset_snapshot/correct` 行承担（before/after 只含变更列）。
    头行 count＝本批**实际生效**行数（同侧行/历史行不计），全部与业务同事务。

    返回 updated_count：携带 side 时＝**实际翻转（append 新行）条数**（同侧行与
    历史行不计）；无 side 时为 0（除 side 外没有可改的记录级字段）。
    """
    from sqlalchemy import update

    # 过滤掉值为None的字段
    update_data = {k: v for k, v in updates.items() if v is not None}

    if not update_data or not record_ids:
        return 0

    _reject_legacy_classification(
        [k for k in update_data if k in LEGACY_FIELD_REPLACEMENTS
         or k in ("fund_type_id", "account_id", "liquidity_rating_id", "owner_id")],
        where="资产记录（批量更新）")

    # side 从 update_data 拦截（不得落入 basic_updates 直写）
    new_side = update_data.pop("side", None)
    if new_side is not None and new_side not in ("asset", "liability"):
        raise InvalidSideError(f"无效的侧别（仅 asset/liability）：{new_side!r}")

    forbidden = [k for k in update_data
                 if k in ("asset_name", "amount", "asset_date")]
    if forbidden:
        raise WriteOnceConflict(
            f"批量更新不得携带账本事实字段：{forbidden}；金额更正请逐行走 correct 或走截图采纳")

    # side＝append 更正：逐行翻转，维护 old_id→new_id 映射（单事务）
    # 先定型「本批实际会变哪些行」：头行的 count 必须与真实生效行数一致，
    # 虚报行数会让取证时对不上账（同侧行/历史行不产生任何写入）。
    flip_ids: list[int] = []
    if new_side is not None:
        for record_id in record_ids:
            row = db.get(Asset, record_id)
            if row is None or row.superseded_by is not None:
                continue  # 已 superseded 的历史行不被动
            if (row.side or "asset") == new_side:
                continue  # side 相同跳过，不产生新行
            flip_ids.append(record_id)

    # 批次头行（ADR-0027 D3/D4 ①值桶）：位置在逐行更新**之前**、与业务同事务。
    if new_side is not None:
        targets = flip_ids
    else:
        targets = db.execute(
            select(Asset.id).where(Asset.id.in_(record_ids))).scalars().all()
    if targets:
        # 头行与逐行 correct 明细**同 reason**（spec 实施备注：明细行 action='correct'
        # 属强制集，只给头行带理由会让明细被自己的强制点 422 掉）
        audit_service.record(db, batch_head_change(
            "update", len(targets), targets,
            scope={"via": "batch-update", "fields": sorted(update_data),
                   "side": new_side},
            reason=reason, reason_note=reason_note))

    id_map: dict[int, int] = {}
    for record_id in flip_ids:
        row = db.get(Asset, record_id)   # identity map 命中，无额外 SQL
        # 逐行 correct 是这次批量改判的明细（spec E1：明细也必须有 reason）
        corrected, _ = correct_asset(db, record_id, amount=row.amount,
                                     side=new_side, commit=False,
                                     reason=reason, reason_note=reason_note)
        id_map[record_id] = corrected.id
    record_ids = [id_map.get(rid, rid) for rid in record_ids]

    # 除 side 外没有可改的记录级字段（分类已上移到项）：仍按契约更新 updated_at
    basic_updates = {k: v for k, v in update_data.items() if k != "ids"}
    basic_updates['updated_at'] = datetime.now()
    result = db.execute(
        update(Asset)
        .where(Asset.id.in_(record_ids))
        .values(**basic_updates)
    )

    db.commit()
    # side 路径：updated_count＝实际翻转条数（append 新行；同侧/历史行未计）
    if new_side is not None:
        return len(id_map)
    return result.rowcount


def batch_delete_records(db: Session, record_ids: list[int],
                         scope: dict | None = None,
                         reason: str | None = None,
                         reason_note: str | None = None) -> int:
    """批量删除资产记录（**硬删**；ADR-0027 ①值桶：头行 + 逐行明细）。

    用 ORM 逐条删除（而非 Core delete）：ORM 会按 `Asset` 上的关系/事件走，
    Core delete 会绕过它们。记录行自 0027 起不再挂任何关联表（旧 EAV 绑定表已
    物理删除），删除只影响 assets 与本行的审计留痕。

    留痕形状（ADR-0027 D3/D6）：
    - 头行 `entity='batch', action='execute'`，`after={op:'delete', count, ids_hash, scope}`；
      批次理由（`reason`/`reason_note`）**头行与逐行明细同值**（spec E1 起：任何
      `actor='owner'` 的 delete 行都不允许无 reason，明细不能只靠头行解释）；
    - 明细行逐条 `entity='asset', action='delete'`，`before`＝关键字段、`after=NULL`，
      全批共用同一条 `reason`（spec E1 起：`actor='owner'` 的无理由 delete 行
      一律 422，明细也不能例外，否则「这批删了、为什么删」只有头行能答）；
    - 与删除**同一事务**：审计写失败 → 整批不删（ADR-0007 可信 > 便利）。

    单次上限 `BATCH_WRITE_LIMIT`（ADR-0027 D4）：超限 400 `BATCH_LIMIT_EXCEEDED`，
    不做无上限硬删——一次点错能清掉整本账正是要拦的。

    Args:
        db: 数据库会话
        record_ids: 要删除的记录ID列表
        scope: 头行 scope 补充字段（筛选条件/调用来源）

    Returns:
        删除的记录数量
    """
    if not record_ids:
        return 0

    unique_ids = list(dict.fromkeys(record_ids))
    _ensure_under_batch_limit(len(unique_ids))

    assets = db.execute(
        select(Asset).where(Asset.id.in_(unique_ids))
    ).scalars().all()
    _ensure_under_batch_limit(len(assets))

    if assets:
        # 头行先落（此时行数已确定），再逐行明细：读端按 id 升序即「批次 → 明细」。
        audit_service.record(db, batch_head_change(
            "delete", len(assets), [a.id for a in assets],
            scope={"via": "batch-delete", **(scope or {})},
            reason=reason, reason_note=reason_note))
        for asset in assets:
            audit_service.record(db, _change(
                "asset", asset.id, "delete",
                before=asset_key_fields(asset), after=None,
                evidence_ref=asset.source_evidence_id,
                # spec E1 收口后 R5「逐行不带」作废：审计里不允许存在 owner 的
                # 无理由 delete 行（否则整批删除的明细就是不可解释的离场证据）。
                # 全批共用同一条理由，与头行一致。
                reason=reason, reason_note=reason_note))

    for asset in assets:
        db.delete(asset)

    db.commit()
    return len(assets)


def get_asset_history_by_name(db: Session, asset_name: str) -> dict:
    """根据资产名称查询所有历史记录。

    v5 身份层：经「现名→历史名」解析到资产项后按 item_id 聚合（改名不断史，
    拍板 10）；解析不到（未迁移数据/测试直插行）回落 asset_name 字符串匹配（双轨兼容）。
    """
    item_id = resolve_item_id(db, asset_name)
    if item_id is not None:
        all_records = db.execute(
            select(Asset).where(Asset.asset_item_id == item_id)
        ).scalars().all()
        sample_q = (
            select(Asset)
            .where(Asset.asset_item_id == item_id)
            .order_by(Asset.asset_date.desc())
            .limit(3)
        )
    else:
        all_records = db.execute(
            select(Asset).where(Asset.asset_name == asset_name)
        ).scalars().all()
        sample_q = (
            select(Asset)
            .where(Asset.asset_name == asset_name)
            .order_by(Asset.asset_date.desc())
            .limit(3)
        )

    total_count = len(all_records)
    all_ids = [r.id for r in all_records]

    # 获取示例记录（最新的3条）
    sample_records = db.execute(sample_q).scalars().all()

    return {
        "total_count": total_count,
        "sample_records": list(sample_records),
        "all_ids": all_ids
    }


def batch_create_assets(
    db: Session,
    records: list[dict],
    conflict_strategy: str = "skip"
) -> tuple[list[Asset], list[dict]]:
    """批量创建资产——v5 写语义收敛（spec §4）。

    - 移除 overwrite 静默覆盖分支；收到 overwrite → ValueError（router 400）。
    - skip：冲突行进显式 skipped[] 清单返回，不再默默丢弃。
    - raise：抛 IntegrityError（router 409）。
    - 身份经 get_or_create_item 解析；name 快照＝写入时 display_name。
    - 冲突检测只看 active 行（superseded_by IS NULL）。

    留痕（ADR-0027 ①值桶）：实际创建成功的行写 1 条头行（`entity='batch'`）＋
    逐行 `entity='asset', action='create'` 明细，**与创建同事务**：中途抛错则整批
    回滚、审计一行不留（天然一致，无需补偿）。

    Returns:
        (创建的资产列表, skipped 冲突清单)
    """
    from sqlalchemy.exc import IntegrityError

    if conflict_strategy == "overwrite":
        raise ValueError("overwrite 策略已冻结移除（write-once）；请逐行走 correct 更正")

    assets: list[Asset] = []
    skipped: list[dict] = []
    for record in records:
        _reject_legacy_classification(
            ["attribute_ids"] if record.get("attribute_ids") else [],
            where="资产记录（批量创建）")
        side, amt = _normalize_side_amount(record["amount"], record.get("side", "asset"))
        item_id = record.get("asset_item_id")
        if item_id:
            item = db.get(AssetItem, int(item_id))
            if item is None:
                raise ValueError(f"asset_item_id 不存在: {item_id}")
        else:
            item = get_or_create_item(db, record["asset_name"])

        existing = _active_row(db, item.id, record["asset_date"])
        if existing:
            if conflict_strategy == "skip":
                skipped.append({
                    "asset_name": item.display_name,
                    "asset_date": record["asset_date"],
                    "existing_id": existing.id,
                    "reason": "duplicate_active",
                })
                continue
            elif conflict_strategy == "raise":
                raise IntegrityError(
                    f"Asset with name '{record['asset_name']}' on date "
                    f"'{record['asset_date']}' already exists",
                    params=None,
                    orig=None
                )

        # 创建新资产（分类在资产项上，不随行写）
        asset = Asset(
            asset_name=item.display_name,   # name 快照单点生成
            asset_item_id=item.id,
            amount=amt,
            asset_date=record["asset_date"],
            side=side,
            cost_basis=resolve_cost_basis(db, item.id, record["asset_date"]),  # 2.1 建行冗余
        )
        db.add(asset)
        db.flush()
        assets.append(asset)

    # 留痕（ADR-0027 ①值桶）：头行 + 逐行明细，与创建同事务（失败整批回滚）。
    if assets:
        audit_service.record(db, batch_head_change(
            "create", len(assets), [a.id for a in assets],
            scope={"via": "batch", "conflict_strategy": conflict_strategy,
                   "requested": len(records)}))
        for asset in assets:
            audit_service.record(db, _change(
                "asset", asset.id, "create",
                before=None, after=asset_key_fields(asset),
                evidence_ref=asset.source_evidence_id))

    db.commit()

    # 刷新所有资产以加载关系
    for asset in assets:
        db.refresh(asset)

    return assets, skipped


def get_assets_by_asset_names(db: Session, asset_names: list[str]) -> list[Asset]:
    """根据资产名称列表获取资产（经「现名→历史名」解析到资产项，兼容直插行）。"""
    if not asset_names:
        return []

    item_ids = [resolve_item_id(db, n) for n in asset_names]
    item_ids = [i for i in item_ids if i is not None]
    conds = [Asset.asset_name.in_(asset_names)]
    if item_ids:
        conds.append(Asset.asset_item_id.in_(item_ids))

    result = db.execute(
        select(Asset)
        .where(or_(*conds))
        .order_by(Asset.asset_date.desc(), Asset.asset_name)
    ).scalars().all()

    return list(result)
