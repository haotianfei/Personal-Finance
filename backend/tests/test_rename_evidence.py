"""票 04：存量改名脚本 + `image_meta` 回填 + 孤儿报告 的验收测试。

被测对象：

- `backend/scripts/rename_evidence_files.py`（ADR-0022 D4 / spec §3 第 6–7 条）
- `backend/scripts/backfill_evidence_image_meta.py`（ADR-0022 D2 / spec §2 D5）

沙箱是**临时目录 + 临时文件 SQLite**（`create_all` 建全表，不跑迁移）：两条脚本
都按路径开会话并真的动磁盘，内存库与 `StaticPool` 在这里没有意义。**不碰**
`deploy/data/` 与真实 `data/evidence/`。

覆盖：

1. dry-run：打印计划、文件与库**零变化**；
2. apply：全部符合新名正则、内容 sha 不变、`file_path` 回写、旧路径消失；
3. apply 二跑：全 `noop`，文件与库零变化（幂等）；
4. `original_filename` 逐条不变（该 NULL 的仍 NULL）+ 审计表零新增；
5. 文件缺失 / 目标冲突 / sha 不符 / 扩展名非白名单 → 跳过并计入报告，不覆盖；
6. 时间戳来源：`created_at` 优先（mtime 故意设成另一个日期）；缺失才退化 mtime；
7. 失败即停：`os.replace` 中途报错 → 停止、已处理清单可见、剩余行原样；
8. 孤儿报告：只列「磁盘有、库无 `file_path`」的文件（隐藏文件忽略），不删；
9. 回填：只碰 `image_meta IS NULL`，坏图/缺失跳过并计数，已有值一字不改。

测试数据一律合成（示例组 / `shot_upload_ab12cd34.jpg` 之类），不抄运行库的真实
文件名、机构名或摘要。
"""
import hashlib
import json
import os
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  —— 全表注册进 Base.metadata
from database import Base
from models import AuditLog, ScreenshotEvidence, SourceGroup
from scripts import backfill_evidence_image_meta as bf
from scripts import rename_evidence_files as rn
from services import screenshot_service as ss

# spec §3 的验收正则（逐字）
NAME_RE = re.compile(r"^\d{8}-\d{6}_sg\d+_[0-9a-f]{12}\.(jpe?g|png|webp)$")
GROUP_SLUG = "example-app"
GROUP_LABEL = "示例App"


# ---------------------------------------------------------------- 沙箱
@pytest.fixture()
def sandbox(tmp_path):
    """临时文件库（可被 `--db` 指向）+ 临时 evidence 目录。"""
    db_path = tmp_path / "copy.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    root = tmp_path / "evidence"
    root.mkdir()
    try:
        yield SimpleNamespace(db=session, db_path=str(db_path), root=str(root),
                              tmp_path=tmp_path, engine=engine)
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def group(sandbox):
    row = SourceGroup(slug=GROUP_SLUG, label=GROUP_LABEL, asset_prefix=f"{GROUP_LABEL}-")
    sandbox.db.add(row)
    sandbox.db.commit()
    return row


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _add_evidence(db, root, *, rel, created_at, group_id, data=None,
                  original_filename=None, file_sha256=None):
    """直插一条凭证行，并把它 `file_path` 指向的文件真的写到盘上。"""
    payload = data if data is not None else f"evidence-{rel}".encode()
    path = _write(root, rel, payload)
    ev = ScreenshotEvidence(
        source_group_id=group_id, file_path=rel,
        file_sha256=file_sha256 or _sha(payload),
        original_filename=original_filename, created_at=created_at)
    db.add(ev)
    db.commit()
    return ev, path


def _dirty_rows(db, root, gid, n=3):
    """造 n 条历史脏名行（不同月目录，模拟存量）。"""
    rows = []
    months = ["2026/09", "2026/10", "2026/07"][:n]
    for i, month in enumerate(months):
        ev, path = _add_evidence(
            db, root, rel=f"{month}/shot_upload_ab12cd{i}_deadbeef0{i}01.jpg",
            created_at=datetime(2026, 9, 13 + i, 17 + i, 40 + i, 49 + i), group_id=gid)
        rows.append((ev, path))
    return rows


def _disk(root) -> dict:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as f:
                out[rel] = _sha(f.read())
    return out


def _paths(db, gid) -> dict:
    return {ev.id: (ev.file_path, ev.original_filename) for ev in
            db.execute(select(ScreenshotEvidence).order_by(ScreenshotEvidence.id)).scalars()}


def _audit_count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar()


def _new_session(db_path):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)(), engine


# ---------------------------------------------------------------- 1. dry-run
def test_dry_run_prints_plan_and_changes_nothing(sandbox, group, capsys):
    """spec §3-6：dry-run 打印 old→new + 孤儿报告，文件与库零变化。"""
    _dirty_rows(sandbox.db, sandbox.root, group.id)
    _write(sandbox.root, "2026/10/leftover_orphan.png", b"orphan")
    before_disk, before_db = _disk(sandbox.root), _paths(sandbox.db, group.id)

    assert rn.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root]) == 0
    out = capsys.readouterr().out

    assert "待改名 3 条" in out
    assert out.count("→") == 3                       # 三条 old→new 全打印
    assert "leftover_orphan.png" in out              # 孤儿报告随 dry-run 一起出
    assert "dry-run" in out

    assert _disk(sandbox.root) == before_disk        # 文件零变化
    assert _paths(sandbox.db, group.id) == before_db  # 库零变化


def test_dry_run_plan_matches_new_naming_rule(sandbox, group):
    """计划的 new_rel 与上传链同源：同目录 + 验收正则 + 组 id/sha 前 12。"""
    rows = _dirty_rows(sandbox.db, sandbox.root, group.id)
    items = rn.plan(sandbox.db, sandbox.root)

    assert [i.status for i in items] == [rn.STATUS_RENAME] * 3
    for item, (ev, _path) in zip(items, rows):
        assert NAME_RE.fullmatch(item.new_rel.rsplit("/", 1)[-1]), item.new_rel
        assert item.new_rel.rsplit("/", 1)[0] == ev.file_path.rsplit("/", 1)[0]  # 不换目录
        assert f"_sg{group.id}_" in item.new_rel
        assert item.new_rel.endswith(f"_{ev.file_sha256[:12]}.jpg")


# ---------------------------------------------------------------- 2. apply
def test_apply_renames_files_and_backfills_file_path(sandbox, group):
    """spec §3-7：全部符合新规则、文件都在且 sha 不变、旧路径消失。"""
    rows = _dirty_rows(sandbox.db, sandbox.root, group.id)
    sha_by_old = {ev.file_path: ev.file_sha256 for ev, _ in rows}

    items, result = rn.run(sandbox.db, sandbox.root, apply=True)
    assert result.failed == [] and len(result.renamed) == 3

    after = _disk(sandbox.root)
    for item in result.renamed:
        assert NAME_RE.fullmatch(item.new_rel.rsplit("/", 1)[-1])
        assert item.new_rel in after                       # 新路径真的存在
        assert after[item.new_rel] == sha_by_old[item.old_rel]   # 内容 sha 不变
        assert item.old_rel not in after                   # 旧文件名消失
        ev = sandbox.db.get(ScreenshotEvidence, item.ev_id)
        assert ev.file_path == item.new_rel                # file_path 已回写
        assert ev.file_sha256 == sha_by_old[item.old_rel]


def test_apply_keeps_original_filename_and_writes_no_audit(sandbox, group):
    """spec §3-7 后半：`original_filename` 逐条不变、审计表零新增行。"""
    ev_named, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/shot_upload_c0ffee01_aabbccdd0011.jpg",
        created_at=datetime(2026, 9, 20, 0, 19, 25), group_id=group.id,
        original_filename=None)
    ev_real, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/01/Screenshot_20260101_120000_example_0123456789ab.jpg",
        created_at=datetime(2026, 1, 1, 12, 1, 40), group_id=group.id,
        original_filename="Screenshot_20260101_120000_example.jpg")
    before = _paths(sandbox.db, group.id)
    assert _audit_count(sandbox.db) == 0

    _, result = rn.run(sandbox.db, sandbox.root, apply=True)
    assert len(result.renamed) == 2

    after = _paths(sandbox.db, group.id)
    assert after[ev_named.id][1] is None                    # 该 NULL 的仍 NULL
    assert after[ev_real.id][1] == "Screenshot_20260101_120000_example.jpg"
    assert {k: v[1] for k, v in after.items()} == {k: v[1] for k, v in before.items()}
    assert _audit_count(sandbox.db) == 0                    # 不写审计


# ---------------------------------------------------------------- 3. 幂等
def test_second_apply_is_noop(sandbox, group):
    """二跑：全部 noop，文件与库零变化（幂等）。"""
    _dirty_rows(sandbox.db, sandbox.root, group.id)
    rn.run(sandbox.db, sandbox.root, apply=True)
    disk_after_first, db_after_first = _disk(sandbox.root), _paths(sandbox.db, group.id)

    items, result = rn.run(sandbox.db, sandbox.root, apply=True)
    assert result.renamed == []
    assert [i.status for i in items] == [rn.STATUS_NOOP] * 3
    assert _disk(sandbox.root) == disk_after_first
    assert _paths(sandbox.db, group.id) == db_after_first


# ---------------------------------------------------------------- 4. 跳过分支
def test_missing_file_is_reported_and_skipped(sandbox, group):
    """库里有行、盘上没文件 → 计入「文件缺失」，其余行照常改名。"""
    rows = _dirty_rows(sandbox.db, sandbox.root, group.id)
    os.remove(rows[0][1])

    items = rn.plan(sandbox.db, sandbox.root)
    assert [i.status for i in items] == [rn.STATUS_MISSING, rn.STATUS_RENAME, rn.STATUS_RENAME]

    _, result = rn.run(sandbox.db, sandbox.root, apply=True)
    assert result.failed == [] and len(result.renamed) == 2
    assert sandbox.db.get(ScreenshotEvidence, rows[0][0].id).file_path == rows[0][0].file_path


def test_target_conflict_is_skipped_without_overwrite(sandbox, group):
    """目标名已被占用 → 跳过并报告；不覆盖、不加后缀、不动原行。"""
    created = datetime(2026, 10, 30, 23, 22, 13)
    ev, src = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/10/shot_upload_4ksjjuyn_aabbccdd0011.jpg",
        created_at=created, group_id=group.id)
    target = (f"2026/10/{ss.evidence_file_name(created, group.id, ev.file_sha256, '.jpg')}")
    _write(sandbox.root, target, b"another-row-content")

    items = rn.plan(sandbox.db, sandbox.root)
    assert items[0].status == rn.STATUS_CONFLICT
    assert "内容不同" in items[0].detail

    _, result = rn.run(sandbox.db, sandbox.root, apply=True)
    assert result.failed == [] and result.renamed == []
    with open(src, "rb") as f:
        assert f.read() == b"evidence-2026/10/shot_upload_4ksjjuyn_aabbccdd0011.jpg"
    with open(os.path.join(sandbox.root, target), "rb") as f:
        assert f.read() == b"another-row-content"          # 占位文件一字未动
    assert sandbox.db.get(ScreenshotEvidence, ev.id).file_path == ev.file_path


def test_sha_mismatch_is_reported_and_skipped(sandbox, group):
    """盘上内容与 `file_sha256` 不符 → 不改名（名字里编码的 sha 不能说谎）。"""
    ev, path = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/shot_upload_zz99yy88_aabbccdd0012.jpg",
        created_at=datetime(2026, 9, 15, 23, 49, 40), group_id=group.id)
    _write(sandbox.root, ev.file_path, b"content-replaced-after-upload")

    items = rn.plan(sandbox.db, sandbox.root)
    assert items[0].status == rn.STATUS_SHA_MISMATCH
    assert os.path.isfile(path)


def test_unsupported_ext_is_skipped(sandbox, group):
    """扩展名不在白名单（`.gif`）→ 不改名，只在报告里点名。"""
    ev, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/shot_upload_gg77hh66_aabbccdd0013.gif",
        created_at=datetime(2026, 9, 16, 0, 19, 25), group_id=group.id)
    items = rn.plan(sandbox.db, sandbox.root)
    assert items[0].status == rn.STATUS_UNSUPPORTED_EXT
    assert rn.run(sandbox.db, sandbox.root, apply=True)[1].renamed == []


# ---------------------------------------------------------------- 5. 时间戳来源
def test_timestamp_comes_from_created_at_not_mtime(sandbox, group):
    """时间戳取 `created_at`：mtime 故意设成另一个日期，名字里必须没有它。"""
    created = datetime(2026, 10, 30, 23, 22, 13)
    ev, path = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/shot_upload_mm44nn33_aabbccdd0014.jpg",
        created_at=created, group_id=group.id)
    old = datetime(2020, 1, 2, 3, 4, 5).timestamp()
    os.utime(path, (old, old))

    item = rn.plan(sandbox.db, sandbox.root)[0]
    assert item.timestamp_source == "created_at"
    assert item.new_rel.rsplit("/", 1)[-1].startswith("20261030-232213")
    assert "20200102" not in item.new_rel


def test_created_at_missing_falls_back_to_mtime(tmp_path):
    """`created_at` 读不出（异常数据）→ 退化到文件 mtime（兜底口径）。"""
    path = tmp_path / "x.jpg"
    path.write_bytes(b"x")
    mtime = datetime(2026, 7, 5, 8, 9, 10).timestamp()
    os.utime(path, (mtime, mtime))

    assert rn.rename_timestamp(None, str(path)) == (
        datetime.fromtimestamp(mtime), "mtime")
    frozen = datetime(2026, 11, 5, 16, 40, 12)
    assert rn.rename_timestamp(frozen, str(path)) == (frozen, "created_at")


# ---------------------------------------------------------------- 6. 失败即停
def test_apply_stops_on_failure_and_prints_processed_list(sandbox, group, monkeypatch, capsys):
    """单条失败 → 立即停止，已处理清单可见，剩余行保持原样（不半截乱搬）。"""
    rows = _dirty_rows(sandbox.db, sandbox.root, group.id)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(rn.os, "replace", flaky)
    code = rn.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root, "--apply"])
    out = capsys.readouterr().out

    assert code == 1
    assert "失败并停止" in out and "已处理清单" in out
    # 第一条已提交改名，后两条原样（文件还在旧名上、库未改）
    first, second, third = rows
    assert NAME_RE.fullmatch(sandbox.db.get(ScreenshotEvidence, first[0].id).file_path.rsplit("/", 1)[-1])
    for ev, path in (second, third):
        assert sandbox.db.get(ScreenshotEvidence, ev.id).file_path == ev.file_path
        assert os.path.isfile(path)


# ---------------------------------------------------------------- 7. 孤儿
def test_orphan_report_lists_only_unreferenced_files(sandbox, group):
    """只报告「磁盘有、库无 file_path」的文件；隐藏文件忽略；不删任何东西。"""
    _dirty_rows(sandbox.db, sandbox.root, group.id, n=2)
    _write(sandbox.root, "2026/07/shot_upload_4_eoadaq_c6a70027a783.jpg", b"orphan-1")
    _write(sandbox.root, "2026/11/leftover_85366be5598e.png", b"orphan-2")
    _write(sandbox.root, "2026/11/.DS_Store", b"junk")

    orphans = rn.find_orphans(sandbox.db, sandbox.root)
    assert orphans == ["2026/07/shot_upload_4_eoadaq_c6a70027a783.jpg",
                       "2026/11/leftover_85366be5598e.png"]
    for rel in orphans:                                    # 只报告，不删
        assert os.path.isfile(os.path.join(sandbox.root, rel))


def test_report_orphans_cli_is_readonly(sandbox, group, capsys):
    """`--report-orphans` 只出报告：不写盘、不写库。"""
    _dirty_rows(sandbox.db, sandbox.root, group.id)
    _write(sandbox.root, "2026/10/leftover_orphan.png", b"orphan")
    before_disk, before_db = _disk(sandbox.root), _paths(sandbox.db, group.id)

    assert rn.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root,
                    "--report-orphans"]) == 0
    out = capsys.readouterr().out
    assert "leftover_orphan.png" in out and "只报告，不删除" in out
    assert "待改名" not in out
    assert _disk(sandbox.root) == before_disk
    assert _paths(sandbox.db, group.id) == before_db


def test_cli_rejects_unknown_paths(tmp_path, capsys):
    """路径不存在 → 退出码 2，不开会话不动盘。"""
    assert rn.main(["--db", str(tmp_path / "nope.db"),
                    "--evidence-root", str(tmp_path)]) == 2
    assert "数据库不存在" in capsys.readouterr().err


def test_cli_apply_runs_against_copy_db(sandbox, group, capsys):
    """`--db` 指向副本库时 CLI 全程可用（业主演练的同一条路径）。"""
    _dirty_rows(sandbox.db, sandbox.root, group.id)
    sandbox.db.close()

    assert rn.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root,
                    "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已改名 3 条" in out and "未写审计" in out

    session, engine = _new_session(sandbox.db_path)
    try:
        rows = session.execute(select(ScreenshotEvidence)).scalars().all()
        assert all(NAME_RE.fullmatch(r.file_path.rsplit("/", 1)[-1]) for r in rows)
        assert _audit_count(session) == 0
    finally:
        session.close()
        engine.dispose()
    assert len(_disk(sandbox.root)) == 3


# ---------------------------------------------------------------- 8. 元数据回填
def _jpeg(path, *, size=(48, 72)):
    Image.new("RGB", size, (18, 52, 86)).save(str(path), format="JPEG")
    return path


def test_backfill_fills_null_meta_only(sandbox, group, tmp_path):
    """只回填 `image_meta IS NULL` 的行；已有值一字不改；dry-run 不写库。"""
    jpeg_bytes = _jpeg(tmp_path / "sample.jpg").read_bytes()
    ev_null, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/a.jpg",
        created_at=datetime(2026, 9, 20, 0, 19, 25), group_id=group.id, data=jpeg_bytes)
    ev_meta, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/b.jpg",
        created_at=datetime(2026, 9, 21, 0, 19, 25), group_id=group.id,
        # 内容与 a.jpg 不同 → 不同 sha：同组同 sha 会撞 ux_screenshot_evidence_sha_group。
        # 该行只用来验证「已有 image_meta 一字不改」，不会被回填读取，故无需是合法图。
        data=jpeg_bytes + b"\x00meta")
    sentinel = json.dumps({"sentinel": True})
    ev_meta.image_meta = sentinel
    sandbox.db.commit()

    dry = bf.backfill(sandbox.db, sandbox.root, apply=False)
    assert [ev_id for ev_id, _rel, _m in dry.would_fill] == [ev_null.id]
    assert sandbox.db.get(ScreenshotEvidence, ev_null.id).image_meta is None   # dry-run 未写

    done = bf.backfill(sandbox.db, sandbox.root, apply=True)
    assert [ev_id for ev_id, _rel, _m in done.would_fill] == [ev_null.id]
    meta = json.loads(sandbox.db.get(ScreenshotEvidence, ev_null.id).image_meta)
    assert set(meta) == {"width", "height", "bytes", "format", "exif"}
    assert (meta["width"], meta["height"]) == (48, 72) and meta["format"] == "JPEG"
    assert sandbox.db.get(ScreenshotEvidence, ev_meta.id).image_meta == sentinel


def test_backfill_skips_bad_image_and_missing_file(sandbox, group):
    """坏图 / 文件缺失 → 跳过并计数，仍留 NULL（可重跑重试），不伪造。"""
    ev_bad, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/broken.png",
        created_at=datetime(2026, 9, 20, 0, 19, 25), group_id=group.id,
        data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    ev_gone, path = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/gone.jpg",
        created_at=datetime(2026, 9, 21, 0, 19, 25), group_id=group.id, data=b"gone")
    os.remove(path)

    result = bf.backfill(sandbox.db, sandbox.root, apply=True)
    assert result.would_fill == []
    assert [e for e, _ in result.bad_image] == [ev_bad.id]
    assert [e for e, _ in result.missing] == [ev_gone.id]
    assert sandbox.db.get(ScreenshotEvidence, ev_bad.id).image_meta is None
    assert sandbox.db.get(ScreenshotEvidence, ev_gone.id).image_meta is None


def test_backfill_cli_reports_and_writes(sandbox, group, tmp_path, capsys):
    """CLI：dry-run 打印将回填的行，`--apply` 才落库。"""
    jpeg_bytes = _jpeg(tmp_path / "sample.jpg").read_bytes()
    ev, _ = _add_evidence(
        sandbox.db, sandbox.root, rel="2026/09/a.jpg",
        created_at=datetime(2026, 9, 20, 0, 19, 25), group_id=group.id, data=jpeg_bytes)
    ev_id = ev.id
    sandbox.db.close()

    assert bf.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root]) == 0
    out = capsys.readouterr().out
    assert "将回填 1 条" in out and "dry-run" in out

    session, engine = _new_session(sandbox.db_path)
    try:
        assert session.get(ScreenshotEvidence, ev_id).image_meta is None
    finally:
        session.close()
        engine.dispose()

    assert bf.main(["--db", sandbox.db_path, "--evidence-root", sandbox.root, "--apply"]) == 0
    assert "已回填 1 条" in capsys.readouterr().out

    session, engine = _new_session(sandbox.db_path)
    try:
        meta = json.loads(session.get(ScreenshotEvidence, ev_id).image_meta)
        assert (meta["width"], meta["height"], meta["format"]) == (48, 72, "JPEG")
    finally:
        session.close()
        engine.dispose()