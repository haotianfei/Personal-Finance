"""ReadModel 的 DB 实现：**只调用既有读函数**（ADR-0013 D3）。

依赖白名单：`analysis_service` / `appreciation_service` / `goal_service` /
`allocation_service` / `signal_service` / `metric_service` / `meta_service`。
不 import 任何账本写服务（asset/principal/transfer/lineage/screenshot）。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from services import (
    allocation_service, analysis_service, appreciation_service, band_service,
    goal_service, meta_service, metric_service,
)

# 配置维度（ADR-0019 新词汇）：与 `allocation_service.SUPPORTED_DIMENSIONS` 同源，
# 旧名 fund_type / liquidity_rating / account 已随 EAV 表下线（服务层会 422）。
DIMENSIONS = allocation_service.SUPPORTED_DIMENSIONS
ZERO = Decimal("0")


class DbReadModel:
    """只读视图（同一 Session；不提交、不写）。"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------------------------------------------------- 锚点
    def anchor(self, as_of: date | None = None) -> date | None:
        dates = analysis_service._get_all_snapshot_dates(self.db)
        if as_of is not None:
            return as_of
        return dates[-1] if dates else None

    def schema_generation(self) -> int:
        return meta_service.SCHEMA_GENERATION

    # ---------------------------------------------------------- 数值
    def net_worth(self, as_of=None) -> dict:
        anchor = self.anchor(as_of)
        if anchor is None:
            return {"as_of": None, "total": None, "assets": None,
                    "liabilities": None, "change_pct": None}
        total, assets, liabilities = analysis_service._sum_as_of(self.db, anchor)
        change_pct = None
        dates = analysis_service._get_all_snapshot_dates(self.db)
        prior = [d for d in dates if d < anchor]
        if prior:
            prev_total, _, _ = analysis_service._sum_as_of(self.db, prior[-1])
            if prev_total != ZERO:
                change_pct = (total - prev_total) / prev_total
        return {"as_of": anchor, "total": total, "assets": assets,
                "liabilities": liabilities, "change_pct": change_pct}

    def appreciation(self, as_of=None) -> dict:
        anchor = self.anchor(as_of)
        if anchor is None:
            return {"total": None, "pct": None, "items_missing_principal": 0}
        portfolio = appreciation_service.get_appreciation(self.db, anchor)["portfolio"]
        return {
            "total": Decimal(portfolio["appreciation_total"]),
            "pct": (Decimal(portfolio["rate"])
                    if portfolio["rate"] is not None else None),
            "items_missing_principal": len(portfolio["items_missing_principal"]),
        }

    def returns(self, as_of=None) -> dict:
        anchor = self.anchor(as_of)
        if anchor is None:
            return {"twr": None, "xirr": None}
        payload = analysis_service.get_returns(self.db, date_to=anchor)
        twr = payload["twr"]["period"]["period_return"]
        xirr = payload["xirr"]
        return {"twr": Decimal(twr) if twr is not None else None,
                "xirr": Decimal(xirr) if xirr is not None else None}

    # ---------------------------------------------------------- 目标
    def goal_progress(self, as_of=None, goal_id=None) -> dict | None:
        anchor = self.anchor(as_of)
        if goal_id is not None:
            goal = goal_service._load_goal(self.db, goal_id)
            versions = goal_service._versions(self.db, goal.id)
            version = goal_service._active_version(versions)
            if version is None:
                return None
        else:
            target = goal_service.resolve_target(self.db, anchor)
            if target is None:
                return None
            goal, version = target
        payload = goal_service.progress_payload(
            self.db, goal, version, anchor)
        grade = payload.get("grade") or {}
        return {
            "goal_id": goal.id,
            "goal_version_id": version.id,
            "version_no": version.version_no,
            "target_amount": Decimal(payload["target_amount"]),
            "target_rate_annual": (Decimal(payload["target_rate_annual"])
                                   if payload["target_rate_annual"] else None),
            "progress": (Decimal(payload["progress"])
                         if payload["progress"] is not None else None),
            "gap": Decimal(payload["gap"]),
            "required_annualized": (Decimal(payload["required_annualized"])
                                    if payload["required_annualized"] is not None
                                    else None),
            "remaining_months": payload["remaining_months"],
            "grade_state": grade.get("state"),
        }

    # ---------------------------------------------------------- 目标 / 复盘 as-of
    def goal_version_as_of(self, goal_id: int, as_of: date) -> dict | None:
        """as-of 目标版本＋锚点净值/达成度/判灯（不复用 active-only resolve）。"""
        target = goal_service.resolve_version_as_of(self.db, goal_id, as_of)
        if target is None:
            return None
        goal, version = target
        dates = analysis_service._get_all_snapshot_dates(self.db)
        anchor = max((d for d in dates if d <= as_of), default=None)
        net_worth = (analysis_service._sum_as_of(self.db, anchor)[0]
                     if anchor else ZERO)
        calc_date = anchor or as_of
        values = goal_service.compute_progress(net_worth, version, calc_date)
        band = band_service.resolve_goal_band(version)
        grade = (band_service.grade(values["required_annualized"], band, [],
                                    one_sided=True, basis="as-of")
                 if band is not None else None)
        return {
            "goal_id": goal.id,
            "goal_version_id": version.id,
            "version_no": version.version_no,
            "target_amount": Decimal(str(version.target_amount)),
            "target_date": version.target_date,
            "target_rate_annual": (Decimal(str(version.target_rate_annual))
                                   if version.target_rate_annual is not None
                                   else None),
            "risk_profile": version.risk_profile,
            "life_stage": version.life_stage,
            "effective_from": version.effective_from,
            "reason": version.reason,
            "criterion_json": version.criterion_json,
            "anchor_date": anchor,
            "net_worth": net_worth,
            "progress": values["progress"],
            "gap": values["gap"],
            "required_annualized": values["required_annualized"],
            "remaining_months": values["remaining_months"],
            "grade": grade,
        }

    def allocation_rows_as_of(self, as_of: date) -> dict:
        """as-of 配置行：actual 取锚点分析，target/band 取 revision。"""
        anchor = self.anchor(as_of)
        if anchor is None:
            return {"rows": [], "skipped_deleted_targets": 0,
                    "history_from": None, "total_assets": None}
        dates = analysis_service._get_all_snapshot_dates(self.db)
        anchor = max((d for d in dates if d <= as_of), default=None)
        if anchor is None:
            return {"rows": [], "skipped_deleted_targets": 0,
                    "history_from": None, "total_assets": None}

        total_assets = analysis_service._sum_as_of(self.db, anchor)[1]
        actual: dict[tuple, dict] = {}
        for dimension in DIMENSIONS:
            analysis = allocation_service.analyze_allocation(
                self.db, dimension, anchor)
            for item in _walk(analysis.items):
                if item.target_percent is None:
                    continue
                actual[(dimension, str(item.id))] = {
                    "actual": Decimal(item.actual_percent),
                    "name": item.name,
                }

        rows = []
        history_dates = []
        current_ids = set()
        for target in allocation_service.get_allocation_targets(self.db):
            current_ids.add(target.id)
            created = target.created_at
            if created is not None and hasattr(created, "date"):
                if created.date() > as_of:
                    continue
            revision = band_service.resolve_revision_as_of(
                self.db, target.id, as_of)
            # 存量行可能落着同义拼写（`risk`），而 `actual` 的键是主名（`risk_level`）；
            # 只折拼写、不校验值域——读历史行不该因拼写新老而丢失实际比例。
            key = (_canonical_dimension(target.dimension), str(target.target_id))
            actual_row = actual.get(key)
            band = band_service.resolve_band(revision) if revision else None
            target_percent = (Decimal(str(revision.target_percent))
                              if revision is not None else None)
            actual_percent = (actual_row["actual"].quantize(Decimal("0.01"))
                              if actual_row else None)
            deviation = None
            state = None
            if band is not None:
                history_dates.append(revision.effective_date)
                if actual_percent is not None and total_assets > ZERO:
                    deviation = (actual_percent - target_percent).quantize(
                        Decimal("0.01"))
                    state = band_service.evaluate(deviation, band)
            rows.append({
                "dimension": target.dimension,
                "target_id": str(target.target_id),
                "target_ref": (actual_row["name"] if actual_row
                               else target.target_id),
                "target_percent": (str(target_percent.quantize(Decimal("0.01")))
                                   if target_percent is not None else None),
                "actual_percent": (str(actual_percent)
                                   if actual_percent is not None else None),
                "deviation_pp": (str(deviation)
                                 if deviation is not None else None),
                "revision_id": revision.id if revision is not None else None,
                "band": _band_summary(band),
                "state": state,
                "history": "revision" if revision is not None else "missing",
            })

        from models import AllocationTargetRevision
        from sqlalchemy import select as _select
        deleted = [r[0] for r in self.db.execute(
            _select(AllocationTargetRevision.allocation_target_id)
            .where(AllocationTargetRevision.op == "delete",
                   AllocationTargetRevision.effective_date <= as_of)
            .distinct())]
        skipped = len([i for i in deleted if i not in current_ids])
        return {
            "rows": rows,
            "skipped_deleted_targets": skipped,
            "history_from": (min(history_dates).isoformat()
                             if history_dates else None),
            "total_assets": total_assets,
        }

    # ---------------------------------------------------------- 配置 / 信号
    def allocation_rows(self, as_of=None) -> list[dict]:
        anchor = self.anchor(as_of)
        if anchor is None:
            return []
        rows: list[dict] = []
        for dimension in DIMENSIONS:
            analysis = allocation_service.analyze_allocation(
                self.db, dimension, anchor)
            for item in _walk(analysis.items):
                if item.target_percent is None:
                    continue
                band = item.band
                rows.append({
                    "dimension": dimension,
                    "target_id": str(item.id),
                    "name": item.name,
                    "target_pct": Decimal(item.target_percent),
                    "actual_pct": Decimal(item.actual_percent),
                    "deviation_pp": Decimal(item.deviation),
                    "deviation_amount": (Decimal(item.deviation_amount)
                                         if item.deviation_amount is not None
                                         else None),
                    "band": band.state if band is not None else None,
                    "source": band.source if band is not None else None,
                    "recommendation": item.recommendation,
                })
        return rows

    def signals(self, as_of=None) -> dict:
        anchor = self.anchor(as_of)
        status = metric_service.get_status(self.db, anchor, include_details=False)
        return {
            "domains": [{"domain": d.domain, "state": d.state}
                        for d in status.domains],
            "events": [{"rule_id": e.rule_id, "period": e.period,
                        "severity": e.severity, "status": e.status,
                        "domain": e.domain} for e in status.events],
        }


def _walk(items):
    for item in items:
        yield item
        yield from _walk(item.children)


def _canonical_dimension(dimension: str | None) -> str:
    """库内维度拼写 → 主名（`risk` → `risk_level`）。

    `allocation_service.DIMENSION_ALIASES` 是拼写的单点定义处：这里只折同义名，
    不做值域校验（旧值 422 属于写路径；读历史行不该因为拼写新老而整条失败）。
    """
    name = (dimension or "").strip()
    return allocation_service.DIMENSION_ALIASES.get(name, name)


def _band_summary(band: dict | None) -> dict | None:
    if band is None:
        return None
    return {"kind": band["kind"], "threshold": band["threshold"],
            "red": band.get("red"), "source": band["source"]}