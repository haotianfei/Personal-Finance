"""名称 → 资产项身份解析（单点，ADR-0026 / 票 04）。

背景：`assets.asset_name` 是写入时的显示名快照，按名回查在改名后必然漂移；
别名（`asset_item_names.origin='alias'`）与历史名又各自沉淀在名史里。若每个调用点
自己写一遍「名字怎么变回 id」，就会出现两处判定不一致——这正是票 02 实测的
「66 对近似命名里 92% 会被静默新建」的根因。

本模块只做一件事：**给一个名字，按固定优先级说清它属于哪个资产项**。

    现名（display_name） > 历史名（origin in migration/rename） > 别名（origin=alias）

- 归一化沿用 `services/name_norm.py` 的 `_cmp_norm`（与录入匹配同一真源）；
- 每一层内部先比原文精确、再比归一后相等；同一层命中多个 item → **歧义**，
  返回 `candidates` 而不是随便挑一个（调用方决定报错还是回退）；
- 默认排除 `archived`（归档项不该再被录入命中）；需要时显式 `include_archived=True`。

性能：`build_index` 一次装载全部项与名史（当前 133 项 / 146 行），批处理时复用；
单次 `resolve(db, name)` 是便捷入口，内部现建索引（小库足够）。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AssetItem, AssetItemName
from services.name_norm import _cmp_norm


@dataclass(frozen=True)
class NameResolution:
    """一次解析的结果。

    - `item` 有值且 `matched_by` 非空＝唯一命中；
    - `item is None` 且 `candidates` 非空＝歧义（同层多名所有者）；
    - 两者皆空＝不认识这个名字。
    """
    item: AssetItem | None = None
    matched_by: str | None = None       # display | history | alias
    candidates: tuple[int, ...] = ()


@dataclass
class NameIndex:
    """一次性装载的解析索引（批处理复用）。"""
    items: dict[int, AssetItem] = field(default_factory=dict)
    display: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    history: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    alias: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def level(self, name: str) -> list[tuple[str, list[int]]]:
        key = _cmp_norm(name)
        return [("display", self.display.get(key, [])),
                ("history", self.history.get(key, [])),
                ("alias", self.alias.get(key, []))]


def build_index(db: Session, *, include_archived: bool = True) -> NameIndex:
    """装载解析索引。`include_archived=False` 时归档项不进索引（录入侧用）。"""
    idx = NameIndex()
    for item in db.execute(select(AssetItem)).scalars():
        if not include_archived and item.status == "archived":
            continue
        idx.items[item.id] = item
        idx.display[_cmp_norm(item.display_name)].append(item.id)
    for row in db.execute(select(AssetItemName)).scalars():
        if row.item_id not in idx.items:
            continue
        bucket = idx.alias if row.origin == "alias" else idx.history
        bucket[_cmp_norm(row.name)].append(row.item_id)
    return idx


def resolve_with(index: NameIndex, name: str) -> NameResolution:
    """在已装载索引上解析（优先级：现名 > 历史名 > 别名）。"""
    raw = (name or "").strip()
    if not raw:
        return NameResolution()
    key = _cmp_norm(raw)

    # ① 现名原文精确优先（即便归一后与他人撞名，原文命中更可信）
    exact = [iid for iid in index.display.get(key, [])
             if index.items[iid].display_name == raw]
    if len(exact) == 1:
        return NameResolution(index.items[exact[0]], "display")
    if len(exact) > 1:
        return NameResolution(None, None, tuple(sorted(exact)))

    # ② 逐层归一后比对
    for level, ids in index.level(raw):
        uniq = sorted(set(ids))
        if len(uniq) == 1:
            return NameResolution(index.items[uniq[0]], level)
        if len(uniq) > 1:
            return NameResolution(None, None, tuple(uniq))
    return NameResolution()


def resolve(db: Session, name: str, *, include_archived: bool = True) -> NameResolution:
    """便捷入口：现建索引再解析（批处理请改用 build_index + resolve_with）。"""
    return resolve_with(build_index(db, include_archived=include_archived), name)


def conflicts_with(
        index: NameIndex, name: str, *, exclude_item_id: int,
) -> list[tuple[int, str]]:
    """目标名（归一后）已被哪些**其他**资产项占用。

    三层都查（现名/历史名/别名）：历史名与别名同样要能解析回项，若新名撞上别人的
    历史名，解析就会歧义——所以撞名判定必须覆盖三层，而不是只看 display_name。
    返回 [(item_id, level)]，空表＝无冲突。
    """
    raw = (name or "").strip()
    if not raw:
        return []
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for level, ids in index.level(raw):
        for iid in sorted(set(ids)):
            if iid == exclude_item_id:
                continue            # 自己的历史名/别名不算撞名（撤销改名走这条路）
            if (iid, level) in seen:
                continue
            seen.add((iid, level))
            out.append((iid, level))
    return out