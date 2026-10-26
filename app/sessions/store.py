import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Session:
    session_id: str
    customer_id: str
    # Claude conversation, append-only (required for thinking blocks to stay valid).
    messages: list[dict] = field(default_factory=list)
    # tool_results owed for the previous turn's last tool calls (e.g. submit_response);
    # they open the next user message so the transcript stays valid and append-only.
    pending_tool_results: list[dict] = field(default_factory=list)
    card_last4: str | None = None
    candidates: list[str] = field(default_factory=list)
    last_status: str | None = None


class SessionStore(Protocol):
    def get(self, session_id: str) -> Session | None: ...

    def save(self, session: Session) -> None: ...


def new_session(customer_id: str) -> Session:
    return Session(session_id=f"S-{uuid.uuid4().hex}", customer_id=customer_id)


class InMemorySessionStore:
    def __init__(self):
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            raw = self._data.get(session_id)
        return Session(**json.loads(raw)) if raw else None

    def save(self, session: Session) -> None:
        with self._lock:
            self._data[session.session_id] = json.dumps(asdict(session), ensure_ascii=False)


class SQLiteSessionStore:
    """Durable local store; DynamoDB (keyed by session_id, with TTL) plays this role in AWS."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self._conn.commit()

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return Session(**json.loads(row[0])) if row else None

    def save(self, session: Session) -> None:
        body = json.dumps(asdict(session), ensure_ascii=False)
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO sessions (id, body) VALUES (?, ?)", (session.session_id, body))
            self._conn.commit()
