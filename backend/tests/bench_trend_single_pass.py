"""趋势取数计时（day 粒度）：改前「逐期 as-of 查询」vs 改后「单次扫描」。

用法（仓库根）：

    .venv/bin/python3 backend/tests/bench_trend_single_pass.py               # 真库副本计时
    .venv/bin/python3 backend/tests/bench_trend_single_pass.py --reps 3
    .venv/bin/python3 backend/tests/bench_trend_single_pass.py --equivalence # 五档对账（A9）
    .venv/bin/python3 backend/tests/bench_trend_single_pass.py --synthetic   # 合成 730 期库
    .venv/bin/python3 backend/tests/bench_trend_single_pass.py --synthetic \
        --dates 730 --items 133 --reps 3 --old-reps -1                      # 极端规模（只测新）

取数方式说明：

- **真库只读**：用 `sqlite3` 的 backup API 复制到临时目录后再打开副本（不直接
  `cp` 运行库——WAL 存在时 `cp` 可能抽到半写状态，backup API 走已提交快照）。
- **合成数据**：当前库只有数十个快照日，跑不到 730 期；`--synthetic` 造
  「每日一个快照日 × N 个资产项」的合成库（虚构名称/整数金额），用来验证
  耗时不再随账期线性增长。
- 输出**只有**账期数、行数与毫秒数：不含任何真实财务数据、主机名或路径。

旧实现用 `tests/test_trend_single_pass.py` 里的参考副本（改前逐期循环），
保证两边比较的是同一份「旧口径」代码。
"""
import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models  # noqa: E402,F401  —— 在 Base.metadata 上注册全部表
from database import Base  # noqa: E402
from models import Asset, AssetItem  # noqa: E402
from services.analysis_service import get_total_trend  # noqa: E402
from test_trend_single_pass import diff_points, reference_trend  # noqa: E402  —— 改前实现副本

WINDOWS = (30, 90, 730)
PERIOD_TYPES = ("day", "week", "month", "quarter", "year")


# ---------------------------------------------------------------- 数据库准备


def copy_db_via_backup(src: Path, dst: Path) -> None:
    """sqlite3 backup API 复制（只读打开源库，不 cp 运行库）。"""
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(dst)
    try:
        with dst_con:
            src_con.backup(dst_con)
    finally:
        src_con.close()
        dst_con.close()


def build_synthetic(path: Path, n_dates: int = 730, n_items: int = 40) -> None:
    """合成库：每日一个快照日 × n_items 个资产项（虚构名称与整数金额）。"""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    start = date(2024, 1, 1)
    items = [{
        "id": i + 1,
        "display_name": f"示例-{i + 1:02d}",
        "status": "archived" if i == n_items - 1 else "active",
        "archived_effective_from": (start + timedelta(days=n_dates // 2)
                                    if i == n_items - 1 else None),
        "asset_class_id": 1, "liquidity_days": 0,
        "institution_id": 1, "owner_id": 1,
    } for i in range(n_items)]
    rows = [{
        "asset_name": f"示例-{i + 1:02d}",
        "asset_item_id": i + 1,
        "amount": Decimal(1000 + i * 10 + d),
        "asset_date": start + timedelta(days=d),
        "side": "liability" if i % 10 == 9 else "asset",
    } for d in range(n_dates) for i in range(n_items)]
    with engine.begin() as conn:
        conn.execute(AssetItem.__table__.insert(), items)
        for i in range(0, len(rows), 5000):
            conn.execute(Asset.__table__.insert(), rows[i:i + 5000])
    engine.dispose()


def open_session(path: Path):
    engine = create_engine(f"sqlite:///{path}")
    return engine, sessionmaker(bind=engine)()


# ---------------------------------------------------------------- 计时


def timeit(fn, reps: int) -> tuple[float, float]:
    """返回 (首次耗时 ms, 重复中位数 ms)；`reps<=0` 表示不测（返回 NaN）。"""
    timings = []
    for _ in range(max(reps, 0)):
        t0 = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - t0) * 1000)
    if not timings:
        return float("nan"), float("nan")
    return timings[0], statistics.median(timings)


def measure(db, period_type: str, date_from: date | None, reps: int,
            old_reps: int) -> dict:
    new_first, new_med = timeit(
        lambda: get_total_trend(db, period_type, date_from=date_from), reps)
    old_first, old_med = timeit(
        lambda: reference_trend(db, period_type, date_from=date_from), old_reps)
    return {"new_first": new_first, "new_med": new_med,
            "old_first": old_first, "old_med": old_med}


def metric_rows(db, label_prefix: str, reps: int, old_reps: int,
                max_date: date | None = None):
    rows = []
    base = max_date or db.execute(
        select(Asset.asset_date).where(Asset.superseded_by.is_(None))
        .order_by(Asset.asset_date.desc()).limit(1)).scalar()
    if base is None:
        return rows
    for days in WINDOWS:
        date_from = base - timedelta(days=days - 1)
        n_periods = len(get_total_trend(db, "day", date_from=date_from))
        m = measure(db, "day", date_from, reps, old_reps)
        rows.append({
            "window": f"{label_prefix}近 {days} 天",
            "periods": n_periods,
            **m,
            "speedup": (m["old_med"] / m["new_med"]) if m["new_med"] else float("inf"),
        })
    return rows


def render(rows) -> str:
    head = ("| 档位（day 粒度） | 账期数 | 旧·逐期 as-of 首次/中位 (ms) "
            "| 新·单次扫描 首次/中位 (ms) | 中位提速 |")
    sep = "|---|---|---|---|---|"
    body = [
        f"| {r['window']} | {r['periods']} "
        f"| {r['old_first']:.0f} / {r['old_med']:.0f} "
        f"| {r['new_first']:.0f} / {r['new_med']:.0f} "
        f"| {r['speedup']:.1f}× |"
        for r in rows
    ]
    return "\n".join([head, sep, *body])


def equivalence_rows(db) -> list[dict]:
    """五档粒度旧新逐点逐字段对账（差异行数应为 0）。"""
    rows = []
    for period_type in PERIOD_TYPES:
        new, old, diffs = diff_points(db, period_type)
        rows.append({
            "period_type": period_type,
            "points": len(new),
            "old_points": len(old),
            "diffs": len(diffs),
            "first": diffs[0] if diffs else "—",
        })
    return rows


def render_equivalence(rows) -> str:
    head = "| 粒度 | 新实现点数 | 旧实现点数 | 差异行数 | 首个差异 |"
    sep = "|---|---|---|---|---|"
    body = [f"| {r['period_type']} | {r['points']} | {r['old_points']} "
            f"| {r['diffs']} | {r['first']} |" for r in rows]
    return "\n".join([head, sep, *body])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "person_fin.db"))
    parser.add_argument("--reps", type=int, default=3,
                        help="新实现重复次数（中位数）")
    parser.add_argument("--old-reps", type=int, default=0,
                        help="旧实现重复次数（默认同 --reps；大库可设 1 省时，"
                             "0/负 表示不测旧实现）")
    parser.add_argument("--synthetic", action="store_true",
                        help="用合成 730 期库计时（不读真库）")
    parser.add_argument("--dates", type=int, default=730,
                        help="合成库快照日数（默认 730）")
    parser.add_argument("--items", type=int, default=40,
                        help="合成库资产项数（默认 40，≈真库每月快照的日均行数）")
    parser.add_argument("--equivalence", action="store_true",
                        help="只跑五档粒度旧新对账（差异行数应为 0）")
    args = parser.parse_args()
    old_reps = args.old_reps or args.reps

    with tempfile.TemporaryDirectory() as tmp:
        if args.synthetic:
            db_path = Path(tmp) / "synthetic.db"
            build_synthetic(db_path, n_dates=args.dates, n_items=args.items)
            label = "[合成库] "
        else:
            db_path = Path(tmp) / "snapshot.db"
            copy_db_via_backup(Path(args.db), db_path)
            label = "[真库副本] "

        engine, db = open_session(db_path)
        try:
            active_rows = db.execute(
                select(Asset.id).where(Asset.superseded_by.is_(None))).all()
            dates = db.execute(
                select(Asset.asset_date).where(Asset.superseded_by.is_(None))
                .distinct()).all()
            # 预热：先把资产行读进页缓存（否则第一个档位背冷启动成本，各档不可比）。
            # 一次完整 day 请求即可覆盖连接上的两张表（assets / asset_items）。
            get_total_trend(db, "day")
            print(f"数据形状（{label.strip()}）：active 行 {len(active_rows)}、"
                  f"快照日 {len(dates)}、资产项 "
                  f"{db.execute(select(AssetItem.id)).all().__len__()}")
            if args.equivalence:
                print()
                print(render_equivalence(equivalence_rows(db)))
                return 0
            rows = metric_rows(db, label, args.reps, old_reps)
            print()
            print(render(rows))
        finally:
            db.close()
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())