"""Durable intent to deliver one generated file through its original Host route.

This module has no network capability. Only trusted adapters can attach intents;
model parameters never select a recipient, workspace or bot.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from qunling.assets import AssetError
from qunling.scope import Scope
from qunling.tasks import TaskConflict, TaskStatus, TaskStore

PUBLIC_STATES = {"queued", "started", "sent", "unconfirmed", "cancelled", "failed"}


def validate_route(scope: Scope, route: dict) -> dict:
    if not isinstance(scope, Scope) or not isinstance(route, dict) or set(route) != {
        "instance_uuid", "workspace_uuid", "placement_generation",
    }:
        raise AssetError("delivery_scope", "无法确认后台交付所属的运行实例。")
    for key in ("instance_uuid", "workspace_uuid"):
        if not isinstance(route[key], str) or not 1 <= len(route[key]) <= 256 or any(ord(c) < 32 for c in route[key]):
            raise AssetError("delivery_scope", "后台交付的工作空间无效。")
    if type(route["placement_generation"]) is not int or route["placement_generation"] <= 0:
        raise AssetError("delivery_scope", "后台交付的运行代次无效。")
    expected = hashlib.sha256(json.dumps([route["instance_uuid"], route["workspace_uuid"]]).encode()).hexdigest()
    kind, _, target = scope.conversation_id.partition(":")
    if scope.workspace_id != expected or kind not in {"group", "person"} or not target.isascii() or not target.isdigit():
        raise AssetError("delivery_scope", "后台交付的会话与工作空间不匹配。")
    if not 1 <= len(target) <= 20 or int(target) == 0:
        raise AssetError("delivery_scope", "后台交付的会话编号无效。")
    return dict(route)


@dataclass(frozen=True)
class DeliveryClaim:
    id: str
    task_id: str
    scope: Scope
    request_key: str
    route: dict
    token: str


class Outbox:
    def __init__(self, tasks: TaskStore, *, clock=time.time):
        self.tasks, self.clock = tasks, clock
        with tasks._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ql_file_outbox (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope_key TEXT NOT NULL,
                scope_json TEXT NOT NULL, request_key TEXT NOT NULL, route_json TEXT NOT NULL,
                instance_uuid TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, expires REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                token TEXT, lease_until REAL, error_code TEXT, asset_ref TEXT,
                UNIQUE(task_id,request_key)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ql_file_outbox_work ON ql_file_outbox(instance_uuid,state,retry_at)")

    def attach(self, scope: Scope, task_id: str, request_key: str, route: dict) -> str:
        route = validate_route(scope, route)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 256:
            raise AssetError("delivery_scope", "后台交付的请求编号无效。")
        with self.tasks._db() as db:
            task = self.tasks._find(db, scope, task_id)
            if task["status"] in {TaskStatus.CANCELLED, TaskStatus.FAILED}:
                raise AssetError("delivery_unavailable", "任务已停止，不能安排自动交付。")
            previous = db.execute("SELECT * FROM ql_file_outbox WHERE task_id=? AND request_key=?",
                                  (task_id, request_key)).fetchone()
            if previous:
                if previous["scope_key"] != scope.key or json.loads(previous["route_json"]) != route:
                    raise AssetError("delivery_scope", "已登记的交付目标不能变更。")
                return "queued" if previous["state"] == "claimed" else previous["state"]
            if db.execute("SELECT COUNT(*) FROM ql_file_outbox").fetchone()[0] >= 2048:
                raise AssetError("delivery_limit", "后台交付记录已满，请稍后再试。")
            now = self.clock()
            db.execute("""INSERT INTO ql_file_outbox
                (id,task_id,scope_key,scope_json,request_key,route_json,instance_uuid,state,created,updated,expires)
                VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
                (uuid.uuid4().hex, task_id, scope.key, json.dumps(scope.as_dict()), request_key,
                 json.dumps(route), route["instance_uuid"], now, now, now + 86400))
            return "queued"

    def state(self, scope: Scope, task_id: str) -> str | None:
        with self.tasks._db() as db:
            self.tasks._find(db, scope, task_id)
            row = db.execute("SELECT state FROM ql_file_outbox WHERE task_id=? AND scope_key=? "
                             "ORDER BY created DESC,id DESC LIMIT 1", (task_id, scope.key)).fetchone()
            return ("queued" if row[0] == "claimed" else row[0]) if row else None

    def cancel(self, scope: Scope, task_id: str) -> None:
        with self.tasks._db() as db:
            self.tasks._find(db, scope, task_id)
            db.execute("UPDATE ql_file_outbox SET state=CASE WHEN state='started' THEN 'unconfirmed' "
                       "ELSE 'cancelled' END,token=NULL,lease_until=NULL,updated=?,error_code='cancelled' "
                       "WHERE task_id=? AND scope_key=? AND state IN ('queued','claimed','started')",
                       (self.clock(), task_id, scope.key))

    def claim(self, instance_uuid: str) -> DeliveryClaim | None:
        with self.tasks._db() as db:
            now = self.clock()
            # Lease loss before sending is recoverable; once sending starts the
            # transport may have accepted bytes and must never be replayed.
            db.execute("UPDATE ql_file_outbox SET state=CASE WHEN state='started' THEN 'unconfirmed' ELSE 'queued' END,"
                       "token=NULL,lease_until=NULL,updated=?,error_code='worker_interrupted' "
                       "WHERE instance_uuid=? AND state IN ('claimed','started') AND lease_until<=?",
                       (now, instance_uuid, now))
            db.execute("UPDATE ql_file_outbox SET state='failed',updated=?,error_code='expired' "
                       "WHERE instance_uuid=? AND state='queued' AND expires<=?", (now, instance_uuid, now))
            db.execute("UPDATE ql_file_outbox SET state='cancelled',updated=?,error_code='task_cancelled' "
                       "WHERE instance_uuid=? AND state='queued' AND task_id IN "
                       "(SELECT id FROM ql_tasks WHERE status='cancelled')", (now, instance_uuid))
            db.execute("UPDATE ql_file_outbox SET state='failed',updated=?,error_code='task_failed' "
                       "WHERE instance_uuid=? AND state='queued' AND task_id IN "
                       "(SELECT id FROM ql_tasks WHERE status='failed')", (now, instance_uuid))
            row = db.execute("SELECT o.* FROM ql_file_outbox o JOIN ql_tasks t ON t.id=o.task_id "
                             "WHERE o.instance_uuid=? AND o.state='queued' AND o.retry_at<=? "
                             "AND t.status='completed' ORDER BY o.created,o.id LIMIT 1", (instance_uuid, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE ql_file_outbox SET state='claimed',token=?,lease_until=?,updated=? WHERE id=?",
                       (token, now + 90, now, row["id"]))
            return DeliveryClaim(row["id"], row["task_id"], Scope(**json.loads(row["scope_json"])),
                                 row["request_key"], json.loads(row["route_json"]), token)

    def update(self, claim: DeliveryClaim, state: str, *, error_code=None, asset_ref=None, delay=0) -> bool:
        if state not in PUBLIC_STATES:
            raise ValueError("Invalid outbox state")
        with self.tasks._db() as db:
            now = self.clock()
            row = db.execute("SELECT o.state,t.status FROM ql_file_outbox o JOIN ql_tasks t ON t.id=o.task_id "
                             "WHERE o.id=? AND o.scope_key=? AND o.token=? AND o.lease_until>?",
                             (claim.id, claim.scope.key, claim.token, now)).fetchone()
            if not row or row[0] not in {"claimed", "started"}:
                return False
            if state == "started" and row[1] != TaskStatus.COMPLETED:
                return False
            keep = state == "started"
            db.execute("UPDATE ql_file_outbox SET state=?,updated=?,retry_at=?,error_code=?,asset_ref=COALESCE(?,asset_ref),"
                       "token=?,lease_until=? WHERE id=?",
                       (state, now, now + delay, error_code, asset_ref, claim.token if keep else None,
                        now + 90 if keep else None, claim.id))
            return True

    def active(self, claim: DeliveryClaim) -> bool:
        with self.tasks._db() as db:
            return db.execute("SELECT 1 FROM ql_file_outbox o JOIN ql_tasks t ON t.id=o.task_id "
                              "WHERE o.id=? AND o.token=? AND o.state='started' "
                              "AND o.lease_until>? AND t.status='completed'",
                              (claim.id, claim.token, self.clock())).fetchone() is not None

    def reconcile_sent(self, instance_uuid: str, assets) -> None:
        """Only observe existing confirmed receipts; never retry an upload."""
        with self.tasks._db() as db:
            rows = db.execute("SELECT id,scope_json,request_key,asset_ref FROM ql_file_outbox "
                              "WHERE instance_uuid=? AND state='unconfirmed' AND asset_ref IS NOT NULL "
                              "ORDER BY updated DESC LIMIT 128", (instance_uuid,)).fetchall()
        for row in rows:
            try:
                sent = assets.delivery(Scope(**json.loads(row["scope_json"])),
                                       row["request_key"], row["asset_ref"]) == "sent"
            except AssetError:
                continue
            if sent:
                with self.tasks._db() as db:
                    db.execute("UPDATE ql_file_outbox SET state='sent',updated=?,error_code=NULL "
                               "WHERE id=? AND state='unconfirmed'", (self.clock(), row["id"]))

    def prune_final(self, scope: Scope, task_id: str, *, older_than: float, remove_files) -> bool:
        """Fence concurrent attach while removing an expired task and its bytes."""
        with self.tasks._db() as db:
            try:
                task = self.tasks._find(db, scope, task_id)
            except TaskConflict:
                return False
            if task["status"] not in {"completed", "failed", "cancelled"} or task["updated_at"] >= older_than:
                return False
            now = self.clock()
            db.execute("UPDATE ql_file_outbox SET state='failed',updated=?,error_code='expired' "
                       "WHERE task_id=? AND state='queued' AND expires<=?", (now, task_id, now))
            db.execute("UPDATE ql_file_outbox SET state=CASE WHEN state='started' THEN 'unconfirmed' ELSE 'failed' END,"
                       "token=NULL,lease_until=NULL,updated=?,error_code='worker_interrupted' "
                       "WHERE task_id=? AND state IN ('claimed','started') AND lease_until<=?", (now, task_id, now))
            active = db.execute("SELECT 1 FROM ql_file_outbox WHERE task_id=? AND scope_key=? "
                                "AND state IN ('queued','claimed','started')", (task_id, scope.key)).fetchone()
            if active:
                return False
            remove_files()
            db.execute("DELETE FROM ql_file_outbox WHERE task_id=? AND scope_key=?", (task_id, scope.key))
            db.execute("DELETE FROM ql_task_receipts WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM ql_task_storage WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM ql_tasks WHERE id=?", (task_id,))
            return True
