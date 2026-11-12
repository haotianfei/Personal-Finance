"""存量凭证改名（ADR-0022 D4 / spec §2 D4 / 票 04）：历史文件名 → 服务端规范名。

默认 **dry-run**（打印清单，不写盘、不写库）；只有 `--apply` 才动。

    python scripts/rename_evidence_files.py                        # 打印 old→new 清单
    python scripts/rename_evidence_files.py --apply                # 真动手
    python scripts/rename_evidence_files.py --report-orphans       # 只看孤儿文件
    python scripts/rename_evidence_files.py --db X --evidence-root Y [--apply]   # 副本演练

行为（spec §2 D4）：

- 新名与上传链**同源**：直接调 `screenshot_service.evidence_file_name(...)`，本脚本
  不自带第二份命名规则（复制一份格式串＝迟早两套口径）；
- 时间戳取该行 `created_at`（上传时刻，精确到秒）；读不出（异常数据）才退化到文件
  mtime——mtime 会被复制/解压改掉，只能兜底，不能当主口径；
- **只改文件名、不换目录**：仍留在 `<上传年>/<月>/`，月目录与 created_at 不一致也不搬
  （搬目录动的是两份风险，收益为零）；
- 已是新名 → `noop` 跳过（**幂等**，二跑零写入）；目标已存在 → 跳过并报告
  （不覆盖、不静默加后缀，spec D1「冲突即停」的精神）；
- 单条失败**立即停止**并打印已处理清单（已提交的改名不回滚——文件移动不可事务化）；
- **不写审计、不产映射表**（业主 2026-11-05 裁决：开发阶段视作系统新建）；历史审计里
  的旧路径保持原样（审计是当时快照，不篡改）；
- 每轮结束报告「磁盘有、库里无 `file_path` 指向」的孤儿文件，**只报告不删**。

红线：`--apply` 由业主在停服窗口对运行库执行（spec §5）。演练请用 `VACUUM INTO`
副本库 + 从备份复制出来的 evidence 目录。

退出码：0＝跑完（跳过项写在 stdout）；1＝中途失败停止；2＝参数/环境错误。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select                 # noqa: E402
from sqlalchemy.orm import Session, sessionmaker              # noqa: E402

import database                                              # noqa: E402
from models import ScreenshotEvidence                        # noqa: E402
from services import screenshot_service as ss                # noqa: E402
from services.evidence_service import default_evidence_root   # noqa: E402

# 计划条目的状态（也是报告里的分类口径，全部进 stdout，不含彩色/图标）
STATUS_RENAME = "rename"                    # 待改名 / 已改名
STATUS_NOOP = "noop"                        # 已是新名（幂等跳过）
STATUS_MISSING = "missing"                  # 库里有行、盘上没文件
STATUS_CONFLICT = "conflict"                # 目标路径已被占用（不覆盖）
STATUS_SHA_MISMATCH = "sha_mismatch"        # 盘上内容与 file_sha256 不符
STATUS_UNSUPPORTED_EXT = "unsupported_ext"  # 扩展名不在白名单（不改，不猜）

SKIP_LABELS = {
    STATUS_NOOP: "已符合新名",
    STATUS_MISSING: "文件缺失",
    STATUS_CONFLICT: "目标冲突",
    STATUS_SHA_MISMATCH: "内容与记录不符",
    STATUS_UNSUPPORTED_EXT: "扩展名不支持",
}


# ---------------------------------------------------------------- 共用管道
# 下面四个助手是**本票两个脚本共用**的薄管道（回填脚本 import 它们），
# 不含任何命名/白名单口径——那些一律来自服务层。


def normalize_rel(rel: Optional[str]) -> str:
    """`file_path` 列值 → 正斜杠相对路径（与 create_all/0019 起的存储口径一致）。"""
    return str(rel or "").replace(os.sep, "/").strip()


def evidence_path(evidence_root: str, rel: str) -> str:
    """相对 `file_path` → 绝对路径（不改写、不解析符号链接）。"""
    return os.path.join(evidence_root, rel.replace("/", os.sep))


def open_session(db_path: str) -> tuple[Session, object]:
    """按路径开一个会话（不碰全局 engine：`--db` 要能指向副本库）。

    返回 `(session, engine)`，调用方负责 `session.close()` + `engine.dispose()`；
    不开 WAL/外键 PRAGMA——本脚本只 UPDATE 一列，与运行库的连接口径无关。
    """
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)(), engine


def iter_evidence_files(evidence_root: str) -> Iterable[str]:
    """遍历证据目录下所有文件 → 相对路径（跳过 `.` 开头的隐藏文件）。"""
    for dirpath, _dirnames, filenames in os.walk(evidence_root):
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, evidence_root).replace(os.sep, "/")


# ---------------------------------------------------------------- 计划
@dataclass
class PlanItem:
    """一条凭证的改名计划（或跳过原因）。dry-run 只产出它，不动任何东西。"""
    ev_id: int
    old_rel: str
    new_rel: str = ""
    status: str = STATUS_RENAME
    detail: str = ""
    timestamp_source: str = "created_at"
    ext: str = ""


@dataclass
class RunResult:
    renamed: list[PlanItem] = field(default_factory=list)
    skipped: list[PlanItem] = field(default_factory=list)
    failed: list[tuple[PlanItem, str]] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return bool(self.failed)

    def counts(self) -> dict:
        out = {s: 0 for s in SKIP_LABELS}
        for item in self.skipped:
            out[item.status] = out.get(item.status, 0) + 1
        return out


def rename_timestamp(created_at: Optional[datetime], src_path: str
                     ) -> tuple[datetime, str]:
    """新名的时间戳取值：`created_at` 优先；缺失才退化到文件 mtime（兜底）。

    返回 `(datetime, 来源标签)`。来源标签进报告，让操作者一眼看出哪些行用了兜底
    ——mtime 会随复制/解压改变，用它命名等于把易变值写进不可变路径。
    """
    if created_at is not None:
        return created_at, "created_at"
    return datetime.fromtimestamp(os.path.getmtime(src_path)), "mtime"


def plan_rename(ev: ScreenshotEvidence, evidence_root: str) -> PlanItem:
    """单条计划：算新 rel → 冲突/缺失/内容校验（**只读**）。"""
    old_rel = normalize_rel(ev.file_path)
    item = PlanItem(ev_id=ev.id, old_rel=old_rel)
    if not old_rel:
        item.status, item.detail = STATUS_MISSING, "file_path 为空"
        return item
    src = evidence_path(evidence_root, old_rel)
    if not os.path.isfile(src):
        item.status, item.detail = STATUS_MISSING, "盘上无此文件"
        return item

    # 扩展名取自**现文件名**（不是 original_filename）：历史 23 条的原始名已被
    # 置 NULL，退化成 evidence_ext(None) 会把 .jpg 改成 .png——名字与内容不符比
    # 名字不可读更糟。白名单外的扩展名不改名，只在报告里点名。
    ext = os.path.splitext(old_rel)[1].lower()
    item.ext = ext
    if ext not in ss.EVIDENCE_EXTS:
        item.status, item.detail = STATUS_UNSUPPORTED_EXT, f"扩展名 {ext or '（无）'}"
        return item

    created_at, ts_source = rename_timestamp(ev.created_at, src)
    item.timestamp_source = ts_source
    item.new_rel = f"{old_rel.rsplit('/', 1)[0] + '/' if '/' in old_rel else ''}" \
                   f"{ss.evidence_file_name(created_at, ev.source_group_id, ev.file_sha256, ext)}"

    if item.new_rel == old_rel:                 # 已是新名 → 幂等
        item.status = STATUS_NOOP
        return item

    dst = evidence_path(evidence_root, item.new_rel)
    if os.path.exists(dst):
        # 不覆盖：目标可能是另一行的文件。同名同内容也不动（两行共用一文件会让
        # 「一 sha 一行一文件」失真），交给人看。
        same = (os.path.isfile(dst)
                and ss._file_sha256(dst) == (ev.file_sha256 or ""))
        item.status = STATUS_CONFLICT
        item.detail = "目标已存在且内容相同" if same else "目标已存在且内容不同"
        return item

    if ss._file_sha256(src) != (ev.file_sha256 or ""):
        # 名字里编码了 sha 前 12 位：内容不符时改名等于让文件名撒谎，宁可不改。
        item.status, item.detail = STATUS_SHA_MISMATCH, "盘上 sha256 ≠ file_sha256"
        return item

    return item


def plan(db: Session, evidence_root: str) -> list[PlanItem]:
    """全部凭证的改名计划（按 id 排序；不写盘、不写库）。"""
    rows = db.execute(select(ScreenshotEvidence).order_by(ScreenshotEvidence.id)).scalars().all()
    return [plan_rename(ev, evidence_root) for ev in rows]


def apply_plan(db: Session, evidence_root: str, items: list[PlanItem]) -> RunResult:
    """按计划执行：`os.replace` → 更新 `file_path` → commit，逐条。

    单条失败**立即停止**（不继续后面的行）：改名失败通常意味着磁盘/权限出了状况，
    继续跑只会把问题放大；已提交的行保持已改名状态（文件移动不可事务化），
    重跑即可从断点接上（幂等）。
    """
    result = RunResult(skipped=[i for i in items if i.status != STATUS_RENAME])
    for item in items:
        if item.status != STATUS_RENAME:
            continue
        moved = False
        src = evidence_path(evidence_root, item.old_rel)
        dst = evidence_path(evidence_root, item.new_rel)
        try:
            ev = db.get(ScreenshotEvidence, item.ev_id)
            if ev is None:
                raise RuntimeError(f"凭证 #{item.ev_id} 在库中不存在")
            if normalize_rel(ev.file_path) != item.old_rel:
                raise RuntimeError("行已变化（file_path 与计划不一致），请重跑 dry-run")
            os.replace(src, dst)
            moved = True
            ev.file_path = item.new_rel          # 不写审计：开发阶段视作系统新建
            db.commit()
        except Exception as exc:                 # noqa: BLE001 —— 任何异常都要停并留痕
            db.rollback()
            if moved:                            # 盘已动、库未落 → 尽力换回去，别留分裂
                try:
                    os.replace(dst, src)
                except OSError:
                    pass
            result.failed.append((item, f"{type(exc).__name__}: {exc}"))
            break
        result.renamed.append(item)
    return result


# ---------------------------------------------------------------- 孤儿
def find_orphans(db: Session, evidence_root: str) -> list[str]:
    """磁盘上存在、库里无 `file_path` 指向的文件（**只报告，不删**）。

    排序输出便于比对；不判重、不猜归属——孤儿可能是有意的留档，也可能是
    上传链半途失败留下的，删不删是业主的决定（spec §4 明确本轮不删）。
    """
    referenced = {normalize_rel(p) for p in
                  db.execute(select(ScreenshotEvidence.file_path)).scalars()}
    return sorted(p for p in iter_evidence_files(evidence_root) if p not in referenced)


# ---------------------------------------------------------------- 输出
def _describe_skip(item: PlanItem) -> str:
    label = SKIP_LABELS.get(item.status, item.status)
    return f"  #{item.ev_id:<4} {item.old_rel}  [{label}] {item.detail}".rstrip()


def print_plan(items: list[PlanItem], *, apply: bool) -> None:
    pending = [i for i in items if i.status == STATUS_RENAME]
    skipped = [i for i in items if i.status != STATUS_RENAME]
    verb = "已改名" if apply else "待改名"
    print(f"\n{verb} {len(pending)} 条：")
    for i in pending:
        print(f"  #{i.ev_id:<4} {i.old_rel}  →  {i.new_rel}")
    print(f"\n跳过 {len(skipped)} 条：")
    for i in skipped:
        print(_describe_skip(i))
    if not skipped:
        print("  （无）")


def print_counts(items: list[PlanItem]) -> None:
    total = len(items)
    pending = sum(1 for i in items if i.status == STATUS_RENAME)
    parts = [f"待改名 {pending}"]
    for status, label in SKIP_LABELS.items():
        parts.append(f"{label} {sum(1 for i in items if i.status == status)}")
    print(f"\n合计：共 {total} 条 | " + " / ".join(parts))


def print_orphans(orphans: list[str]) -> None:
    print(f"\n孤儿文件（磁盘有、库里无 file_path 指向）：{len(orphans)} 个（只报告，不删除）")
    for rel in orphans:
        print(f"  {rel}")
    if not orphans:
        print("  （无）")


def print_failure(result: RunResult, pending_total: int) -> None:
    item, detail = result.failed[0]
    print(f"\n!! 第 {len(result.renamed) + 1} 条失败并停止：#{item.ev_id} "
          f"{item.old_rel} → {item.new_rel}\n   {detail}")
    print(f"\n已处理清单（已提交，共 {len(result.renamed)} 条）：")
    for i in result.renamed:
        print(f"  #{i.ev_id:<4} {i.old_rel}  →  {i.new_rel}")
    if not result.renamed:
        print("  （无）")
    rest = pending_total - len(result.renamed) - 1
    print(f"\n剩余 {rest} 条未处理，保持原样；修正后重跑即可从此处接上（幂等）。")


def run(db: Session, evidence_root: str, *, apply: bool) -> tuple[list[PlanItem], RunResult]:
    """dry-run / apply 的统一入口：先出计划，再（可选）执行。"""
    items = plan(db, evidence_root)
    if not apply:
        return items, RunResult(skipped=[i for i in items if i.status != STATUS_RENAME])
    result = apply_plan(db, evidence_root, items)
    return items, result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="存量凭证改名（默认 dry-run，不写盘不写库）")
    ap.add_argument("--db", default=database.DB_PATH,
                    help=f"数据库路径（默认 {database.DB_PATH}；演练传副本库）")
    ap.add_argument("--evidence-root", default="",
                    help="证据根目录（默认项目 data/evidence）")
    ap.add_argument("--apply", action="store_true", help="确认改名（缺省只打印清单）")
    ap.add_argument("--report-orphans", action="store_true",
                    help="只报告「磁盘有、库里无」的孤儿文件，不改名")
    args = ap.parse_args(argv)

    evidence_root = args.evidence_root or default_evidence_root()
    if not os.path.isfile(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    if not os.path.isdir(evidence_root):
        print(f"证据目录不存在：{evidence_root}", file=sys.stderr)
        return 2
    if args.apply and args.report_orphans:
        print("--apply 与 --report-orphans 互斥：先看孤儿，再决定改名。", file=sys.stderr)
        return 2

    db, engine = open_session(args.db)
    try:
        print(f"模式：{'--report-orphans（只读）' if args.report_orphans else ('--apply' if args.apply else 'dry-run（只读，不写盘不写库）')}")
        print(f"数据库：{args.db}")
        print(f"证据根目录：{evidence_root}")
        print("（提示：上方 database_path 日志是 import 期算出的默认路径，实际以本行 --db 为准）")

        if args.report_orphans:
            orphans = find_orphans(db, evidence_root)
            print_orphans(orphans)
            return 0

        items, result = run(db, evidence_root, apply=args.apply)
        print_plan(items, apply=args.apply)
        print_counts(items)
        if result.stopped:
            print_failure(result, sum(1 for i in items if i.status == STATUS_RENAME))
        elif args.apply:
            print(f"\n已回写 {len(result.renamed)} 条 file_path（未写审计、未改 original_filename）。")
        print_orphans(find_orphans(db, evidence_root))
        return 1 if result.stopped else 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())