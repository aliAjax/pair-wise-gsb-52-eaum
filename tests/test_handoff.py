import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


SCHOOL_A = "school-A"
SCHOOL_B = "school-B"
CREATE_DATA = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600,
               'delivered_minutes': 120, 'review_due_days': 30, 'goals_count': 3, 'consent': False}


def manager(org):
    return Actor("mgr-%s" % org, "case_manager", org)


def specialist(org):
    return Actor("sp-%s" % org, "specialist", org)


PARENT = Actor("parent", "parent_rep")
ADMIN = Actor("admin", "admin")


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.service.create(manager(SCHOOL_A), "IEP-30001", CREATE_DATA)
        self.record = self.service.act(PARENT, self.record["id"], self.record["version"], "consent",
                                       {'guardian_confirmed': True, 'consent_scope': '语言康复'})
        self.record = self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"], "activate", {})
        self.record = self.service.act(specialist(SCHOOL_A), self.record["id"], self.record["version"],
                                       "log_service", {'session_minutes': 60, 'provider': 'SP-1'})
        self.todo = self.service.create_todo(manager(SCHOOL_A), self.record["id"], "阶段评估待办")

    def tearDown(self):
        self.temp.cleanup()

    def test_freeze_supplement_confirm_and_readonly_history(self):
        # 原校发起转出：计划冻结、交接单pending、未完成待办失效
        result = self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"],
                                  "transfer_out", {'to_org': SCHOOL_B, 'batch_no': 'TB-001'})
        self.assertEqual(result["record"]["state"], "transferring")
        self.assertEqual(result["record"]["payload"]["plan_status"], "frozen")
        self.assertEqual(result["transfer"]["status"], "pending")
        todos = self.service.list_todos(manager(SCHOOL_A), self.record["id"])
        self.assertEqual(todos[0]["status"], "invalidated")
        self.assertTrue(todos[0]["requires_reconfirm"])

        # 冻结后原校仍可补录服务；新校/外部机构补录被隔离
        self.record = self.service.get_record(manager(SCHOOL_A), self.record["id"])
        self.record = self.service.act(specialist(SCHOOL_A), self.record["id"], self.record["version"],
                                       "log_service", {'session_minutes': 30, 'provider': 'SP-2'})
        self.assertEqual(self.record["payload"]["delivered_minutes"], 210)
        with self.assertRaises(PermissionDenied) as blocked:
            self.service.act(specialist(SCHOOL_B), self.record["id"], self.record["version"],
                             "log_service", {'session_minutes': 30, 'provider': 'SP-9'})
        self.assertIn("quarantine_id", blocked.exception.extra)
        self.assertEqual(blocked.exception.extra["original_input"]["provider"], "SP-9")
        # 冻结中改计划字段同样隔离
        with self.assertRaises(PermissionDenied):
            self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"], "review",
                             {'progress_note': 'x'})

        # 新校确认：接手有效目标与服务进度，机构归属变更
        result = self.service.act(manager(SCHOOL_B), self.record["id"], self.record["version"],
                                  "confirm_transfer", {})
        self.assertEqual(result["record"]["state"], "active")
        self.assertEqual(result["record"]["owning_org"], SCHOOL_B)
        snapshot = result["record"]["payload"]["handover"]
        self.assertEqual(snapshot["from_org"], SCHOOL_A)
        self.assertEqual(snapshot["to_org"], SCHOOL_B)
        self.assertEqual(snapshot["batch_no"], "TB-001")
        self.assertEqual(len(snapshot["active_goals"]), 3)
        self.assertEqual(snapshot["delivered_minutes"], 210)

        # 原校角色只能查看历史，任何写入都被隔离
        with self.assertRaises(PermissionDenied):
            self.service.act(manager(SCHOOL_A), result["record"]["id"], result["record"]["version"],
                             "log_service", {'session_minutes': 10, 'provider': 'SP-1'})
        history = self.service.timeline(manager(SCHOOL_A), self.record["id"])
        self.assertTrue(any(event["action"] == "transfer_out" for event in history))
        self.assertTrue(any(event["action"] == "confirm_transfer" for event in history))
        # 新校可以续做并重新确认承接的待办
        self.service.act(specialist(SCHOOL_B), result["record"]["id"], result["record"]["version"],
                         "log_service", {'session_minutes': 20, 'provider': 'SP-3'})
        with self.assertRaises(Conflict):
            self.service.complete_todo(manager(SCHOOL_B), self.record["id"], self.todo["id"])
        reconfirmed = self.service.reconfirm_todo(manager(SCHOOL_B), self.record["id"], self.todo["id"], "新校续接")
        self.assertEqual(reconfirmed["status"], "open")
        done = self.service.complete_todo(manager(SCHOOL_B), self.record["id"], self.todo["id"])
        self.assertEqual(done["status"], "done")

        # 隔离区保留所有越权/冲突原输入
        quarantined = self.service.list_quarantine(ADMIN)
        reasons = " ".join(item["reason"] for item in quarantined)
        self.assertIn("原建档机构", reasons)
        self.assertIn("查看历史", reasons)
        self.assertTrue(all("payload" in item for item in quarantined))

    def test_concurrent_handoff_late_writes_conflict_and_keep_input(self):
        self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"],
                         "transfer_out", {'to_org': SCHOOL_B})
        self.record = self.service.get_record(manager(SCHOOL_A), self.record["id"])
        version = self.record["version"]

        # 原校尝试再次发起交接（并发重复提交）→ 冲突并隔离
        with self.assertRaises(Conflict):
            self.service.act(manager(SCHOOL_A), self.record["id"], version,
                             "transfer_out", {'to_org': SCHOOL_B})
        # 原校冒充接收方确认 → 越权隔离
        with self.assertRaises(PermissionDenied) as wrong_school:
            self.service.act(manager(SCHOOL_A), self.record["id"], version, "confirm_transfer", {})
        self.assertIn("quarantine_id", wrong_school.exception.extra)
        # 第一次确认成功
        first = self.service.act(manager(SCHOOL_B), self.record["id"], version, "confirm_transfer", {})
        # 两校并发确认：晚到的确认返回冲突，原输入保留
        with self.assertRaises(Conflict) as late:
            self.service.act(manager(SCHOOL_B), self.record["id"], version, "confirm_transfer", {})
        self.assertEqual(late.exception.status, 409)
        self.assertIn("quarantine_id", late.exception.extra)
        self.assertEqual(late.exception.extra["original_input"], {})
        quarantined = self.service.list_quarantine(ADMIN)
        self.assertGreaterEqual(len(quarantined), 3)

    def test_guardian_withdraws_in_flight_then_restore_and_continue(self):
        self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"],
                         "transfer_out", {'to_org': SCHOOL_B, 'batch_no': 'TB-002'})
        self.record = self.service.get_record(PARENT, self.record["id"])
        # 家长途中撤回：交接单失败、同意失效、待办随状态变更失效
        result = self.service.act(PARENT, self.record["id"], self.record["version"],
                                  "withdraw_consent", {'reason': '搬迁计划取消'})
        self.assertEqual(result["record"]["state"], "transfer_failed")
        self.assertEqual(result["transfer"]["status"], "withdrawn")
        self.assertFalse(result["record"]["payload"]["consent"])
        self.assertIn("consent_withdrawn_at", result["record"]["payload"])

        # 新校晚到确认 → 冲突隔离；外部机构在撤回后登记服务 → 隔离
        with self.assertRaises(Conflict):
            self.service.act(manager(SCHOOL_B), result["record"]["id"], result["record"]["version"],
                             "confirm_transfer", {'transfer_id': result["transfer"]["id"]})
        with self.assertRaises(PermissionDenied):
            self.service.act(specialist(SCHOOL_A), result["record"]["id"], result["record"]["version"],
                             "log_service", {'session_minutes': 15, 'provider': 'SP-1'})

        # 家长重新授权 → 原校恢复原批次，归属回到原校并可续做
        record = self.service.act(PARENT, result["record"]["id"], result["record"]["version"],
                                  "grant_consent", {'guardian_confirmed': True, 'consent_scope': '语言康复'})
        restored = self.service.act(manager(SCHOOL_A), record["id"], record["version"], "restore_batch",
                                    {'transfer_id': result["transfer"]["id"]})
        self.assertEqual(restored["record"]["state"], "active")
        self.assertEqual(restored["record"]["owning_org"], SCHOOL_A)
        self.assertEqual(restored["transfer"]["status"], "restored")
        self.assertGreaterEqual(restored["reopened_todos"], 1)
        self.service.act(specialist(SCHOOL_A), restored["record"]["id"], restored["record"]["version"],
                         "log_service", {'session_minutes': 20, 'provider': 'SP-1'})
        # 同意链全程可追溯
        consents = self.service.consent_timeline(PARENT, self.record["id"])
        self.assertEqual([item["status"] for item in consents], ["granted", "withdrawn", "granted"])

    def test_new_school_decline_then_restore_batch(self):
        frozen = self.service.act(manager(SCHOOL_A), self.record["id"], self.record["version"],
                                  "transfer_out", {'to_org': SCHOOL_B, 'batch_no': 'TB-003'})
        failed = self.service.act(manager(SCHOOL_B), frozen["record"]["id"], frozen["record"]["version"],
                                  "decline_transfer", {})
        self.assertEqual(failed["record"]["state"], "transfer_failed")
        self.assertEqual(failed["transfer"]["status"], "declined")
        restored = self.service.act(manager(SCHOOL_A), failed["record"]["id"], failed["record"]["version"],
                                    "restore_batch", {'transfer_id': failed["transfer"]["id"]})
        self.assertEqual(restored["record"]["state"], "active")
        self.assertEqual(restored["record"]["owning_org"], SCHOOL_A)
        self.assertEqual(restored["transfer"]["status"], "restored")
        # 恢复后可再发起新一轮交接
        again = self.service.act(manager(SCHOOL_A), restored["record"]["id"], restored["record"]["version"],
                                 "transfer_out", {'to_org': 'school-C'})
        self.assertEqual(again["record"]["state"], "transferring")

    def test_backfill_ownership_by_migration_batch(self):
        # 模拟旧数据：有迁移批次但缺少机构归属
        rid = self.repository_create_legacy("IEP-LEGACY-1", "BATCH-OLD")
        rid2 = self.repository_create_legacy("IEP-LEGACY-2", "BATCH-OLD")
        result = self.service.backfill_ownership(ADMIN, "BATCH-OLD", SCHOOL_A)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.service.get_record(ADMIN, rid)["owning_org"], SCHOOL_A)
        self.assertEqual(self.service.get_record(ADMIN, rid2)["owning_org"], SCHOOL_A)
        # 已回填的不重复写
        again = self.service.backfill_ownership(ADMIN, "BATCH-OLD", SCHOOL_A)
        self.assertEqual(again["updated"], 0)
        timeline = self.service.timeline(ADMIN, rid)
        self.assertTrue(any(event["action"] == "ownership_backfilled" for event in timeline))

    def repository_create_legacy(self, reference, batch_no):
        prepared = self.service.rules.prepare_create(
            {'student_id': reference, 'disability': 'autism', 'service_minutes': 100,
             'delivered_minutes': 0, 'review_due_days': 10, 'goals_count': 1, 'consent': True})
        record = self.service.repository.create(reference, "active", prepared, "legacy",
                                                owning_org="", batch_no=batch_no)
        return record["id"]
