"""TWR/XIRR 双口径收益计算（v5 二期·子步 2.2，规格 .scratch/v5-appreciation/spec.md §2.2）。

**valuation 纯函数模块：零 I/O**——不 import sqlalchemy/db；输入全部为调用方
准备好的 `(date, Decimal)` 估值序列与带符号现金流，输出计算结果＋降级原因。
数值边界一律诚实降级（rate=None＋reason），绝不编数。

契约：
- TWR：**重估日（快照日）切段**，段内现金流按 Modified Dietz 权重 w＝(e−d)/(e−s)
  逐段计＋几何链接；年化＝(1+R)^(365/天数)−1。现金流＝外部口径增量（Δ累计投入
  本金，注入正/撤出负），t0 当日流计入期初值不重复计。大额现金流＝单笔 ≥ 期初
  组合 15%，无当日重估值 → large_flow_approx 标记（调用方转 TWR_LARGE_FLOW_APPROX
  降级：MD 均匀时点假设对大额流误差最大处）。
- XIRR：输入 [(date, signed)]（投资者视角：期初市值负、注入负、撤出正、期末
  市值一笔正）；365 天贴现；牛顿迭代（guess=0.1）＋失败回退二分；不收敛或无
  一正一负 → None。
"""
from datetime import date
from decimal import Decimal
from math import isfinite

ZERO = Decimal("0")
ONE = Decimal("1")
QUANT_RATE = Decimal("0.000001")
LARGE_FLOW_RATIO = Decimal("0.15")   # 大额现金流前瞻定义＝期初组合 15%（10-20% 取中）


def _f(v) -> float:
    return float(v)


def value_on(series: list[tuple[date, Decimal]], d: date) -> Decimal | None:
    """carry-forward：series（按 date 升序）中 ≤d 的最后一个值。"""
    val = None
    for dt, v in series:
        if dt <= d:
            val = v
        else:
            break
    return val


def twr_modified_dietz(series: list[tuple[date, Decimal]],
                       flows: list[tuple[date, Decimal]]) -> dict:
    """时间加权收益率（Modified Dietz 逐段＋几何链接）。

    series: [(date, 市值)] 升序、非空；flows: [(date, Δ本金)] 注入正/撤出负，
    仅取 (t0, t_last] 内的参与切段（t0 当日已计入期初值、不重复计流）。
    返回 {period_return, annualized, subperiods[], large_flow_approx,
          large_flow_dates[], reason}；不可算时 rate=None＋reason（绝不编数）。
    """
    empty = {"period_return": None, "annualized": None, "subperiods": [],
             "large_flow_approx": False, "large_flow_dates": [], "reason": None}
    if not series:
        return {**empty, "reason": "无估值序列"}
    series = sorted(series, key=lambda p: p[0])
    t0, t_last = series[0][0], series[-1][0]
    v_start = series[0][1]
    if v_start is None or v_start <= ZERO:
        return {**empty, "reason": "期初市值缺失或 ≤0，TWR 不可得"}

    series_dates = {d for d, _ in series}
    in_flows = [(d, a) for d, a in sorted(flows, key=lambda p: p[0])
                if t0 < d <= t_last]

    # 子区间边界＝估值快照日（重估日切段）；段 i＝(bounds[i], bounds[i+1]]，
    # 段内现金流按 Modified Dietz 权重 w＝(e−d)/(e−s) 计（段尾流 w=0、不赚收益）
    bounds = sorted(series_dates)
    n_seg = len(bounds) - 1

    # 大额现金流前瞻标注：单笔 ≥15% 期初组合且当日无重估值
    large_dates = [d for d, a in in_flows
                   if abs(a) >= LARGE_FLOW_RATIO * v_start and d not in series_dates]

    # 现金流归属：首个 **≥d** 的段尾（估值日恰有流＝段尾零权——MD 期末流约定，
    # 段尾市值已含/已扣该流；段中流＝按日数计权；d==t_last 落末段零权）
    seg_flows: list[list[tuple[date, Decimal]]] = [[] for _ in range(max(n_seg, 0))]
    for d, a in in_flows:
        j = next((k for k in range(1, len(bounds)) if bounds[k] >= d), len(bounds) - 1)
        seg_flows[j - 1].append((d, a))

    link = ONE
    subs: list[dict] = []
    for i in range(n_seg):
        s, e = bounds[i], bounds[i + 1]
        v0 = value_on(series, s)
        v1 = value_on(series, e)
        if v0 is None or v1 is None or v0 <= ZERO:
            return {**{"subperiods": subs, "large_flow_approx": bool(large_dates),
                       "large_flow_dates": [d.isoformat() for d in large_dates]},
                    "period_return": None, "annualized": None,
                    "reason": "子区间期初市值缺失或 ≤0，TWR 不可得"}
        days = (e - s).days
        num = v1 - v0
        den = v0
        for d, a in seg_flows[i]:
            w = (Decimal(days - (d - s).days) / Decimal(days)) if days > 0 else ZERO
            num -= a
            den += w * a
        if den <= ZERO:
            return {**{"subperiods": subs, "large_flow_approx": bool(large_dates),
                       "large_flow_dates": [d.isoformat() for d in large_dates]},
                    "period_return": None, "annualized": None,
                    "reason": "Modified Dietz 分母 ≤0，TWR 不可得"}
        ret = (num / den).quantize(QUANT_RATE)
        link *= (ONE + ret)
        subs.append({
            "start": s.isoformat(), "end": e.isoformat(),
            "v0": str(v0), "v1": str(v1),
            "flows": [{"date": d.isoformat(), "amount": str(a)} for d, a in seg_flows[i]],
            "return": str(ret),
        })

    total_days = (t_last - t0).days
    if not subs:
        return {**empty, "reason": "无有效子区间（仅单一估值日）"}
    period_return = (link - ONE).quantize(QUANT_RATE)
    annualized = None
    reason = None
    if total_days > 0:
        annualized = Decimal(str(
            (1.0 + _f(period_return)) ** (365.0 / total_days) - 1.0
        )).quantize(Decimal("0.0001"))
    else:
        reason = "区间内仅单一估值日，年化不可得"
    return {
        "period_return": str(period_return),
        "annualized": str(annualized) if annualized is not None else None,
        "subperiods": subs,
        "large_flow_approx": bool(large_dates),
        "large_flow_dates": [d.isoformat() for d in large_dates],
        "reason": reason,
    }


def xirr_rate(cashflows: list[tuple[date, Decimal | float]],
              tol: float = 1e-12, max_newton: int = 80,
              max_bisect: int = 300) -> float | None:
    """XIRR（货币加权年化收益率）：365 天贴现，牛顿（guess=0.1）＋失败回退二分。

    需同时存在正负现金流（无一正一负 → None）；不收敛 → None。绝不编数。
    """
    cfs = sorted([(d, _f(a)) for d, a in cashflows if a is not None],
                 key=lambda p: p[0])
    if len(cfs) < 2:
        return None
    if not (any(a > 0 for _, a in cfs) and any(a < 0 for _, a in cfs)):
        return None
    t0 = cfs[0][0]
    times = [(d - t0).days / 365.0 for d, _ in cfs]
    amounts = [a for _, a in cfs]
    scale = max(1.0, sum(abs(a) for a in amounts))

    def npv(r: float) -> float:
        base = 1.0 + r
        if base <= 1e-12:
            return float("inf")
        total = 0.0
        for a, t in zip(amounts, times):
            try:
                total += a * base ** (-t)
            except (OverflowError, ValueError):
                return float("inf") if a > 0 else float("-inf")
        return total

    # 牛顿迭代（带 guess）
    r = 0.1
    for _ in range(max_newton):
        f0 = npv(r)
        if not isfinite(f0):
            break
        if abs(f0) < tol * scale:
            return r
        df = 0.0
        for a, t in zip(amounts, times):
            try:
                df += -t * a * (1.0 + r) ** (-t - 1.0)
            except (OverflowError, ValueError):
                df = float("inf") if -t * a > 0 else float("-inf")
        if not isfinite(df) or abs(df) < 1e-18:
            break
        r = max(r - f0 / df, -1.0 + 1e-9)

    # 二分回退：[lo, hi] 上找符号翻转（hi 倍增扩张）
    lo = -0.999999
    hi = 1.0
    f_lo, f_hi = npv(lo), npv(hi)
    for _ in range(60):
        if f_hi <= 0:
            break
        hi *= 2.0
        f_hi = npv(hi)
    if f_lo != f_lo or f_hi != f_hi:      # NaN 防御（正常路径不产生）
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_bisect):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < tol * scale or hi - lo < tol:
            return mid
        if (fm > 0) == (f_lo > 0):
            lo, f_lo = mid, fm
        else:
            hi, f_hi = mid, fm
    r = (lo + hi) / 2.0
    return r if abs(npv(r)) < 1e-5 * scale else None
