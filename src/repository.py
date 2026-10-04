"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


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
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    batch_id TEXT NOT NULL,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL,
                    state TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    initiated_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    org TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantined_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_org TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    input TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS migration_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    org TEXT NOT NULL,
                    applied_count INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_open ON transfers(record_id) WHERE state='initiated';
                CREATE INDEX IF NOT EXISTS idx_transfers_record ON transfers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_todos_record ON todos(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_quarantine_record ON quarantined_writes(record_id, id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "created_by_org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN created_by_org TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _transfer_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,created_by_org) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now, org),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def _apply_mutation(self, connection: sqlite3.Connection, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> int:
        now = _now()
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
        )
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return version

    def _invalidate_todos(self, connection: sqlite3.Connection, record_id: int, org: str) -> List[int]:
        rows = connection.execute("SELECT id FROM todos WHERE record_id=? AND org=? AND status='open'", (record_id, org)).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            connection.execute(
                "UPDATE todos SET status='invalidated', updated_at=? WHERE record_id=? AND org=? AND status='open'",
                (_now(), record_id, org),
            )
        return ids

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._apply_mutation(connection, record_id, expected_version, state, payload, actor_id, action, details)
            connection.commit()
        return self.get(record_id)

    def initiate_transfer(self, record: Dict[str, Any], expected_version: int, payload: Dict[str, Any], actor_id: str, transfer: Dict[str, Any], details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            open_row = connection.execute("SELECT id FROM transfers WHERE record_id=? AND state='initiated'", (record["id"],)).fetchone()
            if open_row is not None:
                raise Conflict("已存在进行中的交接")
            details["invalidated_todos"] = self._invalidate_todos(connection, record["id"], transfer["from_org"])
            self._apply_mutation(connection, record["id"], expected_version, record["state"], payload, actor_id, "transfer_initiate", details)
            connection.execute(
                "INSERT INTO transfers(record_id,batch_id,from_org,to_org,state,snapshot,initiated_by,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["id"], transfer["batch_id"], transfer["from_org"], transfer["to_org"], "initiated", json.dumps(record["payload"], ensure_ascii=False, sort_keys=True), actor_id, 1, now, now),
            )
            connection.commit()
        return self.get(record["id"])

    def confirm_transfer(self, record: Dict[str, Any], expected_version: int, payload: Dict[str, Any], actor_id: str, transfer_id: int, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM transfers WHERE id=? AND record_id=?", (transfer_id, record["id"])).fetchone()
            if row is None or row["state"] != "initiated":
                raise Conflict("交接不存在或已处理")
            details["invalidated_todos"] = self._invalidate_todos(connection, record["id"], row["from_org"])
            self._apply_mutation(connection, record["id"], expected_version, record["state"], payload, actor_id, "transfer_confirm", details)
            connection.execute(
                "UPDATE transfers SET state='confirmed', confirmed_by=?, version=?, updated_at=? WHERE id=?",
                (actor_id, int(row["version"]) + 1, now, transfer_id),
            )
            connection.commit()
        return self.get(record["id"])

    def restore_transfer(self, record: Dict[str, Any], expected_version: int, payload: Dict[str, Any], actor_id: str, transfer_id: int, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM transfers WHERE id=? AND record_id=?", (transfer_id, record["id"])).fetchone()
            if row is None or row["state"] != "failed":
                raise Conflict("仅失败的交接批次可恢复")
            open_row = connection.execute("SELECT id FROM transfers WHERE record_id=? AND state='initiated'", (record["id"],)).fetchone()
            if open_row is not None:
                raise Conflict("已存在进行中的交接")
            details["invalidated_todos"] = self._invalidate_todos(connection, record["id"], row["from_org"])
            self._apply_mutation(connection, record["id"], expected_version, record["state"], payload, actor_id, "transfer_restore", details)
            connection.execute(
                "UPDATE transfers SET state='initiated', version=?, updated_at=? WHERE id=?",
                (int(row["version"]) + 1, now, transfer_id),
            )
            connection.commit()
        return self.get(record["id"])

    def withdraw_consent(self, record: Dict[str, Any], expected_version: int, payload: Dict[str, Any], actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transfer = connection.execute("SELECT * FROM transfers WHERE record_id=? AND state='initiated'", (record["id"],)).fetchone()
            if transfer is not None:
                payload["transfer_state"] = "failed"
                details["failed_batch"] = transfer["batch_id"]
                details["invalidated_todos"] = self._invalidate_todos(connection, record["id"], transfer["from_org"])
                connection.execute(
                    "UPDATE transfers SET state='failed', version=?, updated_at=? WHERE id=?",
                    (int(transfer["version"]) + 1, now, transfer["id"]),
                )
            else:
                details["failed_batch"] = None
                details["invalidated_todos"] = []
            self._apply_mutation(connection, record["id"], expected_version, record["state"], payload, actor_id, "withdraw_consent", details)
            connection.commit()
        return self.get(record["id"])

    def get_open_transfer(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE record_id=? AND state='initiated'", (record_id,)).fetchone()
        return self._transfer_row(row) if row is not None else None

    def get_latest_transfer(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()
        return self._transfer_row(row) if row is not None else None

    def list_transfers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM transfers WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._transfer_row(row) for row in rows]

    def todo_create(self, record: Dict[str, Any], expected_version: int, title: str, org: str, actor_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO todos(record_id,org,title,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (record["id"], org, title, "open", actor_id, now, now),
            )
            details["todo_id"] = int(cursor.lastrowid)
            self._apply_mutation(connection, record["id"], expected_version, record["state"], record["payload"], actor_id, "todo_create", details)
            connection.commit()
        return self.get(record["id"])

    def todo_update(self, record: Dict[str, Any], expected_version: int, todo_id: int, status: str, actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        expected_current = {"todo_complete": "open", "todo_reconfirm": "invalidated"}[action]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM todos WHERE id=? AND record_id=?", (todo_id, record["id"])).fetchone()
            if row is None:
                raise NotFound("待办不存在")
            if row["status"] != expected_current:
                raise Conflict("待办状态已变化，请刷新后重试")
            connection.execute("UPDATE todos SET status=?, updated_at=? WHERE id=?", (status, now, todo_id))
            details["todo_id"] = todo_id
            details["todo_status"] = status
            self._apply_mutation(connection, record["id"], expected_version, record["state"], record["payload"], actor_id, action, details)
            connection.commit()
        return self.get(record["id"])

    def get_todo(self, record_id: int, todo_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM todos WHERE id=? AND record_id=?", (todo_id, record_id)).fetchone()
        if row is None:
            raise NotFound("待办不存在")
        return dict(row)

    def list_todos(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM todos WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def quarantine_write(self, record_id: int, actor_id: str, actor_org: str, action: str, reason: str, data: Dict[str, Any]) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            cursor = connection.execute(
                "INSERT INTO quarantined_writes(record_id,action,actor_id,actor_org,reason,input,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, action, actor_id, actor_org, reason, json.dumps(data, ensure_ascii=False, sort_keys=True), now),
            )
            quarantine_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "write_quarantined", actor_id, int(row["version"]), json.dumps({"quarantine_id": quarantine_id, "blocked_action": action, "reason": reason}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return quarantine_id

    def list_quarantine(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM quarantined_writes WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item["input"])
            result.append(item)
        return result

    def backfill_owner_org(self, batch_id: str, org: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        applied = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO migration_batches(batch_id,org,applied_count,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (batch_id, org, 0, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("迁移批次已存在") from exc
            rows = connection.execute("SELECT * FROM records").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if payload.get("owner_org"):
                    continue
                source = row["created_by_org"] or org
                payload["owner_org"] = source
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE records SET version=?, payload=?, updated_by=?, updated_at=? WHERE id=?",
                    (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "org_backfilled", actor_id, version, json.dumps({"batch_id": batch_id, "owner_org": source}, ensure_ascii=False, sort_keys=True), now),
                )
                applied += 1
            connection.execute("UPDATE migration_batches SET applied_count=? WHERE batch_id=?", (applied, batch_id))
            connection.commit()
        return {"batch_id": batch_id, "org": org, "applied": applied}

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
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
