#!/usr/bin/env python3
"""db_cleanup_p0.py —— 零信息损失的库内/文件层清理（P0 档）。

做什么（三件事，互不依赖，可单选）：
  1. export-history   删除 file_path 指向的文件已不存在的导出历史行
                      （会话区表，非账本；保留"有文件"与"无路径"的历史行）。
  2. dangling-targets 导出 allocation_targets 中 target_id 无法解析到 attributes
                      的悬空行清单（只读；删或重挂由人决定）。
  3. backups          按"每天保留最新 + 全局保留最新 2 份"裁剪 data/backup/ 下的
                      backup_*.zip；rollback_* 与裸 .db 一律不动。

安全约定：
  - 默认 dry-run（只打印计划，不写库不删文件），--apply 才执行。
  - 账本表（assets/asset_items/asset_attributes/principal_changes/audit_log/…）
    一律不 UPDATE、不 DELETE。
  - 每次实际执行写一条 audit_log（entity='maintenance'，actor=system），
    走 audit_chain 正规链化入口，不手搓 prev_hash。

用法：
  .venv/bin/python scripts/db_cleanup_p0.py export-history --apply
  .venv/bin/python scripts/db_cleanup_p0.py dangling-targets
  .venv/bin/python scripts/db_cleanup_p0.py backups --apply
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DB_PATH = Path(os.environ.get("DATABASE_PATH") or REPO_ROOT / "data" / "person_fin_dev.db")
BACKUP_DIR = REPO_ROOT / "data" / "backup"
EXPORT_ROOTS = (
    REPO_ROOT / "data" / "auto-export",
    REPO_ROOT / "data" / "exports",
)
REPORT_DIR = REPO_ROOT / ".scratch" / "db-audit"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_file(raw: str) -> Path | None:
    """file_path 可能是绝对路径、容器内路径（/app/…、/data/…）或相对路径。

    解析顺序：原样 → 仓库相对 → 按 basename 在导出目录下查找。
    返回实际存在的路径，找不到返回 None。
    """
    if not raw:
        return None
    candidates = [Path(raw), REPO_ROOT / raw.lstrip("/")]
    name = os.path.basename(raw)
    candidates += [root / name for root in EXPORT_ROOTS]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _audit(conn: sqlite3.Connection, action: str, before: dict, after: dict) -> None:
    from services.audit_service import make_change, record_sqlite

    change = make_change(entity="maintenance", row_id=None, action=action,
                         before=before, after=after, evidence_ref=None)
    record_sqlite(conn, change, actor="system")


def cmd_export_history(apply: bool) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, file_path, export_type, filename FROM export_history ORDER BY id"
        ).fetchall()
        total = len(rows)
        dangling, kept_file, kept_nopath = [], 0, 0
        for row in rows:
            raw = row["file_path"] or ""
            if not raw.strip():
                kept_nopath += 1
                continue
            if _resolve_file(raw):
                kept_file += 1
            else:
                dangling.append((row["id"], row["export_type"], row["filename"], raw))

        print(f"export_history: total={total} kept_file={kept_file} "
              f"kept_nopath={kept_nopath} dangling={len(dangling)}")
        for rid, etype, fname, raw in dangling[:5]:
            print(f"  e.g. id={rid} type={etype} name={fname} path={raw}")
        if not apply:
            print("dry-run：未删除。加 --apply 执行。")
            return 0
        if not dangling:
            print("无悬空行，无需处理。")
            return 0

        ids = [d[0] for d in dangling]
        before = {"table": "export_history", "rows": total, "dangling": len(ids)}
        with conn:  # 单事务：删行 + 审计同进同退
            conn.executemany("DELETE FROM export_history WHERE id = ?",
                             [(i,) for i in ids])
            after = {"table": "export_history", "rows": total - len(ids),
                     "deleted": len(ids)}
            _audit(conn, "export_history_prune", before, after)
        left = conn.execute("SELECT COUNT(*) FROM export_history").fetchone()[0]
        print(f"已删除 {len(ids)} 行；export_history 现有 {left} 行。")
        return 0
    finally:
        conn.close()


def cmd_dangling_targets(apply: bool) -> int:
    """apply 无效：本命令永远只读，产出异常清单供人工裁决。

    target_id 是字符串且无 FK，实际存在两套键约定：按属性 id（fund_type）与
    按属性 name（liquidity_rating）。故解析顺序为 id → name，并区分三类异常：
      orphan      id/name 都解析不到属性节点
      mismatch    解析到节点但节点 type 与 dimension 不一致（跨维度错挂）
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.dimension, t.target_id, t.target_percent,
                   t.parent_id, t.created_at, t.updated_at
            FROM allocation_targets t ORDER BY t.dimension, t.id
            """
        ).fetchall()
        issues = []
        for row in rows:
            key = str(row["target_id"])
            attr = conn.execute(
                "SELECT id, name, type FROM attributes WHERE CAST(id AS TEXT) = ?",
                (key,),
            ).fetchone()
            if attr is None:
                attr = conn.execute(
                    "SELECT id, name, type FROM attributes WHERE name = ?",
                    (key,),
                ).fetchone()
            if attr is None:
                issues.append(("orphan", row, None))
            elif attr["type"] != row["dimension"]:
                issues.append(("mismatch", row, attr))

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / "dangling-targets.csv"
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["kind", "id", "dimension", "target_id", "target_percent",
                             "resolved_name", "resolved_type"])
            for kind, row, attr in issues:
                writer.writerow([kind, row["id"], row["dimension"], row["target_id"],
                                 row["target_percent"],
                                 attr["name"] if attr else "",
                                 attr["type"] if attr else ""])
        print(f"异常 target 行数={len(issues)}；清单已写入 {out.relative_to(REPO_ROOT)}")
        for kind, row, attr in issues:
            extra = f" -> {attr['name']}({attr['type']})" if attr else ""
            print(f"  {kind} id={row['id']} dimension={row['dimension']} "
                  f"target_id={row['target_id']} pct={row['target_percent']}{extra}")
        return 0
    finally:
        conn.close()


def _plan_backup_prune() -> tuple[list[Path], list[Path]]:
    """返回 (keep, drop)。规则：rollback_* 与裸 .db 永不动；
    backup_*.zip 每天保留最新一份，另全局保留最新 2 份。"""
    keep: list[Path] = []
    drop: list[Path] = []
    bundles = sorted(BACKUP_DIR.glob("backup_*.zip"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    newest_two = set(bundles[:2])
    seen_days: set[str] = set()
    for path in bundles:
        day = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")
        if path in newest_two or day not in seen_days:
            keep.append(path)
            seen_days.add(day)
        else:
            drop.append(path)
    return keep, drop


def cmd_backups(apply: bool) -> int:
    if not BACKUP_DIR.is_dir():
        print(f"目录不存在：{BACKUP_DIR}")
        return 1
    keep, drop = _plan_backup_prune()
    freed = sum(p.stat().st_size for p in drop)
    print(f"backup bundles: keep={len(keep)} drop={len(drop)} "
          f"预计释放={freed / 1024 / 1024:.1f}MB")
    for path in keep:
        print(f"  keep {path.name}")
    for path in drop[:5]:
        print(f"  drop {path.name} ({path.stat().st_size / 1024 / 1024:.0f}MB)")
    if len(drop) > 5:
        print(f"  ... 另 {len(drop) - 5} 个")
    if not apply:
        print("dry-run：未删除。加 --apply 执行。")
        return 0
    for path in drop:
        path.unlink()
    conn = _connect()
    try:
        with conn:
            _audit(conn, "backup_prune",
                   {"bundles": len(keep) + len(drop), "freed_mb": round(freed / 1024 / 1024, 1)},
                   {"bundles": len(keep)})
    finally:
        conn.close()
    print(f"已删除 {len(drop)} 个包，释放 {freed / 1024 / 1024:.1f}MB。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 零损失清理（默认 dry-run）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("export-history", "删除指向已不存在文件的导出历史行"),
        ("dangling-targets", "导出悬空 allocation_targets 清单（只读）"),
        ("backups", "按天裁剪 data/backup 下的 zip 包"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--apply", action="store_true", help="实际执行（默认只打印计划）")
    args = parser.parse_args()
    return {
        "export-history": cmd_export_history,
        "dangling-targets": cmd_dangling_targets,
        "backups": cmd_backups,
    }[args.cmd](args.apply)


if __name__ == "__main__":
    raise SystemExit(main())