"""0031：教学规则拆表 + 四个历史死键清理（L3）。

规格：`.scratch/parser-rules-refactor/spec.md` D2.1 / D3 / D4（2026-11-05 冻结）
上游：ADR-0004（校正规则反哺层）；ADR-0020 D3（真理源收口范式）

背景：`source_groups.rules` 是一个 `Text` 列存 JSON，现在混着三类东西——

1. **组级解析配置**：`layout`（str）、`amount_header`（列表与字符串两种写法都出现过），
   保留在原列（它的生命周期是「这个组怎么解析」，与教学规则不同）；
2. **用户教学规则**：`rename[]` / `ignore[]` / `exclude[]`，四条写路径（`PUT /source-groups/{id}`
   整串替换、`PUT /{id}/parse-rules` 只 merge 四键、`POST .../correction-rules` 原子追加、
   测试脚本直写）互不知情，判重靠进程内比较（`source_group_rules_service.rule_identity`），
   没有来源、没有时间、无法统计命中；
3. **四个已死的历史键**：`name_col` / `total_header` / `amount_suffix` / `anchor_summary`
   ——后端零引用（grep 证据：`backend/` 全树仅 `tests/test_parser_amount_binding.py:68`
   的**测试函数名**里出现过 `total_header` 字样，无任何代码读取这四个键；前端
   `settings/source-groups/page.tsx:791` 只在提示文案里列出它们）。

本迁移做两件事，都是**只搬不改语义**：

- `rename` / `ignore` / `exclude` 三键逐条搬进新表 `source_group_learned_rules`
  （`origin='migration'`，`sort_order` = 数组下标——rename 靠它保持级联顺序），
  并从 JSON 里删除这三键；
- 顺手删除四个死键（spec D4：迁移顺带清理，避免后续 fail-fast 的「未知键 422」把
  存量库的组全部判成非法配置）。

记账沿用原列的那部分：**若删完只剩空 dict 则整列写 NULL**。理由：`rules=NULL` 与
`rules='{}'` 在旧读路径里等价（`get_group_config` 两者都得到空配置），但 D4 起读路径
要 fail-fast 校验合法键集合，「无配置」与「空配置」两种表示并存只会制造第二种歧义，
故统一归一成 NULL。

同时建好第二张表 `parser_vocabularies`（spec D2.1：全局解析词表的**增量覆盖**，
`生效词表 = (builtin ∪ add_items) − remove_items`，只存差额，未落库即全用出厂值）。
本期只建表，读写由 P1 的服务层接入。

否决过的方案（见 spec §2 D3）：① 继续混在 JSON（无法统计命中、无法记录来源与时间）；
② 用一张通用 `rules` 表存所有层（组级解析配置与教学规则的生命周期不同，混表会让
「删组」的级联语义变复杂）；③ 迁移时静默丢弃坏 JSON 的组（坏 JSON 意味着该组的教学
规则**早已静默失效**——`get_group_config` 解析失败即降级 `None`——跳过就等于把它永久
丢掉，故改为中止）。

守卫与安全：

- `source_groups` 不存在 → 返回 `skipped`（测试桩库会出现这种形状；建表也一并跳过，
  因为新表的外键指向它）。
- **坏 JSON / 非对象 / 三键不是数组 → 抛异常中止整个迁移**，消息只带来源组 id 与出错
  位置，**绝不打印 rules 内容**（里面有真实机构名与来源组标识，属公开仓库红线）。
  所有组**先全部解析完再开始写**，所以任何坏 JSON 都在第一次写入之前炸掉。
- **归一**：`exclude` / `ignore` 支持两种形态（裸 str 与 `{match, note}`），统一落成
  `match` 列；裸 str 的 `replace` 为 NULL。取不到 `match`（缺键 / 非字符串 / 空串）的
  条目跳过并计数——与运行期同口径（引擎对空 match 就是跳过，否则 `'' in name` 恒真会
  误伤整表）；纯空白的 match 运行期是照用的，故照样搬表，不改语义。
- **幂等**：插入用 `INSERT OR IGNORE`（吃新建的唯一索引），重推 JSON 只在真的删掉了
  键时才写回；二跑时三键已不存在、表里已有行 → 零写入，返回 `noop`。
- **对账**：迁移前按「归一后能落表的条数」逐型统计，迁移后要求
  `新表行数 = 迁移前 + 本次新增` 且 `表内 = JSON 应有的条数`，不符即中止。
- 事务由 runner（`services/db_migration_service._run_script`）负责，本脚本不 commit。
"""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

GROUP_TABLE = "source_groups"
RULE_TABLE = "source_group_learned_rules"
VOCAB_TABLE = "parser_vocabularies"

# 教学规则三键（搬表并从 JSON 删除）；顺序即 spec 的三型顺序，也是报数顺序
LEARNED_KEYS = ("rename", "ignore", "exclude")
# 后端零读取的历史死键（见模块 docstring 的 grep 证据）
DEAD_KEYS = ("name_col", "total_header", "amount_suffix", "anchor_summary")
# 一次遍历要摘掉的全部键
DROP_KEYS = LEARNED_KEYS + DEAD_KEYS

# 列定义必须与 models.py 逐字一致（表名/列名/约束名/索引名）。
# 与 `Base.metadata.create_all` 的产物逐列对照过：create_all 先行时下面的
# `IF NOT EXISTS` 全是 no-op，本 DDL 只服务「不经 create_all 的修复/重建路径」。
VOCAB_DDL = """
CREATE TABLE IF NOT EXISTS "parser_vocabularies" (
\t"key" TEXT NOT NULL,
\tadd_items TEXT NOT NULL,
\tremove_items TEXT NOT NULL,
\tupdated_at DATETIME NOT NULL,
\tPRIMARY KEY ("key")
)
"""

RULE_DDL = """
CREATE TABLE IF NOT EXISTS "source_group_learned_rules" (
\tid INTEGER NOT NULL,
\tsource_group_id INTEGER NOT NULL,
\trule_type TEXT NOT NULL,
\t"match" TEXT NOT NULL,
\t"replace" TEXT,
\tnote TEXT,
\tsort_order INTEGER NOT NULL,
\torigin TEXT NOT NULL,
\thit_count INTEGER NOT NULL,
\tlast_hit_at DATETIME,
\tcreated_at DATETIME NOT NULL,
\tupdated_at DATETIME NOT NULL,
\tPRIMARY KEY (id),
\tCONSTRAINT ck_sglr_type CHECK (rule_type IN ('rename','ignore','exclude')),
\tCONSTRAINT ck_sglr_origin CHECK (origin IN ('manual','import','migration')),
\tFOREIGN KEY(source_group_id) REFERENCES source_groups (id) ON DELETE CASCADE
)
"""

INDEX_DDLS = (
    'CREATE INDEX IF NOT EXISTS ix_sglr_group'
    f' ON "{RULE_TABLE}" (source_group_id, sort_order)',
    # replace 可空，SQLite 里 NULL 互不相等，普通唯一约束会放过重复行 → 用 COALESCE 归一
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_sglr_group_type_match_replace'
    f' ON "{RULE_TABLE}" (source_group_id, rule_type, "match", COALESCE("replace", \'\'))',
)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _create_schema(conn) -> None:
    for ddl in (VOCAB_DDL, RULE_DDL, *INDEX_DDLS):
        conn.execute(ddl)


def _new_stats() -> dict:
    return {
        "missing_match": 0,        # 缺 match 键 / 非字符串 / 空串——与引擎同口径跳过
        "bad_shape": 0,            # 形态不认识（rename 里出现裸 str、条目是数字等）
        "rename_without_replace": 0,  # rename 缺 replace：行照落（保住 match），但记一笔
        "duplicates": 0,           # 同组同型同 (match, replace) 的重复条目（唯一索引会并）
        "already_present": 0,      # 新表里已有的同键行（二跑/增量场景）
    }


def _abort(msg: str) -> "RuntimeError":
    return RuntimeError(f"0031 中止：{msg}")


def _parse_group_rules(gid: int, raw: str) -> dict:
    """解析一个组的 rules。坏 JSON / 非对象一律中止（不打印内容）。"""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise _abort(
            f"来源组 id={gid} 的 rules 不是合法 JSON（{type(e).__name__}）。"
            "坏 JSON 意味着该组的教学规则早已静默失效（get_group_config 解析失败即降级"
            " None），跳过迁移等于把它永久丢掉；请先人工修复该组 rules（内容不入日志），"
            "再重跑本迁移。"
        ) from e
    if not isinstance(parsed, dict):
        raise _abort(
            f"来源组 id={gid} 的 rules 解析结果不是对象（{type(parsed).__name__}），"
            "无法按三键搬迁。请先人工修复该组 rules，再重跑本迁移。"
        )
    return parsed


def _normalize_entry(rule_type: str, entry, idx: int, stats: dict) -> dict | None:
    """单条教学规则归一成待插入行；返回 None 表示跳过（并已计数）。"""
    replace = note = None
    if isinstance(entry, dict):
        match = entry.get("match")
        note = entry.get("note")
        replace = entry.get("replace")
    elif isinstance(entry, str) and rule_type != "rename":
        # 旧写法：ignore/exclude 允许裸字符串；rename 只认对象（引擎对裸 str 直接 continue，
        # 那种条目本来就是死配置，这里同理跳过并计数）
        match = entry
    else:
        stats["bad_shape"] += 1
        return None

    if not isinstance(match, str) or not match:
        # 只跳过**真正的空串**：运行期 `if sub` 对空串是 falsy（`'' in name` 恒真会误伤整表），
        # 纯空白则照旧当子串用。迁移不改语义，纯空白条目照样搬表。
        stats["missing_match"] += 1
        return None
    if not isinstance(note, str):
        note = None
    if rule_type == "rename":
        if not isinstance(replace, str):
            stats["rename_without_replace"] += 1
            replace = None
    else:
        replace = None            # 只有 rename 用 replace
    return {"rule_type": rule_type, "match": match, "replace": replace,
            "note": note, "sort_order": idx}


def _rows_for_group(gid: int, parsed: dict, stats: dict) -> list[dict]:
    """一个组的三键 → 归一后的待插入行（同键去重，保留首次出现的下标）。"""
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for rule_type in LEARNED_KEYS:
        if rule_type not in parsed:
            continue
        arr = parsed[rule_type]
        if arr is None:
            continue              # "exclude": null → 视为空，键仍然会被摘掉
        if not isinstance(arr, list):
            raise _abort(
                f"来源组 id={gid} 的 rules.{rule_type} 不是数组（{type(arr).__name__}），"
                "无法逐条搬迁。请先人工修复该组 rules，再重跑本迁移。"
            )
        for idx, entry in enumerate(arr):
            row = _normalize_entry(rule_type, entry, idx, stats)
            if row is None:
                continue
            key = (row["rule_type"], row["match"], row["replace"] or "")
            if key in seen:
                stats["duplicates"] += 1
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _count_by_type(conn) -> dict[str, int]:
    counts = {t: 0 for t in LEARNED_KEYS}
    for rule_type, n in conn.execute(
            f'SELECT rule_type, count(*) FROM "{RULE_TABLE}" GROUP BY rule_type'):
        if rule_type in counts:
            counts[rule_type] = int(n)
    return counts


def _existing_keys(conn) -> set[tuple[int, str, str, str]]:
    return {
        (int(gid), rule_type, match, rep or "")
        for gid, rule_type, match, rep in conn.execute(
            f'SELECT source_group_id, rule_type, "match", COALESCE("replace", \'\')'
            f' FROM "{RULE_TABLE}"')
    }


def _count_orphans(conn) -> int:
    """新表里指向不存在来源组的行数（迁移不该造出这种行）。"""
    return conn.execute(
        f'SELECT count(*) FROM "{RULE_TABLE}" r WHERE NOT EXISTS'
        f' (SELECT 1 FROM "{GROUP_TABLE}" g WHERE g.id = r.source_group_id)'
    ).fetchone()[0]


def _rewrite_json(conn, gid: int, parsed: dict) -> str:
    """从 JSON 里摘掉三键 + 四死键。

    返回 `"none"`（没有要摘的键，不写回）/ `"written"`（写回剩余键）/ `"nulled"`
    （摘完为空 → 整列写 NULL）。
    """
    kept = {k: v for k, v in parsed.items() if k not in DROP_KEYS}
    if len(kept) == len(parsed):
        return "none"             # 本来就没有要摘的键：不写回（保持幂等零副作用）
    if not kept:
        conn.execute(f'UPDATE "{GROUP_TABLE}" SET rules = NULL WHERE id = ?', (gid,))
        return "nulled"
    conn.execute(f'UPDATE "{GROUP_TABLE}" SET rules = ? WHERE id = ?',
                 (json.dumps(kept, ensure_ascii=False), gid))
    return "written"


def upgrade(conn) -> str:
    if not _table_exists(conn, GROUP_TABLE):
        # 桩库/不完整库：新表外键指向该表，建了也没用。真跑应用时 ORM 会立刻暴露缺表。
        log.warning("0031 跳过：缺少 %s 表", GROUP_TABLE)
        return "skipped"

    _create_schema(conn)

    stats = _new_stats()
    source_rows = conn.execute(
        f'SELECT id, rules FROM "{GROUP_TABLE}"'
        " WHERE rules IS NOT NULL AND trim(rules) <> '' ORDER BY id"
    ).fetchall()

    # 先全部解析+归一，再开始任何写入：坏 JSON 必须在第一次写入之前炸掉
    staged: list[tuple[int, dict, list[dict]]] = []
    expected = {t: 0 for t in LEARNED_KEYS}
    for gid, raw in source_rows:
        gid = int(gid)
        parsed = _parse_group_rules(gid, raw)
        rows = _rows_for_group(gid, parsed, stats)
        for row in rows:
            expected[row["rule_type"]] += 1
        staged.append((gid, parsed, rows))

    before = _count_by_type(conn)
    orphans_before = _count_orphans(conn)
    existing = _existing_keys(conn)
    now = datetime.now().isoformat()
    inserted = {t: 0 for t in LEARNED_KEYS}
    already = {t: 0 for t in LEARNED_KEYS}
    for gid, _parsed, rows in staged:
        for row in rows:
            key = (gid, row["rule_type"], row["match"], row["replace"] or "")
            if key in existing:
                stats["already_present"] += 1
                already[row["rule_type"]] += 1
                continue
            cur = conn.execute(
                f'INSERT OR IGNORE INTO "{RULE_TABLE}"'
                ' (source_group_id, rule_type, "match", "replace", note, sort_order,'
                '  origin, hit_count, created_at, updated_at)'
                " VALUES (?,?,?,?,?,?, 'migration', 0, ?, ?)",
                # 列的默认值在 ORM 侧（Python 端 default=），裸 SQL 不填就是 NOT NULL 违反，
                # 而 `OR IGNORE` 会把这种违反一并吞掉 —— 下面的 rowcount 检查就是拦这个。
                (gid, row["rule_type"], row["match"], row["replace"],
                 row["note"], row["sort_order"], now, now),
            )
            if not cur.rowcount:
                # INSERT OR IGNORE 吞掉了「内存里认为不存在」的行：唯一索引口径与
                # _existing_keys 的取数口径出现了分叉，宁可中止也不要把规则丢掉。
                raise _abort(
                    f"来源组 id={gid} 的 {row['rule_type']} 规则被唯一索引忽略"
                    "（键已存在于新表但未被预检命中），请人工核查。")
            existing.add(key)
            inserted[row["rule_type"]] += 1

    after = _count_by_type(conn)
    for rule_type in LEARNED_KEYS:
        # ① 表内 = JSON 应有的条数（每条都能落表，没有被唯一索引静默吃掉）
        if inserted[rule_type] + already[rule_type] != expected[rule_type]:
            raise _abort(
                f"{rule_type} 规则对账不符：JSON 应有可落表条数 {expected[rule_type]}，"
                f"实际 本次新增 {inserted[rule_type]} + 表内已有 {already[rule_type]}")
        # ② 行数对账：迁移后 = 迁移前 + 本次新增（二跑时三者相等）
        if after[rule_type] != before[rule_type] + inserted[rule_type]:
            raise _abort(
                f"{rule_type} 规则行数对账不符：迁移前 {before[rule_type]} + 本次新增 "
                f"{inserted[rule_type]} != 迁移后 {after[rule_type]}")

    rewritten = nulled = 0
    for gid, parsed, _rows in staged:
        status = _rewrite_json(conn, gid, parsed)
        if status == "none":
            continue
        rewritten += 1
        if status == "nulled":
            nulled += 1

    orphans = _count_orphans(conn)
    if orphans > orphans_before:
        # 迁移只搬「已存在组的规则」，凭空造出悬空引用＝这里的取数/写入有 bug，宁可中止
        raise _abort(
            f"新表出现 {orphans - orphans_before} 条指向不存在来源组的规则"
            f"（总悬空 {orphans}），请人工核查。")

    skipped = sum(stats[k] for k in ("missing_match", "bad_shape", "rename_without_replace"))
    total_inserted = sum(inserted.values())
    log.info(
        "learned_rules_split: inserted=%d (rename=%d ignore=%d exclude=%d)"
        " rewritten=%d nulled=%d already_present=%d skipped=%d duplicates=%d",
        total_inserted, inserted["rename"], inserted["ignore"], inserted["exclude"],
        rewritten, nulled, stats["already_present"], skipped, stats["duplicates"])

    if total_inserted == 0 and rewritten == 0:
        return "noop"             # 二跑：三键已搬完、JSON 已净，零写入
    return (
        f"inserted={total_inserted}"
        f" (rename={inserted['rename']}, ignore={inserted['ignore']},"
        f" exclude={inserted['exclude']})"
        f" rewritten={rewritten} nulled={nulled}"
        f" already_present={stats['already_present']}"
        f" skipped={skipped} duplicates={stats['duplicates']}"
        f" orphans={orphans}"
    )