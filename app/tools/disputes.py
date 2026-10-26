"""Dispute registration against the slow legacy system, made idempotent.

The legacy system is not idempotent: calling it twice for the same transaction
creates two disputes (the duplicates Operations reported). We keep our own record
keyed by transaction_id, reserve it *before* calling the legacy, and never call
the legacy again while an attempt is in flight. A call that exceeds the timeout
is left running; its result completes the record in the background.
"""

import itertools
import logging
import random
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable

from app.clock import Clock
from app.tools.mock_data import SEED_DISPUTES

log = logging.getLogger(__name__)

PENDING = "pending_registration"
REGISTERED = "registered"
FAILED = "failed"


class LegacyDisputeSystemMock:
    """Simulated legacy service: sometimes takes more than 5 seconds."""

    def __init__(
        self,
        latency: Callable[[], float],
        failure_probability: float = 0.0,
        rng: random.Random | None = None,
    ):
        self._latency = latency
        self._failure_probability = failure_probability
        self._rng = rng or random.Random()
        self._ids = itertools.count(10001)
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    @classmethod
    def with_random_latency(cls, slow_probability: float, slow_range: tuple[float, float], fast_max: float, **kw):
        rng = random.Random()

        def latency() -> float:
            if rng.random() < slow_probability:
                return rng.uniform(*slow_range)
            return rng.uniform(0.1, fast_max)

        return cls(latency=latency, **kw)

    def create_dispute(self, transaction_id: str, reason: str, amount: float) -> dict:
        with self._lock:
            self.calls.append({"transaction_id": transaction_id, "reason": reason, "amount": amount})
        time.sleep(self._latency())
        if self._rng.random() < self._failure_probability:
            raise ConnectionError("legacy dispute system error")
        with self._lock:
            dispute_id = f"D-{next(self._ids)}"
        return {"dispute_id": dispute_id, "status": "created"}


@dataclass
class DisputeRecord:
    reference: str
    customer_id: str
    transaction_id: str
    amount: float
    reason: str
    created_on: date
    status: str = PENDING
    legacy_dispute_id: str | None = None
    attempts: int = 0
    done: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("done")
        data["created_on"] = self.created_on.isoformat()
        return data


class DisputeRepository:
    """Our own dispute ledger (DynamoDB in AWS, conditional put on transaction_id)."""

    def __init__(self, seed: list[dict] | None = None):
        self._lock = threading.RLock()
        self._by_txn: dict[str, DisputeRecord] = {}
        for row in SEED_DISPUTES if seed is None else seed:
            record = DisputeRecord(
                reference=f"R-{uuid.uuid4().hex[:8].upper()}",
                customer_id=row["customer_id"],
                transaction_id=row["transaction_id"],
                amount=row["amount"],
                reason="histórico",
                created_on=date.fromisoformat(row["created_on"]),
                status=REGISTERED,
                legacy_dispute_id=row["legacy_dispute_id"],
            )
            record.done.set()
            self._by_txn[record.transaction_id] = record

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def find_by_transaction(self, transaction_id: str) -> DisputeRecord | None:
        with self._lock:
            return self._by_txn.get(transaction_id)

    def put(self, record: DisputeRecord) -> None:
        with self._lock:
            self._by_txn[record.transaction_id] = record

    def recent_for_customer(self, customer_id: str, since: date) -> list[DisputeRecord]:
        with self._lock:
            return [
                r for r in self._by_txn.values()
                if r.customer_id == customer_id and r.created_on >= since and r.status != FAILED
            ]


class IdempotentDisputeService:
    def __init__(
        self,
        repository: DisputeRepository,
        legacy: LegacyDisputeSystemMock,
        clock: Clock,
        timeout_s: float,
        max_attempts: int = 2,
    ):
        self._repo = repository
        self._legacy = legacy
        self._clock = clock
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="legacy-dispute")

    def create(self, customer_id: str, transaction_id: str, reason: str, amount: float) -> tuple[DisputeRecord, bool]:
        """Returns (record, created_now). Never calls the legacy twice for one transaction."""
        with self._repo.lock:
            existing = self._repo.find_by_transaction(transaction_id)
            if existing and existing.status != FAILED:
                return existing, False
            record = DisputeRecord(
                reference=f"R-{uuid.uuid4().hex[:8].upper()}",
                customer_id=customer_id,
                transaction_id=transaction_id,
                amount=amount,
                reason=reason,
                created_on=self._clock.today(),
            )
            self._repo.put(record)

        self._submit(record)
        if not record.done.wait(self._timeout_s):
            log.warning("legacy dispute system slow for %s; continuing in background (%s)", transaction_id, record.reference)
        return record, True

    def _submit(self, record: DisputeRecord) -> None:
        record.attempts += 1
        future = self._pool.submit(self._legacy.create_dispute, record.transaction_id, record.reason, record.amount)
        future.add_done_callback(lambda f: self._on_done(record, f))

    def _on_done(self, record: DisputeRecord, future: Future) -> None:
        exc = future.exception()
        if exc is None:
            with self._repo.lock:
                record.legacy_dispute_id = future.result()["dispute_id"]
                record.status = REGISTERED
            record.done.set()
            log.info("dispute %s registered as %s", record.reference, record.legacy_dispute_id)
            return
        # The legacy answered with an error, so nothing was created: retrying is safe.
        if record.attempts < self._max_attempts:
            log.warning("legacy error for %s (%r), retrying", record.reference, exc)
            self._submit(record)
            return
        with self._repo.lock:
            record.status = FAILED
        record.done.set()
        log.error("dispute %s failed after %d attempts", record.reference, record.attempts)
