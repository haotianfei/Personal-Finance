from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

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
    period_type: str = Query("month", description="Period type: day, month, quarter, year"),
    db: Session = Depends(get_db),
):
    """Get available periods for alert checking."""
    return alert_service.get_available_periods(db, period_type)
