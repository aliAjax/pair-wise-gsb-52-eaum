import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, QuarantinedConflict


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
ORG_A = 'SCHOOL-A'
ORG_B = 'SCHOOL-B'


class TransferTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def make_active(self, reference='IEP-90001', org=ORG_A):
        record = self.service.create(Actor('creator', 'case_manager', org), reference, CREATE_DATA)
        record = self.service.act(Actor('guardian', 'parent_rep'), record['id'], record['version'], 'consent', {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
        record = self.service.act(Actor('manager', 'case_manager', org), record['id'], record['version'], 'activate', {})
        return record

    def initiate(self, record, from_org=ORG_A, to_org=ORG_B):
        return self.service.act(Actor('manager', 'case_manager', from_org), record['id'], record['version'], 'transfer_initiate', {'to_org': to_org})


class TransferChainTest(TransferTestBase):
    def test_full_handover_chain(self):
        record = self.make_active()
        self.assertEqual(record['payload']['owner_org'], ORG_A)
        record = self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'todo_create', {'title': '补录缺席服务'})
        todo = self.service.todos(Actor('manager', 'case_manager', ORG_A), record['id'])[0]
        self.assertEqual(todo['status'], 'open')

        record = self.initiate(record)
        self.assertTrue(record['payload']['frozen'])
        self.assertEqual(record['payload']['transfer_state'], 'initiated')
        todo = self.service.todos(Actor('manager', 'case_manager', ORG_A), record['id'])[0]
        self.assertEqual(todo['status'], 'invalidated')

        with self.assertRaises(Conflict):
            self.service.act(Actor('reviewer', 'administrator', ORG_A), record['id'], record['version'], 'review', {'progress_note': '冻结中'})

        record = self.service.act(Actor('sp', 'specialist', ORG_A), record['id'], record['version'], 'log_service', {'session_minutes': 60, 'provider': 'SP-3'})
        self.assertEqual(record['payload']['delivered_minutes'], 180)

        record = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'transfer_confirm', {})
        self.assertEqual(record['payload']['owner_org'], ORG_B)
        self.assertFalse(record['payload']['frozen'])
        self.assertEqual(record['payload']['transfer_state'], 'confirmed')
        self.assertEqual(record['payload']['delivered_minutes'], 180)
        self.assertEqual(record['payload']['goals_count'], 4)

        with self.assertRaises(QuarantinedConflict) as ctx:
            self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'amend', {'amendment_reason': '越权', 'updated_goals': ['目标X']})
        self.assertEqual(ctx.exception.details['input']['amendment_reason'], '越权')
        quarantined = self.service.quarantined(Actor('receiver', 'case_manager', ORG_B), record['id'])
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0]['actor_org'], ORG_A)
        self.assertEqual(quarantined[0]['input']['updated_goals'], ['目标X'])

        record = self.service.act(Actor('admin', 'administrator', ORG_B), record['id'], record['version'], 'review', {'progress_note': '新校复查'})
        record = self.service.act(Actor('manager', 'case_manager', ORG_B), record['id'], record['version'], 'amend', {'amendment_reason': '新校调整', 'updated_goals': ['目标A', '目标B', '目标C']})
        self.assertEqual(record['payload']['goals_count'], 3)

        history = self.service.timeline(Actor('manager', 'case_manager', ORG_A), record['id'])
        actions = [event['action'] for event in history]
        self.assertIn('transfer_initiate', actions)
        self.assertIn('transfer_confirm', actions)
        self.assertIn('write_quarantined', actions)
        transfers = self.service.transfers(Actor('manager', 'case_manager', ORG_A), record['id'])
        self.assertEqual(transfers[0]['state'], 'confirmed')
        self.assertEqual(transfers[0]['from_org'], ORG_A)
        self.assertEqual(transfers[0]['to_org'], ORG_B)

    def test_double_submission_conflict_preserves_input(self):
        record = self.make_active()
        record = self.initiate(record)
        with self.assertRaises(QuarantinedConflict) as ctx:
            self.initiate(record)
        self.assertEqual(ctx.exception.details['input']['to_org'], ORG_B)

        confirmed = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'transfer_confirm', {})
        with self.assertRaises(QuarantinedConflict):
            self.service.act(Actor('receiver2', 'case_manager', ORG_B), confirmed['id'], confirmed['version'], 'transfer_confirm', {})
        quarantined = self.service.quarantined(Actor('receiver', 'case_manager', ORG_B), record['id'])
        self.assertEqual(len(quarantined), 2)
        self.assertEqual({item['action'] for item in quarantined}, {'transfer_initiate', 'transfer_confirm'})

    def test_stale_write_during_open_transfer_is_quarantined(self):
        record = self.make_active()
        stale_version = record['version']
        record = self.initiate(record)
        with self.assertRaises(QuarantinedConflict) as ctx:
            self.service.act(Actor('sp', 'specialist', ORG_A), record['id'], stale_version, 'log_service', {'session_minutes': 30, 'provider': 'SP-9'})
        self.assertEqual(ctx.exception.details['input']['session_minutes'], 30)


class ConsentWithdrawalTest(TransferTestBase):
    def test_withdraw_mid_transfer_fail_restore_and_continue(self):
        record = self.make_active()
        record = self.initiate(record)
        record = self.service.act(Actor('guardian', 'parent_rep'), record['id'], record['version'], 'withdraw_consent', {'reason': '家庭原因'})
        self.assertFalse(record['payload']['consent'])
        self.assertTrue(record['payload']['consent_withdrawn'])
        self.assertEqual(record['payload']['transfer_state'], 'failed')
        transfer = self.service.transfers(Actor('manager', 'case_manager', ORG_A), record['id'])[0]
        self.assertEqual(transfer['state'], 'failed')

        with self.assertRaises(QuarantinedConflict):
            self.service.act(Actor('ext', 'external_provider', 'EXT-1'), record['id'], record['version'], 'log_service', {'session_minutes': 45, 'provider': 'EXT-1'})
        with self.assertRaises(QuarantinedConflict):
            self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'transfer_confirm', {})

        with self.assertRaises(Conflict):
            self.initiate(record)

        restored = self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'transfer_restore', {})
        self.assertTrue(restored['payload']['frozen'])
        self.assertEqual(restored['payload']['transfer_state'], 'initiated')
        self.assertFalse(restored['payload']['consent'])
        transfer = self.service.transfers(Actor('manager', 'case_manager', ORG_A), record['id'])[0]
        self.assertEqual(transfer['state'], 'initiated')

        record = self.service.act(Actor('guardian', 'parent_rep'), restored['id'], restored['version'], 'consent', {'guardian_confirmed': True, 'consent_scope': '转学后继续服务'})
        self.assertTrue(record['payload']['consent'])
        self.assertFalse(record['payload']['consent_withdrawn'])
        record = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'transfer_confirm', {})
        self.assertEqual(record['payload']['owner_org'], ORG_B)
        self.assertEqual(record['payload']['transfer_state'], 'confirmed')

    def test_external_provider_allowed_before_withdrawal(self):
        record = self.make_active()
        record = self.service.act(Actor('ext', 'external_provider', 'EXT-1'), record['id'], record['version'], 'log_service', {'session_minutes': 45, 'provider': 'EXT-1'})
        self.assertEqual(record['payload']['delivered_minutes'], 165)
        record = self.service.act(Actor('guardian', 'parent_rep'), record['id'], record['version'], 'withdraw_consent', {})
        with self.assertRaises(QuarantinedConflict):
            self.service.act(Actor('ext', 'external_provider', 'EXT-1'), record['id'], record['version'], 'log_service', {'session_minutes': 45, 'provider': 'EXT-1'})


class TodoTest(TransferTestBase):
    def test_todo_invalidation_and_reconfirm(self):
        record = self.make_active()
        record = self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'todo_create', {'title': '补录服务记录'})
        todo_id = self.service.todos(Actor('manager', 'case_manager', ORG_A), record['id'])[0]['id']
        record = self.initiate(record)

        with self.assertRaises(Conflict):
            self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'todo_complete', {'todo_id': todo_id})

        record = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'transfer_confirm', {})
        with self.assertRaises(QuarantinedConflict):
            self.service.act(Actor('manager', 'case_manager', ORG_A), record['id'], record['version'], 'todo_reconfirm', {'todo_id': todo_id})

        record = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'todo_reconfirm', {'todo_id': todo_id})
        todo = self.service.todos(Actor('receiver', 'case_manager', ORG_B), record['id'])[0]
        self.assertEqual(todo['status'], 'open')
        record = self.service.act(Actor('receiver', 'case_manager', ORG_B), record['id'], record['version'], 'todo_complete', {'todo_id': todo_id})
        todo = self.service.todos(Actor('receiver', 'case_manager', ORG_B), record['id'])[0]
        self.assertEqual(todo['status'], 'done')


class MigrationBackfillTest(TransferTestBase):
    def test_backfill_owner_org_by_batch(self):
        legacy = self.service.create(Actor('creator', 'case_manager'), 'IEP-LEGACY-1', CREATE_DATA)
        self.assertEqual(legacy['payload']['owner_org'], '')
        owned = self.make_active(reference='IEP-90002')

        with self.assertRaises(PermissionDenied):
            self.service.backfill_owner_org(Actor('manager', 'case_manager', ORG_A), 'MIG-1', 'ORG-LEGACY')

        result = self.service.backfill_owner_org(Actor('admin', 'admin'), 'MIG-1', 'ORG-LEGACY')
        self.assertEqual(result['applied'], 1)
        legacy = self.service.get_record(Actor('admin', 'admin'), legacy['id'])
        self.assertEqual(legacy['payload']['owner_org'], 'ORG-LEGACY')
        owned = self.service.get_record(Actor('admin', 'admin'), owned['id'])
        self.assertEqual(owned['payload']['owner_org'], ORG_A)
        timeline = self.service.timeline(Actor('admin', 'admin'), legacy['id'])
        backfill_events = [event for event in timeline if event['action'] == 'org_backfilled']
        self.assertEqual(backfill_events[0]['details']['batch_id'], 'MIG-1')

        with self.assertRaises(Conflict):
            self.service.backfill_owner_org(Actor('admin', 'admin'), 'MIG-1', 'ORG-LEGACY')

    def test_backfill_prefers_created_by_org(self):
        record = self.service.repository.create('IEP-90003', 'draft', {'student_id': 'S-1', 'owner_org': ''}, 'creator', ORG_A)
        self.service.repository.backfill_owner_org('MIG-2', 'ORG-LEGACY', 'admin')
        record = self.service.get_record(Actor('admin', 'admin'), record['id'])
        self.assertEqual(record['payload']['owner_org'], ORG_A)


if __name__ == '__main__':
    unittest.main()
