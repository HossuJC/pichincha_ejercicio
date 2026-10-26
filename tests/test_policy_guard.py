from datetime import date

import pytest

from app.clock import FixedClock
from app.config import Settings
from app.policy.guard import PolicyGuard
from app.rag.loader import load_active_reglamento
from app.schemas import Decision
from app.tools.disputes import DisputeRepository
from app.tools.transactions import TransactionRepository

TODAY = date(2026, 10, 26)


def _txn(tid, day, amount=10.0, status="posted", customer="C-1"):
    return {"customer_id": customer, "transaction_id": tid, "date": day, "amount": amount,
            "currency": "USD", "merchant_id": "M-X", "status": status}


def _history(n, customer="C-1", day="2026-10-20"):
    return [{"customer_id": customer, "transaction_id": f"H-{i}", "amount": 1.0, "created_on": day,
             "legacy_dispute_id": f"D-{i}"} for i in range(n)]


@pytest.fixture
def reglamento():
    return load_active_reglamento(Settings().reglamento_dir, TODAY)


def _guard(reglamento, txns, history=None):
    return PolicyGuard(reglamento, TransactionRepository(txns), DisputeRepository(history or []), FixedClock(TODAY))


def test_thresholds_come_from_reglamento(reglamento):
    t = reglamento.thresholds
    assert (t.max_days_to_dispute, t.review_amount_usd, t.recent_disputes_window_days, t.max_recent_disputes) == (60, 500, 30, 3)


@pytest.mark.parametrize("day,expected", [("2026-08-27", None), ("2026-08-26", Decision.NO_PROCEDE)])
def test_60_day_window_boundary(reglamento, day, expected):
    # 2026-08-27 -> 60 days (allowed); 2026-08-26 -> 61 days (expired)
    assert _guard(reglamento, [_txn("T", day)]).evaluate("C-1", "T").required_outcome == expected


def test_pending_never_proceeds(reglamento):
    result = _guard(reglamento, [_txn("T", "2026-10-24", amount=900, status="pending")]).evaluate("C-1", "T")
    assert result.required_outcome == Decision.NO_PROCEDE
    assert result.violations[0].section == "2"


@pytest.mark.parametrize("amount,expected", [(500.00, None), (500.01, Decision.REVISION_ASESOR)])
def test_amount_threshold(reglamento, amount, expected):
    assert _guard(reglamento, [_txn("T", "2026-10-20", amount)]).evaluate("C-1", "T").required_outcome == expected


@pytest.mark.parametrize("previous,expected", [(3, None), (4, Decision.REVISION_ASESOR)])
def test_more_than_three_recent_disputes(reglamento, previous, expected):
    guard = _guard(reglamento, [_txn("T", "2026-10-20")], _history(previous))
    assert guard.evaluate("C-1", "T").required_outcome == expected


def test_old_disputes_outside_window_do_not_count(reglamento):
    guard = _guard(reglamento, [_txn("T", "2026-10-20")], _history(5, day="2026-09-01"))
    assert guard.evaluate("C-1", "T").allowed


def test_transaction_of_another_customer_is_not_found(reglamento):
    guard = _guard(reglamento, [_txn("T", "2026-10-20", customer="C-2")])
    assert guard.evaluate("C-1", "T").violations[0].rule == "transaccion_no_encontrada"


@pytest.mark.parametrize("cancelled_on,allowed", [(None, False), (date(2026, 10, 5), False), (date(2026, 9, 30), True)])
def test_subscription_requires_cancellation_before_charge(reglamento, cancelled_on, allowed):
    guard = _guard(reglamento, [_txn("T", "2026-10-05")])
    result = guard.evaluate("C-1", "T", merchant_category="suscripcion_streaming", cancellation_requested_on=cancelled_on)
    assert result.allowed is allowed


def test_provided_dataset_expectations():
    settings = Settings()
    reglamento = load_active_reglamento(settings.reglamento_dir, TODAY)
    guard = PolicyGuard(reglamento, TransactionRepository(), DisputeRepository(), FixedClock(TODAY))
    outcome = lambda tid, **kw: guard.evaluate("C-1001", tid, **kw).required_outcome  # noqa: E731
    assert outcome("T-02") is None  # eligible unless it is treated as a subscription
    assert outcome("T-02", merchant_category="suscripcion_streaming") == Decision.NO_PROCEDE
    assert outcome("T-03") == Decision.REVISION_ASESOR
    assert outcome("T-04") == Decision.NO_PROCEDE
    assert outcome("T-05") == Decision.NO_PROCEDE
    assert outcome("T-06") == Decision.NO_PROCEDE
