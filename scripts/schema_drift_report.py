#!/usr/bin/env python3
"""schema_drift_report.py —— 生产库 vs `models.py` 的结构漂移报告（只读）。

定位：`backend/models.py` 是**规范**（`create_all` 即按它建表）；编号迁移只负责
把老库收敛到它。本脚本把两者逐表逐项比对，把差异分成三类输出：

  BLOCKER  语义差异：列缺失/多余、索引缺失、触发器缺失、CHECK 缺失、NOT NULL 收紧
           → 必须修（否则「测试库能跑、生产拒绝」或反之）
  KNOWN    已登记的表述差异：列序、类型拼写、模型可空 vs 物理 NOT NULL
           → 保留；变更时同步本文件 `KNOWN` 常量与 database-map §6
  INFO     自动索引名（sqlite_autoindex_*）等噪声 → 忽略

用法：
  .venv/bin/python scripts/schema_drift_report.py                 # 对比 data/person_fin_dev.db
  .venv/bin/python scripts/schema_drift_report.py --db /path/x.db --json
退出码：0＝无 BLOCKER；1＝有 BLOCKER；2＝环境错误。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

# database.py 在 import 时向 stdout 打一行 database_path —— --json 会被污染，先吞掉
with contextlib.redirect_stdout(io.StringIO()):
    import models  # noqa: E402,F401  —— 注册全部表
    from database import Base  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "person_fin_dev.db"

# 已登记的「表述差异」：(表, 维度) → 原因。维度写法与 _diff 输出一致。
KNOWN = {
    ("assets", "order"): "0008 重建把 item/superseded/cost_basis 追加到末尾",
    ("assets", "notnull:asset_item_id"): "模型可空（双轨/测试兼容）vs 物理 NOT NULL",
    ("screenshot_evidence", "order"): "0006/0007 ALTER 追加 supplement_rows/engine",
    ("attributes", "order"): "0005 ALTER 追加 remind_enabled",
    ("audit_log", "order"): "0010 ALTER 追加 idempotency_key",
    ("audit_log", "type:prev_hash"): "模型 VARCHAR(64) vs 0009 CHAR(64)（亲和性相同）",
    ("audit_chain_seals", "type:last_hash"): "模型 VARCHAR(64) vs 0018 CHAR(64)",
    ("audit_chain_seals", "type:prev_seal_hash"): "模型 VARCHAR(64) vs 0018 CHAR(64)",
    ("audit_chain_seals", "type:seal_hash"): "模型 VARCHAR(64) vs 0018 CHAR(64)",
    ("allocation_targets", "order"): "0004/0017 ALTER 追加阈值列",
    ("goal_versions", "type:target_amount"): "NUMERIC(15, 2) vs NUMERIC(15,2) 仅空格",
    ("goal_versions", "type:target_rate_annual"): "NUMERIC(8, 4) vs NUMERIC(8,4) 仅空格",
    ("goals", "default:is_primary"): "模型 Python 默认 0 vs 0015 server default 0",
    ("assets", "default:side"): "模型 Python 默认 'asset' vs 0002 server DEFAULT",
    ("allocation_targets", "default:alert_threshold"): "0004 server DEFAULT 5.00（模型为 Python 默认）",
    ("idempotency_keys", "extra_table"): "非 ORM 表：0010 裸 SQL 建立",
    ("preview_jobs", "extra_table"): "非 ORM 表：0011 裸 SQL 建立",
    ("schema_version", "extra_table"): "非 ORM 表：runner 裸 SQL 建立",
}

_TYPE_NOISE = re.compile(r"[\s]+")

# 非 ORM 表：由迁移/runner 以裸 SQL 建立（design 决定，见 database-map §1）
NON_ORM_TABLES = {"idempotency_keys", "preview_jobs", "schema_version"}


def _norm_type(t: str | None) -> str:
    """SQLite 类型等价归一：去空白；DECIMAL 与 NUMERIC 同为 NUMERIC 亲和。"""
    norm = _TYPE_NOISE.sub("", (t or "").upper())
    return norm.replace("DECIMAL(", "NUMERIC(")


def _snapshot(path: str) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        snap = {}
        for table in names:
            info = list(conn.execute(f'PRAGMA table_info("{table}")'))
            idx = {}
            for r in conn.execute(f'PRAGMA index_list("{table}")'):
                name = r[1]
                if name.startswith("sqlite_autoindex_"):
                    continue
                cols = [x[2] for x in conn.execute(f'PRAGMA index_info("{name}")')]
                idx[name] = {"unique": bool(r[2]), "partial": bool(r[4]), "cols": tuple(cols)}
            trg = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,))}
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()[0] or ""
            checks = set(re.findall(r"CONSTRAINT\s+(\w+)\s+CHECK", sql, re.I))
            snap[table] = {
                "cols": {r[1]: {"type": _norm_type(r[2]), "notnull": r[3],
                                "dflt": r[4], "pk": bool(r[5])}
                         for r in info},
                "order": [r[1] for r in info],
                "idx": idx,
                "trg": trg,
                "checks": checks,
            }
        return snap
    finally:
        conn.close()


def _fresh_snapshot() -> dict:
    tmp = tempfile.mkdtemp(prefix="drift_")
    path = os.path.join(tmp, "models.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return _snapshot(path)


def _diff(prod: dict, ref: dict) -> list[dict]:
    out: list[dict] = []

    def add(level, table, dim, detail):
        key = (table, dim)
        level = "KNOWN" if key in KNOWN else level
        reason = KNOWN.get(key)
        out.append({"level": level, "table": table, "dim": dim,
                    "detail": detail, "reason": reason})

    for table in sorted(set(ref) - set(prod)):
        add("BLOCKER", table, "missing_table", "库中缺表")
    for table in sorted(set(prod) - set(ref)):
        level = "KNOWN" if table in NON_ORM_TABLES else "BLOCKER"
        add(level, table, "extra_table", "非 ORM 表（裸 SQL 建立）/ 或 models 缺表")

    for table in sorted(set(prod) & set(ref)):
        P, R = prod[table], ref[table]
        for col in sorted(set(R["cols"]) - set(P["cols"])):
            add("BLOCKER", table, f"missing_column:{col}", "库中缺列")
        for col in sorted(set(P["cols"]) - set(R["cols"])):
            add("BLOCKER", table, f"extra_column:{col}", "库中有列、models 无")
        if P["order"] != R["order"]:
            add("KNOWN", table, "order", {"prod": P["order"], "models": R["order"]})
        for col in sorted(set(P["cols"]) & set(R["cols"])):
            pc, rc = P["cols"][col], R["cols"][col]
            if pc["type"] != rc["type"]:
                add("BLOCKER", table, f"type:{col}",
                    {"prod": pc["type"], "models": rc["type"]})
            # SQLite 怪癖：`INTEGER PRIMARY KEY` 的 pragma notnull 报 0（隐含非空），
            # 与 create_all 生成的 `INTEGER NOT NULL PRIMARY KEY` 语义等价 → 跳过
            if pc["notnull"] != rc["notnull"] and not (pc["pk"] and rc["pk"]):
                add("BLOCKER", table, f"notnull:{col}",
                    {"prod": pc["notnull"], "models": rc["notnull"]})
            if (pc["dflt"] or "") != (rc["dflt"] or ""):
                # 服务端默认（迁移 ALTER 带 DEFAULT）vs 模型 Python 侧默认：
                # 只影响裸 SQL 插入，不影响 ORM 写路径 → 记为已知
                add("KNOWN", table, f"default:{col}",
                    {"prod": pc["dflt"], "models": rc["dflt"]})
        for name in sorted(set(R["idx"]) - set(P["idx"])):
            add("BLOCKER", table, f"missing_index:{name}", R["idx"][name])
        for name in sorted(set(P["idx"]) - set(R["idx"])):
            add("BLOCKER", table, f"extra_index:{name}", P["idx"][name])
        for name in sorted(set(P["idx"]) & set(R["idx"])):
            if P["idx"][name] != R["idx"][name]:
                add("BLOCKER", table, f"index_def:{name}",
                    {"prod": P["idx"][name], "models": R["idx"][name]})
        for name in sorted(R["trg"] - P["trg"]):
            add("BLOCKER", table, f"missing_trigger:{name}", "库中缺触发器")
        for name in sorted(P["trg"] - R["trg"]):
            add("KNOWN", table, f"migration_only_trigger:{name}",
                "触发器只在迁移中建立（create_all 不建触发器）")
        for name in sorted(R["checks"] - P["checks"]):
            add("BLOCKER", table, f"missing_check:{name}", "库中缺 CHECK")
        for name in sorted(P["checks"] - R["checks"]):
            add("BLOCKER", table, f"extra_check:{name}", "库中有 CHECK、models 无")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生产库 vs models.py 结构漂移（只读）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="把 KNOWN 也算失败")
    args = ap.parse_args()

    if not Path(args.db).is_file():
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2

    prod, ref = _snapshot(args.db), _fresh_snapshot()
    diffs = _diff(prod, ref)
    blockers = [d for d in diffs if d["level"] == "BLOCKER"]
    known = [d for d in diffs if d["level"] == "KNOWN"]

    if args.json:
        print(json.dumps({"blockers": blockers, "known": known,
                          "tables": len(prod)}, ensure_ascii=False, indent=2))
    else:
        print(f"库：{args.db}")
        print(f"表：{len(prod)}（models={len(ref)}）｜BLOCKER={len(blockers)} KNOWN={len(known)}")
        for d in blockers:
            print(f"  [BLOCKER] {d['table']}.{d['dim']}  {d['detail']}")
        for d in known:
            print(f"  [KNOWN]   {d['table']}.{d['dim']}  {d['reason'] or d['detail']}")

    if blockers or (args.strict and known):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())