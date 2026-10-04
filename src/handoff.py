"""跨校交接链用例：冻结、补录窗口、接手、撤回、失败恢复。"""
import uuid
from typing import Any, Dict, Optional

from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .rules import TRANSFER_FAILED_STATE


class HandoffService:
    def __init__(self, repository: Any, rules: Any) -> None:
        self.repository = repository
        self.rules = rules

    @staticmethod
    def _org_guard(actor: Actor, expected_org: str, message: str) -> str:
        org = expected_org or actor.organization
        if actor.role != "admin":
            if not actor.organization or actor.organization != org:
                raise PermissionDenied(message)
        return org

    def _consent_valid(self, record_id: int, payload: Dict[str, Any]) -> bool:
        latest = self.repository.latest_consent(record_id)
        if latest is not None:
            return latest["status"] == "granted"
        return bool(payload.get("consent"))

    def _load_transfer(self, record: Dict[str, Any], data: Dict[str, Any], only_pending: bool = True) -> Dict[str, Any]:
        data = data or {}
        transfer_id = data.get("transfer_id")
        if transfer_id is None:
            pending = self.repository.find_pending_transfer(record["id"])
            if pending is None:
                raise Conflict("没有进行中的交接单，晚到写入按冲突处理")
            transfer_id = pending["id"]
        if isinstance(transfer_id, bool) or not isinstance(transfer_id, int):
            raise ValidationError("transfer_id必须是整数")
        transfer = self.repository.get_transfer(int(transfer_id))
        if int(transfer["record_id"]) != int(record["id"]):
            raise ValidationError("交接单与计划不匹配")
        if only_pending and transfer["status"] != "pending":
            raise Conflict("交接单已结束（%s），晚到写入按冲突处理" % transfer["status"])
        return transfer

    def transfer_out(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        info = self.rules.validate_transfer_out(data)
        from_org = self._org_guard(actor, record.get("owning_org", ""), "仅原建档学校可以发起转出")
        if not self._consent_valid(record["id"], record["payload"]):
            raise Conflict("监护人同意已失效，无法发起转出")
        if not info["to_org"] or info["to_org"] == from_org:
            raise ValidationError("接收学校必须与原校不同")
        batch_no = (data or {}).get("batch_no")
        if batch_no is not None:
            batch_no = text(data, "batch_no")
        else:
            batch_no = "TB-%s-%s" % (record["id"], uuid.uuid4().hex[:8])
        _, payload, _ = self.rules.apply_action(record, "transfer_out", {})
        return self.repository.freeze_for_transfer(
            record_id=record["id"],
            expected_version=int(expected_version),
            payload=payload,
            actor_id=actor.user_id,
            batch_no=batch_no,
            from_org=from_org,
            to_org=info["to_org"],
            reason=info["reason"],
        )

    def confirm_transfer(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        transfer = self._load_transfer(record, data, only_pending=True)
        if actor.role != "admin" and actor.organization != transfer["to_org"]:
            raise PermissionDenied("仅接收学校可以确认接手")
        if not self._consent_valid(record["id"], record["payload"]):
            raise Conflict("监护人已撤回同意，不能确认接手")
        _, base_payload, _ = self.rules.apply_action(
            {"state": record["state"], "payload": record["payload"]}, "confirm_transfer", {}
        )
        snapshot = self.rules.handover_snapshot(record["payload"], transfer["to_org"],
                                                transfer["from_org"], transfer["batch_no"])
        prepared = dict(base_payload)
        prepared["handover"] = snapshot
        prepared["batch_no"] = transfer["batch_no"]

        def builder(_: Dict[str, Any]) -> Dict[str, Any]:
            return prepared

        return self.repository.advance_transfer(
            "confirm_transfer", record["id"], transfer["id"], int(expected_version), actor.user_id, builder
        )

    def decline_transfer(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        transfer = self._load_transfer(record, data, only_pending=True)
        if actor.role != "admin" and actor.organization != transfer["to_org"]:
            raise PermissionDenied("仅接收学校可以拒绝接手")
        return self.repository.advance_transfer(
            "decline_transfer", record["id"], transfer["id"], int(expected_version), actor.user_id, lambda _: None
        )

    def withdraw_in_flight(self, actor: Actor, record: Dict[str, Any], expected_version: int,
                           data: Dict[str, Any], transfer: Dict[str, Any]) -> Dict[str, Any]:
        reason = (data or {}).get("reason", "监护人撤回同意")
        _, payload, _ = self.rules.apply_action(
            {"state": record["state"], "payload": record["payload"]}, "withdraw_consent", data or {}
        )

        def builder(_: Dict[str, Any]) -> Dict[str, Any]:
            return payload

        return self.repository.advance_transfer(
            "withdraw_consent", record["id"], transfer["id"], int(expected_version), actor.user_id, builder,
            consent={"status": "withdrawn", "scope": "", "reason": str(reason), "batch_no": transfer["batch_no"]},
        )

    def restore_batch(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        transfer = self._load_transfer(record, data, only_pending=False)
        if actor.role != "admin" and actor.organization != transfer["from_org"]:
            raise PermissionDenied("仅原建档学校可以恢复原批次")
        _, payload, _ = self.rules.apply_action(record, "restore_batch", {})
        result = self.repository.restore_transfer_batch(
            record["id"], transfer["id"], int(expected_version), payload, actor.user_id
        )
        result["reopened_todos"] = self.repository.reopen_batch_todos(
            record["id"], transfer["batch_no"], actor.user_id
        )
        return result
