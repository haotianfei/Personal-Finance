"""截图批次 API：建批次壳 / 定案（跨图核对唯一校验点）。

规格：ADR-0029 D5/D9/D10；票 04。

两个端点，刻意都很窄：

- `POST /api/screenshot-batches`：只建壳（`status='open'`），拿 `batch_id` 给前端逐图
  apply 时带上；**不做任何图/行写入**（批次是可选对象，旧路径不受影响）。
- `POST /api/screenshot-batches/{id}/finalize`：校验成员齐 → 逐条核对金额 → 单事务
  写核对记录 + 建/校验构成边 + 置 `finalized` + 一条审计。错误码见
  `services/batch_service.finalize` 的校验顺序说明。

鉴权/身份：本仓单用户系统（ADR-0013 前提），审计 actor 由 `audit_service` 缺省 owner。
"""
import logging
from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError, ErrorCode
from services import batch_service
from services.idempotency_service import idempotency_guard

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_data_date(value) -> Optional[_date]:
    """批次数据日期：空/缺省 → None；非法 → 400（严格 YYYY-MM-DD，与 apply 同口径）。"""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400,
                       "data_date 必须为 YYYY-MM-DD 字符串",
                       {"fields": [{"field": "data_date",
                                    "message": "必须为 YYYY-MM-DD"}]})
    text = value.strip()
    if not text:
        return None
    try:
        return _date.fromisoformat(text)
    except ValueError:
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400,
                       "data_date 必须为 YYYY-MM-DD",
                       {"fields": [{"field": "data_date",
                                    "message": "必须为 YYYY-MM-DD"}]}) from None


@router.post("", status_code=201,
             dependencies=[Depends(idempotency_guard("POST /api/screenshot-batches"))])
def create_screenshot_batch(payload: dict, db: Session = Depends(get_db)):
    """建批次壳 → 201 `{batch_id, status: "open", expected_members}`。

    入参 `{data_date?, source_group?, expected_members?, note?}`：
    - `expected_members`（**预计几张图**）是 finalize 判「成员齐没齐」的唯一依据，
      不传就只能要求「至少一条成员」（见 batch_service._ensure_complete）；
    - `source_group` 可传 slug / label；未知组 → 400 `SOURCE_GROUP_UNKNOWN`；
    - `note` ≤200 字。
    """
    if not isinstance(payload, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400, "请求体必须为对象")
    batch = batch_service.create_batch(
        db,
        data_date=_parse_data_date(payload.get("data_date")),
        source_group=payload.get("source_group"),
        expected_members=payload.get("expected_members"),
        note=payload.get("note"),
    )
    return {"batch_id": batch.id, "status": batch.status,
            "expected_members": batch.expected_members}


@router.post("/{batch_id}/finalize",
             dependencies=[Depends(idempotency_guard(
                 "POST /api/screenshot-batches/{id}/finalize"))])
def finalize_screenshot_batch(batch_id: int, payload: dict,
                              db: Session = Depends(get_db)):
    """定案：校验 → 单事务落核对记录/构成边/状态 → 一条审计。

    入参（**冻结契约**，前端已按它对接）：

    ```
    {"reconciliations": [{
        "parent_item_id": 123,              # 必填（唯一锚；可 null → 按 name 解析）
        "parent_evidence_id": None,         # 可空：手选父项时本批截图里没有这一行
        "parent_row_index": None,           # 可空：同上（不占位，null 就是 null）
        "aggregate_amount": "1234.56",
        "children": [{"evidence_id": 45, "row_index": 2, "item_id": 88,
                      "amount": "600.00"}]  # 可省（默认 []）；item_id 可 null
     }],
     "accept_mismatch": {"reason": "…"}}    # **仅顶层**，作用于所有差额条目
    ```

    **不要**用 Pydantic 必填把这两个字段卡成非空（前端手选父项那一支会直接 422）：
    校验层一律 null 容忍，`parent_evidence_id is None` 即判定 `parent_source="manual"`
    并写进审计（否则 `"preview_row"`）。

    错误 details 也一并冻结（前端按键取值）：`BATCH_NOT_COMPLETE` → `{missing_evidence_ids}`；
    `RECONCILE_MISMATCH` → `{reconciliations:[{parent_item_id, aggregate_amount,
    children_sum, diff}], accepted:false}`；`RECONCILE_PARENT_NOT_FOUND` →
    `{parent_row_index, name}`；`RECONCILE_REASON_REQUIRED` → `{}`。

    幂等：`Idempotency-Key` 同键重放 → 首次响应原样回放（不重复建边）；无键重复调用
    → 409 `BATCH_ALREADY_FINALIZED`。**两条路都能保证「构成边只建一次」**。
    """
    if not isinstance(payload, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, 400, "请求体必须为对象")
    try:
        return batch_service.finalize(
            db, batch_id,
            payload.get("reconciliations"),
            accept_mismatch=payload.get("accept_mismatch"),
        )
    except ApiError:
        raise
    except HTTPException:
        raise
    except Exception as e:      # 兜底：不把内部异常细节吐给前端
        logger.exception("screenshot_batch_finalize_failed batch_id=%s", batch_id)
        raise ApiError(ErrorCode.INTERNAL, 500, f"批次定案失败: {e}") from e