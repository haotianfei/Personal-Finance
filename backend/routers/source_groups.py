"""截图来源组管理 API（ticket 01）。

来源组为一个 App 一个组（ADR-0003），可增删改查；维护与**机构**
（`institutions`）的 N:M 关联——0027 起账户维度即机构，旧
`attributes(type='account')` 字典表已物理删除。请求/响应键沿用
`account_ids`（前端契约），值语义是 `institutions.id`。
删除行为（evidence-manager D6，票 03 起）：关联机构 → 400；组内有**已使用**
凭证 → 409 `SOURCE_GROUP_HAS_USED_EVIDENCE`；只有**未使用**凭证 → 默认 409
`SOURCE_GROUP_HAS_UNUSED_EVIDENCE`（`?purge_unused=true` 才连凭证一起删）。
"""
import hashlib
import os
import re
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_db
from errors import ApiError, ErrorCode
from services.idempotency_service import idempotency_guard
from models import Institution, ScreenshotEvidence, SourceGroup, SourceGroupAccount
from services import (
    audit_service, evidence_service, parser_rules_service, screenshot_service,
)
from schemas import AUDIT_REASON_NOTE_MAX, AuditReason
from services.evidence_service import used_expr
from services.source_group_rules_service import mutate_rules, serialize_group

# 序列化抽到 services.source_group_rules_service 共享（规则写端点与本路由
# 复用同一函数，保证响应同形）；保留旧名别名，routers/screenshots 等既有
# import 不受影响。
_serialize_group = serialize_group

router = APIRouter()


def _resolve_accounts(db: Session, account_ids) -> list[Institution]:
    """校验账号 id 均为 institutions 行（0027 起账户维度＝机构），返回实体列表。"""
    accounts: list[Institution] = []
    for aid in account_ids or []:
        inst = db.get(Institution, aid)
        if not inst:
            raise HTTPException(status_code=400, detail=f"账户 {aid} 不存在或不是机构")
        accounts.append(inst)
    return accounts


def _ensure_unique(db: Session, slug: str, label: str,
                   exclude_id: int | None = None) -> None:
    q = select(SourceGroup).where((SourceGroup.slug == slug) | (SourceGroup.label == label))
    if exclude_id is not None:
        q = q.where(SourceGroup.id != exclude_id)
    if db.execute(q).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="来源组标识或名称已存在")


# ---------------------------------------------------------------- 留痕（ADR-0027 ②口径桶）
#
# 来源组是「读数口径」的载体：解析规则（layout / 金额列 / 忽略词）、账户映射、
# 引导图，任一变了同一批截图解析出的行就变。快照分型（ADR-0027 D5）：
#   create → 关键字段全集落 after；update → **仅变更字段**；delete → 关键字段全集落 before。
# 校验失败/被拒的请求一律不产审计行（记录点都落在「写库前一刻」）。


def _image_ref(value):
    """`guide_image` 的审计投影：站内 URL 原样，内联 base64/超长文本→摘要。

    引导图既可能是上传后的站内 URL（`GUIDE_IMAGE_URL_PREFIX/...`），也可能是旧
    客户端直接塞进 body 的 data URL / base64。后者是二进制，**绝不能**原样进审计表
    （永久保留、可整库导出）：只留 sha256 前 16 位与长度，够判「换没换」，取不回图。
    """
    if not value:
        return None
    if isinstance(value, str) and value.startswith(f"{GUIDE_IMAGE_URL_PREFIX}/"):
        return value
    text = value if isinstance(value, str) else str(value)
    return {"data_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "data_len": len(text)}


def _group_fields(sg: SourceGroup) -> dict:
    """关键字段快照（不含 id；供 update 的差分用）。"""
    return {
        "slug": sg.slug,
        "label": sg.label,
        "asset_prefix": sg.asset_prefix,
        "guide": sg.guide,
        "guide_image": _image_ref(sg.guide_image),
        "rules": sg.rules,
        "enabled": bool(sg.enabled),
        "sort_order": sg.sort_order,
        "account_ids": [inst.id for inst in sg.institutions],
    }


def _group_snapshot(sg: SourceGroup) -> dict:
    """create/delete 用的全集快照（带 id，取证时能直指被删的那一行）。"""
    return {"id": sg.id, **_group_fields(sg)}


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """只留值真的变了的键（update 分型：两侧保持同一组键）。

    键集取**并集**：`parse-rules` 的「传 null / 空值＝删键」会让某个键从 after 里
    消失，只遍历 after 会漏掉这类删除。删掉的键在 before/after 两侧分别记原值与
    None（与请求语义「置空」一致）。
    """
    keys = list(after) + [key for key in before if key not in after]
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return ({key: before.get(key) for key in changed},
            {key: after.get(key) for key in changed})


# --- CRUD ---
def list_source_groups(db: Session) -> list[dict]:
    """列出全部来源组（按 sort_order 排序）。"""
    groups = db.execute(select(SourceGroup).order_by(SourceGroup.sort_order, SourceGroup.label)).scalars().all()
    return [_serialize_group(sg) for sg in groups]


def create_source_group(data: dict, db: Session) -> dict:
    """创建来源组；slug 缺省时自动生成。

    `rules` 与 `PUT /{id}` 走**同一套校验**（`parser_rules_service.validate_group_rules()`
    → 未知顶层键 / 类型错 422 `SOURCE_GROUP_RULES_INVALID`，归一后写列、空配置写 NULL）。

    历史缺口：创建口曾把 `data["rules"]` 原样写进列，未知键（含 0031 已搬表的
    rename/ignore/exclude 与四个历史死键）能从创建口绕过校验进来，之后每次真写入都
    被 fail-fast 拦住、组自己修不好。这里补上校验——创建与更新同口径，收口一处。
    """
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="来源组名称不能为空")
    slug = (data.get("slug") or "").strip() or f"sg-{uuid.uuid4().hex[:8]}"
    _ensure_unique(db, slug, label)
    accounts = _resolve_accounts(db, data.get("account_ids"))

    # 校验先于构造实体：失败时本请求零写入（会话未 commit，连接关闭即回滚）
    rules_json = parser_rules_service.serialize_group_rules(
        parser_rules_service.validate_group_rules(data.get("rules")))

    sg = SourceGroup(
        slug=slug,
        label=label,
        asset_prefix=(data.get("asset_prefix") or "").strip(),
        guide=data.get("guide"),
        guide_image=data.get("guide_image"),
        rules=rules_json,
        enabled=bool(data.get("enabled", True)),
        sort_order=int(data.get("sort_order", 0) or 0),
    )
    sg.institutions = accounts
    db.add(sg)
    db.flush()                     # 拿 id 后再记审计行（同事务；失败路径不留痕）
    audit_service.record(db, audit_service.make_change(
        "source_group", sg.id, "create", None, _group_snapshot(sg)))
    db.commit()
    db.refresh(sg)
    return _serialize_group(sg)


def update_source_group(source_group_id: int, data: dict, db: Session) -> dict:
    """更新来源组；account_ids（机构 id）提供时整体替换关联机构。

    `rules` 是**整份替换**语义：先过 `parser_rules_service.validate_group_rules()`
    （未知顶层键 / 类型错 → 422 `SOURCE_GROUP_RULES_INVALID`），再以归一后的 JSON
    字符串写列（空配置写 NULL）。校验**先于任何字段赋值**——校验失败时本请求不改动
    该行任何列（会话未 commit，连接关闭即回滚）。
    """
    sg = db.get(SourceGroup, source_group_id)
    if not sg:
        raise HTTPException(status_code=404, detail="来源组不存在")

    before = _group_fields(sg)
    new_rules = None
    if "rules" in data:
        new_rules = parser_rules_service.serialize_group_rules(
            parser_rules_service.validate_group_rules(data["rules"]))

    if "label" in data and data["label"]:
        _ensure_unique(db, sg.slug, data["label"].strip(), exclude_id=sg.id)
        sg.label = data["label"].strip()
    if "slug" in data and data["slug"]:
        new_slug = data["slug"].strip()
        _ensure_unique(db, new_slug, sg.label, exclude_id=sg.id)
        # 0029 起凭证存 `screenshot_evidence.source_group_id` 外键，组改名由 id
        # 天然免疫：**这里刻意不做任何「历史凭证同步」**——曾经存在的字符串级联
        # （bulk UPDATE 凭证行）随字符串列一并删除。别再加回来：它不但没有正确性
        # 作用（id 不会因改名失效），还会让人误以为改名需要同步、掩盖真正的问题。
        sg.slug = new_slug
    if "asset_prefix" in data:
        sg.asset_prefix = (data["asset_prefix"] or "").strip()
    if "guide" in data:
        sg.guide = data["guide"]
    if "guide_image" in data:
        sg.guide_image = data["guide_image"]
    if "rules" in data:
        sg.rules = new_rules
    if "enabled" in data:
        sg.enabled = bool(data["enabled"])
    if "sort_order" in data:
        sg.sort_order = int(data["sort_order"] or 0)
    if "account_ids" in data:
        sg.institutions = _resolve_accounts(db, data["account_ids"])

    before_changed, after_changed = _changed_fields(before, _group_fields(sg))
    if after_changed:              # 空变更＝没有写入事实，不产审计行
        audit_service.record(db, audit_service.make_change(
            "source_group", sg.id, "update", before_changed, after_changed))

    db.commit()
    db.refresh(sg)
    return _serialize_group(sg)


def delete_source_group(source_group_id: int, db: Session, *,
                        purge_unused: bool = False,
                        evidence_root: str | None = None,
                        reason: str | None = None,
                        reason_note: str | None = None) -> dict:
    """删除来源组（evidence-manager D6）：绝不静默级联删凭证。

    守卫顺序（每道都先于任何写入）：

    1. 组不存在 → 404；组仍关联机构 → 400（既有契约，与凭证无关）。
    2. 组内有**已使用**凭证 → 409 `SOURCE_GROUP_HAS_USED_EVIDENCE`（附数量）。
       这类凭证是证据链的一环，任何参数都不能删（spec §4：不提供强制删除后门）。
    3. 组内只有**未使用**凭证 → 默认 409 `SOURCE_GROUP_HAS_UNUSED_EVIDENCE`
       （附数量与提示）。想连同废图一起删，必须显式传 `purge_unused=true`——
       删组是低频动作，「以为只删了一个空组，实际永久删了 12 张图」是不可接受
       的默认行为（spec D6 否决项）。
    4. `purge_unused=True` → 同事务先删该组未使用凭证（行 + 审计），再删组；
       **文件删除在 commit 之后**（先删文件后回滚会留下「行还在、文件没了」）。

    返回 `{"ok": True, "purged_evidence": <条数>, "freed_bytes": <字节>}`：
    `purged_evidence=0` 明示「没删任何凭证」，让破坏范围在响应里可见。
    """
    sg = db.get(SourceGroup, source_group_id)
    if not sg:
        raise HTTPException(status_code=404, detail="来源组不存在")

    linked = db.execute(
        select(SourceGroupAccount).where(SourceGroupAccount.source_group_id == sg.id)
    ).scalars().first()
    if linked:
        raise HTTPException(status_code=400, detail="该来源组仍关联账户，请先取消关联后再删除")

    used_count = int(db.execute(
        select(func.count(ScreenshotEvidence.id)).where(
            ScreenshotEvidence.source_group_id == sg.id,
            used_expr(ScreenshotEvidence.id))       # ← D4 单源判定，勿另写 SQL
    ).scalar_one())
    if used_count:
        raise ApiError(
            ErrorCode.SOURCE_GROUP_HAS_USED_EVIDENCE, 409,
            f"该来源组仍有 {used_count} 张已使用凭证，不能删除（证据链）",
            {"source_group_id": sg.id, "count": used_count,
             "hint": "已使用凭证是账本行的证据来源，任何参数都不删；请先处理关联行"})

    unused_count = int(db.execute(
        select(func.count(ScreenshotEvidence.id)).where(
            ScreenshotEvidence.source_group_id == sg.id,
            ~used_expr(ScreenshotEvidence.id))
    ).scalar_one())
    if unused_count and not purge_unused:
        raise ApiError(
            ErrorCode.SOURCE_GROUP_HAS_UNUSED_EVIDENCE, 409,
            f"该来源组仍有 {unused_count} 张未使用凭证，未删除任何东西",
            {"source_group_id": sg.id, "count": unused_count,
             "hint": "可在「截图凭证」页清理这些凭证，或带 purge_unused=true 一并删除"})

    purged, rel_paths = (0, [])
    if unused_count:
        # 组 purge 的每条凭证明细行都带同一 reason（spec 验收 5）
        purged, rel_paths = evidence_service.purge_group_evidence(
            db, sg.id, evidence_root=evidence_root,
            reason=reason, reason_note=reason_note)

    before = _group_snapshot(sg)    # 删前快照：组行自身原来无声（D5 delete→before 全集）
    db.delete(sg)
    db.flush()
    audit_service.record(db, audit_service.make_change(
        "source_group", sg.id, "delete", before, None,
        reason=reason, reason_note=reason_note))
    db.commit()

    freed = evidence_service.remove_evidence_files(rel_paths, evidence_root=evidence_root)
    return {"ok": True, "purged_evidence": purged, "freed_bytes": freed}


# --- 路由（薄壳） ---
@router.get("")
def http_list_source_groups(db: Session = Depends(get_db)):
    return list_source_groups(db)


@router.post("", dependencies=[Depends(idempotency_guard("POST /api/source-groups"))])
def http_create_source_group(data: dict, db: Session = Depends(get_db)):
    return create_source_group(data, db)


@router.get("/recommend")
def http_recommend_source_group(filename: str, db: Session = Depends(get_db)):
    """文件名内嵌 App 包名 → 推荐来源组标识（slug/label）。"""
    result = screenshot_service.recommend_source_group(db, filename)
    return {"source_group": result, "label": result}


@router.get("/{source_group_id}")
def http_get_source_group(source_group_id: int, db: Session = Depends(get_db)):
    sg = db.get(SourceGroup, source_group_id)
    if not sg:
        raise HTTPException(status_code=404, detail="来源组不存在")
    return _serialize_group(sg)


@router.put("/{source_group_id}", dependencies=[Depends(idempotency_guard("PUT /api/source-groups/id"))])
def http_update_source_group(source_group_id: int, data: dict, db: Session = Depends(get_db)):
    return update_source_group(source_group_id, data, db)


@router.delete("/{source_group_id}", dependencies=[Depends(idempotency_guard("DELETE /api/source-groups/id"))])
def http_delete_source_group(
    source_group_id: int,
    purge_unused: bool = Query(False, description="true/1 → 连同该组未使用凭证一并删除"),
    db: Session = Depends(get_db),
    reason: Annotated[Optional[AuditReason], Query(
        description="审计原因（ADR-0027 封闭枚举；缺省＝不记录；纠正类缺失即 422）")] = None,
    reason_note: Annotated[Optional[str], Query(
        max_length=AUDIT_REASON_NOTE_MAX, description="原因说明（≤200 字）")] = None,
):
    """删来源组；`purge_unused=true` 时连未使用凭证一起删。

    审计原因（spec E3 第 4 组）：`?reason=&reason_note=` 透传到组行与每条凭证明细行；
    `action='delete'` 属纠正类，缺 reason → 422 `AUDIT_REASON_REQUIRED`（组与凭证
    都不落库，同事务回滚）。
    """
    return delete_source_group(source_group_id, db, purge_unused=purge_unused,
                              reason=reason, reason_note=reason_note)


# --- 解析规则（组级解析配置）定向局部更新 ---
# PUT /{id} 是整份替换，与截图页规则增量写并发时会丢更新；parse-rules 端点只定向
# merge **组级解析键**，body 里没出现的键不动，写路径仍走 source_group_rules_service
# 的全局锁，与其他规则写端点互斥。
#
# 0031 之后教学规则三键（rename/ignore/exclude）已搬进 `source_group_learned_rules`
# 表，**本端点从不处理它们**（属校正规则面板，走 POST /api/screenshots/{slug}/
# correction-rules 族）。它们与 0031 清掉的四个历史死键
# （name_col/total_header/amount_suffix/anchor_summary）一律按「未知键」→ 422。
# 合法键集合、类型校验与归一的口径单点在 parser_rules_service.validate_group_rules()
# ——此处只做别名，不另立一份常量（避免两处漂移）。
PARSE_LAYOUTS = parser_rules_service.LAYOUT_VALUES
PARSE_RULE_FIELDS = parser_rules_service.GROUP_RULE_KEYS


def update_parse_rules(source_group_id: int, data: dict, db: Session) -> dict:
    """部分更新 rules 的组级解析键（layout / amount_header / amount_col / ignore_words）。

    语义：**body 里出现的键才动**——值有效则写入（归一后，`amount_header` 的 str
    写法统一归一为 list），null / 空值则删键（回落自动检测或出厂词表）；body 里没
    出现的键原样保留。未知键（含已搬表的三键与四个历史死键）→ 422
    `SOURCE_GROUP_RULES_INVALID`，报文列出未知键与合法键集合。

    校验分两道，口径同一个 `validate_group_rules`：

    1. **body 本身**先过一遍：未知键 / 类型错在碰库之前就 422，同时拿到归一 patch；
    2. **合并结果**再整体过一遍：存量里若还残留迁移前的键（rename / custom_key 等），
       在这里就 422 暴露出来（D4 fail-fast——宁可报错，也不要让这些键继续静默失效）。

    既有坏 JSON 的 rules **不再宽容重置**：读到无法解析的存量列直接 422
    `SOURCE_GROUP_RULES_INVALID`，让人先修数据（D4 fail-fast）。修复入口是
    `PUT /api/source-groups/{id}` 整份覆盖 rules，不是「改一个键顺手抹掉其它」。
    空 body（`{}`）＝纯读回，不做任何写入、也不做 ② 的整体校验（避免只读操作被
    存量脏键拦住；真正的写入才 fail-fast）。
    返回更新后的 SourceGroup 序列化对象（与 GET 单项同形）；组不存在 → 404。
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    patch = parser_rules_service.validate_group_rules(data)   # ① body → 422 在这里

    def mutator(rules: dict) -> dict:
        if not data:
            return rules          # 空 body ＝ 纯读回：没有任何写入，存量键不参与校验
        merged = dict(rules)      # 未触碰的键先原样保留，交给 ② 判合法性
        for field in PARSE_RULE_FIELDS:
            if field not in data:
                continue
            if field in patch:
                merged[field] = patch[field]
            else:
                merged.pop(field, None)      # null / 空值 → 删键
        return parser_rules_service.validate_group_rules(merged)   # ② 合并结果整体校验

    def audit_hook(db_: Session, sg, before_rules: dict, after_rules: dict) -> None:
        """锁内、commit 前的留痕（`source_group/update`，仅变更的解析键）。"""
        before_changed, after_changed = _changed_fields(before_rules, after_rules)
        if after_changed:
            audit_service.record(db_, audit_service.make_change(
                "source_group", sg.id, "update", before_changed, after_changed))

    try:
        sg = mutate_rules(db, source_group_id, mutator, audit=audit_hook)
    except ValueError as exc:      # parse_rules(lenient=False) 的坏 JSON 信号
        raise ApiError(ErrorCode.SOURCE_GROUP_RULES_INVALID, 422, str(exc)) from exc
    if sg is None:
        raise HTTPException(status_code=404, detail="来源组不存在")
    return _serialize_group(sg)


@router.put("/{source_group_id}/parse-rules", dependencies=[Depends(idempotency_guard("PUT /api/source-groups/parse-rules"))])
def http_update_parse_rules(source_group_id: int, data: dict,
                            db: Session = Depends(get_db)):
    return update_parse_rules(source_group_id, data, db)


# --- 示例图（guide_image）上传 ---
# 存储目录 <项目根>/data/guide_images（与 evidence / ocr_cache 同一绝对路径
# 约定：三级 dirname(__file__) 定位项目根，绝不依赖 cwd——uvicorn 以 backend/
# 为 cwd 启动，仓库根与运行 cwd 不同）。URL 前缀必须在 /api 下：前端经
# next.config 的 /api/:path* rewrite 转发到后端，本地与 Docker 一致成立；
# main.py 以 StaticFiles 挂载同一目录。
GUIDE_IMAGE_EXT_WHITELIST = {".png", ".jpg", ".jpeg", ".webp"}
GUIDE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
GUIDE_IMAGE_URL_PREFIX = "/api/static/guide-images"


def guide_image_root() -> str:
    """示例图存储目录绝对路径。薄函数便于测试注入 tmp 目录。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "guide_images")


def save_guide_image(source_group_id: int, file: UploadFile, db: Session) -> dict:
    """上传来源组示例图：校验 → 落盘 → 更新 guide_image URL → 清理旧文件。

    - 扩展名白名单 png/jpg/jpeg/webp（大小写不敏感），否则 400（中文）；
    - 边写边计数，超 5MB 即中断、删半成品并 400；
    - 文件名 `{sanitized-slug}-{uuid8}{ext}`（slug 消毒防路径注入）；
    - 覆盖成功（DB 提交）后才删除旧文件，且仅当旧 URL 属于本目录系统
      （前缀匹配 /api/static/guide-images/），绝不误删外部/历史链接资源；
    - 返回更新后的 SourceGroup 序列化对象（与 GET 单项同形）；组不存在 404。
    """
    sg = db.get(SourceGroup, source_group_id)
    if sg is None:
        raise HTTPException(status_code=404, detail="来源组不存在")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in GUIDE_IMAGE_EXT_WHITELIST:
        raise HTTPException(status_code=400,
                            detail="仅支持 png / jpg / jpeg / webp 格式的图片")

    root = guide_image_root()
    os.makedirs(root, exist_ok=True)
    safe_slug = re.sub(r"[^A-Za-z0-9._-]", "_", sg.slug or "group") or "group"
    dest_name = f"{safe_slug}-{uuid.uuid4().hex[:8]}{ext}"
    dest = os.path.join(root, dest_name)

    written = 0
    oversized = False
    digest = hashlib.sha256()       # 落盘同时算摘要：审计只留摘要，绝不留图片字节
    try:
        with open(dest, "wb") as out:
            while chunk := file.file.read(1 << 16):
                written += len(chunk)
                if written > GUIDE_IMAGE_MAX_BYTES:
                    oversized = True
                    break
                digest.update(chunk)
                out.write(chunk)
    except HTTPException:
        raise
    except OSError:
        if os.path.isfile(dest):
            os.remove(dest)
        raise HTTPException(status_code=400, detail="图片保存失败，请重试")
    if oversized or written == 0:
        if os.path.isfile(dest):
            os.remove(dest)
        if oversized:
            raise HTTPException(status_code=400, detail="图片大小不能超过 5MB")
        raise HTTPException(status_code=400, detail="上传的图片为空")

    old_url = sg.guide_image or ""
    sg.guide_image = f"{GUIDE_IMAGE_URL_PREFIX}/{dest_name}"
    db.flush()
    # ADR-0027 D5：`after` 只记「有没有图 + 内容摘要前 16 位」——引导图是二进制，
    # 原样入审计表等于永久保留一份不可删的副本（而且体积炸掉审计行）。
    audit_service.record(db, audit_service.make_change(
        "source_group", sg.id, "update",
        {"guide_image": _image_ref(old_url)},
        {"has_image": True, "image_sha256": digest.hexdigest()[:16]}))
    db.commit()
    db.refresh(sg)

    # 提交成功后再清旧文件：写盘失败/组已删等路径不会误删在用的旧图
    if old_url.startswith(f"{GUIDE_IMAGE_URL_PREFIX}/"):
        old_name = os.path.basename(old_url)
        old_path = os.path.join(root, old_name)
        if old_name != dest_name and os.path.isfile(old_path):
            os.remove(old_path)

    return _serialize_group(sg)


@router.post("/{source_group_id}/guide-image", dependencies=[Depends(idempotency_guard("POST /api/source-groups/guide-image"))])
def http_upload_guide_image(source_group_id: int,
                            file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    return save_guide_image(source_group_id, file, db)
