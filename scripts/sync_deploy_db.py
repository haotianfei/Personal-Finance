#!/usr/bin/env python3
"""把运行库同步为本地一致快照（默认 dry-run，--apply 才落盘）。

为什么不用 `cp`：运行库 `deploy/data/person_fin.db` 常带未合并的 `-wal`（可达数 MB），
裸拷主文件只会得到一段时间以前的陈旧快照，且 schema/行数都可能落后
（MEMORY.md §八 2026-11-06 实测踩坑）。本脚本走 sqlite backup API 取一致快照。

行为：
  1. 只读打开源库（URI mode=ro，WAL 一并生效）；
  2. backup API 写入同目录临时文件 `<dst>.tmp`；
  3. 校验临时文件：integrity_check=ok、schema_version 最大值、五张表行数与源库逐项相等；
  4. 若目标已存在，先把 dst / dst-wal / dst-shm 移入归档目录（**移动，不删除**）；
  5. `os.replace` 原子落位；落位后打印目标库统计与 sha256；
  6. 源库全程只读，失败时删掉临时文件、原目标不动。

用法：
  python3 scripts/sync_deploy_db.py                 # dry-run，只打印计划与两侧统计
  python3 scripts/sync_deploy_db.py --apply         # 执行
  python3 scripts/sync_deploy_db.py --apply --src deploy/data/person_fin.db --dst data/person_fin_dev.db
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = os.path.join(REPO, "deploy", "data", "person_fin.db")
DEFAULT_DST = os.path.join(REPO, "data", "person_fin_dev.db")


def db_role(path: str) -> str:
    """库角色四档（与 backend/database.py 的 derive_db_role 同规则，见 spec D3）。

    本脚本刻意只用标准库（可在无 venv 的机器上直接 `python3` 跑），故不 import
    backend.database；改动规则时两处必须同步（权威实现+测试在 backend 侧）。
    """
    name = os.path.basename(path or "")
    if name.endswith("_dev.db"):
        return "dev"
    if name.endswith("_staging.db"):
        return "staging"
    if name == "person_fin.db":
        return "prod"
    return "unknown"


# 逐项对拍的行数锚点：账本 + 凭证 + 分类法 + 审计链
COUNT_TABLES = (
    "assets",
    "asset_items",
    "audit_log",
    "screenshot_evidence",
    "source_groups",
)


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str):
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        except sqlite3.Error as exc:  # 表可能尚未存在（更老的库）
            return f"ERR:{exc}"

    out = {
        "integrity": one("pragma integrity_check"),
        "schema_version": one("select max(version) from schema_version"),
        "chain": one("select last_id || '/' || row_count from audit_chain_state"),
        "seals": one("select count(*) from audit_chain_seals"),
    }
    for table in COUNT_TABLES:
        out[table] = one(f"select count(*) from {table}")
    return out


def _fmt(s: dict) -> str:
    return (f"schema={s['schema_version']} chain={s['chain']} seals={s['seals']} "
            + " ".join(f"{t}={s[t]}" for t in COUNT_TABLES)
            + f" integrity={s['integrity']}")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_existing(dst: str, archive_root: str) -> list[tuple[str, str]]:
    """把现有 dst 三件（db/wal/shm）移入归档目录，返回 [(src, dst)]。"""
    moved: list[tuple[str, str]] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(archive_root, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = dst + suffix
        if not os.path.exists(src):
            continue
        name = os.path.basename(dst) + suffix
        target = os.path.join(archive_root, name)
        if os.path.exists(target):
            target = os.path.join(archive_root, f"{name}.bak-{stamp}")
        shutil.move(src, target)
        moved.append((src, target))
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC, help=f"源库（只读），默认 {DEFAULT_SRC}")
    ap.add_argument("--dst", default=DEFAULT_DST, help=f"目标快照，默认 {DEFAULT_DST}")
    ap.add_argument("--archive-root", default=None, help="归档目录，默认 <dst 所在目录>/archived-db-<YYYYMMDD>")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只 dry-run）")
    args = ap.parse_args()

    src, dst = os.path.abspath(args.src), os.path.abspath(args.dst)
    if not os.path.exists(src):
        print(f"[FAIL] 源库不存在: {src}", file=sys.stderr)
        return 2
    if src == dst:
        print("[FAIL] src 与 dst 相同，拒绝执行", file=sys.stderr)
        return 2

    archive_root = args.archive_root or os.path.join(
        os.path.dirname(dst), f"archived-db-{datetime.now().strftime('%Y%m%d')}")

    src_conn = _ro(src)
    src_stats = stats(src_conn)

    print(f"[src] {src}  role={db_role(src)}")
    print(f"      {_fmt(src_stats)}")
    wal = src + "-wal"
    print(f"      主文件 {os.path.getsize(src):,} B | wal {os.path.getsize(wal):,} B" if os.path.exists(wal)
          else f"      主文件 {os.path.getsize(src):,} B | 无 wal")
    if src_stats["integrity"] != "ok":
        print(f"[FAIL] 源库 integrity_check 非 ok: {src_stats['integrity']}", file=sys.stderr)
        src_conn.close()
        return 2

    if os.path.exists(dst):
        dst_conn = _ro(dst)
        print(f"[dst] {dst}  role={db_role(dst)}")
        print(f"      {_fmt(stats(dst_conn))}   ({os.path.getsize(dst):,} B)")
        dst_conn.close()
    else:
        print(f"[dst] {dst}  role={db_role(dst)}  不存在")

    print(f"[plan] 归档目录 {archive_root}")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(dst + suffix):
            print(f"       move {dst + suffix} -> {archive_root}/")
    print(f"       backup 快照 -> {dst}.tmp -> {dst}")

    if not args.apply:
        print("[dry-run] 未做任何改动；加 --apply 执行")
        src_conn.close()
        return 0

    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        dst_conn = sqlite3.connect(tmp)
        src_conn.backup(dst_conn)          # 一致快照：单文件，无 WAL
        dst_conn.close()

        snap = stats(_ro(tmp))
        diffs = [k for k in ("integrity", "schema_version", *COUNT_TABLES) if snap[k] != src_stats[k]]
        if diffs:
            raise RuntimeError(f"快照与源库不一致: {diffs}\n  src={_fmt(src_stats)}\n  tmp={_fmt(snap)}")
        print(f"[ok]   快照校验通过: {_fmt(snap)}")

        moved = archive_existing(dst, archive_root)
        for old, new in moved:
            print(f"[move] {old} -> {new}")
        os.replace(tmp, dst)
        print(f"[ok]   已落位 {dst} ({os.path.getsize(dst):,} B) sha256={_sha256(dst)}")
        final = stats(_ro(dst))
        print(f"[final] {_fmt(final)}")
    except Exception as exc:  # noqa: BLE001 - 失败必须保持目标可回退
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"[FAIL] {exc}", file=sys.stderr)
        print("[note] 目标库未改动（临时文件已清理）", file=sys.stderr)
        src_conn.close()
        return 1
    finally:
        src_conn.close()

    print("[done] 源库未做任何写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())