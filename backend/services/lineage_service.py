"""血缘链接服务（v5 二期·子步 2.4，规格 .scratch/v5-appreciation/spec.md §2.4）。

`register_lineage` ＝ **用例编排（单事务语义，写序失败安全）**：
校验（全部前置，零写入）→ 新项创建（`get_or_create_item`，目标名必须未占用）→
链创建（每旧项一条）→ 口径承接（新项初始 `principal_changes` origin='lineage'、
reason 引链 id、值＝人确认预填可改）→ 审计（`lineage_link` ×n ＋伴生 `principal_set`
经单点）→ **旧项归档最后执行**（`set_item_status` 自带提交：前面任何失败＝零提交，
归档自身不再失败）。

- 视图边界：本步只建链/读链（前身/后继双向）；**跨项拼接合并收益不做**（非目标）。
- 改名不走血缘（拍板 10）：本服务无 rename 路径，显示名史已承载。
- 链不可变（拍板 8）：无 update/delete 端口；承接口径错误＝对新项追加更正（不改史）。
- 组合收益不受影响：origin='lineage' 不属外部流（returns 过滤 external/transfer，
  回归测试锁定）。
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from errors import ApiError, ErrorCode
from models import AssetItem, AssetItemName, LineageLink, PrincipalChange
from services import audit_service
from services.asset_service import get_or_create_item, set_item_status
from services.audit_service import make_change as _change

LINK_TYPES = ("merge", "split", "revision")
ZERO = Decimal("0")
ONE = Decimal("1")
RATIO_SUM_TOLERANCE = Decimal("0.0001")   # Σratio == 1 ± 1e-4（spec §1）


def _name_taken(db: Session, name: str) -> bool:
    """目标名是否已被任何资产项占用（镜像 get_or_create_item 解析序，只读）。"""
    hit = db.execute(
        select(AssetItem.id)
        .where(or_(AssetItem.display_name == name, AssetItem.legacy_name == name))
    ).first()
    if hit:
        return True
    return db.execute(
        select(AssetItemName.id).where(AssetItemName.name == name)
    ).first() is not None


def register_lineage(db: Session, from_item_ids: list[int], to_item_name: str,
                     link_type: str, split_ratio, principal,
                     reason: str | None, effective_date: date | None = None,
                     note: str | None = None) -> tuple[LineageLink, list[LineageLink], PrincipalChange]:
    """登记一条血缘（旧项组→新项）＋口径承接。返回（首链, 全部链, 承接行）。

    Raises:
        ApiError: link_type 非法／目标名占用／ratio 非法(400 BAD_REQUEST)；旧项
            不存在(404 NOT_FOUND)；旧项已归档(409 ITEM_ARCHIVED)；reason 缺失
            (400 PRINCIPAL_REASON_REQUIRED，R-9 同源复用)。
    """
    if link_type not in LINK_TYPES:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"link_type 仅支持 {'|'.join(LINK_TYPES)}",
                       details={"field": "link_type"})
    ids = list(dict.fromkeys(from_item_ids or []))
    if not ids:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "至少一个旧项（from_item_ids 为空）",
                       details={"field": "from_item_ids"})
    name = (to_item_name or "").strip()
    if not name:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "新项名称不能为空",
                       details={"field": "to_item_name"})

    olds: list[AssetItem] = []
    for iid in ids:
        item = db.get(AssetItem, iid)
        if item is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {iid}",
                           details={"field": "from_item_ids"})
        if item.status == "archived":
            raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                           f"已归档资产项不可作为血缘前身（{item.display_name}，R-37）")
        olds.append(item)
    if _name_taken(db, name):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"新项名称已被占用: {name}——血缘目标必须是新项（改名请走显示名史，拍板 10）",
                       details={"field": "to_item_name"})

    ratio: Decimal | None = None
    if link_type == "split":
        if split_ratio is None:
            raise ApiError(ErrorCode.BAD_REQUEST, 400, "split 必须携带 split_ratio",
                           details={"field": "split_ratio"})
        try:
            ratio = Decimal(str(split_ratio))
        except (InvalidOperation, ValueError, TypeError):
            raise ApiError(ErrorCode.BAD_REQUEST, 400, f"split_ratio 非法: {split_ratio!r}")
        if not (ZERO < ratio <= ONE):
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           "split_ratio 须落在 (0, 1]", details={"field": "split_ratio"})
    elif split_ratio is not None and str(split_ratio).strip() != "":
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"{link_type} 不应携带 split_ratio（仅 split）",
                       details={"field": "split_ratio"})

    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "血缘承接必须附理由（R-9：承接值是人确认的裁决）")
    try:
        amt = Decimal(str(principal)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"承接口径非法: {principal!r}",
                       details={"field": "principal"})
    eff = effective_date if isinstance(effective_date, date) else date.today()

    # ---- 全部校验通过后才开始写入；旧项归档放最后（见模块 docstring 失败安全序）
    target = get_or_create_item(db, name)          # 新项（_name_taken 已保证未占用）

    links: list[LineageLink] = []
    for it in olds:
        link = LineageLink(from_item_id=it.id, to_item_id=target.id,
                           link_type=link_type, split_ratio=ratio,
                           note=(note or "").strip() or None,
                           created_at=datetime.now().isoformat())
        db.add(link)
        links.append(link)
    db.flush()

    ids_txt = "、".join(f"#{lk.id}" for lk in links)
    row = PrincipalChange(
        asset_item_id=target.id, new_amount=amt, effective_date=eff,
        reason=f"血缘承接 {ids_txt}（{link_type}）",
        evidence_ref=None, origin="lineage", transfer_event_id=None,
        created_at=datetime.now().isoformat(),
    )
    db.add(row)
    db.flush()

    for lk, it in zip(links, olds):
        audit_service.record(db, _change(
            "lineage_link", lk.id, "lineage_link", before=None,
            after={"from_item_id": it.id, "from_name": it.display_name,
                   "to_item_id": target.id, "to_name": target.display_name,
                   "link_type": link_type,
                   "split_ratio": str(ratio) if ratio is not None else None}))
    audit_service.record(db, _change("principal_change", row.id, "principal_set",
                                     before=None, after=str(amt)))

    for it in olds:                                # 归档最后：此刻前无提交，失败＝零副作用
        # 归档自**人声明的生效日**起算（与 principal_changes.effective_date 同一天）：
        # 合并前后序列必须在此日无缝衔接，不得用 today 把前身的历史追溯抹掉。
        # 归档前身＝合并这一人裁决的**派生物**（人裁决本身已由上面的 lineage_link
        # 审计行承载理由），故以系统身份落行（spec E2 口径，audit-reason-enforce）。
        set_item_status(db, it.id, "archived", effective_from=eff,
                        actor="system", reason="normalization")

    db.commit()
    db.refresh(row)
    return links[0], links, row


def _target_field(target, key):
    """targets 元素兼容 dict 与 pydantic 模型（服务层可被直接调用）。"""
    if isinstance(target, dict):
        return target.get(key)
    return getattr(target, key, None)


def _parse_ratio(raw) -> Decimal:
    try:
        ratio = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"split_ratio 非法: {raw!r}",
                       details={"field": "targets"})
    if not ratio.is_finite():
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"split_ratio 非法: {raw!r}",
                       details={"field": "targets"})
    if not (ZERO < ratio <= ONE):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "split_ratio 须落在 (0, 1]", details={"field": "targets"})
    return ratio


def _parse_principal(raw) -> Decimal:
    try:
        amt = Decimal(str(raw)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"承接口径非法: {raw!r}",
                       details={"field": "targets"})
    if not amt.is_finite() or amt < ZERO:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "承接口径不可为负",
                       details={"field": "targets"})
    return amt


def item_dict(db: Session, item: AssetItem) -> dict:
    """资产项行形状（与 `/api/asset-items` 列表一致，追加归档生效日）。"""
    aliases = db.execute(
        select(AssetItemName.name).where(AssetItemName.item_id == item.id)
    ).scalars().all()
    return {
        "id": item.id,
        "display_name": item.display_name,
        "channel": item.channel,
        "status": item.status,
        "legacy_name": item.legacy_name,
        "aliases": sorted(set(aliases)),
        "archived_effective_from": (
            item.archived_effective_from.isoformat()
            if item.archived_effective_from else None),
    }


def register_split(db: Session, from_item_id: int, targets: list[dict],
                   effective_date: date | None = None,
                   reason: str | None = None) -> dict:
    """一对多血缘拆分：父项 → 多个子项（1→N），父项自生效日退出计量。

    校验全部前置（零写入）；写入序：子项 → 每子项 split 链 → 每子项
    `PrincipalChange(origin='lineage')` → 审计 → **最后**归档父项（自带 audit），
    与 `register_lineage` 的失败安全序一致。

    Args:
        targets: `[{name, ratio, principal, note?}, ...]`（route 已做模型绑定）。

    Raises:
        ApiError: targets < 2／ratio 越界或合计≠1／名称空/重复/被占用／principal
            非法或为负（400 BAD_REQUEST）；父项不存在（404 NOT_FOUND）；父项已归档
            （409 ITEM_ARCHIVED）；reason 缺失（400 PRINCIPAL_REASON_REQUIRED）。
    """
    # ---- 1) targets 数量
    if not targets or len(targets) < 2:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "拆分至少需要两个目标项（1→1 请用 revision）",
                       details={"field": "targets"})

    # ---- 2) ratio 区间 + 合计（先逐项，再合计；全精度比较）
    ratios: list[Decimal] = []
    for t in targets:
        ratios.append(_parse_ratio(_target_field(t, "ratio")))
    if abs(sum(ratios, ZERO) - ONE) > RATIO_SUM_TOLERANCE:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "拆分比例合计必须为 1（容差 1e-4）",
                       details={"field": "targets"})

    # ---- 3) name：去空白非空、targets 内唯一、未被任何资产项占用（含显示名史）
    names: list[str] = []
    for t in targets:
        name = str(_target_field(t, "name") or "").strip()
        if not name:
            raise ApiError(ErrorCode.BAD_REQUEST, 400, "目标项名称不能为空",
                           details={"field": "targets"})
        if name in names:
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           f"目标项名称在本次拆分中重复: {name}",
                           details={"field": "targets"})
        if _name_taken(db, name):
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           f"目标项名称已被占用: {name}——拆分目标必须是新项",
                           details={"field": "targets"})
        names.append(name)

    # ---- 4) principal：>= 0，两位小数
    amounts = [_parse_principal(_target_field(t, "principal")) for t in targets]

    # ---- 5) 父项存在且 active（父项 id 不可能成为 target：名称已被占用已在上一步拒绝）
    parent = db.get(AssetItem, from_item_id)
    if parent is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {from_item_id}",
                       details={"field": "from_item_id"})
    if parent.status == "archived":
        raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                       f"已归档资产项不可作为血缘前身（{parent.display_name}，R-37）",
                       details={"field": "from_item_id"})

    # ---- 6) reason 非空（R-9）
    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "血缘承接必须附理由（R-9：承接值是人确认的裁决）")
    clean_reason = str(reason).strip()
    eff = effective_date if isinstance(effective_date, date) else date.today()

    # ---- 全部校验通过后才开始写入
    try:
        children: list[AssetItem] = []
        for name in names:
            child = get_or_create_item(db, name)   # _name_taken 已保证未占用
            children.append(child)
        if any(c.id == parent.id for c in children):   # 防御：名称解析不应命中父项
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           "父项不可同时作为拆分目标",
                           details={"field": "targets"})

        links: list[LineageLink] = []
        for child, ratio, t in zip(children, ratios, targets):
            link = LineageLink(
                from_item_id=parent.id, to_item_id=child.id,
                link_type="split", split_ratio=ratio,
                note=(str(_target_field(t, "note") or "").strip() or None),
                created_at=datetime.now().isoformat(),
            )
            db.add(link)
            links.append(link)
        db.flush()

        entries: list[PrincipalChange] = []
        for child, link, amt in zip(children, links, amounts):
            row = PrincipalChange(
                asset_item_id=child.id, new_amount=amt, effective_date=eff,
                reason=f"血缘拆分承接 #{link.id}：{clean_reason}",
                evidence_ref=None, origin="lineage", transfer_event_id=None,
                created_at=datetime.now().isoformat(),
            )
            db.add(row)
            entries.append(row)
        db.flush()

        for link, child, ratio, amt in zip(links, children, ratios, amounts):
            audit_service.record(db, _change(
                "lineage_link", link.id, "lineage_link", before=None,
                after={"from_item_id": parent.id, "from_name": parent.display_name,
                       "to_item_id": child.id, "to_name": child.display_name,
                       "link_type": "split", "split_ratio": str(ratio)}))
        for row, amt in zip(entries, amounts):
            audit_service.record(db, _change(
                "principal_change", row.id, "principal_set", before=None,
                after=str(amt)))
        audit_service.record(db, _change(
            "lineage_split", parent.id, "create", before=None,
            after={"from_item_id": parent.id, "from_name": parent.display_name,
                   "children": [
                       {"asset_item_id": child.id, "name": child.display_name,
                        "ratio": str(ratio), "principal": str(amt)}
                       for child, ratio, amt in zip(children, ratios, amounts)],
                   "effective_date": eff.isoformat(), "reason": clean_reason}))

        # 归档最后执行（set_item_status 自带 audit＋commit）：此前无提交，失败＝零副作用
        set_item_status(db, parent.id, "archived", effective_from=eff,
                        actor="system", reason="normalization")
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "from_item": item_dict(db, parent),
        "links": [link_dict(db, lk) for lk in links],
        "entries": [{
            "asset_item_id": row.asset_item_id,
            "amount": str(row.new_amount),
            "effective_date": row.effective_date.isoformat(),
            "reason": row.reason,
            "origin": row.origin,
        } for row in entries],
        "archived_effective_from": eff.isoformat(),
    }


def _existing_links(db: Session) -> list[LineageLink]:
    return db.execute(select(LineageLink).order_by(LineageLink.id)).scalars().all()


def _reachable(start: int, adjacency: dict[int, set[int]]) -> set[int]:
    """从 start 出发沿 from→to 方向可达的项 id（不含 start，除非成环回访）。"""
    seen: set[int] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in adjacency.get(cur, ()):  # 有界：seen 去重
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def attach_links(db: Session, from_item_ids: list[int], to_item_ids: list[int],
                 link_type: str, ratios, effective_date: date | None,
                 reason: str | None) -> dict:
    """既有项之间补登记血缘（`.scratch/lineage-attach/spec.md` §1.1）。

    与 `register_lineage` 的区别：后继项**已存在**，不新建项、不写
    `principal_changes`（历史承接值已由后继项自身记录承载，重复写会污染
    本金口径）。校验全部前置、零写入；写入序：链 → 审计（逐链＋汇总）→
    **最后**逐个归档前身（`set_item_status(..., effective_from=eff)`）。
    """
    # ---- 1) 类型与形状 -------------------------------------------------
    if link_type not in LINK_TYPES:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"link_type 仅支持 {'|'.join(LINK_TYPES)}",
                       details={"field": "link_type"})
    from_ids = list(dict.fromkeys(from_item_ids or []))
    to_ids = list(dict.fromkeys(to_item_ids or []))
    if not from_ids:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "至少一个前身项（from_item_ids 为空）",
                       details={"field": "from_item_ids"})
    if not to_ids:
        raise ApiError(ErrorCode.BAD_REQUEST, 400, "至少一个后继项（to_item_ids 为空）",
                       details={"field": "to_item_ids"})
    if len(from_ids) > 1 and len(to_ids) > 1:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "多前身与多后继同时出现（语义歧义：merge 或 split 二选一）",
                       details={"field": "to_item_ids"})
    if link_type == "revision" and not (len(from_ids) == 1 and len(to_ids) == 1):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "revision 必须是 1:1", details={"field": "to_item_ids"})
    if link_type == "merge" and len(to_ids) != 1:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "merge 必须只有一个后继项", details={"field": "to_item_ids"})
    if link_type == "split" and len(from_ids) != 1:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "split 必须只有一个前身项", details={"field": "from_item_ids"})

    # ---- 2) id 存在性（前身/后继） -------------------------------------
    from_items: list[AssetItem] = []
    for iid in from_ids:
        item = db.get(AssetItem, iid)
        if item is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {iid}",
                           details={"field": "from_item_ids"})
        from_items.append(item)
    to_items: list[AssetItem] = []
    for iid in to_ids:
        item = db.get(AssetItem, iid)
        if item is None:
            raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {iid}",
                           details={"field": "to_item_ids"})
        to_items.append(item)

    # ---- 3) 两侧不得相交 ------------------------------------------------
    overlap = set(from_ids) & set(to_ids)
    if overlap:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       f"前身与后继不得相交: {sorted(overlap)}",
                       details={"field": "to_item_ids"})

    # ---- 4) 前身必须 active --------------------------------------------
    for item in from_items:
        if item.status == "archived":
            raise ApiError(ErrorCode.ITEM_ARCHIVED, 409,
                           f"已归档资产项不可作为血缘前身（{item.display_name}，R-37）",
                           details={"field": "from_item_ids"})

    # ---- 5) 成环 / 重复对（沿已有链做传递闭包） -------------------------
    existing = _existing_links(db)
    adjacency: dict[int, set[int]] = {}
    existing_pairs: set[tuple[int, int]] = set()
    for lk in existing:
        adjacency.setdefault(lk.from_item_id, set()).add(lk.to_item_id)
        existing_pairs.add((lk.from_item_id, lk.to_item_id))

    for f in from_ids:
        for t in to_ids:
            if f in _reachable(t, adjacency):
                raise ApiError(ErrorCode.LINEAGE_CYCLE, 400,
                               f"补登记 {f}→{t} 会形成血缘环（{t} 已能到达 {f}）",
                               details={"field": "to_item_ids"})
    for f in from_ids:
        for t in to_ids:
            if (f, t) in existing_pairs:
                raise ApiError(ErrorCode.LINEAGE_EXISTS, 409,
                               f"血缘关系已存在: {f}→{t}",
                               details={"field": "to_item_ids"})

    # ---- 6) 生效日 / 理由（补登记必须显式声明，不复用 today 缺省） -------
    if not isinstance(effective_date, date):
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "effective_date 必填（补登记必须显式声明切换日）",
                       details={"field": "effective_date"})
    if not reason or not str(reason).strip():
        raise ApiError(ErrorCode.PRINCIPAL_REASON_REQUIRED, 400,
                       "血缘补登记必须附理由（R-9）")
    clean_reason = str(reason).strip()

    # ---- 7) 比例：仅 split 且多后继时必需 ------------------------------
    ratio_by_to: dict[int, Decimal | None] = {t: None for t in to_ids}
    raw_ratios = [r for r in (ratios or []) if str(r).strip() != ""]
    if link_type == "split" and len(to_ids) > 1:
        if len(raw_ratios) != len(to_ids):
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           f"split 需为每个后继提供比例（期望 {len(to_ids)} 个）",
                           details={"field": "ratios"})
        parsed: list[Decimal] = []
        for raw in raw_ratios:
            try:
                ratio = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                raise ApiError(ErrorCode.BAD_REQUEST, 400, f"比例非法: {raw!r}",
                               details={"field": "ratios"})
            if not ratio.is_finite() or not (ZERO < ratio <= ONE):
                raise ApiError(ErrorCode.BAD_REQUEST, 400,
                               "比例须落在 (0, 1]", details={"field": "ratios"})
            parsed.append(ratio)
        if abs(sum(parsed, ZERO) - ONE) > RATIO_SUM_TOLERANCE:
            raise ApiError(ErrorCode.BAD_REQUEST, 400,
                           "拆分比例合计必须为 1（容差 1e-4）",
                           details={"field": "ratios"})
        for t, ratio in zip(to_ids, parsed):
            ratio_by_to[t] = ratio
    elif raw_ratios:
        raise ApiError(ErrorCode.BAD_REQUEST, 400,
                       "ratios 仅适用于 split 且后继多于一个",
                       details={"field": "ratios"})

    # ---- 全部校验通过后才开始写入 --------------------------------------
    created_at = datetime.now().isoformat()
    try:
        links: list[LineageLink] = []
        pairs: list[tuple[LineageLink, AssetItem, AssetItem]] = []
        for f in from_items:
            for t in to_items:
                link = LineageLink(
                    from_item_id=f.id, to_item_id=t.id, link_type=link_type,
                    split_ratio=ratio_by_to.get(t.id), note=clean_reason,
                    created_at=created_at,
                )
                db.add(link)
                links.append(link)
                pairs.append((link, f, t))
        db.flush()

        for link, f, t in pairs:
            audit_service.record(db, _change(
                "lineage_link", link.id, "lineage_link", before=None,
                after={"from_item_id": f.id, "from_name": f.display_name,
                       "to_item_id": t.id, "to_name": t.display_name,
                       "link_type": link_type,
                       "split_ratio": (str(link.split_ratio)
                                       if link.split_ratio is not None else None)}))
        audit_service.record(db, _change(
            "lineage_attach", from_items[0].id, "create", before=None,
            after={"from_item_ids": from_ids,
                   "to_item_ids": to_ids,
                   "link_type": link_type,
                   "effective_date": effective_date.isoformat(),
                   "reason": clean_reason,
                   "link_ids": [link.id for link in links]}))

        # 归档最后执行（set_item_status 自带 audit＋commit）：此前无提交，
        # 失败＝零副作用；与 register_lineage 失败安全序一致。
        for item in from_items:
            set_item_status(db, item.id, "archived", effective_from=effective_date,
                            actor="system", reason="normalization")
        db.commit()
    except Exception:
        db.rollback()
        raise

    for item in from_items:
        db.refresh(item)
    return {
        "links": [link_dict(db, link) for link in links],
        "from_items": [item_dict(db, item) for item in from_items],
        "to_items": [item_dict(db, item) for item in to_items],
        "archived_effective_from": effective_date.isoformat(),
        "principal_written": False,
    }


def family_item_ids(db: Session, item_id: int) -> list[int]:
    """以 item_id 为起点在 lineage_links 上做双向传递闭包（含自身、防环）。"""
    links = _existing_links(db)
    forward: dict[int, set[int]] = {}
    backward: dict[int, set[int]] = {}
    for lk in links:
        forward.setdefault(lk.from_item_id, set()).add(lk.to_item_id)
        backward.setdefault(lk.to_item_id, set()).add(lk.from_item_id)

    visited = {item_id}
    stack = [item_id]
    while stack:
        cur = stack.pop()
        for nxt in list(forward.get(cur, ())) + list(backward.get(cur, ())):
            if nxt not in visited:
                visited.add(nxt)
                stack.append(nxt)
    return sorted(visited)


def get_family(db: Session, item_id: int) -> dict:
    """身份族视图（spec §1.2）：成员关系＋链清单。"""
    root = db.get(AssetItem, item_id)
    if root is None:
        raise ApiError(ErrorCode.NOT_FOUND, 404, f"资产项不存在: {item_id}",
                       details={"field": "item_id"})

    links = _existing_links(db)
    forward: dict[int, list[LineageLink]] = {}
    backward: dict[int, list[LineageLink]] = {}
    for lk in links:
        forward.setdefault(lk.from_item_id, []).append(lk)
        backward.setdefault(lk.to_item_id, []).append(lk)

    # 前身：沿 to→from 反向可达；后继：沿 from→to 正向可达。
    relation: dict[int, tuple[str, str | None]] = {item_id: ("self", None)}
    visited = {item_id}

    stack = [item_id]
    while stack:                                   # 前身
        cur = stack.pop()
        for lk in sorted(backward.get(cur, []), key=lambda x: x.from_item_id):
            nxt = lk.from_item_id
            if nxt not in visited:
                visited.add(nxt)
                relation[nxt] = ("predecessor", lk.link_type)
                stack.append(nxt)

    stack = [item_id]
    while stack:                                   # 后继
        cur = stack.pop()
        for lk in sorted(forward.get(cur, []), key=lambda x: x.to_item_id):
            nxt = lk.to_item_id
            if nxt not in visited:
                visited.add(nxt)
                relation[nxt] = ("successor", lk.link_type)
                stack.append(nxt)

    items = {it.id: it for it in db.execute(
        select(AssetItem).where(AssetItem.id.in_(visited))).scalars().all()}
    ordered = [item_id] + sorted(i for i in visited if i != item_id)
    members = []
    for iid in ordered:
        item = items.get(iid)
        if item is None:
            continue
        rel, link_type = relation.get(iid, ("self", None))
        members.append({
            "item_id": item.id,
            "name": item.display_name,
            "relationship": rel,
            "link_type": link_type,
            "effective_from": (item.archived_effective_from.isoformat()
                               if item.archived_effective_from else None),
            "status": item.status,
        })

    family_links = [lk for lk in links
                    if lk.from_item_id in visited and lk.to_item_id in visited]
    return {
        "item_id": item_id,
        "members": members,
        "links": [link_dict(db, lk) for lk in family_links],
    }


def link_dict(db: Session, lk: LineageLink) -> dict:
    src = db.get(AssetItem, lk.from_item_id)
    dst = db.get(AssetItem, lk.to_item_id)
    return {
        "id": lk.id,
        "from_item_id": lk.from_item_id,
        "from_name": src.display_name if src else str(lk.from_item_id),
        "to_item_id": lk.to_item_id,
        "to_name": dst.display_name if dst else str(lk.to_item_id),
        "link_type": lk.link_type,
        "split_ratio": str(lk.split_ratio) if lk.split_ratio is not None else None,
        "note": lk.note,
        "created_at": lk.created_at,
    }


def list_lineage(db: Session, item_id: int | None = None) -> dict:
    """链查询（只读）：给 item_id ＝前身（to 侧命中）/后继（from 侧命中）双向；
    不给＝全部链（创建序）。跨项拼接收益不在本步（视图边界）。"""
    if item_id is not None:
        pred = db.execute(
            select(LineageLink).where(LineageLink.to_item_id == item_id)
            .order_by(LineageLink.id)).scalars().all()
        succ = db.execute(
            select(LineageLink).where(LineageLink.from_item_id == item_id)
            .order_by(LineageLink.id)).scalars().all()
        return {
            "item_id": item_id,
            "predecessors": [link_dict(db, lk) for lk in pred],
            "successors": [link_dict(db, lk) for lk in succ],
        }
    rows = db.execute(select(LineageLink).order_by(LineageLink.id)).scalars().all()
    return {"items": [link_dict(db, lk) for lk in rows]}
