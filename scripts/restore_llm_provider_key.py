#!/usr/bin/env python3
"""从应用自带的历史备份（zip）里恢复 llm_providers 的 api_key。

用途：供应商密钥被误清空后，从 `deploy/data/backup/backup_*.zip` 内的
`person_fin.db` 取回该行密钥，写回运行库。**全程不打印密钥内容**，
只报长度，避免密钥进入终端/日志/会话记录。

用法：
  # 先看计划（默认 dry-run）
  .venv/bin/python scripts/restore_llm_provider_key.py \
      --db deploy/data/person_fin.db \
      --backup deploy/data/backup/backup_20261030_074437.zip \
      --provider-id 3 --enable

  # 确认后落库
  ... --apply

注意：
- 写运行库须以容器运行用户身份执行（库属主即该用户），否则只读报错。
- 默认不改 is_default；需要设为默认才加 --default。
- 只覆盖 api_key（以及可选 enabled/is_default），其余字段不动。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import zipfile


def _load_backup_row(zip_path: str, provider_id: int) -> sqlite3.Row:
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            if "person_fin.db" not in z.namelist():
                sys.exit(f"备份里没有 person_fin.db：{zip_path}")
            z.extract("person_fin.db", td)
        conn = sqlite3.connect(os.path.join(td, "person_fin.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "select id,label,kind,base_url,model,enabled,is_default,"
                "coalesce(api_key,'') as api_key from llm_providers where id = ?",
                (provider_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        sys.exit(f"备份里没有 id={provider_id} 的供应商")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="运行库路径（可写）")
    ap.add_argument("--backup", required=True, help="含 person_fin.db 的备份 zip")
    ap.add_argument("--provider-id", type=int, required=True)
    ap.add_argument("--enable", action="store_true", help="同时置 enabled=1")
    ap.add_argument("--default", action="store_true", help="同时置 is_default=1")
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省 dry-run）")
    args = ap.parse_args()

    src = _load_backup_row(args.backup, args.provider_id)
    key = src["api_key"] or ""
    if not key.strip():
        sys.exit("备份里该行的 api_key 也是空的，没有可恢复内容")

    conn = sqlite3.connect(f"file:{args.db}?mode=rw", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "select id,label,kind,base_url,model,enabled,is_default,"
        "coalesce(api_key,'') as api_key from llm_providers where id = ?",
        (args.provider_id,),
    ).fetchone()
    if cur is None:
        sys.exit(f"运行库里没有 id={args.provider_id} 的供应商")

    print(f"备份来源：{os.path.basename(args.backup)}")
    print(f"目标行：id={cur['id']} {cur['label']}")
    print(f"  api_key : 当前 {len(cur['api_key'])} 字符 → 恢复为 {len(key)} 字符（内容不显示）")
    print(f"  enabled : {cur['enabled']} → {1 if args.enable else cur['enabled']}")
    print(f"  default : {cur['is_default']} → {1 if args.default else cur['is_default']}")

    if not args.apply:
        print("\n[dry-run] 未写入。确认无误后加 --apply。")
        conn.close()
        return 0

    sets = ["api_key = ?"]
    vals: list[object] = [key]
    if args.enable:
        sets.append("enabled = 1")
    if args.default:
        sets.append("is_default = 1")
    if args.default:
        # 默认供应商保持唯一：先把其它行降级，避免出现两个默认
        conn.execute("update llm_providers set is_default = 0 where id <> ?", (args.provider_id,))
    conn.execute(f"update llm_providers set {', '.join(sets)} where id = ?", (*vals, args.provider_id))
    conn.commit()

    after = conn.execute(
        "select enabled,is_default,length(coalesce(api_key,'')) as klen "
        "from llm_providers where id = ?", (args.provider_id,)
    ).fetchone()
    print(f"\n[已写入] enabled={after['enabled']} default={after['is_default']} key_len={after['klen']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())