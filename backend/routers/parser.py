"""解析词表管理 API（P1b）。

规格：`.scratch/parser-rules-refactor/spec.md` D2.1 / D2.2。
写入校验与落库全部在 `services/parser_rules_service.py`，本路由只做薄的
HTTP 壳（解析请求体 → 调服务 → 返回服务层视图），保证「规则写入口径只有一处」。

四个端点：

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/api/parser/vocabularies` | — | 七个词表的 builtin/add/remove/effective 全量视图 |
| PUT | `/api/parser/vocabularies/{key}` | `{add:[...], remove:[...], reason:"..."}` | 整份覆盖增量，写 `parser_vocabulary_update` 审计 |
| POST | `/api/parser/vocabularies/{key}/reset` | `{reason:"..."}` | 删增量行＝回退出厂值，写 `parser_vocabulary_reset` 审计 |

**未知 key → 404 `NOT_FOUND`**（其余错误码见下表）。选 404 的理由：七个 key 是
固定资源集合（`GET` 会列出来），`{key}` 是路径里的**资源标识**，指向不存在的
资源即 404；422 留给请求体不合法（列表形态 / add∩remove 冲突 / remove 无用项 /
缺 reason），两类错误不混淆。详细论证见 `parser_rules_service._require_key`。

错误码一览（均为 `{"error": {code, message, details}}` 信封）：

- 404 `NOT_FOUND`：`{key}` 不在 `VOCABULARY_KEYS`
- 422 `SOURCE_GROUP_RULES_INVALID`：缺 reason、remove 含不在生效词表中的词、
  add 与 remove 同词、列表内含非字符串/空串
- 422 `VALIDATION_ERROR`：请求体不是 JSON 对象（FastAPI 层）

两个写端点都挂了 `idempotency_guard`（与其它写端点同一约定：带 `Idempotency-Key`
头时防重放，不带则放行）；即使客户端不带，PUT 天然幂等——同 payload 重放返回
`changed=false` 且**不再写审计**，reset 对空增量行同理。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from services import parser_rules_service
from services.idempotency_service import idempotency_guard

router = APIRouter()


@router.get("/vocabularies")
def http_list_vocabularies(db: Session = Depends(get_db)):
    """七个词表的全量视图（顺序＝`VOCABULARY_KEYS`，前端照序渲染即可）。"""
    return parser_rules_service.list_vocabularies(db)


@router.put("/vocabularies/{key}",
            dependencies=[Depends(idempotency_guard("PUT /api/parser/vocabularies/key"))])
def http_update_vocabulary(key: str, data: dict | None = None,
                           db: Session = Depends(get_db)):
    """整份覆盖某词表的 add/remove 增量（未提供的列表视为空）。

    响应含 `affected_groups`（当前启用的来源组数），供前端提示「将在 N 个来源组生效」；
    与现状完全一致时返回 `changed=false` 且不写审计。
    """
    payload = data or {}
    return parser_rules_service.update_vocabulary(
        db, key,
        add=payload.get("add"),
        remove=payload.get("remove"),
        reason=payload.get("reason"),
    )


@router.post("/vocabularies/{key}/reset",
             dependencies=[Depends(idempotency_guard("POST /api/parser/vocabularies/reset"))])
def http_reset_vocabulary(key: str, data: dict | None = None,
                          db: Session = Depends(get_db)):
    """删除该词表的增量行 ＝ 完全回退出厂值（reason 必填，写 reset 审计）。"""
    payload = data or {}
    return parser_rules_service.reset_vocabulary(db, key, reason=payload.get("reason"))