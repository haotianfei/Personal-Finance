"""凭证来源组改外键（0029）的**代码侧**验收（spec §3 硬判据）。

沙箱：conftest 的 in-memory `db` fixture（`create_all`，不跑迁移）——本文件只验
应用层契约，不重复迁移车道的活（0029 的双跑幂等 / 回填 / `foreign_key_check`
由迁移车道自验）。四条硬判据各自对应：

1. **改名不断链且无级联**：组改名后凭证仍挂同一个 id，对外展示自动跟随新 slug，
   且没有任何写凭证行的 SQL（级联整段已删除，不能留「双保险」）。
2. **未知组写入 → 400 `SOURCE_GROUP_UNKNOWN` 且零新增行**（也不落盘）。
3. **删组**：仍有凭证 → 409（`SOURCE_GROUP_HAS_USED_EVIDENCE` /
   `SOURCE_GROUP_HAS_UNUSED_EVIDENCE`，改名后也必须拦住，这是旧实现里恒为
   「未引用」的死逻辑）；无凭证 → 200 且组行消失。
4. **对外契约**：`source_group` 取组**当前**值——详情是 `{id, slug, label}` 对象
   （record-provenance D7，2026-11-06 起收敛为与列表项同形），孤儿列表仍是字符串
   slug（该端点在 D7 里明写不动）。

数据全部合成（`example-group` / `示例来源组` / `示例-*`），无真实机构、账号与金额。
"""
import asyncio
import hashlib
import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from starlette.datastructures import UploadFile

from errors import ApiError, ErrorCode
from models import Asset, ScreenshotEvidence, SourceGroup
from routers.screenshots import (
    list_orphan_evidence,
    screenshot_evidence,
    screenshot_preview,
)
from routers.source_groups import (
    create_source_group,
    delete_source_group,
    update_source_group,
)
from services import screenshot_service as ss

D = date(2026, 2, 10)
PNG_HEAD = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- 合成素材 / 助手
def _shot(tmp_path, name, salt=b""):
    """写一张合成 PNG，返回 (绝对路径, sha256)。salt 用于制造不同 sha 的不同「截图」。"""
    payload = PNG_HEAD + b"\x00" * 64 + salt
    img = tmp_path / name
    img.write_bytes(payload)
    return str(img), hashlib.sha256(payload).hexdigest()


def _group(db, slug="example-group", label="示例来源组", prefix="示例-"):
    return create_source_group(
        {"slug": slug, "label": label, "asset_prefix": prefix}, db)


def _upload(db, tmp_path, group_key, name, salt=b"", root=None):
    """走真实写入路径（上传即存证），source_group 入参可以是 slug 或 label。"""
    img, sha = _shot(tmp_path, name, salt)
    return ss.get_or_create_evidence(
        db, sha, img, group_key, name, str(root or (tmp_path / "evidence")))


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar()


def confirms(p) -> dict:
    """D4：create 行必须显式表态。测试里统一走「确认新增」（不是本特性的被测对象）。"""
    return {str(r.index if r.index is not None else pos): {"action": "create"}
            for pos, r in enumerate(p.rows) if r.action == "create"}


def _spy_evidence_writes(db):
    """监听打到 screenshot_evidence 的 UPDATE/DELETE 语句（级联残留探测用）。"""
    statements = []

    def _spy(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().upper()
        if ("SCREENSHOT_EVIDENCE" in statement.upper()
                and (head.startswith("UPDATE") or head.startswith("DELETE"))):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _spy)
    return statements, lambda: event.remove(engine, "before_cursor_execute", _spy)


# ---------------------------------------------------------------- 判据 1：改名不断链
def test_rename_group_keeps_evidence_by_id_and_display_follows_slug(db, tmp_path):
    g = _group(db)
    ev_id = _upload(db, tmp_path, g["slug"], "shot-a.png", b"a")
    before = _count(db, ScreenshotEvidence)
    assert db.get(ScreenshotEvidence, ev_id).source_group_id == g["id"]

    statements, stop = _spy_evidence_writes(db)
    try:
        updated = update_source_group(g["id"], {"slug": "example-group-v2"}, db)
    finally:
        stop()

    assert updated["slug"] == "example-group-v2"
    # 引用不变：行数不变、挂的仍是同一个组 id（无级联也无需级联）
    assert _count(db, ScreenshotEvidence) == before
    assert db.get(ScreenshotEvidence, ev_id).source_group_id == g["id"]
    # 没有任何补偿写：级联整段已物理删除，这里守着「不许以双保险的名义回来」
    assert statements == []
    # 展示自动跟随新 slug（详情取对象里的 slug；孤儿列表仍是字符串 slug——该端点本特性不动）
    assert screenshot_evidence(ev_id, db)["source_group"]["slug"] == "example-group-v2"
    listed = [i for i in list_orphan_evidence(db)["items"] if i["id"] == ev_id]
    assert listed and listed[0]["source_group"] == "example-group-v2"


# ---------------------------------------------------------------- 判据 2：未知组 400
def test_unknown_group_upload_is_400_and_writes_nothing(db, tmp_path):
    img, sha = _shot(tmp_path, "shot.png")
    root = tmp_path / "evidence"
    with pytest.raises(ApiError) as ei:
        ss.get_or_create_evidence(db, sha, img, "no-such-group", "shot.png", str(root))
    assert (ei.value.code, ei.value.status_code) == (ErrorCode.SOURCE_GROUP_UNKNOWN, 400)
    assert ei.value.details == {"source_group": "no-such-group"}
    assert _count(db, ScreenshotEvidence) == 0          # 零新增行
    assert not root.exists()                            # 也没落盘（先解析后落盘）


def test_legacy_only_group_cannot_write_evidence(db, tmp_path):
    """硬编码兜底（`SOURCE_GROUPS`）只算「能解析」，**不算「组存在」**。

    真实事故形状：老库丢了某个组行，只读路径靠 legacy dict 照常工作，写路径却
    要往 NOT NULL 外键里塞 id——所以写入点必须独立解析成行，解析不到就 400，
    绝不静默置 NULL、也绝不自动建组（两者都会把悬空重新放回库里）。
    """
    assert ss.get_group_config(db, "bantuou") is not None   # 只读兜底仍可用
    img, _ = _shot(tmp_path, "shot.png")
    p = ss.compute_preview(
        [ss.ParsedItem(name="示例项", amount=Decimal("100"))], [], "支付宝-")
    with pytest.raises(ApiError) as ei:
        ss.apply_preview(
            db, p, source_group="bantuou", data_date=D, image_path=img, evidence_root=str(tmp_path / "evidence"),
            identity_decisions=confirms(p))
    assert (ei.value.code, ei.value.status_code) == (ErrorCode.SOURCE_GROUP_UNKNOWN, 400)
    assert _count(db, ScreenshotEvidence) == 0          # 凭证零新增（也未落盘）
    assert _count(db, Asset) == 0                       # 资产行零写入
    assert _count(db, SourceGroup) == 0                 # 没有自动建组


def test_preview_endpoint_rejects_unknown_group_with_code(db):
    """HTTP 上传入口同一个码：400 `SOURCE_GROUP_UNKNOWN`，凭证行数不变（也不留临时文件）。"""
    file = UploadFile(filename="shot.png", file=io.BytesIO(PNG_HEAD + b"\x00" * 16))
    with pytest.raises(ApiError) as ei:
        asyncio.run(screenshot_preview(
            file=file, source_group="no-such-group", db=db))
    assert (ei.value.code, ei.value.status_code) == (ErrorCode.SOURCE_GROUP_UNKNOWN, 400)
    assert _count(db, ScreenshotEvidence) == 0


def test_known_group_by_slug_or_label_lands_same_id(db, tmp_path):
    g = _group(db)
    id_by_slug = _upload(db, tmp_path, g["slug"], "by-slug.png", b"1")
    id_by_label = _upload(db, tmp_path, g["label"], "by-label.png", b"2")
    assert db.get(ScreenshotEvidence, id_by_slug).source_group_id == g["id"]
    assert db.get(ScreenshotEvidence, id_by_label).source_group_id == g["id"]


# ---------------------------------------------------------------- 判据 3：删组
def test_delete_blocked_while_evidence_references_group(db, tmp_path):
    g = _group(db)
    _upload(db, tmp_path, g["slug"], "shot.png", b"a")
    # 票 03（D6）：未使用凭证默认也挡住删组 → 409 + 精确码（不再是 400 泛化码）
    with pytest.raises(ApiError) as ei:
        delete_source_group(g["id"], db, reason="normalization")
    assert (ei.value.code, ei.value.status_code) == (
        ErrorCode.SOURCE_GROUP_HAS_UNUSED_EVIDENCE, 409)
    assert "凭证" in ei.value.message
    assert ei.value.details["count"] == 1
    assert db.get(SourceGroup, g["id"]) is not None     # 组仍在


def test_delete_blocked_after_rename(db, tmp_path):
    """旧实现的洞：级联把字符串改成新 slug 后，按字符串查引用恒为「未引用」。"""
    g = _group(db)
    _upload(db, tmp_path, g["slug"], "shot.png", b"a")
    update_source_group(g["id"], {"slug": "example-group-v2"}, db)
    with pytest.raises(ApiError) as ei:
        delete_source_group(g["id"], db, reason="normalization")
    assert (ei.value.code, ei.value.status_code) == (
        ErrorCode.SOURCE_GROUP_HAS_UNUSED_EVIDENCE, 409)


def test_delete_allowed_when_group_has_no_evidence(db):
    g = _group(db)
    result = delete_source_group(g["id"], db, reason="normalization")
    assert result["ok"] is True
    assert result["purged_evidence"] == 0      # 明示「没删任何凭证」
    assert db.get(SourceGroup, g["id"]) is None


# ---------------------------------------------------------------- 判据 4：对外契约
def test_api_detail_and_list_expose_current_slug(db, tmp_path):
    """详情的 `source_group` 是对象（record-provenance D7），孤儿列表仍是字符串 slug。

    两处取的都是组**当前**值（组改名后展示自动跟随，见判据 1）。
    """
    g = _group(db)
    ev_id = _upload(db, tmp_path, g["slug"], "shot.png", b"a")

    detail = screenshot_evidence(ev_id, db)
    assert detail["source_group"] == {"id": g["id"], "slug": g["slug"],
                                      "label": g["label"]}

    items = list_orphan_evidence(db)["items"]           # data_date 为 NULL → 孤儿集
    assert items[0]["source_group"] == g["slug"]


def test_detail_404_for_missing_evidence(db):
    with pytest.raises(HTTPException) as ei:
        screenshot_evidence(999999, db)
    assert ei.value.status_code == 404


# ------------------------------------------------- 单一解析口径（写入侧 vs 只读侧）
def test_resolution_has_one_definition_across_services(db):
    from services.source_group_rules_service import get_group

    g = _group(db)
    # 同一个匹配函数：slug 与 label 都定位到同一行
    assert get_group(db, g["slug"]).id == g["id"]
    assert get_group(db, g["label"]).id == g["id"]
    assert ss.resolve_source_group(db, g["label"]).id == g["id"]
    # 未知组的两种语义各归各位：只读侧 None（端点 404）、写入侧 400
    assert get_group(db, "no-such-group") is None
    with pytest.raises(ApiError) as ei:
        ss.resolve_source_group(db, "no-such-group")
    assert ei.value.code == ErrorCode.SOURCE_GROUP_UNKNOWN