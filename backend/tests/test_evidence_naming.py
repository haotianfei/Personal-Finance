"""凭证文件命名（ADR-0022 D1/D3）与原图信息 `image_meta`（D2）的代码侧验收。

沙箱：conftest 的 in-memory `db` fixture（`create_all`，不跑迁移）——本文件只验
应用层契约，迁移车道的活（0032 双跑幂等 / 脏名清理）由 `test_migration_0032.py` 自验。

逐条对应 `.scratch/evidence-naming/spec.md` §3 的 1–4：

1. 新上传 → `file_path` 匹配 `^\\d{8}-\\d{6}_sg\\d+_[0-9a-f]{12}\\.(jpe?g|png|webp)$`，
   且**不含客户端原名**；`image_meta` 可解析且含 width/height/bytes/format。
2. 同 sha 二次上传 → 不新增行、不改 `file_path`、不重写 `image_meta`。
3. 无 EXIF → `exif == {}`；源图损坏 → `image_meta` 为 NULL，**存证仍成功**。
4. 带 GPS 的合成样图 → `image_meta` 里没有任何 GPS 键。

外加 D1 的两条异常分支：同名不同内容**冲突即停**（不覆盖、不改名）；同名同内容
（库里丢了行、盘上还有文件）复用既有文件。样图全部 Pillow 现画（无真实截图语料），
机构/名称为示例值。

查重范围收窄后的契约（ADR-0024 D1，`evidence-dedup-index` 票 01）：同组同图二次
上传 → 同一行、一份文件；同图跨组上传 → 两行两文件，`source_group_id` 与文件名
里的 `sg<id>` 各说各的组。存储层的复合唯一索引由 `test_migration_0033.py` 自验。
"""
import hashlib
import json
import os
import re
from datetime import date, datetime

import pytest
from PIL import Image
from PIL.ExifTags import IFD
from sqlalchemy import func, select

from models import ScreenshotEvidence
from services import screenshot_service as ss

# spec §3-1 的验收正则（逐字）
NAME_RE = re.compile(r"^\d{8}-\d{6}_sg\d+_[0-9a-f]{12}\.(jpe?g|png|webp)$")
GROUP = "bantuou"
EXIF_WHITELIST = {"DateTimeOriginal", "Make", "Model", "Software", "Orientation"}


@pytest.fixture(autouse=True)
def _default_source_group(db):
    """0029 起凭证只存 `source_group_id`：写入前必须有组行（真库由启动种子保证）。"""
    ss.seed_default_source_groups(db)


# ---------------------------------------------------------------- 合成素材 / 助手
def _save_image(path, fmt="JPEG", size=(48, 72), exif=None):
    img = Image.new("RGB", size, (18, 52, 86))
    if exif is None:
        img.save(path, format=fmt)
    else:
        img.save(path, format=fmt, exif=exif)
    return str(path)


def _jpeg(tmp_path, name="示例截图.jpg", **kw):
    return _save_image(tmp_path / name, "JPEG", **kw)


def _exif_sample(*, gps=False):
    """合成 EXIF：IFD0 四tag + Exif 子 IFD 的 DateTimeOriginal（可选 GPS）。"""
    exif = Image.Exif()
    exif[0x0112] = 1                       # Orientation
    exif[0x010F] = "ExampleMake"
    exif[0x0110] = "ExampleModel"
    exif[0x0131] = "ExampleSoft"
    exif.get_ifd(IFD.Exif)[0x9003] = "2026:10:30 23:22:13"   # DateTimeOriginal
    if gps:
        exif.get_ifd(IFD.GPSInfo)[1] = "N"
        exif.get_ifd(IFD.GPSInfo)[2] = (30.0, 15.0, 0.0)
    return exif


def _sha(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _upload(db, path, root, name=None) -> int:
    return ss.get_or_create_evidence(
        db, _sha(path), path, GROUP, name or os.path.basename(path), str(root))


def _ev(db, ev_id) -> ScreenshotEvidence:
    return db.get(ScreenshotEvidence, ev_id)


def _basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _files(root) -> list:
    return [p for p in root.rglob("*") if p.is_file()]


class _FrozenDatetime(datetime):
    """冻结上传时刻：让「同名冲突」可被确定性地构造出来。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 5, 16, 40, 12)


# ---------------------------------------------------------------- 1. 新命名
def test_new_upload_gets_server_generated_name(db, tmp_path):
    src = _jpeg(tmp_path, "Screenshot_20260101_120000_com.example.app.jpg")
    root = tmp_path / "evidence"
    ev = _ev(db, _upload(db, src, root))

    name = _basename(ev.file_path)
    assert NAME_RE.fullmatch(name), name
    # 基座三件套：上传时刻 + 组 id + sha 前 12
    gid = ss.resolve_source_group(db, GROUP).id
    assert f"_sg{gid}_" in name
    assert name.endswith(f"_{_sha(src)[:12]}.jpg")
    # 时间戳＝上传时刻（不是 data_date、也不是 EXIF 拍摄时间）
    ts = datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
    assert abs((datetime.now() - ts).total_seconds()) < 300
    # 客户端原名不再进入文件路径，但原样留在列里
    assert "Screenshot" not in ev.file_path
    assert ev.original_filename == "Screenshot_20260101_120000_com.example.app.jpg"
    # 目录仍是上传年/月；文件真的落盘
    today = date.today()
    assert ev.file_path.startswith(f"{today.year}/{today.month:02d}/")
    assert (root / ev.file_path).read_bytes() == open(src, "rb").read()


@pytest.mark.parametrize("fmt,ext", [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")])
def test_image_meta_reports_real_format(db, tmp_path, fmt, ext):
    """`format` 取图真身（不是客户端扩展名），扩展名落在白名单内。"""
    src = _save_image(tmp_path / f"示例{ext}", fmt, size=(20, 30))
    ev = _ev(db, _upload(db, src, tmp_path / "evidence"))
    name = _basename(ev.file_path)
    assert NAME_RE.fullmatch(name) and name.endswith(ext)
    meta = json.loads(ev.image_meta)
    assert meta["format"] == fmt
    assert (meta["width"], meta["height"]) == (20, 30)
    assert meta["bytes"] == os.path.getsize(src)


def test_dirty_original_filename_stored_as_null(db, tmp_path):
    """`shot_upload_*`（服务端 mkstemp 临时名）→ NULL，与迁移 0032 同一规则。"""
    src = _jpeg(tmp_path, "shot_upload_4ksjjuyn.jpg")
    ev = _ev(db, _upload(db, src, tmp_path / "evidence"))
    assert ev.original_filename is None
    assert "shot_upload" not in ev.file_path
    assert NAME_RE.fullmatch(_basename(ev.file_path))


def test_filename_helper_uses_sg0_for_missing_group():
    """`source_group_id` 缺失（异常数据）→ `sg0`，格式仍合法可解析。"""
    name = ss.evidence_file_name(
        datetime(2026, 11, 5, 16, 40, 12), None, "e9de4e24a00e" + "0" * 52, ".jpg")
    assert name == "20261105-164012_sg0_e9de4e24a00e.jpg"
    assert NAME_RE.fullmatch(name)


@pytest.mark.parametrize("client_name,expected", [
    ("a.JPG", ".jpg"), ("a.jpeg", ".jpeg"), ("a.PNG", ".png"),
    ("a.webp", ".webp"), ("a.gif", ".png"), ("a", ".png"), ("", ".png"),
])
def test_extension_is_normalized_to_whitelist(client_name, expected):
    """白名单外/大写/无扩展名 → 规范化（验收正则只认 jpe?g|png|webp）。"""
    assert ss.evidence_ext(client_name) == expected


# ---------------------------------------------------------------- 2. 同 sha 复用
def test_same_sha_reuses_row_and_never_rewrites_meta(db, tmp_path):
    src = _jpeg(tmp_path)
    root = tmp_path / "evidence"
    ev_id = _upload(db, src, root)
    ev = _ev(db, ev_id)
    first_path, first_name = ev.file_path, ev.original_filename

    # 哨兵：二次上传若重写 image_meta，这里立刻被抓
    sentinel = json.dumps({"sentinel": True}, ensure_ascii=False)
    ev.image_meta = sentinel
    db.commit()

    assert _upload(db, src, root) == ev_id
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 1
    ev = _ev(db, ev_id)
    assert (ev.file_path, ev.original_filename) == (first_path, first_name)
    assert ev.image_meta == sentinel
    assert len(_files(root)) == 1


# ------------------------------------------------- 2b. 查重范围＝同组（ADR-0024 D1）
def _new_group(db, slug="demo-b", label="示例组乙"):
    """再造一个来源组（真实库由用户自建；这里直接落行）。"""
    from models import SourceGroup

    sg = SourceGroup(slug=slug, label=label, asset_prefix=label + "-", sort_order=1)
    db.add(sg)
    db.commit()
    return sg


def test_second_upload_same_group_reuses_row_and_file(db, tmp_path):
    """同组同图二次上传 → 同一行、不新建文件、文件名里的 `sg` 段不变。"""
    src = _jpeg(tmp_path)
    root = tmp_path / "evidence"
    gid = ss.resolve_source_group(db, GROUP).id

    first = _upload(db, src, root)
    ev = _ev(db, first)
    first_path = ev.file_path

    assert _upload(db, src, root) == first
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 1
    assert _ev(db, first).file_path == first_path
    assert f"_sg{gid}_" in _basename(first_path)
    assert len(_files(root)) == 1                  # 没有第二份文件


def test_same_image_other_group_creates_own_row_and_file(db, tmp_path):
    """同图跨组 → 两行两文件，`source_group_id` 与文件名 `sg` 段各自正确。

    ADR-0024 的既有行为纠正：全局按 sha 查重会把 B 组的图记成 A 组的凭证，
    `source_group_id` 与文件名里的 `sg<id>` 双双说谎。分组后各存一份才对得上事实。
    """
    other = _new_group(db)
    src = _jpeg(tmp_path)
    root = tmp_path / "evidence"
    gid = ss.resolve_source_group(db, GROUP).id

    a_id = _upload(db, src, root)
    b_id = ss.get_or_create_evidence(
        db, _sha(src), src, other.slug, os.path.basename(src), str(root))

    assert b_id != a_id
    ev_a, ev_b = _ev(db, a_id), _ev(db, b_id)
    assert (ev_a.source_group_id, ev_b.source_group_id) == (gid, other.id)
    assert ev_a.file_path != ev_b.file_path
    assert f"_sg{gid}_" in _basename(ev_a.file_path)
    assert f"_sg{other.id}_" in _basename(ev_b.file_path)
    assert len(_files(root)) == 2                  # 两份文件都在盘上
    assert (root / ev_b.file_path).read_bytes() == open(src, "rb").read()

    # 回到原组再传一次：仍命中本组那一行，不会被跨组行带偏（也不会造第三行/第三份文件）
    assert _upload(db, src, root) == a_id
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 2
    assert len(_files(root)) == 2


# ---------------------------------------------------------------- D1 冲突分支
def test_name_conflict_aborts_without_overwriting(db, tmp_path, monkeypatch):
    """同名不同内容 → 抛错停止（不覆盖、也不静默加后缀），报错写清冲突路径。"""
    monkeypatch.setattr(ss, "_datetime", _FrozenDatetime)
    src = _jpeg(tmp_path)
    sha = _sha(src)
    root = tmp_path / "evidence"
    gid = ss.resolve_source_group(db, GROUP).id
    rel = f"2026/11/{ss.evidence_file_name(_FrozenDatetime.now(), gid, sha, '.jpg')}"
    dest = root / rel
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"other-content")

    with pytest.raises(IOError) as ei:
        ss.get_or_create_evidence(db, sha, src, GROUP, os.path.basename(src), str(root))
    assert rel in str(ei.value)                       # 冲突路径可见
    assert dest.read_bytes() == b"other-content"      # 既有文件一字未动
    assert len(_files(root)) == 1                     # 没有造出 .1/.bak 之类的变体
    assert db.execute(
        select(func.count()).select_from(ScreenshotEvidence)).scalar() == 0


def test_existing_identical_file_is_reused(db, tmp_path, monkeypatch):
    """同名同内容（库里丢了行、盘上还有文件）→ 不抛错、不复制第二份。"""
    monkeypatch.setattr(ss, "_datetime", _FrozenDatetime)
    src = _jpeg(tmp_path)
    sha = _sha(src)
    root = tmp_path / "evidence"
    gid = ss.resolve_source_group(db, GROUP).id
    rel = f"2026/11/{ss.evidence_file_name(_FrozenDatetime.now(), gid, sha, '.jpg')}"
    dest = root / rel
    dest.parent.mkdir(parents=True)
    dest.write_bytes(open(src, "rb").read())

    ev = _ev(db, ss.get_or_create_evidence(
        db, sha, src, GROUP, os.path.basename(src), str(root)))
    assert ev.file_path == rel
    assert len(_files(root)) == 1


# ---------------------------------------------------------------- 3/4. image_meta
def test_image_meta_whitelist_fields(db, tmp_path):
    src = _jpeg(tmp_path, exif=_exif_sample(), size=(108, 240))
    ev = _ev(db, _upload(db, src, tmp_path / "evidence"))
    meta = json.loads(ev.image_meta)
    assert set(meta) == {"width", "height", "bytes", "format", "exif"}
    assert (meta["width"], meta["height"]) == (108, 240)
    assert meta["bytes"] == os.path.getsize(src)
    assert meta["format"] == "JPEG"
    assert set(meta["exif"]) == EXIF_WHITELIST          # 白名单，一个不多
    assert meta["exif"]["DateTimeOriginal"] == "2026:10:30 23:22:13"
    assert (meta["exif"]["Make"], meta["exif"]["Model"],
            meta["exif"]["Software"]) == ("ExampleMake", "ExampleModel", "ExampleSoft")
    assert meta["exif"]["Orientation"] == 1


def test_image_meta_without_exif_is_empty_dict(db, tmp_path):
    """源图无 EXIF → `exif` 为 `{}`，不抛错（spec §3-3）。"""
    ev = _ev(db, _upload(db, _jpeg(tmp_path), tmp_path / "evidence"))
    meta = json.loads(ev.image_meta)
    assert meta["exif"] == {}
    assert set(meta) == {"width", "height", "bytes", "format", "exif"}


def test_image_meta_never_stores_gps(db, tmp_path):
    """带 GPS 的合成样图 → 库里没有一个 GPS 键（spec §3-4）。"""
    src = _jpeg(tmp_path, exif=_exif_sample(gps=True))
    with Image.open(src) as img:                      # 前置：源图确实带 GPS
        assert img.getexif().get_ifd(IFD.GPSInfo)

    ev = _ev(db, _upload(db, src, tmp_path / "evidence"))
    assert set(json.loads(ev.image_meta)["exif"]) <= EXIF_WHITELIST
    assert "gps" not in (ev.image_meta or "").lower()


def test_broken_image_degrades_to_null_and_still_saves(db, tmp_path):
    """损坏图片 → `image_meta` NULL，但**存证仍成功**（可信 > 完备）。"""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # 只有幻数，无有效 IHDR
    root = tmp_path / "evidence"
    ev = _ev(db, _upload(db, str(broken), root))
    assert ev.image_meta is None
    assert NAME_RE.fullmatch(_basename(ev.file_path))
    assert (root / ev.file_path).read_bytes() == broken.read_bytes()


def test_missing_pillow_degrades_to_null(db, tmp_path, monkeypatch):
    """Pillow 不可用（依赖缺失/损坏）→ 元数据 NULL，存证照旧。"""
    import sys

    src = _jpeg(tmp_path)                       # 先造图（此时 Pillow 还可用）
    monkeypatch.setitem(sys.modules, "PIL", None)   # 函数内 `from PIL import Image` → ImportError
    root = tmp_path / "evidence"
    ev = _ev(db, _upload(db, src, root))
    assert ev.image_meta is None
    assert (root / ev.file_path).exists()