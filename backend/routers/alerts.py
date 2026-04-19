from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import json

from database import get_db
from services import alert_service
from schemas import AlertRuleCreate, AlertRuleUpdate, AlertRuleOut, AlertResult

router = APIRouter()


@router.get("/rules", response_model=list[AlertRuleOut])
def get_alert_rules(
    active_only: bool = Query(False, description="Only return active rules"),
    db: Session = Depends(get_db),
):
    """Get all alert rules."""
    return alert_service.get_alert_rules(db, active_only=active_only)


@router.post("/rules", response_model=AlertRuleOut)
def create_alert_rule(
    rule: AlertRuleCreate,
    db: Session = Depends(get_db),
):
    """Create a new alert rule."""
    return alert_service.create_alert_rule(db, rule)


@router.put("/rules/{rule_id}", response_model=AlertRuleOut)
def update_alert_rule(
    rule_id: int,
    rule: AlertRuleUpdate,
    db: Session = Depends(get_db),
):
    """Update an existing alert rule."""
    result = alert_service.update_alert_rule(db, rule_id, rule)
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return result


@router.delete("/rules/{rule_id}")
def delete_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """Delete an alert rule."""
    success = alert_service.delete_alert_rule(db, rule_id)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return {"success": True}


@router.get("/check/{rule_id}", response_model=AlertResult)
def check_alert(
    rule_id: int,
    current_period: Optional[str] = Query(None, description="Current period to check"),
    db: Session = Depends(get_db),
):
    """Check a single alert rule and return the result."""
    result = alert_service.check_alert(db, rule_id, current_period)
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Alert rule not found or inactive")
    return result


@router.get("/results", response_model=list[AlertResult])
def get_alert_results(
    current_period: Optional[str] = Query(None, description="Current period to check"),
    db: Session = Depends(get_db),
):
    """Get alert results for all active rules."""
    return alert_service.get_alert_results(db, current_period)


@router.get("/periods")
def get_available_periods(
    period_type: str = Query("month", description="Period type: day, week, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """Get available periods for alert checking."""
    return alert_service.get_available_periods(db, period_type)


@router.get("/export")
def export_alert_rules(
    db: Session = Depends(get_db),
):
    """导出所有预警规则为JSON格式"""
    rules = alert_service.get_alert_rules(db)
    
    export_data = []
    for rule in rules:
        export_data.append({
            "name": rule.name,
            "dimension": rule.dimension,
            "target_id": rule.target_id,
            "period_type": rule.period_type,
            "compare_type": rule.compare_type,
            "compare_period": rule.compare_period,
            "amount_threshold": float(rule.amount_threshold) if rule.amount_threshold else None,
            "percent_threshold": float(rule.percent_threshold) if rule.percent_threshold else None,
            "direction": rule.direction,
            "is_active": rule.is_active,
        })
    
    return JSONResponse(
        content={
            "version": "1.0",
            "export_time": str(datetime.now()),
            "rules": export_data
        },
        headers={
            "Content-Disposition": f"attachment; filename=alert_rules_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        }
    )


@router.post("/import/preview")
async def preview_import_alert_rules(
    request: dict,
    db: Session = Depends(get_db),
):
    """预览导入的预警规则，检测冲突"""
    try:
        data = request
        
        if "rules" not in data or not isinstance(data["rules"], list):
            raise HTTPException(status_code=400, detail="文件格式错误：缺少 rules 字段")
        
        import_rules = data["rules"]
        existing_rules = alert_service.get_alert_rules(db)
        existing_names = {r.name for r in existing_rules}
        
        preview = []
        conflicts = []
        
        for idx, rule in enumerate(import_rules):
            if "name" not in rule:
                continue
                
            is_conflict = rule["name"] in existing_names
            preview.append({
                "index": idx,
                "name": rule.get("name"),
                "dimension": rule.get("dimension"),
                "target_id": rule.get("target_id"),
                "period_type": rule.get("period_type"),
                "compare_type": rule.get("compare_type"),
                "amount_threshold": rule.get("amount_threshold"),
                "percent_threshold": rule.get("percent_threshold"),
                "direction": rule.get("direction"),
                "is_active": rule.get("is_active", True),
                "has_conflict": is_conflict
            })
            
            if is_conflict:
                conflicts.append(rule["name"])
        
        return {
            "success": True,
            "total": len(import_rules),
            "conflict_count": len(conflicts),
            "conflict_names": conflicts,
            "preview": preview
        }
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 格式错误")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览失败: {str(e)}")


@router.post("/import/execute")
async def execute_import_alert_rules(
    file: UploadFile = File(...),
    conflict_strategy: str = Form("skip"),  # skip, overwrite
    db: Session = Depends(get_db),
):
    """执行预警规则导入"""
    if conflict_strategy not in ["skip", "overwrite"]:
        raise HTTPException(status_code=400, detail="无效的冲突处理策略")
    
    if not file.filename.endswith('.json'):
        raise HTTPException(status_code=400, detail="只支持 JSON 文件")
    
    try:
        content = await file.read()
        data = json.loads(content.decode('utf-8'))
        
        if "rules" not in data or not isinstance(data["rules"], list):
            raise HTTPException(status_code=400, detail="文件格式错误：缺少 rules 字段")
        
        import_rules = data["rules"]
        existing_rules = alert_service.get_alert_rules(db)
        existing_rules_by_name = {r.name: r for r in existing_rules}
        
        imported = 0
        skipped = 0
        overwritten = 0
        errors = []
        
        for rule_data in import_rules:
            try:
                if "name" not in rule_data:
                    continue
                
                rule_name = rule_data["name"]
                
                # 检查冲突
                if rule_name in existing_rules_by_name:
                    if conflict_strategy == "skip":
                        skipped += 1
                        continue
                    elif conflict_strategy == "overwrite":
                        # 更新现有规则
                        existing_rule = existing_rules_by_name[rule_name]
                        update_data = AlertRuleUpdate(
                            dimension=rule_data.get("dimension"),
                            target_id=rule_data.get("target_id"),
                            period_type=rule_data.get("period_type"),
                            compare_type=rule_data.get("compare_type"),
                            compare_period=rule_data.get("compare_period"),
                            amount_threshold=rule_data.get("amount_threshold"),
                            percent_threshold=rule_data.get("percent_threshold"),
                            direction=rule_data.get("direction"),
                            is_active=rule_data.get("is_active", True),
                        )
                        alert_service.update_alert_rule(db, existing_rule.id, update_data)
                        overwritten += 1
                        continue
                
                # 创建新规则
                create_data = AlertRuleCreate(
                    name=rule_data.get("name"),
                    dimension=rule_data.get("dimension", "asset_name"),
                    target_id=rule_data.get("target_id"),
                    period_type=rule_data.get("period_type", "month"),
                    compare_type=rule_data.get("compare_type", "previous"),
                    compare_period=rule_data.get("compare_period"),
                    amount_threshold=rule_data.get("amount_threshold"),
                    percent_threshold=rule_data.get("percent_threshold"),
                    direction=rule_data.get("direction", "both"),
                    is_active=rule_data.get("is_active", True),
                )
                alert_service.create_alert_rule(db, create_data)
                imported += 1
                
            except Exception as e:
                errors.append(f"规则 '{rule_data.get('name', 'unknown')}': {str(e)}")
        
        return {
            "success": True,
            "imported": imported,
            "skipped": skipped,
            "overwritten": overwritten,
            "errors": errors,
            "message": f"导入完成：{imported} 个新增，{overwritten} 个覆盖，{skipped} 个跳过"
        }
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 格式错误")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")
