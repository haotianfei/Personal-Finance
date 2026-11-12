"""打印审计留痕契约表（ADR-0027 D9）——只读，不连库、不写文件。

用途：把 `services.audit_contract.ENDPOINT_RULES` 里的 **planned 清单**（＝已知留痕
欠账）按桶分组打到标准输出，供后续逐条开票、也供评审时核对「缺口到底有多少、在哪」。

用法（工作目录 `backend/`，两种都行）：
    ../.venv/bin/python -m scripts.audit_contract_report
    ../.venv/bin/python scripts/audit_contract_report.py

退出码恒 0：本脚本是报告，不是门禁——门禁在 `tests/test_audit_guard.py`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.audit_contract import (  # noqa: E402  （必须在 sys.path 补完之后导入）
    ENDPOINT_RULES,
    RESERVED_ENTITY,
    exempt_rules,
    planned_rules,
    rules_by_bucket,
    traced_rules,
)

BUCKET_ORDER = ("值", "口径", "出处", "离场", "整库", "豁免")


def _line(char: str = "=", width: int = 72) -> str:
    return char * width


def _print_summary() -> None:
    print(_line())
    print("审计留痕契约表总览（ENDPOINT_RULES）")
    print(_line())
    print(f"端点总数      {len(ENDPOINT_RULES)}")
    print(f"  traced      {len(traced_rules()):3d}  （现网已在写审计行）")
    print(f"  planned     {len(planned_rules()):3d}  （显式债务，只许下降）")
    print(f"  exempt      {len(exempt_rules()):3d}  （D2 两类 + 零写入）")
    print()
    print("按桶分布（全表）：")
    grouped = rules_by_bucket()
    for bucket in BUCKET_ORDER:
        rules = grouped.get(bucket, ())
        traced = sum(1 for rule in rules if rule.status == "traced")
        planned = sum(1 for rule in rules if rule.status == "planned")
        exempt = sum(1 for rule in rules if rule.status == "exempt")
        print(f"  {bucket:2s}  共 {len(rules):3d} 条"
              f"（traced {traced:3d} / planned {planned:3d} / exempt {exempt:3d}）")
    if RESERVED_ENTITY:
        print()
        print("预留 entity（planned 端点实现时并入 ENTITY，随后从 RESERVED_ENTITY 删除）：")
        print("  " + ", ".join(sorted(RESERVED_ENTITY)))


def _print_planned() -> None:
    planned = planned_rules()
    print()
    print(_line())
    print(f"planned 清单（{len(planned)} 条待补留痕，按桶分组）")
    print(_line())
    for bucket in BUCKET_ORDER:
        group = [rule for rule in planned if rule.bucket == bucket]
        if not group:
            continue
        print(f"\n[{bucket}] {len(group)} 条")
        for rule in group:
            print(f"  {rule.method:6s} {rule.path}")
            print(f"         entity/action = {rule.entity}/{rule.action}"
                  f"　理由 = {rule.reason}　登记 = {rule.registered}")
        print(f"  -- {bucket} 小计 {len(group)} 条")


def _print_exempt() -> None:
    exempt = exempt_rules()
    print()
    print(_line())
    print(f"exempt 清单（{len(exempt)} 条，不改变被审计事实）")
    print(_line())
    for rule in exempt:
        print(f"  {rule.method:6s} {rule.path}　{rule.reason}")


def main() -> int:
    _print_summary()
    _print_planned()
    _print_exempt()
    print()
    print(_line())
    print("门禁：backend/tests/test_audit_guard.py（表 ⇔ app.routes 双向差集）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())