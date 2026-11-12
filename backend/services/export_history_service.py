"""导出历史与自动导出规则（ADR-0027「离场桶」的唯一写入面）。

`/api/export-history/*` 的三个写端点全部收在这里：写业务行 → **同事务**写审计行 →
commit（业务回滚则审计不留；审计失败则整事务失败）。`POST /validate-cron` 是纯表达式
校验、零写入，不经本模块、**不产审计行**（契约表该行 status=exempt）。

留痕口径（D5 快照分型）：
- `POST /history` → `entity='export', action='create'`；`after` 只记「导出了什么壳」：
  导出类型 / 文件名 / 字节数 / 操作人 / 规则名 / 落盘路径。**绝不记文件内容**
  ——CSV 正文、前 N 行、字节流都不落审计：审计是永久保留、可整库导出的表面，
  把离场数据再抄一份进库里等于自己制造第二个离场面。
- 规则 CRUD → `export/create` / `export/update`（仅变更字段）/ `export/delete`
  （`before` 全集、`after=None`）——自动导出规则＝**持续离场安排**，改目标或周期
  就是改「数据会流去哪」。
- 非法 cron（400）与规则不存在（404）都在任何写入之前抛出 → 不产审计行；
  同值重放的 update 无事实变更 → 不落行。
"""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import AutoExportRule, ExportHistory
from services import audit_service
from services.audit_service import make_change as _change
from services.auto_export_service import reload_scheduler
from services.scheduler_service import get_next_run_time, validate_cron_expression


# ---------------- 关键字段快照 ----------------

def history_key_fields(h: ExportHistory) -> dict:
    """导出历史（离场）审计关键字段——**只有壳，没有内容**。"""
    return {
        "id": h.id,
        "export_type": h.export_type,
        "filename": h.filename,
        "file_size": h.file_size,
        "operator": h.operator,
        "rule_name": h.rule_name,
        "file_path": h.file_path,
    }


def rule_key_fields(r: AutoExportRule) -> dict:
    """自动导出规则（离场安排）审计关键字段。

    `next_run_at`/`last_run_at` 是 `cron_expression` 的派生态，不进快照（记了只是噪声；
    要算随时能算）。
    """
    return {
        "id": r.id,
        "name": r.name,
        "cron_expression": r.cron_expression,
        "export_format": r.export_format,
        "filename_template": r.filename_template,
        "is_active": bool(r.is_active),
    }


def _changed_fields(before: dict, after: dict) -> tuple[dict, dict]:
    """update 分型（D5）：只留真变了的字段，before/after 同键集。"""
    changed = [key for key in after if before.get(key) != after[key]]
    return ({key: before.get(key) for key in changed},
            {key: after[key] for key in changed})


# ---------------- 导出历史 ----------------

def create_history(db: Session, *, export_type: str, filename: str,
                   file_size: Optional[int] = None,
                   operator: Optional[str] = None,
                   rule_name: Optional[str] = None,
                   file_path: Optional[str] = None) -> ExportHistory:
    """登记一次导出（数据离开系统）。`after` 记壳，不记文件内容。"""
    history = ExportHistory(
        export_type=export_type,
        filename=filename,
        file_size=file_size,
        operator=operator,
        rule_name=rule_name,
        file_path=file_path,
    )
    db.add(history)
    db.flush()                                  # 先取主键，审计行要 row_id
    audit_service.record(db, _change(
        "export", history.id, "create", after=history_key_fields(history)))
    db.commit()
    return history


# ---------------- 自动导出规则 ----------------

def create_rule(db: Session, data: dict) -> AutoExportRule:
    """建规则：非法 cron → 400（写入前拒绝，不产审计行）。"""
    is_valid, description = validate_cron_expression(data["cron_expression"])
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {description}")

    rule = AutoExportRule(
        name=data["name"],
        cron_expression=data["cron_expression"],
        export_format=data.get("export_format") or "csv",
        filename_template=data.get("filename_template"),
        next_run_at=get_next_run_time(data["cron_expression"]),
    )
    db.add(rule)
    db.flush()                                  # 先取主键，审计行要 row_id
    audit_service.record(db, _change(
        "export", rule.id, "create", after=rule_key_fields(rule)))
    db.commit()
    db.refresh(rule)
    reload_scheduler()
    return rule


def update_rule(db: Session, rule_id: int, data: dict) -> AutoExportRule:
    """改规则：非法 cron → 400（写入前拒绝）；仅变更字段留痕。"""
    rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")

    before = rule_key_fields(rule)
    # 如果更新了 Cron 表达式，验证并重新计算下次执行时间
    if data.get("cron_expression") is not None:
        is_valid, description = validate_cron_expression(data["cron_expression"])
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {description}")
        rule.cron_expression = data["cron_expression"]
        rule.next_run_at = get_next_run_time(data["cron_expression"])

    if data.get("name") is not None:
        rule.name = data["name"]
    if data.get("export_format") is not None:
        rule.export_format = data["export_format"]
    if data.get("filename_template") is not None:
        rule.filename_template = data["filename_template"]
    if data.get("is_active") is not None:
        rule.is_active = data["is_active"]
        rule.next_run_at = (get_next_run_time(rule.cron_expression)
                            if data["is_active"] else None)

    before_changed, after_changed = _changed_fields(before, rule_key_fields(rule))
    if after_changed:
        audit_service.record(db, _change(
            "export", rule.id, "update",
            before=before_changed, after=after_changed))
    db.commit()
    db.refresh(rule)
    reload_scheduler()
    return rule


def delete_rule(db: Session, rule_id: int, *, reason: str | None = None,
                reason_note: str | None = None) -> None:
    """删规则：`before` 记全集（删的是哪条离场安排），`after=None`。

    `delete` 属纠正类动作（ADR-0027 D3）：`reason` 由调用方给，缺省 None＝
    NULL「未记录原因」（运行时绝不强制）。
    """
    rule = db.query(AutoExportRule).filter(AutoExportRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")

    change = _change("export", rule.id, "delete",
                     before=rule_key_fields(rule), after=None,
                     reason=reason, reason_note=reason_note)
    db.delete(rule)
    db.flush()
    audit_service.record(db, change)
    db.commit()
    reload_scheduler()