"""写路由 × 契约表守卫（ADR-0027 D9 / D12）。

这组测试只回答一个问题：**表与代码还一致吗**。

1. 双向差集：`app.routes` 的写方法集合 vs `ENDPOINT_RULES` 的 `(method, path)` 集合
   ——「未登记的路由」与「已登记但路由不存在」**分开打印**（守卫的全部价值就在这两个
   差集能一眼看出该补哪一边，而不是干瘪的 `assert a == b`）。
2. 表自身不变式（`_validate_rules`）＋ 规模对齐：**不硬编码 131**，以
   `write_route_set(app.routes)` 的规模为准。
3. planned 债务可见：有基线、只许下降；失败时按桶分组打印全部欠账。
4. 抽查：跨桶代表行 ＋ 读端点不得混进表里。

纯静态比对：不连数据库、不起 TestClient。`main` 的 import 只建 FastAPI 实例
（lifespan 里的迁移/调度器不会跑）。
"""
import pytest

from main import app
from services.audit_contract import (
    BUCKETS,
    ENDPOINT_RULES,
    ENTITY,
    RESERVED_ENTITY,
    STATUSES,
    _validate_rules,
    exempt_rules,
    planned_rules,
    rule_for,
    traced_rules,
    write_route_set,
)

_BUCKET_ORDER = ("值", "口径", "出处", "离场", "整库", "豁免")


def _declared() -> set[tuple[str, str]]:
    """契约表侧的口径：`(method, path)` 集合。"""
    return {(rule.method, rule.path) for rule in ENDPOINT_RULES}


def _diff_report(unregistered, phantom) -> str:
    """把两个差集分类打印（未登记 / 幽灵行）。"""
    lines: list[str] = []
    if unregistered:
        lines.append(f"① 未登记的路由（app.routes 有、ENDPOINT_RULES 无）{len(unregistered)} 条"
                     "——新增写端点必须落表（D9）：")
        lines += [f"   + {method:6s} {path}" for method, path in sorted(unregistered)]
    if phantom:
        lines.append(f"② 已登记但路由不存在（ENDPOINT_RULES 有、app.routes 无）{len(phantom)} 条"
                     "——路由删改/改路径必须同步改表行（D12）：")
        lines += [f"   - {method:6s} {path}" for method, path in sorted(phantom)]
    return "\n".join(lines) if lines else "（两侧无差异）"


def _bucket_report(rules) -> str:
    """按桶分组打印规则清单（债务可见性用）。"""
    lines: list[str] = []
    for bucket in _BUCKET_ORDER:
        group = [rule for rule in rules if rule.bucket == bucket]
        if not group:
            lines.append(f"  [{bucket}] 0 条")
            continue
        lines.append(f"  [{bucket}] {len(group)} 条")
        for rule in group:
            lines.append(f"    - {rule.method:6s} {rule.path}"
                         f"  → {rule.entity or '—'}/{rule.action or 'EXEMPT'}"
                         f"  {rule.reason}")
    return "\n".join(lines)


# ---------------------------------------------------------- 1. 表 ⇔ 路由 双向相等


def test_contract_table_matches_real_write_routes():
    """守卫主断言：两边集合必须完全相等，失败时分类打印两个差集。"""
    actual = write_route_set(app.routes)
    declared = _declared()
    unregistered = actual - declared      # 路由有、表无：新写端点漏登记
    phantom = declared - actual           # 表有、路由无：路由删改没同步表

    assert not unregistered and not phantom, (
        f"审计契约表与实际写路由不一致（{len(actual)} 路由 / {len(declared)} 登记行）：\n"
        + _diff_report(unregistered, phantom))
    assert actual == declared


# ---------------------------------------------------------- 2. 表自身不变式 + 规模


def test_rules_are_self_consistent_and_size_matches_routes():
    _validate_rules()          # 表自身不变式；坏表在这里就会 RuntimeError

    routes = write_route_set(app.routes)
    # 规模对齐以路由为准（不写死 131）：表增删行必须与路由增删同步，逐差集见上一个测试
    assert len(ENDPOINT_RULES) == len(routes)
    # 一个端点只许一行（D9）
    assert len(_declared()) == len(ENDPOINT_RULES)
    # 三种 status 恰好划分全表，没有第四类、也没有漏网
    assert (len(traced_rules()) + len(planned_rules()) + len(exempt_rules())
            == len(ENDPOINT_RULES))
    assert {rule.bucket for rule in ENDPOINT_RULES} <= BUCKETS
    assert {rule.status for rule in ENDPOINT_RULES} <= STATUSES
    # exempt ⇔ 无 entity/action（双向；单向漏「豁免了却仍记动作」这种情况）
    for rule in ENDPOINT_RULES:
        has_fields = rule.entity is not None or rule.action is not None
        assert (rule.status == "exempt") != has_fields, (
            f"{rule.method} {rule.path}: exempt 与 entity/action 必须互斥")
    # 预留 entity 一旦并入 ENTITY 就该删掉预留项（否则表与枚举出现两套说法）
    assert RESERVED_ENTITY.isdisjoint(ENTITY), (
        f"这些 entity 已在 ENTITY 里登记，请从 RESERVED_ENTITY 删除："
        f"{sorted(RESERVED_ENTITY & ENTITY)}")


# ---------------------------------------------------------- 3. planned 债务可见


# 债务已于 2026-11-07 清零（表冻结时点 58 条 → 分批清偿到 0）。
# **不许回退**：任何新端点只许直接写成 traced 或 exempt；新增 planned 会让本条立刻变红。
def test_planned_debt_is_visible():
    planned = planned_rules()
    assert len(planned) == 0, (
        f"planned 欠账 {len(planned)} 条（应为 0）。\n"
        "· 变少＝已清偿：把 PLANNED_BASELINE 同步改小（只许下降）；\n"
        "· 变多＝有端点从 traced/exempt 降级，或新增端点只登记不实现。\n"
        "当前 planned 按桶分布：\n" + _bucket_report(planned))
    # 欠账行必须是完整的契约行（entity/action 都得给，否则补写时无从下手）
    for rule in planned:
        assert rule.entity and rule.action and rule.reason
        assert rule.bucket != "豁免"


# ---------------------------------------------------------- 4. 抽查 + 读端点


SPOT_CHECKS = [
    # (method, path, bucket, entity, action, status)
    ("POST", "/api/assets/batch-delete", "值", "batch", "execute", "traced"),
    ("POST", "/api/imports/seed", "值", "import", "import_seed", "traced"),
    ("POST", "/api/backup/restore", "整库", "backup", "restore", "traced"),
    ("POST", "/api/llm-providers", "口径", "llm_provider", "create", "traced"),
    ("PATCH", "/api/screenshots/evidence/{evidence_id}/recognition",
     "豁免", None, None, "exempt"),
]


@pytest.mark.parametrize("method,path,bucket,entity,action,status", SPOT_CHECKS)
def test_spot_check_rules(method, path, bucket, entity, action, status):
    rule = rule_for(method, path)
    assert rule is not None, f"{method} {path} 未登记进契约表"
    assert (rule.bucket, rule.entity, rule.action, rule.status) == (
        bucket, entity, action, status), f"{method} {path} 契约行取值不符：{rule}"
    # 登记行必须对应真实路由（防「凭空登记」）
    assert (method, path) in write_route_set(app.routes)
    # 实体/动作落表时必须是已登记取值（exempt 除外）
    if status != "exempt":
        assert rule.entity in ENTITY or rule.entity in RESERVED_ENTITY
        assert rule.action is not None


def test_read_endpoints_are_not_in_contract_table():
    """读端点不是留痕面：GET/HEAD 不进表，也不进写路由集合。"""
    declared = _declared()
    assert all(method != "GET" for method, _ in declared), (
        "契约表只登记写方法，GET 行出现了："
        f"{sorted(m for m, _ in declared if m == 'GET')}")

    read_paths = {
        route.path for route in app.routes
        if getattr(route, "path", "").startswith("/api")
        and "GET" in (getattr(route, "methods", None) or ())}
    assert read_paths, "app.routes 里应有 /api 下的 GET 端点"
    for path in ("/api/assets", "/api/audit-log"):
        assert path in read_paths, f"{path} 应是 GET 读端点"
        assert rule_for("GET", path) is None
        assert ("GET", path) not in write_route_set(app.routes)