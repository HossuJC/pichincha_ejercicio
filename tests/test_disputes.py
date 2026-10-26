import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from app.clock import FixedClock
from app.tools.disputes import FAILED, PENDING, REGISTERED, DisputeRepository, IdempotentDisputeService, LegacyDisputeSystemMock

CLOCK = FixedClock(date(2026, 10, 26))


def _service(legacy, timeout_s=0.5, max_attempts=2):
    return IdempotentDisputeService(DisputeRepository([]), legacy, CLOCK, timeout_s, max_attempts)


def test_registers_and_returns_legacy_id():
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.01)
    record, created = _service(legacy).create("C-1", "T-1", "no reconocido", 10.0)
    assert created and record.status == REGISTERED and record.legacy_dispute_id.startswith("D-")


def test_second_request_returns_existing_without_calling_legacy():
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.01)
    service = _service(legacy)
    first, _ = service.create("C-1", "T-1", "x", 10.0)
    second, created = service.create("C-1", "T-1", "x", 10.0)
    assert not created and second.reference == first.reference
    assert len(legacy.calls) == 1


def test_concurrent_duplicates_call_legacy_once():
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.2)
    service = _service(legacy)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: service.create("C-1", "T-1", "x", 10.0), range(10)))
    assert len({r.reference for r, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len(legacy.calls) == 1


def test_slow_legacy_returns_pending_then_completes_exactly_once():
    release = threading.Event()
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.0)
    original = legacy.create_dispute

    def slow(*args):
        release.wait(5)
        return original(*args)

    legacy.create_dispute = slow
    service = _service(legacy, timeout_s=0.1)

    record, created = service.create("C-1", "T-1", "x", 10.0)
    assert created and record.status == PENDING and record.legacy_dispute_id is None

    again, created_again = service.create("C-1", "T-1", "x", 10.0)  # customer retries meanwhile
    assert not created_again and again.reference == record.reference

    release.set()
    assert record.done.wait(2)
    assert record.status == REGISTERED and record.legacy_dispute_id
    assert len(legacy.calls) == 1


def test_legacy_error_is_retried_then_marked_failed():
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.0, failure_probability=1.0)
    record, _ = _service(legacy, max_attempts=2).create("C-1", "T-1", "x", 10.0)
    assert record.done.wait(2)
    assert record.status == FAILED and len(legacy.calls) == 2


def test_failed_record_can_be_retried_later():
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.0, failure_probability=1.0)
    service = _service(legacy, max_attempts=1)
    service.create("C-1", "T-1", "x", 10.0)[0].done.wait(2)
    legacy._failure_probability = 0.0
    record, created = service.create("C-1", "T-1", "x", 10.0)
    assert created and record.status == REGISTERED


def test_random_latency_mock_is_sometimes_slower_than_5s():
    legacy = LegacyDisputeSystemMock.with_random_latency(slow_probability=1.0, slow_range=(6, 8), fast_max=1.0)
    assert 6 <= legacy._latency() <= 8


def test_slow_call_does_not_block_beyond_timeout():
    legacy = LegacyDisputeSystemMock(latency=lambda: 1.0)
    start = time.monotonic()
    _service(legacy, timeout_s=0.1).create("C-1", "T-1", "x", 10.0)
    assert time.monotonic() - start < 0.5
