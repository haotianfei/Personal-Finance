"""LLM 供应商配置管理 API。

截图 AI 一致性校验的 OpenAI 兼容供应商（deepseek / dashscope /
openai_compatible）CRUD。api_key 明文存本地库，本模块任何出口经
_serialize_provider 脱敏（前3***后4），绝不回传明文 key。
逻辑函数 + 薄壳 route 分离（照抄 source_groups 模式）。
"""
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from services.idempotency_service import idempotency_guard
from models import LLMProvider
from schemas import AUDIT_REASON_NOTE_MAX, AuditReason
from services import audit_service, llm_verification_service

router = APIRouter()

VALID_KINDS = ("deepseek", "dashscope", "openai_compatible")

# D2：只有这三个字段的**值真的变了**才作废测试结论（label/enabled/排序不影响结论）
CONFIG_FIELDS = ("base_url", "model")
TEST_STATUS_FIELDS = ("last_tested_at", "last_test_ok", "last_test_message",
                      "last_test_elapsed_ms")


def _mask_key(api_key: str) -> str:
    """脱敏：长度>7 → 前3字符+'***'+后4字符；否则一律 '***'。"""
    key = api_key or ""
    if len(key) > 7:
        return key[:3] + "***" + key[-4:]
    return "***"


def _serialize_provider(row: LLMProvider) -> dict:
    """对外序列化契约：只含 api_key_masked，永不含明文 api_key。

    末 4 键是最近一次连通性测试结果（0034）：从未测试时全为 None（不是 False / 0）。
    """
    return {
        "id": row.id,
        "label": row.label,
        "kind": row.kind,
        "base_url": row.base_url,
        "model": row.model,
        "api_key_masked": _mask_key(row.api_key),
        "is_default": bool(row.is_default),
        "enabled": bool(row.enabled),
        "sort_order": row.sort_order,
        "last_tested_at": (row.last_tested_at.isoformat()
                           if row.last_tested_at else None),
        "last_test_ok": row.last_test_ok,
        "last_test_message": row.last_test_message,
        "last_test_elapsed_ms": row.last_test_elapsed_ms,
    }


def _validate(data: dict, db: Session, exclude_id: int | None = None) -> None:
    kind = data.get("kind")
    if kind is not None and kind not in VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind 非法: {kind}（可选 {'/'.join(VALID_KINDS)}）")
    label = data.get("label")
    if label:
        q = select(LLMProvider).where(LLMProvider.label == label.strip())
        if exclude_id is not None:
            q = q.where(LLMProvider.id != exclude_id)
        if db.execute(q).scalar_one_or_none():
            raise HTTPException(status_code=400, detail="供应商名称已存在")


def _clear_other_defaults(db: Session, keep_id: int | None = None) -> None:
    """is_default 全表唯一：把其它行的 is_default 置 False。"""
    q = select(LLMProvider).where(LLMProvider.is_default.is_(True))
    if keep_id is not None:
        q = q.where(LLMProvider.id != keep_id)
    for row in db.execute(q).scalars().all():
        row.is_default = False


def _clear_test_status(row: LLMProvider) -> None:
    """作废最近一次测试结论：四列一并置 NULL（不是 False / 0）。"""
    for field in TEST_STATUS_FIELDS:
        setattr(row, field, None)


# ---------------------------------------------------------------- 留痕（ADR-0027 ②口径桶）
#
# LLM 供应商配置决定「哪家模型、什么地址、什么密钥」参与截图一致性校验——它改的是
# 判定口径，不是缓存。**密钥是审计面的硬约束**（ADR-0027 D6）：`api_key` 原样交给
# `audit_service.make_change()`，由 `audit_contract.scrub()` 单点脱敏成
# `"<redacted>"` ＋ `api_key_fp`（sha256 前 8 位）/ `api_key_len`；
# **本模块不做任何手工脱敏**——手工脱敏一旦与单点规则漂移就会出现明文落库。
#
# 快照分型（D5）：create → 关键字段全集落 after；update → **仅变更字段**（密钥真变时
# 额外打 `rotated_key: True`，回答「这次是不是换了钥匙」）；delete → 全集落 before。

_PROVIDER_FIELDS = ("label", "kind", "base_url", "api_key", "model",
                    "is_default", "enabled", "sort_order")


def _provider_fields(row: LLMProvider) -> dict:
    """关键字段快照（`api_key` 明文进 dict，脱敏由 audit_service 单点完成）。"""
    snap = {field: getattr(row, field) for field in _PROVIDER_FIELDS}
    snap["is_default"] = bool(snap["is_default"])
    snap["enabled"] = bool(snap["enabled"])
    return snap


def _provider_snapshot(row: LLMProvider) -> dict:
    """create/delete 用的全集快照（带 id）。"""
    return {"id": row.id, **_provider_fields(row)}


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """只留值真的变了的键（update 分型：两侧保持同一组键）。"""
    keys = list(after) + [key for key in before if key not in after]
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return ({key: before.get(key) for key in changed},
            {key: after.get(key) for key in changed})


def _apply_config_fields(row: LLMProvider, data: dict) -> bool:
    """写入 base_url / model / api_key，返回「值是否真的变了」。

    两处陷阱（spec D2 判定细节）：

    ① 编辑表单会回传全量字段——若按「字段出现在 payload 里」判断，改个名称也会误清
       旧结论，故一律比较新旧值（写入前 strip，与落库形态一致）；
    ② `api_key` 缺省或空串是既有「不改 key」语义（前端只拿得到脱敏值），不算变更。
    """
    changed = False
    for field in CONFIG_FIELDS:
        if field not in data or not data[field]:
            continue
        value = str(data[field]).strip()
        if value != getattr(row, field):
            setattr(row, field, value)
            changed = True
    api_key = (data.get("api_key") or "").strip()
    if api_key and api_key != row.api_key:
        row.api_key = api_key
        changed = True
    return changed


# --- CRUD 逻辑函数（dict 进出） ---
def list_providers(db: Session) -> list[dict]:
    """全部供应商（按 sort_order、id 排序，脱敏）。"""
    rows = db.execute(
        select(LLMProvider).order_by(LLMProvider.sort_order, LLMProvider.id)
    ).scalars().all()
    return [_serialize_provider(r) for r in rows]


def create_provider(data: dict, db: Session) -> dict:
    """创建供应商；is_default=true 时先清其它行默认。"""
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="供应商名称不能为空")
    if not (data.get("base_url") or "").strip():
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    if not (data.get("model") or "").strip():
        raise HTTPException(status_code=400, detail="model 不能为空")
    _validate(data, db)

    if data.get("is_default"):
        _clear_other_defaults(db)

    row = LLMProvider(
        label=label,
        kind=data.get("kind"),
        base_url=(data.get("base_url") or "").strip(),
        api_key=(data.get("api_key") or "").strip(),
        model=(data.get("model") or "").strip(),
        is_default=bool(data.get("is_default", False)),
        enabled=bool(data.get("enabled", True)),
        sort_order=int(data.get("sort_order", 0) or 0),
    )
    db.add(row)
    db.flush()                       # 拿 id 后再记审计行（同事务；api_key 由单点脱敏）
    audit_service.record(db, audit_service.make_change(
        "llm_provider", row.id, "create", None, _provider_snapshot(row)))
    db.commit()
    db.refresh(row)
    return _serialize_provider(row)


def update_provider(provider_id: int, data: dict, db: Session) -> dict:
    """部分字段更新。

    api_key 缺省或空串 → 不更新 key（前端只展示脱敏值，回传空即保持原 key）。
    is_default=true → 先把其它行置 False，保证全表唯一。
    base_url / model / api_key 的**值真的变了** → 清空最近一次测试结果（spec D2）：
    换了地址或密钥之后「连接成功」不再成立，留着就是谎话。
    """
    row = db.get(LLMProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    _validate(data, db, exclude_id=row.id)

    before = _provider_fields(row)     # 留痕用：密钥明文只在本进程内存里，落库前即脱敏
    if "label" in data and data["label"]:
        row.label = data["label"].strip()
    if "kind" in data and data["kind"]:
        row.kind = data["kind"]
    # base_url / model / api_key 三项：值真的变了才动，并作废旧测试结论
    if _apply_config_fields(row, data):
        _clear_test_status(row)
    if "sort_order" in data:
        row.sort_order = int(data["sort_order"] or 0)
    if "enabled" in data:
        row.enabled = bool(data["enabled"])
    if "is_default" in data:
        if data["is_default"]:
            _clear_other_defaults(db, keep_id=row.id)
            row.is_default = True
        else:
            row.is_default = False

    before_changed, after_changed = _changed_fields(before, _provider_fields(row))
    rotated = "api_key" in after_changed        # 密钥真变（缺省/空串＝不改，不算轮换）
    if rotated:
        after_changed["rotated_key"] = True     # 备注：这次换了钥匙（D4 裁决 4）
    if after_changed:
        audit_service.record(db, audit_service.make_change(
            "llm_provider", row.id, "update", before_changed, after_changed))

    db.commit()
    db.refresh(row)
    return _serialize_provider(row)


def delete_provider(provider_id: int, db: Session, *,
                    audit_reason: str | None = None,
                    audit_reason_note: str | None = None) -> dict:
    """删除供应商。

    默认供应商允许删除（不像来源组那样阻断）；删除后若仍有 enabled 行，
    把其中 sort_order 最小（同序取 id 最小）的自动设为默认——保证
    verify 端点「缺省用 is_default 且 enabled」始终可解析，实现更简单稳妥。
    """
    row = db.get(LLMProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    was_default = bool(row.is_default)
    before = _provider_snapshot(row)   # 删前全集（api_key 由 audit_service 单点脱敏）
    db.delete(row)
    db.flush()
    audit_service.record(db, audit_service.make_change(
        "llm_provider", provider_id, "delete", before, None,
        reason=audit_reason, reason_note=audit_reason_note))
    db.commit()

    if was_default:
        candidates = db.execute(
            select(LLMProvider).where(LLMProvider.enabled.is_(True))
            .order_by(LLMProvider.sort_order, LLMProvider.id)
        ).scalars().all()
        if candidates:
            candidates[0].is_default = True
            db.commit()
    return {"ok": True}


# ---------------- 连通性测试 ----------------
def test_provider(provider_id: int, db: Session) -> dict:
    """对指定供应商发极简 ping 对话，返回 {ok, message, elapsed_ms}。

    取整行明文 api_key（勿用脱敏序列化）；空 key → 400；enabled=False
    也允许测试（连通性不受启用开关限制）。网络异常不抛出——由
    test_provider_connection 消化成 ok=False。

    写库只在本函数内部完成（spec D1 / D6）：四列记最近一次结果；**对外响应形状
    仍是那三键**，不扩不缩（前端与既有测试零改动）。
    """
    row = db.get(LLMProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not (row.api_key or "").strip():
        raise HTTPException(status_code=400, detail="该供应商尚未填写 API key")
    provider = {"id": row.id, "kind": row.kind, "base_url": row.base_url,
                "api_key": row.api_key, "model": row.model}
    result = llm_verification_service.test_provider_connection(provider)

    row.last_tested_at = datetime.now()
    row.last_test_ok = bool(result.get("ok"))
    # 列宽 300：错误摘要先按 300 截断，不靠 SQLite 的宽容类型
    row.last_test_message = str(result.get("message") or "")[:300]
    row.last_test_elapsed_ms = int(result.get("elapsed_ms") or 0)
    db.commit()
    return result


# --- 路由（薄壳） ---
@router.get("")
def http_list_providers(db: Session = Depends(get_db)):
    return list_providers(db)


@router.post("", dependencies=[Depends(idempotency_guard("POST /api/llm-providers"))])
def http_create_provider(data: dict, db: Session = Depends(get_db)):
    return create_provider(data, db)


@router.put("/{provider_id}", dependencies=[Depends(idempotency_guard("PUT /api/llm-providers/id"))])
def http_update_provider(provider_id: int, data: dict, db: Session = Depends(get_db)):
    return update_provider(provider_id, data, db)


@router.delete("/{provider_id}", dependencies=[Depends(idempotency_guard("DELETE /api/llm-providers/id"))])
def http_delete_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    return delete_provider(provider_id, db, audit_reason=reason,
                           audit_reason_note=reason_note)


# /{id}/test 与 GET ""/POST ""/PUT /{id}/DELETE /{id} 无冲突（独特子路径）
@router.post("/{provider_id}/test")
def http_test_provider(provider_id: int, db: Session = Depends(get_db)):
    return test_provider(provider_id, db)
