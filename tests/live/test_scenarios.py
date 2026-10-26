"""End-to-end scenarios through the configured LLM.

Real Claude (costs tokens; ANTHROPIC_API_KEY in the environment or in .env):
    uv run pytest tests/live -m live -v
Any other client via APP_LLM_PROVIDER=custom APP_LLM_FACTORY=module:factory (e.g. a local mock).
"""

import json
import logging
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from app.container import build_container
from app.schemas import AssistRequest
from app.tools.disputes import LegacyDisputeSystemMock
from app.tools.merchants import MerchantProviderMock

SCENARIOS = yaml.safe_load((Path(__file__).parent / "scenarios" / "scenarios.yaml").read_text(encoding="utf-8"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(Settings().llm_provider == "anthropic" and Settings().anthropic_api_key is None,
                       reason="ANTHROPIC_API_KEY no configurada (entorno o .env)"),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scenario(scenario, caplog):
    caplog.set_level(logging.INFO)
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.05)
    container = build_container(Settings(), legacy=legacy,
                                merchant_provider=MerchantProviderMock(latency=lambda: 0.0))
    session_id = None
    for turn in scenario["turns"]:
        response = container.orchestrator.handle(
            AssistRequest(customer_id=scenario["customer_id"], session_id=session_id, message=turn["message"])
        )
        session_id = response.session_id
        print(f"\n[{scenario['name']}] {response.status}: {response.message}")
        # The outage fallback also answers revision_asesor; it must never count as a pass.
        assert "LLM unavailable" not in caplog.text, f"El modelo no respondió: {caplog.text[-500:]}"
        assert response.status in turn["expect_status"], response.model_dump_json(indent=2)
        assert response.message.strip()
        if "expect_candidates" in turn:
            assert sorted(c.transaction_id for c in response.candidates) == turn["expect_candidates"]
        if turn.get("expect_sections"):
            assert set(turn["expect_sections"]) <= {p.section for p in response.policy_refs}
        if turn.get("expect_case"):
            assert response.case_id
        if response.status in ("procede", "pending_registration"):
            break

    assert len(legacy.calls) == scenario["expect_legacy_calls"]
    session = container.orchestrator._sessions.get(session_id)
    assert "4532 1234" not in json.dumps(session.messages) and "4532 1234" not in caplog.text
