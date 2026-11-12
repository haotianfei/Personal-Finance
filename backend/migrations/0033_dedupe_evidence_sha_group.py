"""0033：凭证按（sha, 来源组）去重 + 复合唯一索引（L2）。

规格：`.scratch/evidence-dedup-index/spec.md` D3 / D5（2026-11-06 冻结）
上游：ADR-0024（凭证按来源组去重与复合唯一索引）

背景：`get_or_create_evidence` 的查重一直**全局按 sha**，于是数据库层没有任何
唯一约束，历史也留了 5 组「同 sha 两行」——每组两行的 `source_group_id` 相同
（同一张图在同一组被传了两次），且每组恰有一行被账本/审计引用，另一行在四类
引用上全为 0。全局查重还有一处事实错误：同一张图上传到**另一个来源组**时会
静默复用第一个组的凭证行，`source_group_id` 与文件名里的 `sg<id>` 都跟着说谎。
服务层已同批收窄为「同组查重」（D1），本迁移补上存储层的两件事。

本迁移做两件事，**不动任何文件**（D4：事务管不了文件系统，回滚后行回来了文件
却没了，比留孤儿更糟；孤儿由脚本报告、业主人手清理）：

1. **删**同时满足「同组内还有同 sha 的另一行」+「该行四类引用
   （`assets.source_evidence_id` / `audit_log.evidence_ref` /
   `principal_changes.evidence_ref` / `transfer_events.evidence_ref`）全为 0」的行；
   每组保留引用数最多者（并列取 id 最小）。删前 log 出 id 清单与条数，删后行数对账。
   **判据从严**：不满足的行一律不动，交给人处理。
2. **建** `CREATE UNIQUE INDEX IF NOT EXISTS ux_screenshot_evidence_sha_group
   ON screenshot_evidence(file_sha256, source_group_id)`。不建单列 sha 唯一索引——
   它与 D1 直接冲突（第二组永远拿不到自己的凭证行 / 索引立刻拒绝插入）。

守卫与安全：

- `screenshot_evidence` 不存在 → `skipped`（桩库/不完整库形状）。
- 表存在但缺 `id` / `file_sha256` / `source_group_id` → `skipped`：这种表既不是
  0029/0030 之后的形状也不是本迁移的活，多为测试桩表或旁路程序自建的同名表
  （沿用 0029/0030/0032「形状不在预期则幂等跳过」的先例）。
- 引用表/引用列缺失（桩库）→ 该表引用数按 0 计并记 warning：表不在＝没有任何
  引用行，「0」是事实而不是猜测。
- **幂等**：索引已在（unique 且列序逐字 `(file_sha256, source_group_id)`）且无
  重复行 → `noop`；二跑既不删行也不重建索引。
- **不可自动收敛的重复 → 抛错中止**：删完仍有同组同 sha（例如两行都被引用）时，
  建唯一索引必然失败。这里先抛出点名到 sha 与组的错误，而不是把
  `UNIQUE constraint failed` 甩给启动链——「判据从严」的代价就是可能留活口，
  活口必须响。
- 建索引后校验：`PRAGMA index_list` 里该索引 `unique=1`、`PRAGMA index_info`
  列序逐字；父表 `source_groups` 在时 `foreign_key_check` 空；`integrity_check=ok`。
- 事务由 runner（`services/db_migration_service._run_script`）负责，本脚本不 commit；
  抛错 → 整个脚本回滚（含已删的行），迁移前置备份是唯一回退路径。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "screenshot_evidence"
FK_PARENT = "source_groups"
INDEX = "ux_screenshot_evidence_sha_group"
INDEX_COLUMNS = ("file_sha256", "source_group_id")

# 本迁移必须存在的列；缺任一 → 形状不可识别 → skipped（不做「补列」，那不在本迁移）
REQUIRED_COLUMNS = ("id", "file_sha256", "source_group_id")

# 四类引用（引用表, 引用列）——spec D3 逐字，不多不少。任何一类非 0 即不删。
REFERENCES = (
    ("assets", "source_evidence_id"),
    ("audit_log", "evidence_ref"),
    ("principal_changes", "evidence_ref"),
    ("transfer_events", "evidence_ref"),
)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _index_shape(conn, name: str):
    """索引形状 `(unique, [列名…])`；索引不存在 → None。

    `PRAGMA index_list` 的 3 列老版本与 5 列新版本都以 (seq, name, unique) 开头。
    """
    for row in conn.execute(f'PRAGMA index_list("{TABLE}")'):
        if row[1] == name:
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
            return bool(row[2]), cols
    return None


def _ref_counts(conn, ids: list[int]) -> dict[int, int]:
    """候选行 → 四类引用合计条数（缺失的引用表/列按 0 计并记 warning）。"""
    counts = {i: 0 for i in ids}
    if not ids:
        return counts
    placeholders = ",".join("?" * len(ids))
    for table, column in REFERENCES:
        if not _table_exists(conn, table):
            log.warning("0033：缺少引用表 %s，该表引用数按 0 计", table)
            continue
        if column not in _columns(conn, table):
            log.warning("0033：引用表 %s 缺少列 %s，该表引用数按 0 计", table, column)
            continue
        rows = conn.execute(
            f'SELECT "{column}", count(*) FROM "{table}"'
            f' WHERE "{column}" IS NOT NULL AND "{column}" IN ({placeholders})'
            f' GROUP BY "{column}"', tuple(ids)).fetchall()
        for ev_id, n in rows:
            if ev_id in counts:
                counts[ev_id] += int(n)
    return counts


def _duplicate_groups(conn) -> list[tuple]:
    """同（sha, 组）多行 → [(sha, group_id, 行数)]，按 sha/组排序。"""
    return conn.execute(
        f'SELECT file_sha256, source_group_id, count(*) FROM "{TABLE}"'
        " GROUP BY file_sha256, source_group_id HAVING count(*) > 1"
        " ORDER BY file_sha256, source_group_id").fetchall()


def _plan(conn) -> tuple[list[int], dict]:
    """规划删除：返回 (待删 id 升序, {(sha, 组): 保留 id})。

    判据（spec D3）逐组：保留引用数最多者（并列取 id 最小）；只删「同组还有同 sha
    的其它行」且「四类引用合计为 0」且「不是保留行」的行。三条全中才动手。
    """
    rows = conn.execute(
        f'SELECT id, file_sha256, source_group_id FROM "{TABLE}" ORDER BY id').fetchall()
    groups: dict[tuple, list[int]] = {}
    for ev_id, sha, gid in rows:
        groups.setdefault((sha, gid), []).append(ev_id)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dups:
        return [], {}

    counts = _ref_counts(conn, [i for ids in dups.values() for i in ids])
    to_delete: list[int] = []
    kept: dict[tuple, int] = {}
    for key in sorted(dups, key=lambda k: (k[0] or "", -1 if k[1] is None else k[1])):
        ids = dups[key]
        keep = min(ids, key=lambda i: (-counts.get(i, 0), i))
        kept[key] = keep
        to_delete.extend(i for i in ids if i != keep and counts.get(i, 0) == 0)
    return sorted(to_delete), kept


def _verify(conn) -> None:
    shape = _index_shape(conn, INDEX)
    if shape is None:
        raise RuntimeError(f"0033 中止：索引 {INDEX} 不存在")
    unique, columns = shape
    if not unique:
        raise RuntimeError(f"0033 中止：索引 {INDEX} 不是 UNIQUE（同名索引已存在但 unique=0）")
    if list(columns) != list(INDEX_COLUMNS):
        raise RuntimeError(
            f"0033 中止：索引 {INDEX} 列序不符（期望 {list(INDEX_COLUMNS)} / 实际 {columns}）")

    if _table_exists(conn, FK_PARENT):
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"0033 中止：foreign_key_check 非空 {problems[:5]}")
    else:
        # 桩库形状：无父表时 foreign_key_check 会直接报错，无从校验
        log.warning("0033：缺少外键父表 %s，跳过 foreign_key_check", FK_PARENT)

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0033 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        log.warning("0033 跳过：缺少 %s 表", TABLE)
        return "skipped"

    # ② 形状守卫：缺关键列 → 不是本迁移的目标形状（桩表/旁路同名表）
    cols = _columns(conn, TABLE)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        log.warning(
            "0033 跳过：%s 缺关键列 %s，结构不可识别（多为测试桩表或旁路自建同名表）",
            TABLE, missing)
        return "skipped"

    index_before = _index_shape(conn, INDEX) is not None
    total_before = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]

    # ③ 去重（只删「同组有同学 + 四类引用全 0 + 非保留行」，三条全中才动）
    to_delete, kept = _plan(conn)
    if to_delete:
        log.warning(
            "0033 去重：将删除 %d 行（同组同 sha 且四类引用全为 0），ids=%s；"
            "每组保留（引用最多、并列取 id 最小）：%s",
            len(to_delete), to_delete,
            {f"{str(sha)[:12]}/sg{gid}": keep for (sha, gid), keep in kept.items()})
        placeholders = ",".join("?" * len(to_delete))
        cur = conn.execute(
            f'DELETE FROM "{TABLE}" WHERE id IN ({placeholders})', tuple(to_delete))
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted != len(to_delete):
            raise RuntimeError(
                f"0033 中止：计划删除 {len(to_delete)} 行（{to_delete}），"
                f"实际删除 {deleted} 行")
        total_after = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
        if total_after != total_before - deleted:
            raise RuntimeError(
                f"0033 中止：行数对账不符（前 {total_before} - 删 {deleted} != 后 {total_after}）")
        log.info("0033 去重完成：%d → %d 行", total_before, total_after)

    # ④ 删完仍有重复（例如同组两行都被引用）→ 索引必然建不上，点名到人，不甩 SQL 原生错误
    remaining = _duplicate_groups(conn)
    if remaining:
        left = [f"{str(sha)[:12]}/sg{gid}×{n}" for sha, gid, n in remaining]
        raise RuntimeError(
            f"0033 中止：去重后仍有 {len(remaining)} 组同组同 sha，无法建唯一索引"
            f" {INDEX}。这些行不满足删除判据（多行都被引用），需人工处理：{left}")

    # ⑤ 建复合唯一索引（已存在则幂等跳过；形状在 ⑥ 里逐字校验）
    created = not index_before
    if created:
        conn.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{INDEX}" ON "{TABLE}"'
            f' ({", ".join(INDEX_COLUMNS)})')

    # ⑥ 迁移后校验
    _verify(conn)

    deduped = len(to_delete)
    log.info("0033: deduped=%d index=%s", deduped, "created" if created else "present")
    if deduped == 0 and not created:
        return "noop"
    return (f"deduped={deduped}"
            f" ids={','.join(str(i) for i in to_delete) or '-'}"
            f" index={'created' if created else 'present'}")