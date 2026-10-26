"""PII masking, injection screening, merchant service, RAG loader and session stores."""

from datetime import date

import pytest

from app.config import Settings
from app.rag.loader import load_active_reglamento
from app.rag.retriever import BM25Retriever
from app.security.injection import looks_like_injection
from app.security.pii import mask_pan
from app.sessions.store import InMemorySessionStore, SQLiteSessionStore, new_session
from app.tools.merchants import MerchantProviderMock, MerchantService
from app.tools.mock_data import MERCHANTS


@pytest.mark.parametrize("text", [
    "mi tarjeta es 4532 1234 5678 9012",
    "tarjeta 4532-1234-5678-9012",
    "4532123456789012",
])
def test_pan_is_masked_and_last4_kept(text):
    masked, last4 = mask_pan(text)
    assert "4532" not in masked and last4 == "9012"


def test_dates_and_amounts_are_not_masked():
    text = "cobro de 15.99 el 2026-10-02, referencia 12345"
    assert mask_pan(text) == (text, None)


def test_provider_injection_is_flagged():
    assert looks_like_injection(MERCHANTS["M-ELECTRO"]["description"])
    assert not looks_like_injection(MERCHANTS["M-NETFLIX"]["description"])
    assert not looks_like_injection(MERCHANTS["M-GAMES"]["description"])


def test_merchant_description_is_wrapped_as_untrusted():
    service = MerchantService(MerchantProviderMock(latency=lambda: 0), timeout_s=1, cache_ttl_s=60)
    info = service.get_merchant_info("M-ELECTRO")
    assert info["description"]["injection_suspected"] is True
    assert "untrusted_provider_text" in info["description"]


def test_unknown_merchant_degrades():
    service = MerchantService(MerchantProviderMock(latency=lambda: 0), timeout_s=1, cache_ttl_s=60)
    assert service.get_merchant_info("M-SUPER")["available"] is False


def test_provider_timeout_degrades_and_success_is_cached():
    calls = []
    provider = MerchantProviderMock(latency=lambda: calls.append(1) or (0.5 if len(calls) == 1 else 0))
    service = MerchantService(provider, timeout_s=0.1, cache_ttl_s=60)
    assert service.get_merchant_info("M-NETFLIX")["available"] is False
    assert service.get_merchant_info("M-NETFLIX")["available"] is True
    service.get_merchant_info("M-NETFLIX")
    assert len(calls) == 2


def test_provider_failure_degrades():
    service = MerchantService(MerchantProviderMock(latency=lambda: 0, failure_probability=1.0), timeout_s=1, cache_ttl_s=60)
    assert service.get_merchant_info("M-NETFLIX")["available"] is False


def test_active_reglamento_is_latest_in_force(tmp_path):
    source = (Settings().reglamento_dir / "reglamento_v2026-10.md").read_text(encoding="utf-8")
    (tmp_path / "reglamento_v2026-10.md").write_text(source, encoding="utf-8")
    future = source.replace('version: "2026-10"', 'version: "2027-01"').replace("2026-10-01", "2027-01-01") \
                   .replace("review_amount_usd: 500", "review_amount_usd: 300")
    (tmp_path / "reglamento_v2027-01.md").write_text(future, encoding="utf-8")

    assert load_active_reglamento(tmp_path, date(2026, 10, 26)).version == "2026-10"
    newer = load_active_reglamento(tmp_path, date(2027, 1, 2))
    assert newer.version == "2027-01" and newer.thresholds.review_amount_usd == 300


def test_retriever_finds_relevant_sections():
    retriever = BM25Retriever(load_active_reglamento(Settings().reglamento_dir, date(2026, 10, 26)))
    assert retriever.search("cargo recurrente suscripción cancelación")[0].section == "5"
    assert retriever.search("plazo días para reclamar")[0].section == "1"
    assert retriever.search("transacción pendiente")[0].section == "2"
    assert retriever.search("xyz")  # never empty


@pytest.mark.parametrize("store_factory", [lambda p: InMemorySessionStore(), lambda p: SQLiteSessionStore(p / "s.db")])
def test_session_round_trip(tmp_path, store_factory):
    store = store_factory(tmp_path)
    session = new_session("C-1001")
    session.messages.append({"role": "user", "content": [{"type": "text", "text": "hola"}]})
    session.pending_tool_results = [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]
    store.save(session)
    loaded = store.get(session.session_id)
    assert loaded == session
