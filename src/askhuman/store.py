"""SQLite request state and transactional outbox, suitable for one self-hosted workspace."""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import RoutingConfig
from .models import Answer, AnswerInput, Question, Request


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: Path, *, default_config: RoutingConfig | None = None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL,
                    expires REAL NOT NULL, created REAL NOT NULL,
                    idempotency_key TEXT UNIQUE, fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0, error TEXT,
                    UNIQUE(request_id, channel)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, request_id TEXT, event TEXT NOT NULL,
                    actor TEXT NOT NULL, timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_status ON requests(status, created);
                CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(status, due);
                CREATE TABLE IF NOT EXISTS message_bindings (
                    channel TEXT NOT NULL, external_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    PRIMARY KEY(channel, external_id)
                );
                CREATE TABLE IF NOT EXISTS receiver_cursors (
                    channel TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO config VALUES(1, ?)",
                ((default_config or RoutingConfig()).model_dump_json(),),
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, request_id, event, actor):
        db.execute(
            "INSERT INTO events(request_id,event,actor,timestamp) VALUES(?,?,?,?)",
            (request_id, event, actor, time.time()),
        )

    def config(self) -> RoutingConfig:
        with self.connection() as db:
            return RoutingConfig.model_validate_json(
                db.execute("SELECT body FROM config WHERE id=1").fetchone()[0]
            )

    def configure(self, config: RoutingConfig):
        with self.connection() as db:
            db.execute("UPDATE config SET body=? WHERE id=1", (config.model_dump_json(),))
            self.event(db, None, "configuration_updated", "admin")

    @staticmethod
    def expire(db):
        rows = db.execute(
            "SELECT * FROM requests WHERE status='pending' AND expires<=?", (time.time(),)
        ).fetchall()
        for row in rows:
            body = json.loads(row["body"])
            body["status"] = "expired"
            db.execute(
                "UPDATE requests SET status='expired',body=? WHERE id=?",
                (json.dumps(body), row["id"]),
            )
            Store.event(db, row["id"], "expired", "system")

    def create(self, question: Question, key: str | None) -> Request:
        fingerprint = hashlib.sha256(question.model_dump_json().encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            if key:
                row = db.execute(
                    "SELECT * FROM requests WHERE idempotency_key=?", (key,)
                ).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise Conflict("Idempotency key was already used for a different question")
                    return self.read_row(db, row)
            config = RoutingConfig.model_validate_json(
                db.execute("SELECT body FROM config WHERE id=1").fetchone()[0]
            )
            targets = config.targets(question.recipient)
            now = datetime.now(UTC)
            request = Request(
                **question.model_dump(),
                id=str(uuid.uuid4()),
                status="notified" if question.kind == "notification" else "pending",
                created_at=now,
                expires_at=now + timedelta(seconds=question.timeout_seconds),
            )
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?)",
                (
                    request.id,
                    request.model_dump_json(),
                    request.status,
                    request.expires_at.timestamp(),
                    now.timestamp(),
                    key,
                    fingerprint,
                ),
            )
            web_ids = {c.id for c in config.channels if c.type == "web"}
            for channel in targets:
                db.execute(
                    "INSERT INTO deliveries(request_id,channel,status) VALUES(?,?,?)",
                    (request.id, channel, "delivered" if channel in web_ids else "pending"),
                )
            self.event(db, request.id, "created", "agent")
            return self.read_row(
                db, db.execute("SELECT * FROM requests WHERE id=?", (request.id,)).fetchone()
            )

    @staticmethod
    def read_row(db, row) -> Request:
        if not row:
            raise KeyError("Request not found")
        body = json.loads(row["body"])
        body["deliveries"] = [
            dict(r)
            for r in db.execute(
                "SELECT channel,status,attempts,error FROM deliveries WHERE request_id=?",
                (row["id"],),
            ).fetchall()
        ]
        return Request.model_validate(body)

    def get(self, request_id: str) -> Request:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            return self.read_row(
                db, db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            )

    def list(self, status: str | None = None, limit: int = 100, offset: int = 0) -> list[Request]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            rows = db.execute(
                "SELECT * FROM requests WHERE (? IS NULL OR status=?) "
                "ORDER BY created DESC LIMIT ? OFFSET ?",
                (status, status, limit, offset),
            ).fetchall()
            return [self.read_row(db, row) for row in rows]

    def answer(self, request_id: str, body: AnswerInput, source: str) -> Request:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            request = self.read_row(
                db, db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            )
            if request.status != "pending":
                raise Conflict(f"Request is {request.status}; it cannot accept an answer")
            if request.kind == "approval":
                if body.approved is None:
                    raise ValueError("Approval requires an explicit approved boolean")
                expected = "Approve" if body.approved else "Reject"
                if body.selected_option not in (None, expected):
                    raise ValueError("Selected option contradicts approved boolean")
                body = body.model_copy(
                    update={
                        "selected_option": expected,
                        "answer": body.answer or expected,
                    }
                )
            else:
                if body.approved is not None:
                    raise ValueError("Only approval requests accept an approved boolean")
                if request.options:
                    if body.selected_option not in request.options:
                        raise ValueError("Choose one of the supplied options")
                    body = body.model_copy(update={"answer": body.answer or body.selected_option})
                elif body.selected_option is not None:
                    raise ValueError("This request has no options")
                elif not body.answer:
                    raise ValueError("An answer is required")
            request.response = Answer(
                **body.model_dump(),
                request_id=request_id,
                timestamp=datetime.now(UTC),
                source=source,
            )
            request.status = "answered"
            db.execute(
                "UPDATE requests SET status=?,body=? WHERE id=?",
                (request.status, request.model_dump_json(), request_id),
            )
            self.event(db, request_id, "answered", source + ":" + body.respondent)
            return request

    def cancel(self, request_id: str) -> Request:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            request = self.read_row(
                db, db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            )
            if request.status == "cancelled":
                return request
            if request.status != "pending":
                raise Conflict(f"Cannot cancel a {request.status} request")
            request.status = "cancelled"
            db.execute(
                "UPDATE requests SET status=?,body=? WHERE id=?",
                (request.status, request.model_dump_json(), request_id),
            )
            self.event(db, request_id, "cancelled", "agent")
            return request

    def claim_delivery(self, request_id: str | None = None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            db.execute(
                "UPDATE deliveries SET status='skipped' WHERE status IN ('pending','sending') "
                "AND request_id IN (SELECT id FROM requests WHERE "
                "status NOT IN ('pending','notified') OR expires<=?)",
                (time.time(),),
            )
            row = db.execute(
                "SELECT * FROM deliveries WHERE status IN ('pending','sending') "
                "AND due<=? AND (? IS NULL OR request_id=?) ORDER BY id LIMIT 1",
                (time.time(), request_id, request_id),
            ).fetchone()
            if not row:
                return None
            if row["attempts"] >= 5:
                db.execute(
                    "UPDATE deliveries SET status='failed',error='Delivery attempts exhausted' "
                    "WHERE id=?",
                    (row["id"],),
                )
                return None
            db.execute(
                "UPDATE deliveries SET status='sending',attempts=attempts+1,due=? WHERE id=?",
                (time.time() + 60, row["id"]),
            )
            return dict(row) | {"attempts": row["attempts"] + 1}

    def finish_delivery(self, delivery: dict, error: str | None, *, permanent: bool = False):
        status = (
            "delivered"
            if not error
            else ("failed" if permanent or delivery["attempts"] >= 5 else "pending")
        )
        with self.connection() as db:
            db.execute(
                "UPDATE deliveries SET status=?,error=?,due=? WHERE id=? AND attempts=?",
                (
                    status,
                    error,
                    time.time() + min(300, 2 ** delivery["attempts"]),
                    delivery["id"],
                    delivery["attempts"],
                ),
            )
            self.event(db, delivery["request_id"], "delivery_" + status, delivery["channel"])

    def bind_message(self, channel: str, external_id: str, request_id: str):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO message_bindings VALUES(?,?,?)",
                (channel, external_id, request_id),
            )

    def bound_request(self, channel: str, external_id: str) -> Request | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT request_id FROM message_bindings WHERE channel=? AND external_id=?",
                (channel, external_id),
            ).fetchone()
        return self.get(row[0]) if row else None

    def cursor(self, channel: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM receiver_cursors WHERE channel=?", (channel,)
            ).fetchone()
            return row[0] if row else None

    def set_cursor(self, channel: str, value: str):
        with self.connection() as db:
            db.execute(
                "INSERT INTO receiver_cursors VALUES(?,?) ON CONFLICT(channel) "
                "DO UPDATE SET value=excluded.value",
                (channel, value),
            )

    def receiver_error(self, channel: str, error: str | None):
        with self.connection() as db:
            db.execute(
                "UPDATE deliveries SET error=? WHERE channel=? AND status='delivered' "
                "AND request_id IN (SELECT id FROM requests WHERE status='pending')",
                (error, channel),
            )

    def events(self, request_id: str):
        self.get(request_id)
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT event,actor,timestamp FROM events WHERE request_id=? ORDER BY id",
                    (request_id,),
                ).fetchall()
            ]
