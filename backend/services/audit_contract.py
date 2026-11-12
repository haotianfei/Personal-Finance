"""审计留痕契约（ADR-0027）——**唯一真源**：受控枚举 + 行内脱敏。

本模块只放四类东西，全部是纯数据/纯函数（无 db、无 session、无日志）：

1. `ENTITY` / `ACTION`：审计行 `entity` / `action` 的受控枚举。取值＝**现网已在用的
   拼写**（逐字照抄，不重命名——改名会让历史行与新行的口径分裂、也会炸生产写入）。
   新增只允许「先登记再使用」；`audit_service.make_change()` 是唯一校验点。
2. `REASON` / `REASON_REQUIRED_ACTIONS`：变更原因分类（ADR-0027 D2/D3）。`REASON`
   是全域封闭集，`make_change()` 只在 `reason` **已提供**时校验取值；
   `REASON_REQUIRED_ACTIONS` 是「应当说明原因」的纠正类动作清单——**仅用于测试的
   债务清单**（`tests/test_audit_contract.py::KNOWN_DEBT`），**运行时绝不强制**：
   存量 20+ 个纠正类调用点与前端请求体都还没有 reason 字段，强制会立刻 500。
   缺口由债务清单显式记录，后续票逐条清零（调用点补 reason → 删清单条目）。
3. `SENSITIVE_KEY_PATTERNS` + `redact_value()` / `scrub()`：审计 before/after 的
   密钥脱敏。审计日志是「永久保留、可被整库导出」的表面，任何 api_key/token/
   secret/password/authorization 类字段都不得以明文落库（也不得进日志）。
4. `fingerprint()`：脱敏值的可追溯指纹（sha256 前 8 位 hex）。指纹**不可逆**，
   用于「同一密钥前后是否变过」的比对，不泄露原值。

脱敏规则（`scrub`）：
- 键名**大小写不敏感、子串匹配**任一 `SENSITIVE_KEY_PATTERNS` → 值替换为
  `"<redacted>"`，并在**同一层**追加 `<原键>_fp`（sha256 前 8 位）与
  `<原键>_len`（原值长度）。
- 非字符串值同样处理：先 `str()` 再算指纹/长度（数字密钥也算密钥）。
- 递归 dict/list，**返回新对象、不改入参**；重复 scrub 幂等（已脱敏且已带
  `_fp` 的值不再二次计算，避免把 `"<redacted>"` 的指纹覆盖掉原值指纹）。
- 普通字段原样透传（类型不变），保证既有审计形状契约不受影响。

**不做日志、不 print 任何密钥**：本模块没有任何 logger。
"""
import hashlib
from dataclasses import dataclass

# --------------------------------------------------------------- entity / action

# 受控枚举（现网实际值；来源：services/*.py + services/advisor/*.py 的
# make_change/_change 调用点，以及 db_migration_service 的 dict 路径、
# 0016/0022 迁移的裸 INSERT，加上并行车道已登记的写入面）。
#
# 批次约定（并行车道，ADR-0027 落地批次留痕）：
# - **批次头**统一 `entity='batch'`, `action='execute'`，且
#   `after={'op': 'delete'|'create'|'update', 'count': N,
#          'ids_hash': '<sha256 前 16>', 'scope': {...}}`；
#   逐行明细**仍各自用业务 entity/action**（如 asset_snapshot/delete）——
#   批次头只回答「这一次点了什么」，明细回答「动了哪几行」。
# - `batch_delete` / `batch_create` **仅用于「无法逐行」的场合**（例如按期间批量
#   建行且头行与明细行都要写时，优先逐行，不额外用这两个 action）；
#   能用逐行明细表达就不要用它们，否则审计读者要面对两套粒度。
ENTITY = frozenset({
    "advisor_auto_trigger",     # advisor/service.py（限流留痕，actor=system）
    "advisor_decision",         # advisor/service.py（accept/reject/expire/execution_note）
    "advisor_info_set",         # advisor/info_set.py（freeze）
    "advisor_settings",         # advisor/service.py（update）
    "advisor_suggestion",       # advisor/service.py（create/generated）
    "allocation_target",        # allocation_service.py（create/update/delete）
    "alert",                    # alerts_service.py（阈值规则＝口径）
    "classification",           # classification_service.py（分类/成员字典、项级改判）
    "expense",                  # expense_service.py（支出行/分类/模板）
    "export",                   # export_history_service.py（导出历史与规则＝离场）
    "source_group",             # source_group_service.py（来源组/解析规则/校正规则）
    "asset_item",               # asset_service.py / asset_rename_service.py
    "asset_item_owner",         # asset_owner_service.py（归属改判）
    "asset",                    # 并行车道：批量逐行明细用的记录级 entity（历史拼写是
                                # asset_snapshot；两者并存，勿擅自归并——归并＝改写历史口径）
    "asset_snapshot",           # asset_service.py / screenshot_service.py
    "backup",                   # 并行车道：备份/恢复类操作（restore/upload_db）
    "batch",                    # 并行车道：批次头（action='execute'，见上方批次约定）
    "goal",                     # goal_service.py
    "goal_version",             # goal_service.py
    "import",                   # 并行车道：导入（import_seed/import_confirm）
    "item_composition",         # composition_service.py（构成边 create/remove，ADR-0029）
    "lineage_attach",           # lineage_service.py
    "lineage_link",             # lineage_service.py
    "lineage_split",            # lineage_service.py
    "llm_provider",             # 并行车道：LLM 供应商（rotate_key 等）
    "parser_vocabulary",        # parser_rules_service.py
    "principal_change",         # principal_service.py / lineage_service.py
    "review",                   # advisor/review.py
    "review_proposal",          # advisor/review.py
    "schema",                   # db_migration_service.py / 0016 / 0022 /
                                # rebuild_audit_service.py（离线重建留痕，见 rebuild_import）
    "screenshot_evidence",      # evidence_service.py（delete）
    "screenshot_batch",         # batch_service.py（批次壳 / finalize，ADR-0029 D9）
    "screenshot_preview_row",   # screenshot_service.py（identity_decide）
    "transfer_event",           # transfer_service.py
})

ACTION = frozenset({
    "accept",                   # advisor_decision
    "activate",                 # goal_version
    "adopt",                    # review_proposal
    "apply_row",                # asset_snapshot（截图采纳逐行）
    "archive",                  # asset_item / goal / goal_version（纠正类）
    "batch_create",             # batch：仅「无法逐行」的批量建行（见批次约定）
    "batch_delete",             # batch：仅「无法逐行」的批量删行（见批次约定）
    "change",                   # asset_item_owner（**现网拼写**＝归属改判）
    "classification_change",    # 分类改判新拼写（现网经 asset_item_owner.change）
    "close",                    # review
    "conclusion",               # review
    "correct",                  # asset_snapshot（更正金额/方向）
    "create",
    "delete",
    "dismiss",                  # review_proposal
    "execute",                  # batch：批次头统一动作（after＝op/count/ids_hash/scope）
    "execution_note",           # advisor_decision
    "expire",                   # advisor_decision（actor=system）
    "finalize",                 # screenshot_batch（批次定案；**不属纠正类**，不强制 reason。
                                # 定案不改任何测量值，只写核对痕迹 + 建构成边）
    "freeze",                   # advisor_info_set（actor=system）
    "generated",                # advisor_suggestion（actor=system 等）
    "identity_decide",          # screenshot_preview_row（身份裁决留痕）
    "import_confirm",           # import
    "import_seed",              # import
    "limit",                    # advisor_auto_trigger
    "lineage_link",             # lineage_link（键名即动作，历史拼写，勿改）
    "migration_run",            # schema（runner 迁移记账）
    "note_update",              # asset_item（项级备注改写，ADR-0031 D2/D3：只落审计与
                                # updated_at，不进 attr_history）
    "owner_backfill",           # schema（0016/0022 回填记账，裸 INSERT）
    "owner_change",             # 归属改判新拼写（现网拼写是 change，两者并存）
    "parser_vocabulary_reset",  # parser_vocabulary
    "parser_vocabulary_update", # parser_vocabulary
    "principal_change",         # principal_change（非首次设定）
    "principal_set",            # principal_change（首次设定）
    "rebuild_import",           # schema（离线重建留痕：scripts/rebuild/s4 +
                                # services/rebuild_audit_service.py，非 HTTP 端点）
    "reject",                   # advisor_decision
    "remove",                   # item_composition（解除构成边；**不属纠正类**，不强制 reason。
                                # 刻意不用 delete：delete 在 REASON_REQUIRED_ACTIONS 里，
                                # 会让「解除构成」被 422 卡住，而 ADR-0029 D4 明确解除是
                                # 无摩擦的常规动作）
    "rename",                   # asset_item
    "restore",                  # backup（还原既有库/行；纠正类 → 必须有 reason）
    "rotate_key",               # llm_provider（轮换密钥；密钥值本身由 scrub 脱敏）
    "set_primary",              # goal
    "transfer_register",        # transfer_event
    "unarchive",                # asset_item
    "update",
    "upload_db",                # backup（上传数据库文件）
})

# --------------------------------------------------------------- reason

REASON = frozenset({
    "correction",       # 更正：先前记错了（金额/方向/归属/名称）
    "evolution",        # 演进：事实变了（新增/到期/口径随事实调整）
    "normalization",    # 归一：口径/命名/分类体系统一，值本身不变
    "import",           # 导入：外部数据首次落库
    "migration",        # 迁移：结构或基线搬迁产生
    "other",            # 其它（必须写 reason_note 说清）
})

# 纠正类动作：改的是「已经记下的东西」，**应当**说明原因（ADR-0027 D3）。
# 用途严格限定：**仅供测试构造债务清单**（tests/test_audit_contract.py::KNOWN_DEBT），
# `make_change()` **不做**运行时强制——缺 reason 合法（存量为 NULL＝未记录原因）。
# 理由：现网 20+ 个纠正类调用点与前端请求体都还没有 reason 字段，强制即 500。
# 名单：correct / delete / archive / restore / owner_change / classification_change。
# **拼写以现网为准并登记别名**：归属改判在现网的 action 是 `change`
# （asset_owner_service.append_change），分类改判现网也走同一条 `change`
# （classification_service 经 append_change），故 `change` 作为别名同列在此；
# 并行车道的新拼写 `owner_change`/`classification_change` 也一并列入。
# 不在此列：create/update/activate/unarchive/batch execute 等状态机正常流转，
# 以及 rename（原 brief 未列为纠正类，改名原因走 asset_item_names.reason）。
REASON_REQUIRED_ACTIONS = frozenset({
    "correct",                # 更正既有快照/数值
    "delete",                 # 删除既有记录（记录消失＝最需要理由的改判）
    "archive",                # 归档既有记录（从现役视图消失）
    "restore",                # 还原（备份/回收站 → 现役，对既有状态的改判）
    "owner_change",           # 归属改判（新拼写）
    "classification_change",  # 分类改判（新拼写）
    "change",                 # 现网别名：asset_item_owner 的归属/分类改判
})

# --------------------------------------------------------------- 脱敏

REDACTED = "<redacted>"

# 子串匹配（大小写不敏感）。
#
# **裸 `key` 刻意不进黑名单**（ADR-0027 D7 修订，2026-11-07）：本域里含 "key" 但必须
# 可读的字段太多——parser 词表的 `key`、`idempotency_key`、`dedupe_key`、各类
# `*_key` 业务键。脱敏它们不会泄密，却会把审计行变成不可用的红色方块（取证优先）。
# 因此只收录**明确是凭证**的拼写家族：api_key/apikey/access_key/private_key/
# secret(_key)/password/passwd/token/authorization/credential。
SENSITIVE_KEY_PATTERNS = ("api_key", "apikey", "access_key", "private_key", "secret",
                          "password", "passwd", "token", "authorization", "credential")

# 这些模式只做「整名/后缀」匹配，不做前缀匹配（否则 token_count、token_usage 被误伤）
_NO_PREFIX_PATTERNS = frozenset({"token", "authorization"})

_FP_SUFFIX = "_fp"
_LEN_SUFFIX = "_len"


def fingerprint(value) -> str:
    """sha256(utf8(str(value))) 的十六进制前 8 位——不可逆指纹。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def redact_value(value) -> tuple[str, str, int]:
    """敏感值 → `(REDACTED, fp, length)`；length 是原值 `str()` 长度。"""
    text = value if isinstance(value, str) else str(value)
    return REDACTED, fingerprint(text), len(text)


def _is_sensitive_key(key) -> bool:
    """凭证类键名判定：**整名 / 前缀 / 后缀**匹配，不做任意子串匹配。

    任意子串会把 `max_tokens`、`Dedupe_Key`、`vocab_key` 一起打成红方块，审计行
    随即失去取证价值。故 `normalize(key)` 后要求：等于某模式，或以 `<模式>_` 开头，
    或以 `_<模式>` 结尾（允许 `-`/空格归一为 `_`）。

    例外：`token`/`authorization` **不做前缀匹配**——本域里 `token_count`、
    `token_usage`、`token_limit` 全是可读的计量元数据，前缀匹配会把它们误伤。
    """
    lowered = str(key).lower().replace("-", "_").replace(" ", "")
    for pattern in SENSITIVE_KEY_PATTERNS:
        if lowered == pattern or lowered.endswith("_" + pattern):
            return True
        if pattern not in _NO_PREFIX_PATTERNS and lowered.startswith(pattern + "_"):
            return True
    return False


def _companion_base(key):
    """`<base>_fp` / `<base>_len` → base；否则 None。"""
    text = str(key)
    for suffix in (_FP_SUFFIX, _LEN_SUFFIX):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return None


def scrub(obj):
    """递归脱敏（返回新对象，不改入参；幂等）。规则见模块 docstring。"""
    if isinstance(obj, dict):
        return _scrub_dict(obj)
    if isinstance(obj, (list, tuple)):
        return [_scrub_item(item) for item in obj]
    return obj


def _scrub_item(item):
    """列表元素：容器递归，标量原样（标量本身没有键名可判，不臆测内容）。"""
    if isinstance(item, (dict, list, tuple)):
        return scrub(item)
    return item


def _scrub_dict(obj: dict) -> dict:
    out: dict = {}
    for key, value in obj.items():
        base = _companion_base(key)
        if (base is not None and _is_sensitive_key(base)
                and obj.get(base) == REDACTED):
            # 本层已脱敏键的伴生指纹/长度：原样保留（它们自己含 "key" 子串，
            # 若走敏感分支会被二次脱敏，指纹就永远算不出来）
            out[key] = value
        elif _is_sensitive_key(key):
            _redact_into(out, obj, key, value)
        else:
            out[key] = scrub(value)
    return out


def _redact_into(out: dict, obj: dict, key, value):
    """把单个敏感键的脱敏结果写入 out（幂等：已脱敏且伴生指纹在 → 原样搬运）。"""
    fp_key, len_key = f"{key}{_FP_SUFFIX}", f"{key}{_LEN_SUFFIX}"
    if value == REDACTED and fp_key in obj and len_key in obj:
        out[key] = REDACTED
        out[fp_key] = obj[fp_key]
        out[len_key] = obj[len_key]
        return
    redacted, fp, length = redact_value(value)
    out[key] = redacted
    out[fp_key] = fp
    out[len_key] = length


# ----------------------------------------------------- 端点契约表（ADR-0027 D9/D12）
#
# D9：**每一个写端点**在契约表里有且只有一行——「它属于哪个桶、现状是否留痕、
# 缺的什么时候补、为什么可以豁免」都写在数据里，而不是散在 review 意见里。
# D12：表与真实路由**双向对齐**——`tests/test_audit_guard.py` 取 `app.routes` 的写
# 方法集合与本表 `(method, path)` 集合做双向差集，两边不等即红（新增写端点漏登记
# 会红，删改路由忘同步也会红）。
#
# 六个桶（D1，取值即 `bucket`）：
#   值   —— 改「账本上的数」：快照新增/更正/删除、本金、划转、支出行……
#   口径 —— 改「读数怎么算」：分类字典、阈值、目标比例、解析/校正规则、词表、LLM 配置
#   出处 —— 改「数从哪来」：截图采纳、凭证删除（证据链）
#   离场 —— 数据离开系统：导出、备份文件
#   整库 —— 整库换底：restore / upload-db
#   豁免 —— 不改变任何被审计事实（见 status=exempt 的三种子类）
#
# status 三值：
#   traced  —— 现网**已经**在写审计行。entity/action 照抄现网拼写，别顺手改名
#              （改名＝历史行与新行口径分裂）。
#   planned —— 判定该留痕、但**还没实现**：显式债务，逐条列在
#              `tests/test_audit_guard.py::test_planned_debt_is_visible` 的基线里，
#              数字只许下降；清零后把该测试改成 `== 0`。
#   exempt  —— D2 两类豁免（缓存/派生态、派生字段回写）＋ 零写入端点（410 下线、
#              纯只读、只签一次性令牌、write-once 恒 409、只建批次壳行）。
#              `entity`/`action` 必须皆 None，理由列须说清「为什么不动被审计事实」。
#
# 本段只加数据与纯函数：不碰上面的枚举/脱敏，不 import 任何服务层代码。

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BUCKETS = frozenset({"值", "口径", "出处", "离场", "整库", "豁免"})
STATUSES = frozenset({"traced", "planned", "exempt"})
_REASON_MAX = 20          # 理由列上限（20 字）——表要能一屏读完，长解释写进票里

# 预留 entity：表里 planned 行**将要**用的取值，实现该端点时并入 `ENTITY` 并从这里删掉。
# 之所以分两处：`ENTITY` 的语义是「现网已在使用的取值」；表先冻结（D9）、端点后补
# （后续票），提前把 planned 值塞进 `ENTITY` 会让枚举失去「现网事实」的含义。
# 守卫约束（`_validate_rules`）：预留值**只许**被 planned 行引用；traced/exempt 行
# 必须用 `ENTITY` 里的现网值；两边一旦重叠即说明该并入 `ENTITY`（tests 有断言）。
RESERVED_ENTITY = frozenset({
    # 2026-11-07：五个预留守值已全部并入 `ENTITY`（口径/离场/支出端点开始实现），
    # 此处留空保留结构——下一批契约若需要「先冻结表、后补端点」，仍走同一机制。
})


@dataclass(frozen=True)
class EndpointRule:
    """一个写端点的留痕契约（D9）。取值域见上方本段注释。"""

    method: str            # "POST"——仅 POST/PUT/PATCH/DELETE（D12 的路由枚举口径）
    path: str              # "/api/assets/batch-delete"——FastAPI 原样形状（{param} 风格）
    bucket: str            # 值|口径|出处|离场|整库|豁免
    entity: str | None     # 审计行 entity；exempt ⇔ None
    action: str | None     # 审计行 action；exempt ⇔ None
    status: str            # traced|planned|exempt
    reason: str            # ≤20 字：为什么必须留痕 / 为什么豁免
    registered: str = "2026-11-07"   # 表冻结日（新增行写自己的登记日）


# 逐行照录 `.scratch/audit-best-practice/contract-draft.md`（按 path 字典序，便于与
# 草案逐行对差）。草案里 5 处歧义已按裁决落表，行尾注释标出：
#   1) screenshots/preview  → exempt，理由写「预览不改账本，证据行在 apply 采纳时留」
#   2) source-groups DELETE → planned（组行删除无声，仅凭证删除有行）
#   3) assets/{id} PUT      → exempt（write-once，恒 409）
#   4) llm-providers PUT    → action=update，密钥轮换记 rotate_key 作备注，不单列行
#   5) asset-items rename   → 桶记「口径」（rename 已 traced）；rename/preview 为 exempt
ENDPOINT_RULES: tuple[EndpointRule, ...] = (
    EndpointRule("POST", "/api/alerts/import/execute", "口径", "alert", "execute", "traced", "导入规则改阈值口径"),
    EndpointRule("POST", "/api/alerts/import/preview", "豁免", None, None, "exempt", "纯预览不落库"),
    EndpointRule("POST", "/api/alerts/rules", "口径", "alert", "create", "traced", "新建阈值规则"),
    EndpointRule("DELETE", "/api/alerts/rules/{rule_id}", "口径", "alert", "delete", "traced", "删阈值规则改读数口径"),
    EndpointRule("PUT", "/api/alerts/rules/{rule_id}", "口径", "alert", "update", "traced", "改阈值或方向"),
    EndpointRule("POST", "/api/allocation/targets", "口径", "allocation_target", "create", "traced", "配置目标比例与阈值"),
    EndpointRule("DELETE", "/api/allocation/targets/{target_id}", "口径", "allocation_target", "delete", "traced", "删目标即改偏离口径"),
    EndpointRule("PUT", "/api/allocation/targets/{target_id}", "口径", "allocation_target", "update", "traced", "改目标阈值与比例"),
    EndpointRule("POST", "/api/asset-items/classifications/batch", "口径", "batch", "execute", "traced", "批量改分类，逐项 update"),
    EndpointRule("POST", "/api/asset-items/owners/batch", "口径", "asset_item_owner", "change", "traced", "批量改归属，逐项 change 无表头"),
    EndpointRule("POST", "/api/asset-items/{item_id}/archive", "值", "asset_item", "archive", "traced", "归档使项退出净值口径"),
    EndpointRule("PUT", "/api/asset-items/{item_id}/classification", "口径", "classification", "update", "traced", "分类改判，双轨留痕"),
    # ADR-0031 D2/D3：备注只落审计与 updated_at，**不写** attr_history（D4）
    EndpointRule("PUT", "/api/asset-items/{item_id}/note", "口径", "asset_item", "note_update", "traced", "备注改写项级元数据，不影响计量", "2026-11-10"),
    EndpointRule("POST", "/api/asset-items/{item_id}/owner", "口径", "asset_item_owner", "change", "traced", "归属改判即历史追加"),
    EndpointRule("POST", "/api/asset-items/{item_id}/principal", "值", "principal_change", "principal_set", "traced", "本金生效值变更"),
    # 裁决 5：rename 的桶改记「口径」（改名＝改读数归属口径；审计已 traced）
    EndpointRule("POST", "/api/asset-items/{item_id}/rename", "口径", "asset_item", "rename", "traced", "改名留名史与审计"),
    EndpointRule("POST", "/api/asset-items/{item_id}/rename/preview", "豁免", None, None, "exempt", "仅签发一次性令牌"),
    EndpointRule("PUT", "/api/asset-items/{item_id}/tags", "口径", "classification", "update", "traced", "标签改判只写 attr_history"),
    EndpointRule("POST", "/api/asset-items/{item_id}/unarchive", "值", "asset_item", "unarchive", "traced", "复归使项重入净值"),
    EndpointRule("POST", "/api/asset-owners", "口径", "classification", "create", "traced", "成员字典＝归属主体"),
    EndpointRule("DELETE", "/api/asset-owners/{owner_id}", "口径", "classification", "delete", "traced", "删归属主体"),
    EndpointRule("PUT", "/api/asset-owners/{owner_id}", "口径", "classification", "update", "traced", "改归属主体名或角色"),
    EndpointRule("POST", "/api/assets", "值", "asset_snapshot", "create", "traced", "新增账本行"),
    EndpointRule("POST", "/api/assets/batch", "值", "batch", "execute", "traced", "表头＋逐行 asset/create"),
    EndpointRule("POST", "/api/assets/batch-delete", "值", "batch", "execute", "traced", "表头＋逐行 asset/delete"),
    EndpointRule("POST", "/api/assets/batch-update", "值", "batch", "execute", "traced", "表头＋逐行 correct"),
    EndpointRule("POST", "/api/assets/by-names", "豁免", None, None, "exempt", "只读查询，POST 仅传参"),
    EndpointRule("PUT", "/api/assets/current/{item_id}", "值", "asset_snapshot", "correct", "traced", "当前值就地写＝追加更正"),
    EndpointRule("POST", "/api/assets/history-by-name", "豁免", None, None, "exempt", "只读查询，POST 仅传参"),
    EndpointRule("POST", "/api/assets/tags/batch", "口径", "batch", "execute", "traced", "批量打标签，逐项 update"),
    EndpointRule("DELETE", "/api/assets/{asset_id}", "值", "asset_snapshot", "delete", "traced", "删账本行"),
    # 裁决 3：write-once，恒 409，无字段可改 → 零写入
    EndpointRule("PUT", "/api/assets/{asset_id}", "豁免", None, None, "exempt", "write-once；恒 409"),
    EndpointRule("POST", "/api/assets/{asset_id}/correct", "值", "asset_snapshot", "correct", "traced", "金额或方向更正"),
    EndpointRule("PUT", "/api/assets/{asset_id}/tags", "口径", "classification", "update", "traced", "项级标签改判，双轨留痕"),
    EndpointRule("POST", "/api/attributes", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("DELETE", "/api/attributes/{attribute_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("PUT", "/api/attributes/{attribute_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("POST", "/api/backup/create", "离场", "backup", "create", "traced", "备份＝可带走的整库副本"),
    EndpointRule("DELETE", "/api/backup/delete/{filename}", "离场", "backup", "delete", "traced", "删留存副本，留存链断"),
    EndpointRule("POST", "/api/backup/restore", "整库", "backup", "restore", "traced", "换库留痕写进新库"),
    EndpointRule("POST", "/api/backup/upload-db", "整库", "backup", "upload_db", "traced", "外来库顶替当前库"),
    EndpointRule("POST", "/api/classification/asset-classes", "口径", "classification", "create", "traced", "新建分类字典项"),
    EndpointRule("DELETE", "/api/classification/asset-classes/{class_id}", "口径", "classification", "delete", "traced", "删字典项致历史归组变"),
    EndpointRule("PUT", "/api/classification/asset-classes/{class_id}", "口径", "classification", "update", "traced", "改字典项＝口径变更"),
    EndpointRule("POST", "/api/classification/channels", "口径", "classification", "create", "traced", "新建分类字典项"),
    EndpointRule("DELETE", "/api/classification/channels/{channel_id}", "口径", "classification", "delete", "traced", "删字典项致历史归组变"),
    EndpointRule("PUT", "/api/classification/channels/{channel_id}", "口径", "classification", "update", "traced", "改字典项＝口径变更"),
    EndpointRule("POST", "/api/classification/institutions", "口径", "classification", "create", "traced", "新建分类字典项"),
    EndpointRule("DELETE", "/api/classification/institutions/{institution_id}", "口径", "classification", "delete", "traced", "删字典项致历史归组变"),
    EndpointRule("PUT", "/api/classification/institutions/{institution_id}", "口径", "classification", "update", "traced", "改字典项＝口径变更"),
    EndpointRule("POST", "/api/classification/members", "口径", "classification", "create", "traced", "新建分类字典项"),
    EndpointRule("DELETE", "/api/classification/members/{member_id}", "口径", "classification", "delete", "traced", "删字典项致历史归组变"),
    EndpointRule("PUT", "/api/classification/members/{member_id}", "口径", "classification", "update", "traced", "改字典项＝口径变更"),
    EndpointRule("POST", "/api/classification/tags", "口径", "classification", "create", "traced", "新建分类字典项"),
    EndpointRule("DELETE", "/api/classification/tags/{tag_id}", "口径", "classification", "delete", "traced", "删字典项致历史归组变"),
    EndpointRule("PUT", "/api/classification/tags/{tag_id}", "口径", "classification", "update", "traced", "改字典项＝口径变更"),
    EndpointRule("POST", "/api/expense-categories", "口径", "expense", "create", "traced", "新建支出分类字典"),
    EndpointRule("DELETE", "/api/expense-categories/{category_id}", "口径", "expense", "delete", "traced", "删支出分类字典"),
    EndpointRule("PUT", "/api/expense-categories/{category_id}", "口径", "expense", "update", "traced", "改支出分类字典"),
    EndpointRule("POST", "/api/expense-templates", "口径", "expense", "create", "traced", "新建固定支出模板"),
    EndpointRule("DELETE", "/api/expense-templates/{template_id}", "口径", "expense", "delete", "traced", "删固定支出模板"),
    EndpointRule("PUT", "/api/expense-templates/{template_id}", "口径", "expense", "update", "traced", "改固定支出模板"),
    EndpointRule("POST", "/api/expenses", "值", "expense", "create", "traced", "新增支出行"),
    EndpointRule("POST", "/api/expenses/generate", "值", "expense", "execute", "traced", "按模板批量生成支出"),
    EndpointRule("DELETE", "/api/expenses/{expense_id}", "值", "expense", "delete", "traced", "删支出行"),
    EndpointRule("PUT", "/api/expenses/{expense_id}", "值", "expense", "update", "traced", "改支出金额或日期"),
    EndpointRule("POST", "/api/export-history/history", "离场", "export", "create", "traced", "导出＝数据离开系统"),
    EndpointRule("POST", "/api/export-history/rules", "离场", "export", "create", "traced", "建自动导出＝持续离场"),
    EndpointRule("DELETE", "/api/export-history/rules/{rule_id}", "离场", "export", "delete", "traced", "删自动导出规则"),
    EndpointRule("PUT", "/api/export-history/rules/{rule_id}", "离场", "export", "update", "traced", "改导出目标或周期"),
    EndpointRule("POST", "/api/export-history/validate-cron", "豁免", None, None, "exempt", "纯表达式校验，零写入"),
    EndpointRule("DELETE", "/api/imports/backups/{filename}", "离场", "backup", "delete", "traced", "删导入前备份副本"),
    EndpointRule("POST", "/api/imports/confirm", "值", "import", "import_confirm", "traced", "导入落账本，表头＋逐行 create"),
    EndpointRule("POST", "/api/imports/seed", "值", "import", "import_seed", "traced", "目录批量导入，表头＋逐行 create"),
    EndpointRule("POST", "/api/imports/upload", "豁免", None, None, "exempt", "只建批次壳行，不落账本"),
    EndpointRule("POST", "/api/lineage-links", "值", "lineage_link", "lineage_link", "traced", "合并使源项归档"),
    EndpointRule("POST", "/api/lineage-links/attach", "值", "lineage_attach", "create", "traced", "接管改本金归属"),
    EndpointRule("POST", "/api/lineage-links/split", "值", "lineage_split", "create", "traced", "拆分生成子项本金"),
    EndpointRule("POST", "/api/liquidity-ratings", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("DELETE", "/api/liquidity-ratings/{rating_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("PUT", "/api/liquidity-ratings/{rating_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("POST", "/api/llm-providers", "口径", "llm_provider", "create", "traced", "建供应商；密钥须脱敏"),
    EndpointRule("DELETE", "/api/llm-providers/{provider_id}", "口径", "llm_provider", "delete", "traced", "删供应商配置"),
    # 裁决 4：action 记 update；api_key 真变时另发 rotate_key 作备注，不单列行
    EndpointRule("PUT", "/api/llm-providers/{provider_id}", "口径", "llm_provider", "update", "traced", "改配置；密钥轮换记 rotate_key"),
    EndpointRule("POST", "/api/llm-providers/{provider_id}/test", "豁免", None, None, "exempt", "只回写最近测试结果"),
    EndpointRule("POST", "/api/management/accounts", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("DELETE", "/api/management/accounts/{account_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("PUT", "/api/management/accounts/{account_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("POST", "/api/management/fund-types", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("DELETE", "/api/management/fund-types/{fund_type_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("PUT", "/api/management/fund-types/{fund_type_id}", "豁免", None, None, "exempt", "端点 410 下线，零写入"),
    EndpointRule("PUT", "/api/parser/vocabularies/{key}", "口径", "parser_vocabulary", "parser_vocabulary_update", "traced", "改词表影响解析结果"),
    EndpointRule("POST", "/api/parser/vocabularies/{key}/reset", "口径", "parser_vocabulary", "parser_vocabulary_reset", "traced", "回退出厂词表"),
    EndpointRule("POST", "/api/planning/advice", "值", "advisor_suggestion", "create", "traced", "建议进入决策链"),
    EndpointRule("PUT", "/api/planning/advice/settings", "口径", "advisor_settings", "update", "traced", "改触发与限流开关"),
    EndpointRule("POST", "/api/planning/advice/{suggestion_id}/accept", "值", "advisor_decision", "accept", "traced", "采纳即改变执行事实"),
    EndpointRule("POST", "/api/planning/advice/{suggestion_id}/execution", "值", "advisor_decision", "execution_note", "traced", "回写执行结果"),
    EndpointRule("POST", "/api/planning/advice/{suggestion_id}/reject", "值", "advisor_decision", "reject", "traced", "驳回关闭建议"),
    EndpointRule("POST", "/api/planning/goals", "值", "goal", "create", "traced", "新建目标与首版"),
    EndpointRule("PUT", "/api/planning/goals/{goal_id}", "值", "goal", "update", "traced", "改目标名"),
    EndpointRule("POST", "/api/planning/goals/{goal_id}/archive", "值", "goal", "archive", "traced", "归档目标退出追踪"),
    EndpointRule("POST", "/api/planning/goals/{goal_id}/primary", "值", "goal", "set_primary", "traced", "切换主目标"),
    EndpointRule("POST", "/api/planning/goals/{goal_id}/versions", "值", "goal_version", "create", "traced", "新增目标版本"),
    EndpointRule("DELETE", "/api/planning/goals/{goal_id}/versions/{version_no}", "值", "goal_version", "delete", "traced", "删草稿版本"),
    EndpointRule("PUT", "/api/planning/goals/{goal_id}/versions/{version_no}", "值", "goal_version", "update", "traced", "改草稿版本参数"),
    EndpointRule("POST", "/api/planning/goals/{goal_id}/versions/{version_no}/activate", "值", "goal_version", "activate", "traced", "激活版本改口径基线"),
    EndpointRule("POST", "/api/planning/reviews", "值", "review", "create", "traced", "开复盘冻结快照"),
    EndpointRule("POST", "/api/planning/reviews/{review_id}/close", "值", "review", "close", "traced", "结束复盘"),
    EndpointRule("PUT", "/api/planning/reviews/{review_id}/conclusion", "值", "review", "conclusion", "traced", "写复盘结论"),
    EndpointRule("POST", "/api/planning/reviews/{review_id}/proposals", "值", "review_proposal", "create", "traced", "提调整提案"),
    EndpointRule("POST", "/api/planning/reviews/{review_id}/proposals/{proposal_id}/adopt", "值", "review_proposal", "adopt", "traced", "采纳提案即改执行"),
    EndpointRule("POST", "/api/planning/reviews/{review_id}/proposals/{proposal_id}/dismiss", "值", "review_proposal", "dismiss", "traced", "驳回提案留理由"),
    EndpointRule("POST", "/api/screenshot-batches", "口径", "screenshot_batch", "create", "traced", "建批次壳，跨图核对起点", "2026-11-09"),
    EndpointRule("POST", "/api/screenshot-batches/{batch_id}/finalize", "口径", "screenshot_batch", "finalize", "traced", "定案建构成边改计量口径", "2026-11-09"),
    EndpointRule("POST", "/api/screenshots/apply", "出处", "asset_snapshot", "apply_row", "traced", "逐行采纳＋身份裁决"),
    EndpointRule("POST", "/api/screenshots/evidence/purge-unused", "出处", "screenshot_evidence", "delete", "traced", "批量删未用凭证"),
    EndpointRule("DELETE", "/api/screenshots/evidence/{evidence_id}", "出处", "screenshot_evidence", "delete", "traced", "删凭证断证据链"),
    EndpointRule("PATCH", "/api/screenshots/evidence/{evidence_id}/recognition", "豁免", None, None, "exempt", "派生字段回写，可重算"),
    # 裁决 1：预览只产派生态；真正落账本时的证据行由 screenshots/apply 留痕
    EndpointRule("POST", "/api/screenshots/preview", "豁免", None, None, "exempt", "预览不改账本；证据行在采纳时留痕"),
    EndpointRule("POST", "/api/screenshots/preview-jobs", "豁免", None, None, "exempt", "任务簿记，D2 明列"),
    EndpointRule("DELETE", "/api/screenshots/source-groups/{slug}/correction-rules", "口径", "source_group", "delete", "traced", "删校正规则改解析口径"),
    EndpointRule("PATCH", "/api/screenshots/source-groups/{slug}/correction-rules", "口径", "source_group", "update", "traced", "改校正规则改解析口径"),
    EndpointRule("POST", "/api/screenshots/source-groups/{slug}/correction-rules", "口径", "source_group", "create", "traced", "新增校正规则"),
    EndpointRule("POST", "/api/screenshots/source-groups/{slug}/correction-rules/swap", "口径", "source_group", "update", "traced", "交换规则次序改命中"),
    EndpointRule("POST", "/api/screenshots/supplements/resolve", "豁免", None, None, "exempt", "只读预判，不落库"),
    EndpointRule("POST", "/api/screenshots/verify", "豁免", None, None, "exempt", "纯 LLM 校验，不落库"),
    EndpointRule("POST", "/api/source-groups", "口径", "source_group", "create", "traced", "建来源组改解析口径"),
    # 裁决 2：静态可达性会把本行判成 traced，但组行删除自身无声（只有凭证删除有行）
    EndpointRule("DELETE", "/api/source-groups/{source_group_id}", "口径", "source_group", "delete", "traced", "删组连解析规则一起消失"),
    EndpointRule("PUT", "/api/source-groups/{source_group_id}", "口径", "source_group", "update", "traced", "改组配置与账户映射"),
    EndpointRule("POST", "/api/source-groups/{source_group_id}/guide-image", "口径", "source_group", "update", "traced", "换引导图影响 AI 识别"),
    EndpointRule("PUT", "/api/source-groups/{source_group_id}/parse-rules", "口径", "source_group", "update", "traced", "解析规则＝读数口径"),
    EndpointRule("POST", "/api/transfer-events", "值", "transfer_event", "transfer_register", "traced", "划转登记改本金"),
)


def _validate_rules() -> None:
    """表自身的不变式（import 时跑一次）。失败即 `RuntimeError`——表已坏，别再启动。

    守的是「表自己不能自相矛盾」，与「表 vs 路由」是两件事（后者在 tests 里）：
    - `exempt` ⇔ `entity is None and action is None`（双向，防「豁免了却仍记动作」）；
    - 非 exempt 的 entity/action 必须在 `ENTITY`/`ACTION` 里（预留值只许 planned 用）；
    - `(method, path)` 不重复（一个端点一行，D9）；
    - 桶 ∈ 六值、status ∈ 三值、方法与 `/api/` 前缀合法、理由非空且 ≤20 字。
    """
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()

    for rule in ENDPOINT_RULES:
        where = f"{rule.method} {rule.path}"
        key = (rule.method, rule.path)

        if rule.method not in _WRITE_METHODS:
            problems.append(f"{where}: {rule.method!r} 不是写方法（D12 只枚举 POST/PUT/PATCH/DELETE）")
        if not rule.path.startswith("/api/"):
            problems.append(f"{where}: path 必须以 /api/ 开头")
        if rule.bucket not in BUCKETS:
            problems.append(f"{where}: 桶 {rule.bucket!r} 不在六值内")
        if rule.status not in STATUSES:
            problems.append(f"{where}: status {rule.status!r} 不在三值内")
        if key in seen:
            problems.append(f"{where}: (method, path) 重复——一个端点只许一行")
        seen.add(key)

        if (rule.bucket == "豁免") != (rule.status == "exempt"):
            problems.append(f"{where}: 桶「豁免」与 status={rule.status!r} 必须同进同出")

        if rule.status == "exempt":
            if rule.entity is not None or rule.action is not None:
                problems.append(f"{where}: exempt 必须 entity/action 皆 None")
        elif not rule.entity or not rule.action:
            problems.append(f"{where}: {rule.status} 必须给 entity 与 action")
        elif rule.entity not in ENTITY and rule.entity not in RESERVED_ENTITY:
            problems.append(f"{where}: entity {rule.entity!r} 未登记（ENTITY/RESERVED_ENTITY 都没有）")
        elif rule.action not in ACTION:
            problems.append(f"{where}: action {rule.action!r} 未登记（ACTION）")
        elif rule.entity in RESERVED_ENTITY and rule.status != "planned":
            problems.append(
                f"{where}: 预留 entity {rule.entity!r} 只许 planned 使用"
                "（现网在写就得先并入 ENTITY）")

        if not rule.reason.strip():
            problems.append(f"{where}: 理由不能为空")
        elif len(rule.reason) > _REASON_MAX:
            problems.append(f"{where}: 理由 {len(rule.reason)} 字 > {_REASON_MAX}")

    if problems:
        raise RuntimeError(
            f"审计契约表（ENDPOINT_RULES）自检失败，{len(problems)} 处：\n  "
            + "\n  ".join(problems))


_validate_rules()      # import 时一次；失败＝表已坏，fail fast


# ----------------------------------------------------- 契约表查询（纯函数，无 db）

def rule_for(method: str, path: str) -> EndpointRule | None:
    """按 `(method, path)` 取行；未登记返回 None（方法大小写不敏感）。"""
    key = (method.upper(), path)
    for rule in ENDPOINT_RULES:
        if (rule.method, rule.path) == key:
            return rule
    return None


def _rules_where(status: str) -> tuple[EndpointRule, ...]:
    return tuple(rule for rule in ENDPOINT_RULES if rule.status == status)


def traced_rules() -> tuple[EndpointRule, ...]:
    """现网已在写审计行的端点。"""
    return _rules_where("traced")


def planned_rules() -> tuple[EndpointRule, ...]:
    """该留痕但还没实现的端点＝**显式债务**（数量只许下降）。"""
    return _rules_where("planned")


def exempt_rules() -> tuple[EndpointRule, ...]:
    """豁免端点（D2 两类 ＋ 零写入）。"""
    return _rules_where("exempt")


def rules_by_bucket() -> dict[str, tuple[EndpointRule, ...]]:
    """按桶分组（报告/清单用；桶内保持表内顺序）。"""
    grouped: dict[str, list[EndpointRule]] = {bucket: [] for bucket in BUCKETS}
    for rule in ENDPOINT_RULES:
        grouped[rule.bucket].append(rule)
    return {bucket: tuple(rules) for bucket, rules in grouped.items()}


def write_route_set(app_routes) -> set[tuple[str, str]]:
    """`app.routes` → 写端点集合 `{(method, path)}`（D12 的路由侧口径）。

    口径：path 以 `/api` 开头、方法 ∈ POST/PUT/PATCH/DELETE。`HEAD`/`OPTIONS`
    （含 Starlette 的自动方法）与静态挂载（Mount 无 methods）天然被过滤。
    path 保持 FastAPI 原样形状（`{param}` 风格），与契约表逐字可比。
    """
    routes: set[tuple[str, str]] = set()
    for route in app_routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", None) or ():
            upper = method.upper()
            if upper in _WRITE_METHODS:
                routes.add((upper, path))
    return routes