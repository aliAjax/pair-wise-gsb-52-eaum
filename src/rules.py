"""特殊教育支持计划合规领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'withdraw_consent': {'parent_rep'},
    'grant_consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'review': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
    'transfer_out': {'case_manager', 'administrator'},
    'confirm_transfer': {'case_manager', 'administrator'},
    'decline_transfer': {'case_manager', 'administrator'},
    'restore_batch': {'case_manager', 'administrator'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented'},
    'grant_consent': {'draft': 'consented', 'consent_withdrawn': 'consented', 'active': 'active', 'transfer_failed': 'transfer_failed'},
    'withdraw_consent': {'consented': 'consent_withdrawn', 'active': 'consent_withdrawn', 'transferring': 'consent_withdrawn'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active', 'transferring': 'transferring'},
    'review': {'active': 'under_review'},
    'amend': {'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
    'transfer_out': {'active': 'transferring'},
    'confirm_transfer': {'transferring': 'active'},
    'restore_batch': {'transfer_failed': 'active'},
}
# transfer被家长撤回或被新校拒绝后落到的失败态
TRANSFER_FAILED_STATE = "transfer_failed"

# 交接单状态
TRANSFER_STATUSES = ("pending", "confirmed", "declined", "withdrawn", "restored")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    TRANSFER_FAILED_STATE = TRANSFER_FAILED_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented", "transferring"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_transfer_out(self, data: Dict[str, Any]) -> Dict[str, Any]:
        to_org = text(data or {}, "to_org")
        reason = (data or {}).get("reason", "")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("reason必须是文本")
        return {"to_org": to_org, "reason": reason.strip()}

    def handover_snapshot(self, payload: Dict[str, Any], to_org: str, from_org: str = "", batch_no: str = "") -> Dict[str, Any]:
        """新校确认时生成接手快照：只接手有效目标与服务进度。"""
        goals = payload.get("active_goals")
        if not isinstance(goals, list) or not goals:
            # 旧记录没有显式目标列表时，以目标数量占位接手有效目标
            goals = ["目标%d" % i for i in range(1, int(payload.get("goals_count", 0)) + 1)]
        return {
            "batch_no": batch_no or payload.get("batch_no", ""),
            "from_org": from_org or payload.get("owning_org", ""),
            "to_org": to_org,
            "active_goals": list(goals),
            "service_minutes": int(payload["service_minutes"]),
            "delivered_minutes": int(payload["delivered_minutes"]),
            "compliance_rate": payload.get("compliance_rate", 0.0),
            "handed_over_at": datetime.now(timezone.utc).isoformat(),
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action in ("consent", "grant_consent"):
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            changes["plan_status"] = "consented" if record["state"] in ("draft", "consent_withdrawn") else p.get("plan_status")
            summary = "监护人同意已记录" if action == "consent" else "监护人重新授权"
        elif action == "withdraw_consent":
            changes["consent"] = False
            changes["consent_withdrawn_at"] = datetime.now(timezone.utc).isoformat()
            summary = "监护人已撤回同意"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            session = integer(data, "session_minutes", 1)
            if session + int(p["delivered_minutes"]) > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            changes["delivered_minutes"] = int(p["delivered_minutes"]) + session
            changes["last_provider"] = text(data, "provider")
            changes["missing_minutes"] = int(p["service_minutes"]) - changes["delivered_minutes"]
            changes["compliance_rate"] = round(changes["delivered_minutes"] / int(p["service_minutes"]) * 100, 2)
            summary = "服务记录已登记"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["active_goals"] = changes["updated_goals"]
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        elif action == "transfer_out":
            changes["plan_status"] = "frozen"
            changes["frozen_at"] = datetime.now(timezone.utc).isoformat()
            summary = "原校发起转出，计划已冻结"
        elif action == "confirm_transfer":
            changes["plan_status"] = "active"
            summary = "新校确认接手支持计划"
        elif action == "restore_batch":
            changes["plan_status"] = "active"
            summary = "交接失败，已恢复原批次并可续做"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
