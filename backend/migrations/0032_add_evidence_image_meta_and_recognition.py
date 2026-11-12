"""0032：凭证两列（`image_meta` / `recognition_json`）+ 清空脏的原始文件名（L2）。

规格：`.scratch/evidence-naming/spec.md` D2 / D3 / D5（2026-11-05 冻结）
上游：ADR-0022（凭证文件名由服务端生成，原图信息存入独立 `image_meta` 列）
      ADR-0023（凭证行留存识别快照 `recognition_json`，供日后识别率检查）

背景：`screenshot_evidence` 是凭证的唯一元数据表，只有 9 列——`file_path` /
`file_sha256` / `original_filename` / `data_date` / `supplement_rows` / `engine` /
`created_at` 加主键与来源组外键。两个空档：

1. **没有原图信息**（像素尺寸、字节数、格式、EXIF 子集）。于是回答不了「库里这张
   是不是原图、多大、什么时候被谁截的」。`image_meta` 存**白名单** JSON
   （`width/height/bytes/format` + `exif.DateTimeOriginal/Make/Model/Software/
   Orientation`），**不存 GPS**（无业务价值，位置信息不该在库里多留一份）。
2. **没有识别结果**（OCR 行 / AI 行）。`preview_jobs` 里有过的只是短期缓存、跑完即弃，
   账本只留最终采纳值——「这次识别得好不好」在采纳之后无从复核。
   `recognition_json` 存一次识别行为的快照（`schema:1`，`recognized` 与 `adopted`
   两段对照），与凭证行共存亡。

第三件事是数据清理（spec D3，业主 2026-11-05 裁决）：运行库 28 条凭证里有 **23 条**
`original_filename` 是 `shot_upload_<8 位随机>.jpg`——即服务端
`tempfile.mkstemp(prefix="shot_upload_")` 的临时名。这些名字没有信息量（不是客户端
原名，也不指向任何可读内容），按「不推断、不伪造」原则**直接置 NULL**，而不是猜一个
看起来合理的名字。真正的客户端原名只有 5 条，本迁移**不动**它们。

本迁移做三件事，**不动任何文件**：

- `ALTER TABLE screenshot_evidence ADD COLUMN image_meta TEXT`；
- `ALTER TABLE screenshot_evidence ADD COLUMN recognition_json TEXT`；
- `UPDATE screenshot_evidence SET original_filename = NULL
  WHERE original_filename LIKE 'shot_upload_%'`（受影响行数计入返回值）。

两列都是可空 TEXT，加列是 SQLite 原生能力，**不做表重建**（重建要摘 4 处外键、
重跑索引与 CHECK，风险与收益不成比例；对比 0030 删列的 12 步重建只是不得已）。

存量 `image_meta` 回填与文件改名**不在本迁移**：文件移动不可事务化，由独立脚本
（`backend/scripts/rename_evidence_files.py` 与元数据回填脚本）在迁移之后单独跑，
顺序无关。`file_path` 本迁移一个字节都不改。

守卫与安全：

- `screenshot_evidence` 不存在 → `skipped`（桩库/不完整库形状）。
- 表存在但缺基准列（无 `source_group_id` / `file_path` 等）→ `skipped`：这种表既不是
  0030 之后的目标形状，也不是本迁移的活，多为测试桩表或旁路程序自建的同名表
  （沿用 0029/0030「形状不在预期则幂等跳过」的先例；fail-fast 只留给
  数据/结构与断言不一致）。
- **逐列判定**：两列均已存在 → 不加列（幂等）；只存在一列 → **只补缺的那列**，
  整体不跳（手工补过一列或上次中断的库都能前进）。
- 脏名 `UPDATE` 每次都跑，按其受影响行数报告——二跑影响 0 行。
  返回 `noop` 的判据是「本次没有加任何列 **且** 脏名清理影响 0 行」。
- 迁移后校验：列集合逐字（11 列，两列均为 TEXT 可空）→ `foreign_key_check` 空
  （父表 `source_groups` 在才校验，桩库无父表时记 warning 跳过）→ `integrity_check=ok`。
  任一不符即抛错中止。
- 事务由 runner（`services/db_migration_service._run_script`）负责，本脚本不 commit；
  抛错 → 整个脚本回滚，加列与清名的原子性由 runner 的事务保证。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "screenshot_evidence"
FK_PARENT = "source_groups"

# 0030 重建后的 9 列（列序＝实际 DDL 顺序）。缺任一列 → 不是本迁移的目标形状 → skipped
BASE_COLUMNS = [
    "id",
    "source_group_id",
    "file_path",
    "file_sha256",
    "original_filename",
    "data_date",
    "supplement_rows",
    "engine",
    "created_at",
]
# 本迁移新增的两列（顺序即 ALTER 顺序，也是 create_all 之后的物理顺序）
NEW_COLUMNS = [
    ("image_meta", "TEXT"),
    ("recognition_json", "TEXT"),
]
EXPECTED_COLUMNS = BASE_COLUMNS + [name for name, _ in NEW_COLUMNS]

# 脏原始名的前缀（spec D3）。注意 SQL `LIKE` 里 `_` 是单字符通配符，故该模式比正则
# `^shot_upload_` 略宽（可吃到 `shotXuploadY…`）——按 spec D5 逐字保留该写法；真实
# 客户端原名（如 `Screenshot_20260101_…`）第 2 个字符就不是 `h`，不可能被命中。
DIRTY_NAME_LIKE = "shot_upload_%"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _column_def(conn, table: str, column: str):
    """(type, notnull) —— 只能用来确认「加的是可空 TEXT」，别当结构比对用。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == column:
            return (r[2] or "").upper(), int(r[3])
    return None


def _clean_dirty_filenames(conn) -> int:
    """清空临时文件名式的 `original_filename`，返回受影响行数（二跑 0）。"""
    cur = conn.execute(
        f'UPDATE "{TABLE}" SET original_filename = NULL'
        f" WHERE original_filename LIKE '{DIRTY_NAME_LIKE}'"
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _verify(conn) -> None:
    cols = _columns(conn, TABLE)
    if sorted(cols) != sorted(EXPECTED_COLUMNS):
        raise RuntimeError(
            f"0032 中止：加列后列集合不符（期望 11 列 {EXPECTED_COLUMNS} / 实际 {cols}）"
        )
    for name, _ddl in NEW_COLUMNS:
        got = _column_def(conn, TABLE, name)
        if got != ("TEXT", 0):
            raise RuntimeError(
                f"0032 中止：{TABLE}.{name} 的类型/可空性不符（期望 TEXT 可空 / 实际 {got}）"
            )

    if _table_exists(conn, FK_PARENT):
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"0032 中止：foreign_key_check 非空 {problems[:5]}")
    else:
        # 桩库形状：无父表时 foreign_key_check 会直接报错，无从校验
        log.warning("0032：缺少外键父表 %s，跳过 foreign_key_check", FK_PARENT)

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0032 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        log.warning("0032 跳过：缺少 %s 表", TABLE)
        return "skipped"

    # ② 形状守卫：缺基准列 → 不是本迁移的目标形状（桩表/旁路同名表）
    cols = _columns(conn, TABLE)
    missing = [c for c in BASE_COLUMNS if c not in cols]
    if missing:
        log.warning(
            "0032 跳过：%s 缺基准列 %s，结构不可识别（多为测试桩表或旁路自建同名表）",
            TABLE, missing)
        return "skipped"

    # ③ 逐列判定加列：两列都在 → 不加；只缺一列 → 只补那一列（整体不跳）
    added: list[str] = []
    for name, ddl in NEW_COLUMNS:
        if name in cols:
            continue
        conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" {ddl}')
        added.append(name)

    # ④ 脏原始名清理（D3 的迁移部分）：每次都跑，二跑影响 0 行
    cleaned = _clean_dirty_filenames(conn)

    # ⑤ 迁移后校验
    _verify(conn)

    log.info("add_evidence_image_meta_and_recognition: added=%s cleaned_original_filename=%d",
             added or "-", cleaned)
    if not added and cleaned == 0:
        return "noop"
    return (f"added={','.join(added) or '-'}"
            f" cleaned_original_filename={cleaned}")