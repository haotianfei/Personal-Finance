#!/usr/bin/env python3
"""归档项不参与录入侧 · 端到端验收（spec `.scratch/archived-ingest-scope/spec.md` §3 五步）。

把五步验收跑成 PASS / FAIL / SKIP 报告，用于改动后回归与交付自查：

    ① 未识别项（keep / zero 行）不含归档项（按 item_id 与按名称双验，并用现值交叉核对）
    ② 归档名 → create 行 + `archived_merged_into` 血缘提示（无 merge 血缘时提示应为 None）
    ③ draft 项仍可 update（或可 bind）——本库无 draft 时打印 SKIP，绝不造数据
    ④ apply 双向：(a) 归档名按 create 提交；(b) 显式 target_item_id 指向归档项 → ITEM_ARCHIVED
    ⑤ 锚点等式：total_current_system == 在用现值之和 == 全量现值之和 − 归档项现值之和

跑法（无需先启动后端、不跑 OCR、不起 HTTP 服务）：

    .venv/bin/python scripts/verify_archived_ingest_scope.py
    .venv/bin/python scripts/verify_archived_ingest_scope.py --keep-copy
    .venv/bin/python scripts/verify_archived_ingest_scope.py --group 3
    python3 scripts/verify_archived_ingest_scope.py -h      # 缺依赖时会自动切到 .venv

红线（硬约束，违反即报废）
--------------------------
1. **绝不写现役库**：`data/person_f_dev.db` 只读复制（连同 `-wal` / `-shm`），所有写入只发生在
   副本上。开头即断言实际使用的库路径在临时目录内、且不等于仓库 `data/` 下任何文件；
   副本目录在 `finally` 里删除（`--keep-copy` 时保留并打印路径）。
2. **公开安全**：输出**不打印**任何真实资产项名（`display_name` / `asset_name`）、真实机构名、
   真实金额数值。只打印行数、`asset_items.id`、布尔值、等式是否成立，以及脱敏占位
   （`归档项#14` / `在用项#13` / `新项#136`）。金额只给出「等式成立/不成立」与计数差。
   源码里也没有任何真实机构名——归档项一律按 `status='archived'` 动态定位。
3. 只读现役库、只写副本：脚本不进 git、不改任何既有文件。

实现口径（读它再看断言）
------------------------
- 直接调用 `routers.screenshots` 的 `screenshot_preview` / `screenshot_apply` 路由函数（async），
  用 `Session` 显式传入副本库；不启 uvicorn、不走 HTTP、不跑 OCR。
- **识别行怎么进管线**：本仓库 `engine="ai"` 分支按设计以**空骨架**进 `compute_preview`
  （`routers/screenshots.py:159-160`），`recognition` JSON 只进凭证快照、**不生成预览行**。
  因此这里用 `parse_screenshot` 替身把同一份识别行喂进管线（等价「AI 回传的行」，且不跑 OCR）；
  `recognition` 表单字段照传，顺带覆盖 `parse_client_recognition` 的形状校验与存证链路。
- 凭证根被重定向到副本目录（`routers.screenshots._evidence_root`），临时文件也落在副本目录
  （`tempfile.tempdir`），仓库 `data/evidence` 一个字节都不写。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 仓库根
BACKEND = ROOT / "backend"
DEFAULT_DB = ROOT / "data" / "person_fin_dev.db"
DATA_DIR = ROOT / "data"
# 环境已批准的临时根（不使用 /tmp）：可用 --tmp-root 覆盖
APPROVED_TMP_ROOT = Path("~/.cache/tmp/opencode").expanduser()

EXIT_PASS, EXIT_FAIL = 0, 1
STATUS_PASS, STATUS_FAIL, STATUS_SKIP = "PASS", "FAIL", "SKIP"
SYNTH_DATE = "2026-11-10"        # 合成数据日期（只落在副本里）
SYNTH_AMOUNT = Decimal("100.00")  # 合成金额（不是任何真实持仓数值）


# ================================================================ 报告
class Report:
    """五步结论收集器：每步一条 PASS/FAIL/SKIP + 若干缩进事实行。

    同时把全部上屏文本存进 `lines`，用于末尾的「脱敏自检」——一旦有任何库内名称
    （资产项名 / 来源组名）混进输出，就判失败并大声报错（公开安全红线兜底）。
    """

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, list[str]]] = []
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        self.lines.append(text)
        print(text)

    def add(self, tag: str, status: str, fact: str, details: list[str] | None = None) -> None:
        self.items.append((tag, status, fact, details or []))

    def ok(self, tag: str, fact: str, details: list[str] | None = None) -> None:
        self.add(tag, STATUS_PASS, fact, details)

    def bad(self, tag: str, fact: str, details: list[str] | None = None) -> None:
        self.add(tag, STATUS_FAIL, fact, details)

    def skip(self, tag: str, fact: str, details: list[str] | None = None) -> None:
        self.add(tag, STATUS_SKIP, fact, details)

    def render(self) -> None:
        for tag, status, fact, details in self.items:
            self.emit(f"{tag} {status}  {fact}")
            for line in details:
                self.emit(f"    · {line}")

    @property
    def failed(self) -> bool:
        return any(status == STATUS_FAIL for _, status, _, _ in self.items)

    def summary_line(self) -> str:
        counts = {s: sum(1 for _, st, _, _ in self.items if st == s)
                  for s in (STATUS_PASS, STATUS_FAIL, STATUS_SKIP)}
        return (f"PASS {counts[STATUS_PASS]} / FAIL {counts[STATUS_FAIL]}"
                f" / SKIP {counts[STATUS_SKIP]}")


# ================================================================ 前置：解释器 / 参数
def _ensure_interpreter(argv: list[str]) -> None:
    """缺依赖时自动切到仓库 `.venv/bin/python`（后端依赖只装在那里）。"""
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        venv = ROOT / ".venv" / "bin" / "python"
        if venv.exists() and os.environ.get("VERIFY_REEXEC") != "1":
            os.environ["VERIFY_REEXEC"] = "1"
            os.execv(str(venv), [str(venv), str(Path(__file__).resolve()), *argv])
        raise SystemExit(
            "缺少后端依赖（fastapi）。请用仓库虚拟环境运行："
            "\n    .venv/bin/python scripts/verify_archived_ingest_scope.py")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_archived_ingest_scope.py",
        description=(
            "归档项不参与录入侧 · 端到端验收（spec §3 五步）。\n"
            "现役库只读复制到临时目录，全部写入只发生副本上；副本结束即删（--keep-copy 保留）。\n"
            "输出不打印任何真实资产项名 / 机构名 / 金额数值，只给行数、id、布尔与等式结论。"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 = 全 PASS 或仅 SKIP；1 = 任一 FAIL。\n"
            "示例：\n"
            "  .venv/bin/python scripts/verify_archived_ingest_scope.py\n"
            "  .venv/bin/python scripts/verify_archived_ingest_scope.py --keep-copy\n"))
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="现役库路径（只读复制，绝不直接连接）；默认 data/person_f_dev.db")
    parser.add_argument("--group", type=int, default=None,
                        help="来源组 id；缺省自动挑「组内归档项最多」的组")
    parser.add_argument("--keep-copy", action="store_true",
                        help="保留库副本目录（默认在 finally 里删除）")
    parser.add_argument("--tmp-root", default=str(APPROVED_TMP_ROOT),
                        help=f"临时根目录（默认 {APPROVED_TMP_ROOT}，不用 /tmp）")
    return parser.parse_args(argv)


# ================================================================ 副本与沙箱断言
def snapshot_db(src: Path, workdir: Path) -> Path:
    """把现役库（含 -wal/-shm）复制到临时目录；返回副本路径。绝不连接现役库。"""
    if not src.is_file():
        raise SystemExit(f"现役库不存在：{src}")
    dest = workdir / "snapshot.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(src) + suffix)
        if candidate.is_file():
            shutil.copy2(candidate, Path(str(dest) + suffix))
    # 一致性预检：WAL 复制瞬息窗口可能带来残帧，SQLite 会自行截断非法帧；
    # 这里再跑一次 quick_check，坏了就直接报错（绝不产出「看起来像真的」报告）。
    con = sqlite3.connect(str(dest))
    try:
        verdict = con.execute("PRAGMA quick_check").fetchone()
    finally:
        con.close()
    if not verdict or verdict[0] != "ok":
        raise SystemExit(
            f"库副本 quick_check 未通过（{verdict[0] if verdict else 'unknown'}）；"
            "可能复制期间有写入，请重跑")
    return dest


def assert_sandboxed(db_path: Path, tmp_root_raw: Path) -> Path:
    """硬约束 1：实际使用的库必须在临时目录内，且 ≠ 仓库 data/ 下任何文件。"""
    resolved = db_path.resolve()
    tmp_root = tmp_root_raw.resolve()
    if tmp_root != resolved.parent and tmp_root not in resolved.parents:
        raise SystemExit(f"库路径不在批准的临时目录内，拒绝继续：{resolved}（要求位于 {tmp_root} 之下）")
    if resolved.parent == DATA_DIR.resolve() or DATA_DIR.resolve() in resolved.parents:
        raise SystemExit(f"库路径落在仓库 data/ 下，拒绝继续：{resolved}")
    for candidate in DATA_DIR.glob("**/*.db*"):
        if candidate.is_file() and candidate.resolve() == resolved:
            raise SystemExit(f"库路径与其仓库内文件相同，拒绝继续：{resolved}")
    if not resolved.is_file():
        raise SystemExit(f"库副本不存在：{resolved}")
    return resolved


def leak_check(db, lines: list[str]) -> list[str]:
    """脱敏自检：输出里不得出现任何库内名称（资产项名 / 历史名 / 别名 / 来源组名）。

    只回答「命中几处」，**绝不回显命中的名称本身**（否则自检自己就成了泄漏源）。
    """
    from sqlalchemy import select

    from models import Asset, AssetItem, AssetItemName, SourceGroup

    needles: set[str] = set()
    for name in db.execute(select(AssetItem.display_name)).scalars():
        needles.add(name or "")
    for name in db.execute(select(AssetItem.legacy_name)).scalars():
        needles.add(name or "")
    for name in db.execute(select(AssetItemName.name)).scalars():
        needles.add(name or "")
    for name in db.execute(select(Asset.asset_name).distinct()).scalars():
        needles.add(name or "")
    for label, slug in db.execute(select(SourceGroup.label, SourceGroup.slug)):
        needles.add(label or "")
        needles.add(slug or "")
    needles = {n for n in needles if len(n) >= 3}
    text = "\n".join(lines)
    return [n for n in sorted(needles) if n in text]


# ================================================================ 数据探针（全部只读）
def sql_latest_rows_by_name(db, prefix: str) -> list[tuple[str, int | None, Decimal]]:
    """组内「每个名称的最后一笔有效行」——独立实现（刻意不带归档过滤，用于交叉核对）。"""
    from sqlalchemy import and_, func, select

    from models import Asset

    sub = (select(Asset.asset_name, func.max(Asset.asset_date).label("d"))
           .where(Asset.asset_name.like(prefix + "%"), Asset.superseded_by.is_(None))
           .group_by(Asset.asset_name).subquery())
    rows = db.execute(
        select(Asset.asset_name, Asset.asset_item_id, Asset.amount)
        .join(sub, and_(Asset.asset_name == sub.c.asset_name,
                        Asset.asset_date == sub.c.d,
                        Asset.superseded_by.is_(None)))
        .where(Asset.asset_name.like(prefix + "%"))
    ).all()
    return [(name, item_id, Decimal(str(amount))) for name, item_id, amount in rows]


def pick_group(db, want_id: int | None):
    """挑来源组：缺省取「组内归档项最多」的组（每组前缀只从库里读，源码不硬编码）。"""
    from sqlalchemy import func, select

    from models import Asset, AssetItem, SourceGroup

    archived_ids = list(db.execute(
        select(AssetItem.id).where(AssetItem.status == "archived").order_by(AssetItem.id)
    ).scalars())
    best = None
    for group in db.execute(select(SourceGroup).order_by(SourceGroup.id)).scalars():
        if want_id is not None and group.id != want_id:
            continue
        archived_hits = 0
        for item_id in archived_ids:
            n = db.execute(
                select(func.count()).select_from(Asset)
                .where(Asset.asset_item_id == item_id,
                       Asset.asset_name.like(group.asset_prefix + "%"))
            ).scalar()
            if n:
                archived_hits += 1
        score = (archived_hits, -group.id)
        if best is None or score > best[0]:
            best = (score, group, archived_ids)
    if best is None:
        raise SystemExit("库里没有可用的来源组" + (f"（--group {want_id} 不存在）" if want_id else ""))
    _score, group, archived_ids = best
    return group, archived_ids


def archived_items_in_group(db, prefix: str, archived_ids: list[int]) -> list[dict]:
    """组内归档项：行名集合、最新一笔、merge 血缘目标（只读，不打印任何名称）。"""
    from sqlalchemy import select

    from models import Asset, AssetItem, LineageLink

    out: list[dict] = []
    for item_id in archived_ids:
        item = db.get(AssetItem, item_id)
        if item is None:
            continue
        names = list(db.execute(
            select(Asset.asset_name).where(Asset.asset_item_id == item_id).distinct()
        ).scalars())
        group_names = [n for n in names if n.startswith(prefix)]
        if not group_names:
            continue                                   # 该项不在本组名下，跳过
        latest = db.execute(
            select(Asset.asset_name, Asset.amount)
            .where(Asset.asset_item_id == item_id, Asset.superseded_by.is_(None))
            .order_by(Asset.asset_date.desc()).limit(1)
        ).first()
        merge_target = db.execute(
            select(LineageLink.to_item_id)
            .where(LineageLink.from_item_id == item_id, LineageLink.link_type == "merge")
            .order_by(LineageLink.id).limit(1)
        ).scalars().first()
        out.append({
            "id": item_id,
            "display_name": item.display_name,
            "status": item.status,
            "group_names": sorted(group_names),
            "latest_name": latest[0] if latest else None,
            "latest_amount": Decimal(str(latest[1])) if latest else Decimal("0"),
            "merge_target": merge_target,
        })
    return out


def hit_short_name(db, prefix: str, entry: dict) -> tuple[str | None, str]:
    """归档项 → 「OCR 会读到的短名」（去掉组前缀）；解析不指向本项时返回 (None, 原因)。"""
    from services import screenshot_service as ss

    names: list[str] = []
    if entry["display_name"] and entry["display_name"].startswith(prefix):
        names.append(entry["display_name"])
    names += [n for n in entry["group_names"] if n not in names]
    names += [n for n in ([entry["latest_name"]] if entry["latest_name"] else []) if n not in names]
    for name in names:
        if ss.resolve_item_id(db, name) != entry["id"]:
            continue
        return name[len(prefix):], "ok"
    return None, "该归档项的组内名称解析不指向本项（有同名在用项或别名冲突）"


# ================================================================ 管线驱动（无 HTTP / 无 OCR）
class Pipeline:
    """用 `parse_screenshot` 替身驱动 preview / apply 路由函数（识别行＝AI 回传的行）。"""

    def __init__(self, db, group, *, original_parse=None) -> None:
        import routers.screenshots as shots
        from services import screenshot_service as ss

        self.shots, self.ss = shots, ss
        self.db, self.group = db, group
        self.prefix = group.asset_prefix
        # 真解析函数只捕获一次（多条管线并存时不被彼此的替身污染）
        self._original_parse = original_parse if original_parse is not None else ss.parse_screenshot
        self._items: list[tuple[str, Decimal]] = []

    # ---- 替身：识别行 → ParsedScreenshot（不跑 OCR、不读写 OCR 缓存）
    def _stub(self) -> None:
        ss, items = self.ss, list(self._items)

        def fake_parse(path, sha, rules, vocabularies=None, force_ocr=False, progress_cb=None):
            return ss.ParsedScreenshot(
                items=[ss.ParsedItem(name=name, amount=amount) for name, amount in items],
                total=sum((a for _n, a in items), Decimal("0")),
                date=None, raw_blocks=[], date_text=None)

        ss.parse_screenshot = fake_parse

    def restore(self) -> None:
        self.ss.parse_screenshot = self._original_parse

    def _recognition(self) -> str:
        return json.dumps([{"name": n, "amount": str(a)} for n, a in self._items],
                          ensure_ascii=False)

    @staticmethod
    def _upload():
        from starlette.datastructures import UploadFile

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # 假图：只用于算 sha，不进任何真实解析
        return UploadFile(filename="verify.png", file=io.BytesIO(png))

    # ---- 预览
    def preview(self, items: list[tuple[str, Decimal]], *, unrecognized: str = "keep"):
        import asyncio

        self._items = list(items)
        self._stub()
        return asyncio.run(self.shots.screenshot_preview(
            file=self._upload(), source_group=self.group.slug, unrecognized=unrecognized,
            force_ocr=False, engine="ocr", recognition=self._recognition(), db=self.db))

    # ---- 采纳
    def apply(self, items: list[tuple[str, Decimal]], *, decisions: dict | None = None,
              data_date: str = SYNTH_DATE):
        import asyncio

        self._items = list(items)
        self._stub()
        options = {"source_group": self.group.slug}
        if decisions:
            options["identity_decisions"] = decisions
        return asyncio.run(self.shots.screenshot_apply(
            file=self._upload(), options=json.dumps(options, ensure_ascii=False),
            data_date=data_date, recognition=self._recognition(), db=self.db))


# ================================================================ 五步断言
def step1_unrecognized(report: Report, db, prefix: str,
                       archived: list[dict], pipe: Pipeline) -> None:
    """① 未识别项（keep / zero）不含归档项：按 item_id、按名称、按金额三重交叉核对。"""
    tag = "①"
    archived_ids = {a["id"] for a in archived}
    archived_names = {n for a in archived for n in a["group_names"]}

    keep_prev = pipe.preview([], unrecognized="keep")
    zero_prev = pipe.preview([], unrecognized="all")
    keep_rows = [r for r in keep_prev.rows if r.action == "keep"]
    zero_rows = [r for r in zero_prev.rows if r.action == "zero"]
    rows = keep_rows + zero_rows

    bad_by_id = [r for r in rows if r.item_id in archived_ids]
    bad_by_name = [r for r in rows if r.asset_name in archived_names]
    # 第三重：名称再走一次解析单点，确认落到在用项（防「名称相同但挂了另一个项」）
    from services import screenshot_service as ss
    bad_by_resolve = [r for r in rows
                      if r.asset_name and ss.resolve_item_id(db, r.asset_name) in archived_ids]

    if not rows:
        report.skip(tag, "组内无可处置的在用项（keep/zero 行均为 0），无法验证未识别项口径")
        return

    # 金额交叉核对：在用现值之和 + 归档现值之和 == 全量现值之和；锚点只等于前者
    latest = sql_latest_rows_by_name(db, prefix)
    all_sum = sum((amount for _n, _i, amount in latest), Decimal("0"))
    archived_sum = sum((a["latest_amount"] for a in archived), Decimal("0"))
    in_use_sum = sum((amount for _n, item_id, amount in latest
                      if item_id not in archived_ids), Decimal("0"))
    anchor = keep_prev.total_current_system
    eq_sum = (in_use_sum + archived_sum == all_sum)
    eq_anchor = (anchor == in_use_sum)
    eq_exclude = (anchor == all_sum - archived_sum)

    details = [
        f"在用现值 + 归档现值 == 全量现值：{'成立' if eq_sum else '不成立'}；"
        f"锚点 == 在用现值：{'成立' if eq_anchor else '不成立'}；"
        f"锚点 == 全量 − 归档：{'成立' if eq_exclude else '不成立'}",
    ]
    if bad_by_id or bad_by_name or bad_by_resolve:
        report.bad(tag, f"未识别项含归档项：按 item_id 命中 {len(bad_by_id)} 行、"
                        f"按名称命中 {len(bad_by_name)} 行、按解析命中 {len(bad_by_resolve)} 行",
                   details)
        return
    if not (eq_sum and eq_anchor and eq_exclude):
        report.bad(tag, f"未识别项不含归档项，但对账等式不成立（合计核对失败）", details)
        return
    report.ok(tag, f"keep {len(keep_rows)} 行 / zero {len(zero_rows)} 行："
                   f"归档项 item_id 命中 0、归档名命中 0、解析命中 0；三式相等", details)


def _check_hint_rows(db, prefix: str, archived: list[dict], pipe: Pipeline
                     ) -> tuple[list[str], list[str], int, dict | None]:
    """在一个组上验 ② 的两个分支：create 行、血缘提示与 merge 目标一致。

    返回 (失败清单, 事实行, 命中个数, 首个可用用例)；用例形状
    {entry, short, row, preview} 供 ④(a) 复用。
    """
    hits: list[tuple[dict, str]] = []
    skipped: list[str] = []
    for entry in archived:
        short, why = hit_short_name(db, prefix, entry)
        if short is None:
            skipped.append(f"归档项#{entry['id']} 跳过：{why}")
        else:
            hits.append((entry, short))
    if not hits:
        return [], skipped, 0, None

    prev = pipe.preview([(short, SYNTH_AMOUNT) for _e, short in hits])
    by_source = {r.source_name: r for r in prev.rows}
    failures: list[str] = []
    details: list[str] = list(skipped)
    checked = 0
    for entry, short in hits:
        row = by_source.get(short)
        if row is None:
            failures.append(f"归档项#{entry['id']}：预览里没有该识别行")
            continue
        if row.action != "create":
            failures.append(f"归档项#{entry['id']}：action={row.action}"
                            "（应为 create，归档名不得生成 update）")
            continue
        checked += 1
        hint = row.archived_merged_into
        target = entry["merge_target"]
        if target is None:
            if hint is None:
                details.append(f"归档项#{entry['id']} → create；无血缘，提示为 None（符合预期）")
            else:
                failures.append(f"归档项#{entry['id']}：无 merge 血缘却给了提示（应为 None）")
            continue
        if hint is None:
            failures.append(f"归档项#{entry['id']}：有 merge 血缘（→ 在用项#{target}）但提示为 None")
        elif hint.item_id != target:
            failures.append(f"归档项#{entry['id']}：提示指向项#{hint.item_id}，"
                            f"与 lineage_links 的 merge 目标#{target} 不一致")
        else:
            details.append(f"归档项#{entry['id']} → create + 提示指向在用项#{hint.item_id}"
                           "（= merge 目标）")
    first = hits[0]
    ctx = {"entry": first[0], "short": first[1],
           "row": by_source.get(first[1]), "preview": prev}
    return failures, details, checked, ctx


def step2_create_with_hint(report: Report, db, group, prefix: str, archived: list[dict],
                           pipe: Pipeline, *, original_parse) -> tuple[object | None, dict | None]:
    """② 归档名 → create 行 + 血缘提示；本组全无 merge 血缘时跨组补验「有血缘」分支。"""
    tag = "②"
    failures, details, checked, ctx = _check_hint_rows(db, prefix, archived, pipe)
    extra: list[str] = []
    extra_checked = 0
    # 主组全是「无血缘」时，再找个有 merge 血缘的组把非 None 分支也验掉（只读）
    if not any(a["merge_target"] for a in archived):
        sibling, sibling_prefix, sibling_archived = _group_with_merge_lineage(db, exclude_id=group.id)
        if sibling is not None:
            sub = Pipeline(db, sibling, original_parse=original_parse)
            try:
                sub_fail, sub_details, sub_checked, _ctx = _check_hint_rows(
                    db, sibling_prefix, sibling_archived, sub)
            finally:
                sub.restore()
            if sub_fail:
                failures += [f"（补验组#{sibling.id}）{line}" for line in sub_fail]
            else:
                extra_checked = sub_checked
                extra = [f"补验组#{sibling.id}（该组归档项有 merge 血缘）："] + sub_details
        else:
            extra = ["（本库没有带 merge 血缘的组内归档项，提示非 None 分支未覆盖）"]

    if failures:
        report.bad(tag, "归档名 create / 血缘提示不符合预期（见下）", failures + details + extra)
        return (ctx or {}).get("preview"), None
    if ctx is None:
        report.skip(tag, "组内归档项的名称均解析不指向本项，无法构造 create 行", details + extra)
        return None, None
    report.ok(tag, f"{checked + extra_checked} 个归档名的识别行均为 create，"
                   "血缘提示与 merge 目标一致（无血缘者为 None）", details + extra)
    return ctx["preview"], ctx


def _group_with_merge_lineage(db, *, exclude_id: int):
    """找出「组内存在带 merge 血缘的归档项」的另一个组（只读，返回 (组, 前缀, 归档项列表)）。"""
    from sqlalchemy import select

    from models import Asset, AssetItem, LineageLink, SourceGroup

    merged_from = list(db.execute(
        select(LineageLink.from_item_id).where(LineageLink.link_type == "merge")
    ).scalars())
    if not merged_from:
        return None, None, None
    for group in db.execute(select(SourceGroup).order_by(SourceGroup.id)).scalars():
        if group.id == exclude_id:
            continue
        archived_ids = list(db.execute(
            select(AssetItem.id).where(AssetItem.status == "archived",
                                       AssetItem.id.in_(merged_from)).order_by(AssetItem.id)
        ).scalars())
        if not archived_ids:
            continue
        candidates = []
        for item_id in archived_ids:
            has_row = db.execute(
                select(Asset.id).where(Asset.asset_item_id == item_id,
                                       Asset.asset_name.like(group.asset_prefix + "%")).limit(1)
            ).first()
            if has_row:
                candidates.append(item_id)
        if not candidates:
            continue
        archived = archived_items_in_group(db, group.asset_prefix, candidates)
        if archived:
            return group, group.asset_prefix, archived
    return None, None, None


def step3_draft(report: Report, db, prefix: str, pipe: Pipeline) -> None:
    """③ draft 项仍可 update（或可 bind）——本库无 draft 时 SKIP，绝不造数据。"""
    tag = "③"
    from sqlalchemy import select

    from models import AssetItem

    drafts = list(db.execute(
        select(AssetItem).where(AssetItem.status == "draft").order_by(AssetItem.id)
    ).scalars())
    if not drafts:
        report.skip(tag, "本库无 draft 项（动态查 status='draft' 为 0 条），按口径跳过，不造数据")
        return

    details: list[str] = []
    usable = 0
    for item in drafts:
        if not (item.display_name or "").startswith(prefix):
            continue
        short = item.display_name[len(prefix):]
        prev = pipe.preview([(short, SYNTH_AMOUNT)])
        row = next((r for r in prev.rows if r.source_name == short), None)
        if row is None:
            continue
        usable += 1
        in_use_ids = {e.get("item_id") for e in prev.existing_items}
        if row.action == "update" and row.item_id == item.id:
            details.append(f"在用项#{item.id}（draft）→ update，item_id 正确")
            continue
        if row.action == "update" and row.item_id != item.id:
            report.bad(tag, f"draft 项#{item.id} 被识别为 update，但 item_id={row.item_id}（不是该项）", details)
            return
        # create 行：至少要证明「draft 属在用集、可被 bind」（D1）
        bind_ok = False
        try:
            plan = pipe.ss._validate_decision(
                db, {"action": "bind", "target_item_id": item.id},
                pipe.ss.PreviewRow(source_name=short, source_amount=SYNTH_AMOUNT, action="create"),
                prefix, __import__("datetime").date.fromisoformat(SYNTH_DATE), set())
            bind_ok = plan.get("target_item_id") == item.id
        except Exception:
            bind_ok = False
        if item.id in in_use_ids and bind_ok:
            details.append(f"在用项#{item.id}（draft）→ create 行但可 bind（draft 计入在用集）")
        else:
            report.bad(tag, f"draft 项#{item.id} 既未 update 也不可 bind（draft 被误排除）", details)
            return
    if not usable:
        report.skip(tag, f"本库有 {len(drafts)} 个 draft 项，但均不在本组前缀下，无法在本组验证")
        return
    report.ok(tag, f"{usable} 个 draft 项仍属在用集（可 update 或可 bind）", details)


def step4_apply(report: Report, db, prefix: str, pipe: Pipeline,
                preview2, hint_ctx: dict | None) -> None:
    """④ apply 双向断言：(a) 归档名 create；(b) 显式 target_item_id → archived 要 409。"""
    tag = "④"
    from sqlalchemy import func, select

    from errors import ApiError, ErrorCode
    from models import Asset, AssetItem

    if preview2 is None or hint_ctx is None:
        report.skip(tag, "② 未构造出可用的归档名 create 行，apply 双向断言无法进行")
        return

    entry = hint_ctx["entry"]
    short = hint_ctx["short"]
    archived_id = entry["id"]
    row = hint_ctx["row"]
    if row is None or row.index is None:
        report.skip(tag, "② 的 create 行缺少稳定行号，apply 无法按行表决")
        return

    def assets_total() -> int:
        db.expire_all()
        return int(db.execute(select(func.count()).select_from(Asset)).scalar() or 0)

    def archived_rows(item_id: int) -> int:
        db.expire_all()
        return int(db.execute(select(func.count()).select_from(Asset)
                              .where(Asset.asset_item_id == item_id)).scalar() or 0)

    details: list[str] = []
    # ---------- ④(a) 归档名那一行按 create 提交 ----------
    before_total, before_arch = assets_total(), archived_rows(archived_id)
    decisions = {str(row.index): {"action": "create"}}
    a_status: str
    try:
        pipe.apply([(short, SYNTH_AMOUNT)], decisions=decisions)
        after_total, after_arch = assets_total(), archived_rows(archived_id)
        new_name = prefix + short
        new_item = db.execute(select(AssetItem).where(AssetItem.display_name == new_name)).scalars().first()
        ok = (after_arch == before_arch and
              (after_total == before_total + 1 or (new_item is not None and new_item.id != archived_id)))
        a_status = "pass" if ok else "fail"
        details.append(
            f"(a) 归档名 create 被接受：assets {before_total}→{after_total}"
            f"（{'新项已建出' if new_item is not None else '无新项'}），"
            f"归档项#{archived_id} 行数 {before_arch}→{after_arch}")
    except ApiError as exc:
        db.rollback()
        after_total, after_arch = assets_total(), archived_rows(archived_id)
        if exc.code != ErrorCode.ITEM_ARCHIVED:
            report.bad(tag, f"归档名 create 被非预期错误拦截：code={exc.code} status={exc.status_code}",
                       [f"assets {before_total}→{after_total}；归档项#{archived_id} 行数 {before_arch}→{after_arch}"])
            return
        blocked_ok = (exc.status_code == 409 and after_total == before_total
                      and after_arch == before_arch)
        details.append(
            f"(a) 归档名 create → 行级 409 ITEM_ARCHIVED（写路径兜底 D4），"
            f"assets {before_total}→{after_total}、归档项#{archived_id} 行数 {before_arch}→{after_arch}")
        # 兜底分支之外，再验一次「create 通路本身能把新项建出来」（用未解析的合成名）
        fresh, why = _fresh_name(db, prefix, short)
        if fresh is None:
            a_status = "fail"
            details.append(f"(a) 无法构造未解析的合成名：{why}")
        else:
            prev_f = pipe.preview([(fresh, SYNTH_AMOUNT)])
            row_f = next((r for r in prev_f.rows if r.source_name == fresh), None)
            if row_f is None or row_f.action != "create" or row_f.index is None:
                a_status = "fail"
                details.append("(a) 合成名未产出 create 行，无法验证 +1 通路")
            else:
                t0, a0 = assets_total(), archived_rows(archived_id)
                pipe.apply([(fresh, SYNTH_AMOUNT)],
                           decisions={str(row_f.index): {"action": "create"}})
                t1, a1 = assets_total(), archived_rows(archived_id)
                built = db.execute(select(AssetItem)
                                   .where(AssetItem.display_name == prefix + fresh)
                                   ).scalars().first()
                ok_f = (t1 == t0 + 1 and a1 == a0 and built is not None and built.id != archived_id)
                a_status = "pass" if (blocked_ok and ok_f) else "fail"
                details.append(
                    f"(a) 合成名 create（未解析）→ assets {t0}→{t1}（+{t1 - t0}，"
                    f"{'新项已建出' if built is not None else '无新项'}），"
                    f"归档项#{archived_id} 行数 {a0}→{a1}（不变）")
                details.append("(a) 说明：spec §3 写的是「归档名 create → 归档项行数不变」；"
                               "本库实测写路径兜底先行拦截（409），故 +1 通路另用合成名验证。"
                               "若业主期望归档名裸 create 直接成功，则与 D4 冲突，需裁决。")

    # ---------- ④(b) 显式把 target_item_id 指向归档项 ----------
    before_total, before_arch = assets_total(), archived_rows(archived_id)
    b_status = "fail"
    try:
        pipe.apply([(short, SYNTH_AMOUNT)],
                   decisions={str(row.index): {"action": "bind", "target_item_id": archived_id}})
        details.append("(b) 期望 409 ITEM_ARCHIVED，但调用成功（写路径未拦截）")
    except ApiError as exc:
        db.rollback()
        after_total, after_arch = assets_total(), archived_rows(archived_id)
        same_item = (exc.details or {}).get("asset_item_id")
        ok_b = (exc.code == ErrorCode.ITEM_ARCHIVED and exc.status_code == 409
                and after_total == before_total and after_arch == before_arch)
        b_status = "pass" if ok_b else "fail"
        details.append(
            f"(b) bind → 归档项#{archived_id}：code={exc.code} status={exc.status_code}，"
            f"assets {before_total}→{after_total}、归档项#{archived_id} 行数 {before_arch}→{after_arch}"
            + (f"；details.asset_item_id={same_item}" if same_item is not None else ""))

    if a_status == "pass" and b_status == "pass":
        report.ok(tag, "apply 双向成立：(a) 归档名 create 未落到归档项、新项建出通路正常；"
                       "(b) target_item_id 指向归档项 → 409 ITEM_ARCHIVED 且零写入", details)
    else:
        report.bad(tag, f"apply 断言失败：a={a_status} b={b_status}", details)


def _fresh_name(db, prefix: str, base: str) -> tuple[str | None, str]:
    """构造一个「库里解析不到」的合成短名（保证 create 通路不被兜底/绑定截走）。"""
    from services import screenshot_service as ss

    for candidate in (base + "-验收重录", base + "-验收重录B", base + "-验收重录" + "#2"):
        if ss.resolve_item_id(db, prefix + candidate) is None:
            return candidate, "ok"
    return None, "候选合成名均已被占用"


def step5_anchor(report: Report, db, prefix: str, archived: list[dict],
                 pipe: Pipeline) -> None:
    """⑤ 锚点等式：total_current_system == 在用现值之和 == 全量现值之和 − 归档现值之和。"""
    tag = "⑤"
    from services import screenshot_service as ss

    prev = pipe.preview([])
    anchor = prev.total_current_system
    if anchor is None:
        report.bad(tag, "预览未给出 total_current_system（锚点为 None）")
        return
    latest = sql_latest_rows_by_name(db, prefix)
    archived_ids = {a["id"] for a in archived}
    all_sum = sum((amount for _n, _i, amount in latest), Decimal("0"))
    in_use_indep = sum((amount for _n, item_id, amount in latest
                        if item_id not in archived_ids), Decimal("0"))
    archived_sum = sum((a["latest_amount"] for a in archived), Decimal("0"))
    in_use_service = sum((amount for _n, amount in ss.get_group_existing(db, prefix)),
                         Decimal("0"))

    eq1 = (anchor == in_use_indep)
    eq2 = (anchor == in_use_service)
    eq3 = (anchor == all_sum - archived_sum)
    details = [
        "锚点 == 组内在用现值之和（独立 SQL）：" + ("成立" if eq1 else "不成立"),
        "锚点 == get_group_existing 求和（服务单点）：" + ("成立" if eq2 else "不成立"),
        "锚点 == 全量现值之和 − 归档项现值之和：" + ("成立" if eq3 else "不成立"),
        f"组内名称 {len(latest)} 个、归档项 {len(archived)} 个（金额数值不打印）",
    ]
    if eq1 and eq2 and eq3:
        report.ok(tag, "三式全部成立（金额逐分相等）", details)
    else:
        report.bad(tag, "锚点等式不成立", details)


# ================================================================ 主流程
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_interpreter(argv)
    args = parse_args(argv)

    if not BACKEND.is_dir():
        print(f"找不到后端目录：{BACKEND}", file=sys.stderr)
        return EXIT_FAIL
    global_tmp_root = Path(args.tmp_root).expanduser()
    if not global_tmp_root.is_dir():
        print(f"临时根目录不存在：{global_tmp_root}", file=sys.stderr)
        return EXIT_FAIL

    workdir = Path(tempfile.mkdtemp(prefix="verify-archived-scope-", dir=str(global_tmp_root)))
    report = Report()
    session = engine = None
    exit_code = EXIT_FAIL
    try:
        db_copy = snapshot_db(Path(args.db).expanduser(), workdir)
        db_copy = assert_sandboxed(db_copy, global_tmp_root)   # 硬约束 1：先断言，再连接
        report.emit("=" * 78)
        report.emit("归档项不参与录入侧 · 端到端验收（spec §3 五步）")
        report.emit("=" * 78)
        report.emit(f"库副本：{db_copy}")
        report.emit("        在临时目录内 ✓ / ≠ 仓库 data/ 下任何文件 ✓"
                    "（现役库只读复制，全部写入仅在此副本）")

        # ---- 环境（全部指向副本/临时目录，绝不触仓库 data/）
        os.environ["DATABASE_PATH"] = str(db_copy)
        os.environ["DATABASE_URL"] = f"sqlite:///{db_copy}"
        os.environ["TMPDIR"] = str(workdir)
        tempfile.tempdir = str(workdir)
        sys.path.insert(0, str(BACKEND))

        import structlog
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
        logging.disable(logging.WARNING)              # 库日志可能带名称/金额，一律不上屏

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import routers.screenshots as shots
        import services.screenshot_service as ss

        evidence_dir = workdir / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        shots._evidence_root = lambda: str(evidence_dir)      # 凭证根重定向到副本目录

        engine = create_engine(f"sqlite:///{db_copy}", connect_args={"check_same_thread": False})
        if Path(str(engine.url.database)).resolve() != db_copy:
            raise SystemExit("实际连接的库路径与沙箱副本不一致，拒绝继续")
        session = sessionmaker(bind=engine)()
        session.execute(__import__("sqlalchemy").text("select 1"))
        db = session

        group, archived_ids = pick_group(db, args.group)
        prefix = group.asset_prefix
        archived = archived_items_in_group(db, prefix, archived_ids)
        in_use_items = ss.get_group_existing(db, prefix)
        report.emit(f"来源组：组#{group.id}（前缀 {len(prefix)} 字符，值不打印）")
        report.emit(f"组内在用项：{len(in_use_items)} 个；组内归档项：{len(archived)} 个"
                    f"（id {'、'.join('#%d' % a['id'] for a in archived) or '无'}）")

        if archived:
            pipe = Pipeline(db, group, original_parse=ss.parse_screenshot)
            try:
                step1_unrecognized(report, db, prefix, archived, pipe)
                preview2, hint_ctx = step2_create_with_hint(
                    report, db, group, prefix, archived, pipe,
                    original_parse=ss.parse_screenshot)
                step3_draft(report, db, prefix, pipe)
                step4_apply(report, db, prefix, pipe, preview2, hint_ctx)
                step5_anchor(report, db, prefix, archived, pipe)
            finally:
                pipe.restore()
        else:
            report.skip("①②④⑤", "本组无归档项，验收前置数据不满足"
                                  "（可换 --group 或改用含归档项的库）")

        report.emit()
        report.render()

        # ---- 脱敏自检（公开安全红线兜底）：输出里不得出现库内任何名称
        leaked = leak_check(db, report.lines)
        report.emit(f"脱敏自检：{'PASS（输出不含库内任何名称/机构名）' if not leaked else 'FAIL（发现 %d 处库内名称上屏，已阻断）' % len(leaked)}")

        report.emit("-" * 78)
        verdict = "FAIL" if (report.failed or leaked) else "PASS"
        report.emit(f"结论：{verdict}（{report.summary_line()}）")
        exit_code = EXIT_FAIL if (report.failed or leaked) else EXIT_PASS
        return exit_code
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
        if args.keep_copy:
            print(f"\n副本目录已保留：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())