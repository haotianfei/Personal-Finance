"""库外锚对账（整库回退检测）——纯只读比对，**零写入**。

为什么需要这个模块：`audit_chain.verify()` 的七种判定全部在**同一个库内**取证
（审计行 / `audit_chain_state` / `audit_chain_seals` 三者互证）。整库回退时这三样
被一起恢复成旧版本，自证自然通过——旧库证明旧库自己没被动过，这是能力边界。
要发现「整库回退」，只能拿**库外**的锚来比：备份包 `manifest.json` 里的
`audit_head`（`last_hash` + `row_count`），是库外唯一一份冻结点。

本模块只做四件事：读当前库链头、读备份目录里的锚、比对给判定、启动期只记日志。

红线（勿改）：
- **哈希口径一字不动**：`FIELDS` / `row_hash` / `verify` 属 ADR-0013 D10 冻结面，
  本模块只读它们的输出，不重算、不重写。
- **不写库**：没有 append、没有 seal、不写 manifest；比对结论只出现在响应与日志里。
- 日志/响应里**不得出现绝对路径与金额**（公开安全红线）：`Anchor.source` 恒为
  相对文件名，日志只打哈希前 8 位。

判定五值（`Verdict.status`）：

    match      两侧链头 hash 相同 —— 当前库就是这个锚点的那个库
    db_newer   当前库行数更多 —— 正常（库在往前长，锚是旧的）
    db_older   当前库行数更少 —— **回退嫌疑**（整库被换回旧版本）
    diverged   行数相同但链头不同 —— **分叉嫌疑**（同长度的另一条链）
    unknown    锚里没有审计链头（或只有 hash 没有行数）—— 无法判定，不臆断
"""
import json
import logging
import os
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from models import AuditLog
from services import audit_chain

logger = logging.getLogger(__name__)

#: bundle 内嵌清单名（与 backup_service.MANIFEST_NAME 同值；此处独立声明避免
#: 与 backup_service 形成 import 环——backup_service 要 import 本模块做恢复前对账）。
MANIFEST_NAME = "manifest.json"

#: 判定取值（顺序即文档顺序，不表示严重度）。
STATUSES = ("match", "db_older", "db_newer", "diverged", "unknown")

#: 需要处置的判定（启动期 CRITICAL、恢复前阻断）：回退与分叉。
BLOCKING_STATUSES = ("db_older", "diverged")

#: 严重度（`worst_status` 取最大者）：match 最轻，diverged 最重。
_SEVERITY = {"match": 0, "unknown": 1, "db_newer": 2, "db_older": 3, "diverged": 4}

#: 日志/错误消息里只露哈希前 8 位（够比对、不把整份指纹铺到别处）。
HASH_PREFIX_LEN = 8


@dataclass(frozen=True)
class Anchor:
    """一个库外锚（来自某份 manifest）。

    `source` 恒为**相对文件名**（散落清单＝其文件名；zip bundle＝包文件名），
    绝不带目录（公开安全红线：日志与响应不出现本机绝对路径）。
    `audit_head` 是 manifest 里那条链头的 **hash**，`audit_rows` 是它的行数。
    """

    source: str
    generated_at: Optional[str]
    audit_head: Optional[str]
    audit_rows: Optional[int]


@dataclass(frozen=True)
class Verdict:
    """一次比对的结论。`status` ∈ `STATUSES`，`detail` 是人读理由。"""

    status: str
    detail: str


# ------------------------------------------------------------------ 锚的读取


def anchor_from_manifest(manifest: dict, source: str) -> Anchor:
    """manifest → `Anchor`（字段名以 `backup_service._audit_head()` 的实际形状为准）。

    `audit_head` 形状：`{algorithm, last_id, last_hash, row_count}`；缺键或为 null
    （快照里没有审计表 / 老包）→ `audit_head=None`，比对结论即 `unknown`。
    """
    head = manifest.get("audit_head") if isinstance(manifest, dict) else None
    if not isinstance(head, dict):
        head = {}
    rows = head.get("row_count")
    return Anchor(
        source=source,
        generated_at=manifest.get("created_at") if isinstance(manifest, dict) else None,
        audit_head=head.get("last_hash"),
        audit_rows=int(rows) if isinstance(rows, int) and not isinstance(rows, bool) else None,
    )


def read_anchors(backups_dir: str | None = None) -> list[Anchor]:
    """扫描备份目录里的库外锚，按 `generated_at` 倒序（读不到时间的排最后）。

    认两种落盘形态（都在备份目录**根**，不递归——递归会把 `temp/` 与恢复临时目录
    里的半成品清单也算进来）：

    - 散落的 `manifest*.json`（人工/外部工具留下的清单）；
    - `*.zip` bundle 内嵌的 `manifest.json`（本系统 `create_backup()` 的实际形态）。

    读不懂的文件（坏 JSON / 坏 zip / 无名单）逐个跳过，**不抛异常**：对账是旁证，
    不能因为一份坏包让整个端点或启动流程挂掉。
    """
    directory = backups_dir or _default_backups_dir()
    anchors: list[Anchor] = []
    if not directory or not os.path.isdir(directory):
        return anchors

    for filename in sorted(os.listdir(directory)):
        full = os.path.join(directory, filename)
        if not os.path.isfile(full):
            continue
        lowered = filename.lower()
        if lowered.endswith(".json") and lowered.startswith("manifest"):
            manifest = _read_json_file(full)
        elif lowered.endswith(".zip"):
            manifest = _read_zip_manifest(full)
        else:
            continue
        if manifest is None:
            continue
        anchors.append(anchor_from_manifest(manifest, filename))

    # 倒序：generated_at 是 ISO 文本（字符串序＝时间序）；缺失的（""）自然沉底
    anchors.sort(key=lambda a: a.generated_at or "", reverse=True)
    return anchors


def _default_backups_dir() -> str | None:
    """默认备份目录（延迟 import，避免与本模块的 import 环）。"""
    from services import backup_service

    return backup_service.get_backup_dir()


def _read_json_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("库外锚清单读不懂，已跳过：%s（%s）",
                       os.path.basename(path), e.__class__.__name__)
        return None
    return payload if isinstance(payload, dict) else None


def _read_zip_manifest(path: str) -> dict | None:
    """只读 zip 里的 `manifest.json` 成员（不解包、不碰库字节）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            if MANIFEST_NAME not in zf.namelist():
                return None
            payload = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError,
            UnicodeDecodeError) as e:
        logger.warning("库外锚清单读不懂，已跳过：%s（%s）",
                       os.path.basename(path), e.__class__.__name__)
        return None
    return payload if isinstance(payload, dict) else None


# ------------------------------------------------------------------ 当前库链头


def current_head(db) -> dict:
    """当前库链头 `{last_hash, row_count, max_id}`。

    取法**复用**既有实现，不另写一套：`last_hash`/`row_count` 来自
    `audit_chain.head()`（状态行口径，链的权威读数），`max_id` 用 `verify()` 里
    同一条 `max(audit_log.id)` 查询（封存 lag 需要「物理最大行号」）。
    """
    head = audit_chain.head(db)
    max_id = int(db.execute(select(func.max(AuditLog.id))).scalar() or 0)
    return {
        "last_hash": head["last_hash"],
        "row_count": int(head["row_count"]),
        "max_id": max_id,
    }


# ------------------------------------------------------------------ 比对


def compare(current: dict, anchor: Anchor) -> Verdict:
    """对账判定：`current`＝**被判断的一方**，`anchor`＝**参照锚**（判定表见模块 docstring）。

    同一函数服务两个方向，靠参数摆位表达方向（字段语义不变，别改）：
    - 完整性端点：`current`＝当前库链头、`anchor`＝旧备份的 manifest 锚
      → `db_newer` 表示「库比旧备份更新」（正常）；
    - 恢复前对账：`current`＝**目标包**链头、`anchor`＝当前库链头
      → `db_older` 表示「目标比当前旧」（整库回退），`db_newer` 表示「目标更新」（前滚）。

    顺序即优先级：**先看 hash 是否相同**（相同即同一个库，行数差异只是口径差别，
    不再猜测）；hash 不同才用行数分新旧，行数也相同＝同一长度上的另一条链。
    """
    if not anchor.audit_head:
        return Verdict("unknown", "锚里没有 audit_head，本次无法判定")

    current_hash = current.get("last_hash")
    if current_hash and current_hash == anchor.audit_head:
        return Verdict("match", "当前库链头与锚一致")

    current_rows = current.get("row_count")
    if current_rows is None or anchor.audit_rows is None:
        return Verdict("unknown", "只有链头不同、缺行数，无法判定新旧")

    current_rows, anchor_rows = int(current_rows), int(anchor.audit_rows)
    if current_rows > anchor_rows:
        return Verdict("db_newer", f"当前库更新（{current_rows} 行 > 锚 {anchor_rows} 行）")
    if current_rows < anchor_rows:
        return Verdict("db_older", f"当前库比锚更旧（{current_rows} 行 < 锚 {anchor_rows} 行）")
    return Verdict("diverged", f"行数相同（{current_rows} 行）但链头不同")


def integrity_report(db, backups_dir: str | None = None) -> dict:
    """对账报告（只读）：`{head, seals, anchors, worst_status, checked_at}`。

    - `head`：当前库链头（`last_hash` / `row_count` / `max_id`）；
    - `seals`：日封存进度，`lag = max_id - last_seal_id`＝尚未封存的审计行数
      （无封存时 `last_seal_id=None` 且 `lag=max_id`）；
    - `anchors`：每个锚一条，含 `status`/`detail`（锚本身字段原样带出）；
    - `worst_status`：所有锚里最严重的那一个；**没有锚 → `unknown`**（不是 match，
      没有锚就等于没对过账，不能报平安）。
    """
    head = current_head(db)
    latest_seal = audit_chain.head(db).get("latest_seal")
    last_seal_id = latest_seal.get("last_id") if latest_seal else None
    max_id = head["max_id"]
    seals = {
        "last_seal_id": last_seal_id,
        "max_id": max_id,
        "lag": (max_id - int(last_seal_id)) if last_seal_id is not None else max_id,
    }

    anchors: list[dict] = []
    for anchor in read_anchors(backups_dir):
        verdict = compare(head, anchor)
        anchors.append({**asdict(anchor), "status": verdict.status, "detail": verdict.detail})

    worst_status = (
        max((item["status"] for item in anchors), key=_SEVERITY.__getitem__)
        if anchors else "unknown"
    )
    return {
        "head": head,
        "seals": seals,
        "anchors": anchors,
        "worst_status": worst_status,
        "checked_at": datetime.now().isoformat(),
    }


def startup_check(db, backups_dir: str | None = None) -> dict | None:
    """启动期对账：发现回退/分叉只记 CRITICAL，**绝不抛异常、绝不阻止启动**。

    单用户系统没有值班人：锁死门外比漏报更糟（人进不来，也就没人能看到这条告警）。
    失败时返回 None（调用方按「没查出结果」处理，不据此改任何行为）。
    日志只打状态、文件名（相对）、行数、哈希前 8 位——不含绝对路径与金额。
    """
    try:
        report = integrity_report(db, backups_dir)
        if report["worst_status"] in BLOCKING_STATUSES:
            culprit = next(
                (a for a in report["anchors"] if a["status"] == report["worst_status"]), {})
            logger.critical(
                "audit_integrity_suspect status=%s anchor=%s current_rows=%s"
                " anchor_rows=%s current_head=%s anchor_head=%s",
                report["worst_status"], culprit.get("source"),
                report["head"]["row_count"], culprit.get("audit_rows"),
                hash_prefix(report["head"]["last_hash"]),
                hash_prefix(culprit.get("audit_head")))
        return report
    except Exception:      # noqa: BLE001 —— 这里是最后一道「不阻止启动」的兜底
        logger.critical("audit_integrity_startup_check_failed", exc_info=True)
        return None


def hash_prefix(value: str | None) -> str | None:
    """哈希前 8 位（None → None）。"""
    return value[:HASH_PREFIX_LEN] if value else None