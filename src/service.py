"""业务用例编排、权限检查与审计。"""
import uuid
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, QuarantinedConflict, ValidationError, integer, optional_text, text
from .repository import Repository
from .rules import DomainRules


TRANSFER_ACTIONS = {"transfer_initiate", "transfer_confirm", "transfer_restore"}
TODO_ACTIONS = {"todo_create", "todo_complete", "todo_reconfirm"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _quarantine(self, record_id: int, actor: Actor, action: str, reason: str, data: Dict[str, Any]) -> None:
        quarantine_id = self.repository.quarantine_write(record_id, actor.user_id, actor.organization, action, reason, data or {})
        raise QuarantinedConflict(reason, details={"quarantine_id": quarantine_id, "action": action, "input": data or {}})

    def _require_owner_org(self, actor: Actor, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> None:
        if actor.role == "admin" or actor.role not in self.rules.SCHOOL_ROLES:
            return
        owner = record["payload"].get("owner_org", "")
        if owner and actor.organization != owner:
            self._quarantine(record["id"], actor, action, "机构归属为%s，越权写入已隔离" % owner, data)

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        prepared["owner_org"] = actor.organization
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        data = data or {}
        if action in TRANSFER_ACTIONS:
            return self._transfer_action(actor, record, expected_version, action, data)
        if action in TODO_ACTIONS:
            return self._todo_action(actor, record, expected_version, action, data)
        if action == "withdraw_consent":
            return self._withdraw_consent(actor, record, expected_version, data)
        return self._plan_action(actor, record, expected_version, action, data)

    def _plan_action(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self._require_owner_org(actor, record, action, data)
        self.rules.require_not_frozen(record, action)
        if action == "log_service" and record["payload"].get("consent_withdrawn"):
            self._quarantine(record["id"], actor, action, "监护人已撤回同意，服务登记被隔离", data)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        try:
            return self.repository.mutate(
                record_id=record["id"],
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
            )
        except Conflict:
            if self.repository.get_open_transfer(record["id"]) is not None:
                self._quarantine(record["id"], actor, action, "交接进行中，晚到写入已隔离", data)
            raise

    def _transfer_action(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        payload = record["payload"]
        if action == "transfer_initiate":
            to_org = text(data, "to_org")
            from_org = payload.get("owner_org") or actor.organization
            if not from_org:
                raise ValidationError("缺少原机构归属，无法发起转出")
            if actor.role != "admin" and actor.organization != from_org:
                self._quarantine(record["id"], actor, action, "仅归属机构可发起转出，越权写入已隔离", data)
            if to_org == from_org:
                raise ValidationError("转入机构不能与原机构相同")
            if record["state"] not in ("active", "under_review"):
                raise Conflict("当前状态不允许发起转出")
            if payload.get("consent_withdrawn"):
                raise Conflict("监护人已撤回同意，无法发起转出")
            if payload.get("transfer_state") == "failed":
                raise Conflict("交接已失败，请恢复原批次后续做")
            if self.repository.get_open_transfer(record["id"]) is not None:
                self._quarantine(record["id"], actor, action, "已存在进行中的交接，晚到写入已隔离", data)
            batch_id = optional_text(data, "batch_id") or "TRB-%s" % uuid.uuid4().hex[:12]
            new_payload = dict(payload)
            new_payload.update({"owner_org": from_org, "frozen": True, "transfer_state": "initiated"})
            details = {"summary": "原校发起转出，计划已冻结", "input": data, "batch_id": batch_id, "from_org": from_org, "to_org": to_org}
            try:
                return self.repository.initiate_transfer(record, int(expected_version), new_payload, actor.user_id, {"batch_id": batch_id, "from_org": from_org, "to_org": to_org}, details)
            except Conflict:
                self._quarantine(record["id"], actor, action, "交接发起冲突，晚到写入已隔离", data)
        if action == "transfer_confirm":
            transfer = self.repository.get_open_transfer(record["id"])
            if transfer is None:
                self._quarantine(record["id"], actor, action, "无进行中的交接，晚到写入已隔离", data)
            if actor.role != "admin" and actor.organization != transfer["to_org"]:
                self._quarantine(record["id"], actor, action, "仅转入机构可确认交接，越权写入已隔离", data)
            new_payload = dict(payload)
            new_payload.update({"owner_org": transfer["to_org"], "frozen": False, "transfer_state": "confirmed"})
            details = {
                "summary": "新校确认交接，接手有效目标与服务进度",
                "input": data,
                "batch_id": transfer["batch_id"],
                "from_org": transfer["from_org"],
                "to_org": transfer["to_org"],
                "carried_goals": payload.get("goals_count"),
                "carried_delivered_minutes": payload.get("delivered_minutes"),
            }
            try:
                return self.repository.confirm_transfer(record, int(expected_version), new_payload, actor.user_id, transfer["id"], details)
            except Conflict:
                self._quarantine(record["id"], actor, action, "交接确认冲突，晚到写入已隔离", data)
        transfer = self.repository.get_latest_transfer(record["id"])
        if transfer is None or transfer["state"] != "failed":
            raise Conflict("仅失败的交接批次可恢复")
        if actor.role != "admin" and actor.organization != transfer["from_org"]:
            self._quarantine(record["id"], actor, action, "仅原机构可恢复交接批次，越权写入已隔离", data)
        restored = dict(transfer["snapshot"])
        restored["consent"] = bool(payload.get("consent"))
        restored["consent_withdrawn"] = bool(payload.get("consent_withdrawn"))
        restored["owner_org"] = transfer["from_org"]
        restored["frozen"] = True
        restored["transfer_state"] = "initiated"
        details = {"summary": "交接批次已恢复，可继续办理", "input": data, "batch_id": transfer["batch_id"], "from_org": transfer["from_org"], "to_org": transfer["to_org"]}
        try:
            return self.repository.restore_transfer(record, int(expected_version), restored, actor.user_id, transfer["id"], details)
        except Conflict:
            self._quarantine(record["id"], actor, action, "批次恢复冲突，晚到写入已隔离", data)

    def _withdraw_consent(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        payload = record["payload"]
        if record["state"] == "closed":
            raise Conflict("计划已结束，无同意可撤回")
        if not payload.get("consent"):
            raise Conflict("当前无有效同意可撤回")
        new_payload = dict(payload)
        new_payload["consent"] = False
        new_payload["consent_withdrawn"] = True
        details = {"summary": "监护人已撤回同意", "input": data, "reason": optional_text(data, "reason")}
        try:
            return self.repository.withdraw_consent(record, int(expected_version), new_payload, actor.user_id, details)
        except Conflict:
            self._quarantine(record["id"], actor, "withdraw_consent", "撤回同意冲突，晚到写入已隔离", data)

    def _todo_action(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self._require_owner_org(actor, record, action, data)
        if record["state"] == "closed":
            raise Conflict("计划已结束，待办不可变更")
        if action == "todo_create":
            title = text(data, "title")
            org = actor.organization or record["payload"].get("owner_org", "")
            details = {"summary": "待办已创建", "input": data, "title": title, "org": org}
            try:
                return self.repository.todo_create(record, int(expected_version), title, org, actor.user_id, details)
            except Conflict:
                if self.repository.get_open_transfer(record["id"]) is not None:
                    self._quarantine(record["id"], actor, action, "交接进行中，晚到写入已隔离", data)
                raise
        todo_id = integer(data, "todo_id", 1)
        todo = self.repository.get_todo(record["id"], todo_id)
        if action == "todo_complete":
            if todo["status"] == "invalidated":
                raise Conflict("待办已失效，需重新确认后再完成")
            if todo["status"] != "open":
                raise Conflict("待办已完成")
            details = {"summary": "待办已完成", "input": data}
        else:
            if todo["status"] != "invalidated":
                raise Conflict("仅失效待办需要重新确认")
            details = {"summary": "待办已重新确认", "input": data}
        status = "done" if action == "todo_complete" else "open"
        try:
            return self.repository.todo_update(record, int(expected_version), todo_id, status, actor.user_id, action, details)
        except Conflict:
            if self.repository.get_open_transfer(record["id"]) is not None:
                self._quarantine(record["id"], actor, action, "交接进行中，晚到写入已隔离", data)
            raise

    def transfers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_transfers(record_id)

    def todos(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_todos(record_id)

    def quarantined(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_quarantine(record_id)

    def backfill_owner_org(self, actor: Actor, batch_id: str, org: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可执行迁移回填")
        batch_id = text({"batch_id": batch_id}, "batch_id")
        org = text({"org": org}, "org")
        return self.repository.backfill_owner_org(batch_id, org, actor.user_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
