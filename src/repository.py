"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import TRANSFER_FAILED_STATE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    owning_org TEXT NOT NULL DEFAULT '',
                    migration_batch TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guardian_consents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    batch_no TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL UNIQUE,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_version INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    handover_snapshot TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    requires_reconfirm INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantined_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    batch_no TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL DEFAULT '',
                    actor_org TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS migration_batches (
                    batch_no TEXT PRIMARY KEY,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL DEFAULT '',
                    record_count INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_org ON records(owning_org);
                CREATE INDEX IF NOT EXISTS idx_records_batch ON records(migration_batch);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_consents_record ON guardian_consents(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_transfers_record ON transfers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_todos_record ON todos(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_quarantine_batch ON quarantined_writes(batch_no);
                """
            )
            cols = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "owning_org" not in cols:
                connection.execute("ALTER TABLE records ADD COLUMN owning_org TEXT NOT NULL DEFAULT ''")
            if "migration_batch" not in cols:
                connection.execute("ALTER TABLE records ADD COLUMN migration_batch TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, owning_org: str = "", batch_no: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,owning_org,migration_batch,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, self._dump(payload), owning_org, batch_no, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, self._dump({"state": state, "owning_org": owning_org, "migration_batch": batch_no}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100, organization: str = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state and organization:
                rows = connection.execute("SELECT * FROM records WHERE state=? AND owning_org=? ORDER BY id DESC LIMIT ?", (state, organization, limit)).fetchall()
            elif state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            elif organization:
                rows = connection.execute("SELECT * FROM records WHERE owning_org=? ORDER BY id DESC LIMIT ?", (organization, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, self._dump(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, self._dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 监护人同意 ----

    def latest_consent(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM guardian_consents WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
        return dict(row) if row else None

    def consent_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM guardian_consents WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def apply_consent(self, record_id: int, expected_version: int, new_state: str, payload: Dict[str, Any],
                      actor_id: str, consent_status: str, scope: str, batch_no: str, reason: str,
                      action: str, summary: str) -> Dict[str, Any]:
        """同意授予/撤回与计划状态在同一事务落库，时间线保持单一动作。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version, state FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, self._dump(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO guardian_consents(record_id,status,scope,actor_id,batch_no,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, consent_status, scope, actor_id, batch_no, reason, now),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version,
                 self._dump({"summary": summary, "consent_status": consent_status, "from": row["state"], "to": new_state}),
                 now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 交接单 ----

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFound("交接单不存在")
        item = dict(row)
        item["handover_snapshot"] = json.loads(item["handover_snapshot"]) if item["handover_snapshot"] else None
        return item

    def find_pending_transfer(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transfers WHERE record_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_transfers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM transfers WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["handover_snapshot"] = json.loads(item["handover_snapshot"]) if item["handover_snapshot"] else None
            result.append(item)
        return result

    def freeze_for_transfer(self, record_id: int, expected_version: int, payload: Dict[str, Any],
                            actor_id: str, batch_no: str, from_org: str, to_org: str, reason: str) -> Dict[str, Any]:
        """原校发起转出：冻结计划 + 建立pending交接单，状态变更即失效未完成待办。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if row["state"] != "active":
                connection.rollback()
                raise Conflict("仅生效中的计划可以发起转出")
            pending = connection.execute(
                "SELECT id FROM transfers WHERE record_id=? AND status='pending'", (record_id,)
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("已存在进行中的交接，请勿重复提交")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='transferring',version=?,payload=?,owning_org=?,migration_batch=?,updated_by=?,updated_at=? WHERE id=?",
                (version, self._dump(payload), from_org, batch_no, actor_id, now, record_id),
            )
            try:
                cursor = connection.execute(
                    "INSERT INTO transfers(record_id,batch_no,from_org,to_org,status,record_version,reason,handover_snapshot,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, batch_no, from_org, to_org, "pending", version, reason, "", actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("交接批次号已存在") from exc
            transfer_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE todos SET status='invalidated',requires_reconfirm=1,updated_at=? WHERE record_id=? AND status='open'",
                (now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "transfer_out", actor_id, version,
                 self._dump({"summary": "原校发起转出，计划已冻结", "batch_no": batch_no, "from_org": from_org,
                             "to_org": to_org, "open_todos_invalidated": True}),
                 now),
            )
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return {"record": self._row(record), "transfer": dict(transfer)}

    def advance_transfer(self, action: str, record_id: int, transfer_id: int, expected_version: int,
                         actor_id: str, snapshot_builder: Callable[[Dict[str, Any]], Dict[str, Any]],
                         reason: str = "", consent: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """新校确认/拒绝 或 家长撤回：交接单与计划在同一事务推进。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT state, version, payload, owning_org, migration_batch FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            transfer_row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if transfer_row is None or int(transfer_row["record_id"]) != int(record_id):
                connection.rollback()
                raise NotFound("交接单不存在")
            if transfer_row["status"] != "pending":
                connection.rollback()
                raise Conflict("交接单已结束（%s），晚到写入按冲突处理" % transfer_row["status"])
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            current_payload = json.loads(record_row["payload"])
            new_state = record_row["state"]
            new_payload = current_payload
            new_org = record_row["owning_org"]
            transfer_status = transfer_row["status"]
            snapshot_json = ""
            audit_summary = ""
            invalidate_todos = True

            if action == "confirm_transfer":
                if record_row["state"] != "transferring":
                    connection.rollback()
                    raise Conflict("计划不在交接冻结中，无法确认接手")
                new_state = "active"
                new_payload = snapshot_builder(current_payload)
                snapshot = new_payload.get("handover")
                new_org = transfer_row["to_org"]
                transfer_status = "confirmed"
                snapshot_json = self._dump(snapshot)
                audit_summary = "新校确认接手有效目标与服务进度"
            elif action == "decline_transfer":
                new_state = TRANSFER_FAILED_STATE
                new_payload = current_payload
                transfer_status = "declined"
                audit_summary = "新校拒绝接手，交接失败"
            elif action == "withdraw_consent":
                new_state = TRANSFER_FAILED_STATE if record_row["state"] == "transferring" else record_row["state"]
                built = snapshot_builder(current_payload)
                if built is not None:
                    new_payload = built
                transfer_status = "withdrawn"
                audit_summary = "家长途中撤回同意，交接失败"
            else:
                connection.rollback()
                raise Conflict("未知交接动作")

            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,owning_org=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, self._dump(new_payload), new_org, actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE transfers SET status=?,handover_snapshot=COALESCE(NULLIF(?,''),handover_snapshot),updated_at=? WHERE id=?",
                (transfer_status, snapshot_json, now, transfer_id),
            )
            if invalidate_todos:
                connection.execute(
                    "UPDATE todos SET status='invalidated',requires_reconfirm=1,updated_at=? WHERE record_id=? AND status='open'",
                    (now, record_id),
                )
            if consent:
                connection.execute(
                    "INSERT INTO guardian_consents(record_id,status,scope,actor_id,batch_no,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, consent.get("status", ""), consent.get("scope", ""), actor_id,
                     consent.get("batch_no", ""), consent.get("reason", ""), now),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version,
                 self._dump({"summary": audit_summary, "batch_no": transfer_row["batch_no"],
                             "transfer_status": transfer_status, "from": record_row["state"], "to": new_state}),
                 now),
            )
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        item = dict(transfer)
        item["handover_snapshot"] = json.loads(item["handover_snapshot"]) if item["handover_snapshot"] else None
        return {"record": self._row(record), "transfer": item}

    def restore_transfer_batch(self, record_id: int, transfer_id: int, expected_version: int,
                               payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """交接失败后恢复原批次：计划回到原校并可续做。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT state, version, owning_org FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            transfer_row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if transfer_row is None or int(transfer_row["record_id"]) != int(record_id):
                connection.rollback()
                raise NotFound("交接单不存在")
            if transfer_row["status"] not in ("declined", "withdrawn"):
                connection.rollback()
                raise Conflict("仅失败的交接可以恢复批次")
            if record_row["state"] != TRANSFER_FAILED_STATE:
                connection.rollback()
                raise Conflict("计划不在交接失败状态，无法恢复")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='active',version=?,payload=?,owning_org=?,updated_by=?,updated_at=? WHERE id=?",
                (version, self._dump(payload), transfer_row["from_org"], actor_id, now, record_id),
            )
            connection.execute(
                "UPDATE transfers SET status='restored',updated_at=? WHERE id=?", (now, transfer_id)
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "restore_batch", actor_id, version,
                 self._dump({"summary": "已恢复原批次并可续做", "batch_no": transfer_row["batch_no"],
                             "owning_org": transfer_row["from_org"]}),
                 now),
            )
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        item = dict(transfer)
        item["handover_snapshot"] = json.loads(item["handover_snapshot"]) if item["handover_snapshot"] else None
        return {"record": self._row(record), "transfer": item}

    # ---- 待办 ----

    def create_todo(self, record_id: int, title: str, actor_id: str, batch_no: str = "") -> Dict[str, Any]:
        now = _now()
        record = self.get(record_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO todos(record_id,batch_no,title,status,requires_reconfirm,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, batch_no or record.get("migration_batch", ""), title, "open", 0, actor_id, now, now),
            )
            todo_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, actor_id, "todo_created", record["version"],
                 self._dump({"todo_id": todo_id, "title": title}), now),
            )
            row = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(row)

    def list_todos(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM todos WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def _update_todo(self, connection: sqlite3.Connection, record_id: int, todo_id: int, sets: str, params: tuple) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM todos WHERE id=? AND record_id=?", (todo_id, record_id)).fetchone()
        if row is None:
            raise NotFound("待办不存在")
        connection.execute("UPDATE todos SET %s WHERE id=?" % sets, params + (todo_id,))
        return connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()

    def complete_todo(self, record_id: int, todo_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFound("记录不存在")
            row = connection.execute("SELECT * FROM todos WHERE id=? AND record_id=?", (todo_id, record_id)).fetchone()
            if row is None:
                raise NotFound("待办不存在")
            if row["status"] != "open":
                raise Conflict("待办状态为%s，需先重新确认" % row["status"])
            connection.execute("UPDATE todos SET status='done',updated_at=? WHERE id=?", (now, todo_id))
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "todo_completed", actor_id, record["version"], self._dump({"todo_id": todo_id}), now),
            )
            result = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(result)

    def reconfirm_todo(self, record_id: int, todo_id: int, actor_id: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFound("记录不存在")
            row = connection.execute("SELECT * FROM todos WHERE id=? AND record_id=?", (todo_id, record_id)).fetchone()
            if row is None:
                raise NotFound("待办不存在")
            if row["status"] != "invalidated":
                raise Conflict("仅失效待办需要重新确认")
            connection.execute(
                "UPDATE todos SET status='open',requires_reconfirm=0,updated_at=? WHERE id=?", (now, todo_id)
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "todo_reconfirmed", actor_id, record["version"],
                 self._dump({"todo_id": todo_id, "note": note}), now),
            )
            result = connection.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(result)

    def reopen_batch_todos(self, record_id: int, batch_no: str, actor_id: str) -> int:
        """恢复原批次时把该批次失效待办重新置为可续做。"""
        now = _now()
        with self._connect() as connection:
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFound("记录不存在")
            cursor = connection.execute(
                "UPDATE todos SET status='open',requires_reconfirm=0,updated_at=? "
                "WHERE record_id=? AND status='invalidated' AND (batch_no=? OR batch_no='')",
                (now, record_id, batch_no),
            )
            reopened = cursor.rowcount
            if reopened:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "batch_todos_reopened", actor_id, record["version"],
                     self._dump({"batch_no": batch_no, "reopened": reopened}), now),
                )
        return reopened

    # ---- 越权/冲突写入隔离 ----

    def quarantine_write(self, actor_id: str, actor_role: str, actor_org: str, action: str, reason: str,
                         payload: Dict[str, Any], record_id: int = None, batch_no: str = "") -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO quarantined_writes(record_id,batch_no,actor_id,actor_role,actor_org,action,reason,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, batch_no, actor_id, actor_role, actor_org, action, reason,
                 self._dump(payload or {}), now),
            )
            if record_id is not None:
                version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if version_row is not None:
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record_id, "quarantined", actor_id, version_row["version"],
                         self._dump({"action": action, "reason": reason, "quarantine_id": cursor.lastrowid}), now),
                    )
            return int(cursor.lastrowid)

    def list_quarantine(self, limit: int = 100, batch_no: str = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if batch_no:
                rows = connection.execute(
                    "SELECT * FROM quarantined_writes WHERE batch_no=? ORDER BY id DESC LIMIT ?", (batch_no, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM quarantined_writes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    # ---- 迁移批次与机构归属回填 ----

    def register_migration_batch(self, batch_no: str, from_org: str, actor_id: str, to_org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO migration_batches(batch_no,from_org,to_org,record_count,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_no, from_org, to_org, 0, actor_id, now),
                )
                row = connection.execute("SELECT * FROM migration_batches WHERE batch_no=?", (batch_no,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("迁移批次已存在") from exc
        return dict(row)

    def backfill_ownership(self, batch_no: str, from_org: str, actor_id: str) -> Dict[str, Any]:
        """旧数据缺少机构归属时，按迁移批次回填原建档机构。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, version, owning_org, migration_batch FROM records WHERE migration_batch=? ORDER BY id",
                (batch_no,),
            ).fetchall()
            if not rows:
                connection.rollback()
                raise NotFound("批次下没有可回填的记录")
            updated = 0
            for row in rows:
                if row["owning_org"]:
                    continue
                connection.execute(
                    "UPDATE records SET owning_org=?,updated_by=?,updated_at=? WHERE id=?",
                    (from_org, actor_id, now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "ownership_backfilled", actor_id, row["version"],
                     self._dump({"batch_no": batch_no, "owning_org": from_org}), now),
                )
                updated += 1
            batch_row = connection.execute("SELECT * FROM migration_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if batch_row is None:
                connection.execute(
                    "INSERT INTO migration_batches(batch_no,from_org,to_org,record_count,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_no, from_org, "", len(rows), actor_id, now),
                )
            else:
                connection.execute(
                    "UPDATE migration_batches SET record_count=? WHERE batch_no=?", (len(rows), batch_no)
                )
            connection.commit()
        return {"batch_no": batch_no, "from_org": from_org, "matched": len(rows), "updated": updated}

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), self._dump(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
