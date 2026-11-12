"""标签名长度口径的回归测试（spec `.scratch/tag-create-in-sheet` D5 / §3 第 6 条）。

两条入口共用同一口径，缺一条就会「抽屉能建、字典页报错」这类左右互斥：
1. 前端：设置页标签名字段 `maxLength: 20`；抽屉内新建走同一个 20 字上限。
2. 后端：`TagCreate.name` 的 pydantic 上限 `max_length=20` → 超长 422
   `VALIDATION_ERROR`，且 `details.fields[].message` 里要能看到「20」（前端照这句话
   说人话，不能只说「请求参数校验失败」）。

边界取三条：20 字通过、21 字 422、空名（trim 后为空）仍 422。

数据一律合成（`示例` 前缀 + 虚构词，无真实标签名）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from routers import classification as classification_router


@pytest.fixture()
def api(tmp_path):
    from main import register_exception_handlers

    path = str(tmp_path / "tag_create_limits.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = _override
    app.include_router(classification_router.router, prefix="/api/classification")

    return TestClient(app)


TAG_URL = "/api/classification/tags"


def test_tag_name_max_length_is_20(api):
    """20 字（trim 后）刚好通过：上限不是「19」，也不是列宽 50。"""
    name = "示例" + "甲" * 18                       # 2 + 18 = 20 字（码点）
    assert len(name) == 20

    resp = api.post(TAG_URL, json={"name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == name
    assert body["kind"] == "free"


def test_tag_name_21_chars_rejected_with_20_in_message(api):
    """21 字 → 422 `VALIDATION_ERROR`，且消息里带得上「20」这个数字。"""
    name = "示例" + "乙" * 19                       # 2 + 19 = 21 字
    assert len(name) == 21

    resp = api.post(TAG_URL, json={"name": name})
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    # 具体数字在 details.fields，前端据它说「最多 20 字」
    fields = error["details"]["fields"]
    assert fields, error
    assert any("20" in entry["message"] for entry in fields), error


def test_tag_name_empty_still_rejected(api):
    """空名（含纯空白）仍 422：长度上限不能把「不能为空」这条挤掉。"""
    for blank in ("", "   "):
        resp = api.post(TAG_URL, json={"name": blank})
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_tag_name_over_limit_does_not_create_row(api):
    """超长请求被挡在写库之前：字典里不留半条数据。"""
    name = "示例" + "丙" * 19
    resp = api.post(TAG_URL, json={"name": name})
    assert resp.status_code == 422, resp.text

    listed = api.get(TAG_URL).json()
    assert [row["name"] for row in listed["items"]] == [], listed