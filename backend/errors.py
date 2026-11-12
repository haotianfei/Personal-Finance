"""错误信封与错误码（v5 契约层，spec §2——契约单源 .scratch/v5-contract/spec.md）。

形状（全部非 2xx 统一）：
    {"error": {"code": "<大写蛇形>", "message": "<人读中文>", "details": {...}|null}}
HTTP 状态码保留语义。新代码一律 raise ApiError；存量 176 处 HTTPException
由 main.py 的包装器兜底转码（映射表 + GENERIC_<status> 归一），增量收敛。
"""
from typing import Any

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore
        pass


class ErrorCode(StrEnum):
    # 通用
    BAD_REQUEST = "BAD_REQUEST"                     # 400
    NOT_FOUND = "NOT_FOUND"                         # 404
    VALIDATION_ERROR = "VALIDATION_ERROR"           # 422，details.fields=[{field,message}]
    INTERNAL = "INTERNAL"                           # 500，不泄栈
    # ledger（步骤 3 写语义）
    WRITE_ONCE_CONFLICT = "WRITE_ONCE_CONFLICT"     # 409，details={row_id,hint}
    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"         # 409，details={conflict_records,skipped}
    ITEM_ARCHIVED = "ITEM_ARCHIVED"                 # 409
    ITEM_ALREADY_CORRECTED = "ITEM_ALREADY_CORRECTED"  # 409
    # 血缘补登记（lineage-attach §1.1）
    LINEAGE_CYCLE = "LINEAGE_CYCLE"                 # 400，补链会形成环
    LINEAGE_EXISTS = "LINEAGE_EXISTS"               # 409，同一 (from,to) 已登记
    NAME_UNRESOLVED = "NAME_UNRESOLVED"             # 404
    INVALID_SIDE = "INVALID_SIDE"                   # 400，仅 asset/liability
    PRINCIPAL_REASON_REQUIRED = "PRINCIPAL_REASON_REQUIRED"  # 400，口径变更缺理由（R-9）
    SNAPSHOT_REQUIRED = "SNAPSHOT_REQUIRED"           # 409，转移登记缺当日 active 快照（R-34 注）
    # ingest / backup
    SOURCE_GROUP_UNKNOWN = "SOURCE_GROUP_UNKNOWN"   # 400
    SOURCE_GROUP_RULES_INVALID = "SOURCE_GROUP_RULES_INVALID"  # 422，规则/词表校验失败
    APPLY_CONFLICT = "APPLY_CONFLICT"               # 409，details=conflicts 清单
    # 导入身份裁决（item-identity-drift D4）：有 identity_guess 的行未表态 → 阻断采纳
    IDENTITY_DECISION_REQUIRED = "identity_decision_required"  # 400，details.rows=[...]
    BUNDLE_INVALID = "BUNDLE_INVALID"               # 400
    DB_FILE_INVALID = "DB_FILE_INVALID"             # 400
    EVIDENCE_WRITE_FAILED = "EVIDENCE_WRITE_FAILED"  # 500，存证前置失败阻断识别（步骤 6）
    # 凭证只读端点（evidence-manager D2，票 02）
    EVIDENCE_PATH_INVALID = "EVIDENCE_PATH_INVALID"  # 400，file_path 越出凭证根目录
    EVIDENCE_FILE_MISSING = "EVIDENCE_FILE_MISSING"  # 404，库里有行、盘上没有文件
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"        # 404，凭证行不存在（识别回写的显式调用）
    # 凭证删除与来源组守卫（evidence-manager D5/D6，票 03）
    EVIDENCE_IN_USE = "EVIDENCE_IN_USE"              # 409，已使用凭证不可删（证据链）
    SOURCE_GROUP_HAS_USED_EVIDENCE = "SOURCE_GROUP_HAS_USED_EVIDENCE"      # 409，组内仍有已使用凭证
    SOURCE_GROUP_HAS_UNUSED_EVIDENCE = "SOURCE_GROUP_HAS_UNUSED_EVIDENCE"  # 409，组内仍有未使用凭证
    # 幂等
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"  # 422，同键异体
    # C1 目标（三期-1，ADR-0010 §6.3）
    GOAL_VERSION_CONFLICT = "GOAL_VERSION_CONFLICT"    # 409，并发激活/版本号冲突
    GOAL_VERSION_IMMUTABLE = "GOAL_VERSION_IMMUTABLE"  # 409，改/删非 draft
    # 审计链（三期-3 W1，ADR-0013 D10）
    AUDIT_CHAIN_CONFLICT = "AUDIT_CHAIN_CONFLICT"          # 409，链状态 CAS 失败
    AUDIT_CHAIN_UNINITIALIZED = "AUDIT_CHAIN_UNINITIALIZED"  # 500，0018 未执行/状态行缺失
    AUDIT_ROLLBACK_DETECTED = "AUDIT_ROLLBACK_DETECTED"    # 409，恢复目标比当前库旧/分叉（整库回退）
    # 建议闭环（三期-3 W4/W5，ADR-0013 D9）
    ADVISOR_SUGGESTION_CLOSED = "ADVISOR_SUGGESTION_CLOSED"      # 409，终态再裁决
    ADVISOR_NOT_ACCEPTED = "ADVISOR_NOT_ACCEPTED"                # 409，未采纳写执行备注
    ADVISOR_PROVIDER_UNAVAILABLE = "ADVISOR_PROVIDER_UNAVAILABLE"  # 409，无默认启用供应商
    ADVISOR_DUPLICATE_TRIGGER = "ADVISOR_DUPLICATE_TRIGGER"      # 409，dedupe_key 冲突
    ADVISOR_INFO_SET_CORRUPT = "ADVISOR_INFO_SET_CORRUPT"        # 409，信息集重算不一致
    # 复盘（三期-4 W2/W4，ADR-0014）
    REVIEW_CLOSED = "REVIEW_CLOSED"                              # 409，已关闭复盘再改
    REVIEW_SNAPSHOT_CORRUPT = "REVIEW_SNAPSHOT_CORRUPT"          # 409，快照重算不一致
    REVIEW_PROPOSAL_DECIDED = "REVIEW_PROPOSAL_DECIDED"          # 409，提案终态再裁决
    REVIEW_PROPOSAL_DUPLICATE = "REVIEW_PROPOSAL_DUPLICATE"      # 409，同内容待裁决已存在
    # 标签（P4a）
    TAG_LIMIT_EXCEEDED = "TAG_LIMIT_EXCEEDED"                    # 422，单记录 >5 标签
    # 资产分类（ADR-0019）
    CLASS_NOT_LEAF = "CLASS_NOT_LEAF"                            # 422，类别未落到叶子
    CLASS_SIDE_CONFLICT = "CLASS_SIDE_CONFLICT"                  # 422/409，类别与记录侧别冲突
    CLASS_CYCLE = "CLASS_CYCLE"                                  # 400，类别父子成环
    CLASS_HAS_CHILDREN = "CLASS_HAS_CHILDREN"                    # 409，还有子类别
    CLASS_IN_USE = "CLASS_IN_USE"                                # 409，被资产项引用
    CLASS_CODE_EXHAUSTED = "CLASS_CODE_EXHAUSTED"                # 409，自动编号无空位（勿抛 500）
    DICT_IN_USE = "DICT_IN_USE"                                  # 409，字典项被引用
    DIMS_INCOMPLETE = "DIMS_INCOMPLETE"                          # 422，四维必填未补齐
    # 录入绑定（ADR-0026 D7）
    DUPLICATE_BIND_TARGET_DATE = "DUPLICATE_BIND_TARGET_DATE"    # 409，同批两行链到同一项
    # 改名契约（ADR-0026）
    RENAME_REASON_REQUIRED = "RENAME_REASON_REQUIRED"            # 400，缺原因/影响范围
    RENAME_NOTE_REQUIRED = "RENAME_NOTE_REQUIRED"                # 400，非默认组合缺备注
    NAME_CONFLICT = "NAME_CONFLICT"                              # 409，目标名归一后已被他项占用
    RENAME_CONFIRM_INVALID = "RENAME_CONFIRM_INVALID"            # 409，令牌失效/影响面已变
    # 审计留痕约束（ADR-0027 D3）
    BATCH_LIMIT_EXCEEDED = "BATCH_LIMIT_EXCEEDED"                # 400，单次批量写超 500 行上限
    AUDIT_REASON_REQUIRED = "AUDIT_REASON_REQUIRED"              # 422，纠正类写入缺 reason
    # 构成关系（ADR-0029 D3/D4，汇总项与构成关系）
    COMPOSITION_SELF_PARENT = "COMPOSITION_SELF_PARENT"          # 400，父=子（自己不能构成自己）
    COMPOSITION_CYCLE = "COMPOSITION_CYCLE"                      # 409，祖先链成环
    COMPOSITION_PARENT_CONFLICT = "COMPOSITION_PARENT_CONFLICT"  # 409，一子一父：该子项已有父
    # 汇总行与跨图核对（ADR-0029 D5/D6/D9；票 03/04）
    RECONCILE_CONFLICT = "RECONCILE_CONFLICT"                    # 400，同一行既跳过又标汇总
    RECONCILE_PARENT_NOT_FOUND = "RECONCILE_PARENT_NOT_FOUND"    # 409，汇总行解析不到既有资产项
    # 注：子项解析不到**不是**错误（冻结契约第 2 条：item_id 可为 null，按名解析，
    # 解析不到只进 `unresolved_children` 回执），故不设 CHILD_NOT_FOUND 码。
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"                    # 409，差额≠0 且未接受
    RECONCILE_REASON_REQUIRED = "RECONCILE_REASON_REQUIRED"      # 400，接受差额但理由为空
    BATCH_ALREADY_FINALIZED = "BATCH_ALREADY_FINALIZED"          # 409，批次已定案
    BATCH_NOT_COMPLETE = "BATCH_NOT_COMPLETE"                    # 409，成员不齐/有失败
    BATCH_ABANDONED = "BATCH_ABANDONED"                          # 409，批次已弃置（终态）
    # 取证视图（票 02）
    INVALID_CURSOR = "INVALID_CURSOR"                            # 400，游标无法解码/与查询不一致


# 无 code 的字符串 detail HTTPException 的按状态码转码表；未覆盖状态 → GENERIC_<status>
CODE_BY_STATUS: dict[int, str] = {
    400: ErrorCode.BAD_REQUEST,
    404: ErrorCode.NOT_FOUND,
    422: ErrorCode.VALIDATION_ERROR,
    500: ErrorCode.INTERNAL,
}


class ApiError(Exception):
    """业务错误单点：router/service raise，main handler 转信封。"""

    def __init__(self, code: str, status_code: int, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.status_code = status_code
        self.message = message
        self.details = details


def envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": str(code), "message": message, "details": details}}


# 存量「准信封」dict detail（routers/assets.py 两处）→ 精确码映射
_QUASI_ERROR_MAP: dict[str, tuple[str, int]] = {
    "write_once": (ErrorCode.WRITE_ONCE_CONFLICT, 409),
    "conflict_detected": (ErrorCode.SNAPSHOT_CONFLICT, 409),
    "invalid_side": (ErrorCode.INVALID_SIDE, 400),
}


def build_envelope_from_detail(status_code: int, detail: Any) -> dict:
    """HTTPException.detail → 信封。

    - dict 且含 code：透传（{"error": {code,message,details}} 语义）
    - dict 且含 error（准信封）：映射精确码，details＝其余键（message 提升到顶层）
    - 其它（字符串等）：按 CODE_BY_STATUS 转码，无映射归 GENERIC_<status>
    """
    if isinstance(detail, dict):
        if "code" in detail:
            return envelope(
                detail.get("code"),
                str(detail.get("message") or detail.get("code")),
                detail.get("details"),
            )
        if "error" in detail:
            key = str(detail.get("error"))
            code, _mapped_status = _QUASI_ERROR_MAP.get(key, (key.upper(), status_code))
            details = {k: v for k, v in detail.items() if k not in ("error", "message")}
            return envelope(code, str(detail.get("message") or key),
                            details or None)
        return envelope(CODE_BY_STATUS.get(status_code) or f"GENERIC_{status_code}",
                        str(detail), detail)
    return envelope(CODE_BY_STATUS.get(status_code) or f"GENERIC_{status_code}",
                    str(detail), None)
