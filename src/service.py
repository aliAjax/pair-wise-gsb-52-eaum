"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, DomainError, PermissionDenied, ValidationError, text
from .handoff import HandoffService
from .repository import Repository
from .rules import DomainRules


HANDOFF_ACTIONS = {"transfer_out", "confirm_transfer", "decline_transfer", "restore_batch", "withdraw_consent"}
CONSENT_ACTIONS = {"consent", "grant_consent"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.handoff = HandoffService(repository, rules)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id,
                                      owning_org=actor.organization or "")

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def _consent_valid(self, record_id: int, payload: Dict[str, Any]) -> bool:
        latest = self.repository.latest_consent(record_id)
        if latest is not None:
            return latest["status"] == "granted"
        return bool(payload.get("consent"))

    def _guard_write(self, actor: Actor, record: Dict[str, Any], action: str) -> None:
        """机构归属与交接阶段的写入边界；违规则隔离。"""
        if actor.role == "admin":
            return
        owning = record.get("owning_org", "")
        pending = self.repository.find_pending_transfer(record["id"])
        if action == "log_service":
            # 外部服务/新校在确认前只能由原校建档机构补录；家长撤回后任何服务登记都隔离
            if not self._consent_valid(record["id"], record["payload"]):
                raise PermissionDenied("监护人同意已撤回，外部机构服务登记暂停")
            if record["state"] == "transferring":
                if actor.organization != owning:
                    raise PermissionDenied("新校确认前仅原建档机构可以补录服务")
            else:
                if owning and actor.organization and actor.organization != owning:
                    raise PermissionDenied("机构归属不匹配，原校角色只能查看历史，禁止写入服务记录")
            return
        if owning and actor.organization and actor.organization != owning:
            raise PermissionDenied("机构归属不匹配，原校角色只能查看历史")
        if pending is not None and action not in HANDOFF_ACTIONS:
            raise PermissionDenied("交接冻结中，计划字段不可修改（服务补录除外）")

    def _quarantine(self, actor: Actor, record_id: int, action: str, data: Any, reason: str,
                    batch_no: str = "", error_cls=PermissionDenied) -> DomainError:
        qid = self.repository.quarantine_write(
            actor_id=actor.user_id, actor_role=actor.role, actor_org=actor.organization,
            action=action, reason=reason, payload=data if isinstance(data, dict) else {"raw": data},
            record_id=record_id, batch_no=batch_no,
        )
        message = ("写入已被隔离：%s" % reason) if error_cls is PermissionDenied else str(reason)
        error = error_cls(message)
        error.extra.update({"quarantine_id": qid, "reason": reason, "original_input": data})
        return error

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        data = data or {}

        if action in CONSENT_ACTIONS or action == "withdraw_consent":
            return self._consent_action(actor, record, expected_version, action, data)
        if action in HANDOFF_ACTIONS:
            return self._handoff_action(actor, record, expected_version, action, data)

        try:
            self._guard_write(actor, record, action)
            new_state, new_payload, summary = self.rules.apply_action(record, action, data)
            return self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
            )
        except Conflict as exc:
            batch = record.get("migration_batch", "")
            raise self._quarantine(actor, record_id, action, data, "晚到写入冲突：%s" % exc, batch,
                                   error_cls=Conflict) from exc
        except PermissionDenied as exc:
            batch = record.get("migration_batch", "")
            raise self._quarantine(actor, record_id, action, data, str(exc), batch) from exc

    def _consent_action(self, actor: Actor, record: Dict[str, Any], expected_version: int,
                        action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            pending = self.repository.find_pending_transfer(record["id"])
            if action == "withdraw_consent" and pending is not None and pending["status"] == "pending":
                # 家长在交接途中撤回：撤回同意 + 交接单失败，同一事务
                if not self._consent_valid(record["id"], record["payload"]):
                    raise Conflict("同意已处于撤回状态")
                return self.handoff.withdraw_in_flight(actor, record, expected_version, data, pending)

            new_state, new_payload, summary = self.rules.apply_action(record, action, data)
            consent_status = "granted"
            scope = data.get("consent_scope", record["payload"].get("consent_scope", ""))
            if action == "withdraw_consent":
                consent_status = "withdrawn"
                scope = ""
            result = self.repository.apply_consent(
                record_id=record["id"],
                expected_version=int(expected_version),
                new_state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                consent_status=consent_status,
                scope=scope,
                batch_no=record.get("migration_batch", ""),
                reason=data.get("reason", "") if isinstance(data, dict) else "",
                action=action,
                summary=summary,
            )
            return result
        except Conflict as exc:
            raise self._quarantine(actor, record["id"], action, data, "晚到写入冲突：%s" % exc,
                                   record.get("migration_batch", ""), error_cls=Conflict) from exc
        except PermissionDenied as exc:
            raise self._quarantine(actor, record["id"], action, data, str(exc),
                                   record.get("migration_batch", "")) from exc

    def _handoff_action(self, actor: Actor, record: Dict[str, Any], expected_version: int,
                        action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if action == "transfer_out":
                return self.handoff.transfer_out(actor, record, expected_version, data)
            if action == "confirm_transfer":
                return self.handoff.confirm_transfer(actor, record, expected_version, data)
            if action == "decline_transfer":
                return self.handoff.decline_transfer(actor, record, expected_version, data)
            if action == "restore_batch":
                return self.handoff.restore_batch(actor, record, expected_version, data)
            raise PermissionDenied("不支持的交接动作")
        except (Conflict, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied) and getattr(exc, "extra", None) and exc.extra.get("quarantine_id"):
                raise
            batch = record.get("migration_batch", "")
            if isinstance(exc, Conflict):
                raise self._quarantine(actor, record["id"], action, data, "晚到写入冲突：%s" % exc,
                                       batch, error_cls=Conflict) from exc
            raise self._quarantine(actor, record["id"], action, data, str(exc), batch) from exc

    # ---- 交接单 / 同意时间线 ----

    def list_transfers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_transfers(record_id)

    def consent_timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.consent_timeline(record_id)

    # ---- 待办 ----

    def create_todo(self, actor: Actor, record_id: int, title: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        if actor.role != "admin" and record.get("owning_org") and actor.organization and \
                actor.organization != record["owning_org"]:
            raise PermissionDenied("机构归属不匹配，不能为他校计划创建待办")
        return self.repository.create_todo(record_id, text({"title": title}, "title"), actor.user_id)

    def list_todos(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_todos(record_id)

    def complete_todo(self, actor: Actor, record_id: int, todo_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        try:
            if actor.role != "admin" and record.get("owning_org") and actor.organization and \
                    actor.organization != record["owning_org"]:
                raise PermissionDenied("机构归属不匹配")
            return self.repository.complete_todo(record_id, todo_id, actor.user_id)
        except Conflict as exc:
            raise self._quarantine(actor, record_id, "complete_todo", {"todo_id": todo_id}, str(exc),
                                   record.get("migration_batch", ""), error_cls=Conflict) from exc
        except PermissionDenied as exc:
            raise self._quarantine(actor, record_id, "complete_todo", {"todo_id": todo_id}, str(exc),
                                   record.get("migration_batch", "")) from exc

    def reconfirm_todo(self, actor: Actor, record_id: int, todo_id: int, note: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        if actor.role != "admin" and record.get("owning_org") and actor.organization and \
                actor.organization != record["owning_org"]:
            raise PermissionDenied("机构归属不匹配")
        return self.repository.reconfirm_todo(record_id, todo_id, actor.user_id, note or "重新确认")

    # ---- 迁移批次 / 归属回填 / 隔离区 ----

    def register_migration_batch(self, actor: Actor, batch_no: str, from_org: str, to_org: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可以登记迁移批次")
        return self.repository.register_migration_batch(
            text({"batch_no": batch_no}, "batch_no"),
            text({"from_org": from_org}, "from_org"),
            actor.user_id,
            to_org or "",
        )

    def backfill_ownership(self, actor: Actor, batch_no: str, from_org: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可以回填机构归属")
        return self.repository.backfill_ownership(
            text({"batch_no": batch_no}, "batch_no"), from_org or actor.organization, actor.user_id
        )

    def list_quarantine(self, actor: Actor, batch_no: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.organization:
            items = self.repository.list_quarantine(limit=500, batch_no=batch_no)
            return [item for item in items if item["actor_org"] == actor.organization]
        return self.repository.list_quarantine(limit=500, batch_no=batch_no)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
