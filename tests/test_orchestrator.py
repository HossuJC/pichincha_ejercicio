import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMError, LLMResponse, ScriptedFakeLLM, text, tool_use
from app.main import create_app
from app.tools.disputes import LegacyDisputeSystemMock

NETFLIX_MSG = "No reconozco un cobro de Netflix de principios de octubre, mi tarjeta es 4532 1234 5678 9012"


def step(*blocks, stop="tool_use") -> LLMResponse:
    return LLMResponse(content=list(blocks), stop_reason=stop)


def submit(status, message="ok", transaction_id=None, candidates=(), sections=(), id=None) -> LLMResponse:
    return step(tool_use("submit_response", {
        "status": status, "message": message, "transaction_id": transaction_id,
        "candidate_transaction_ids": list(candidates), "policy_sections": list(sections), "next_steps": [],
    }, id=id))


def last_tool_results(messages) -> list[dict]:
    return [b for b in messages[-1]["content"] if b.get("type") == "tool_result"]


def results_json(messages) -> list[dict]:
    return [json.loads(b["content"]) for b in last_tool_results(messages) if not b.get("is_error")]


@pytest.fixture
def client_for(make_container):
    def _client(llm, **kw):
        container = make_container(llm, **kw)
        return TestClient(create_app(container)), container

    return _client


def post(client, message, customer="C-1001", session_id=None):
    body = {"customer_id": customer, "message": message}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/api/v1/disputes/assist", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_netflix_ambiguity_then_clarification_then_dispute(client_for):
    seen: dict = {}

    def pick_candidates(messages):
        rows = results_json(messages)[0]["transactions"]
        seen["ids"] = [r["transaction_id"] for r in rows]
        return submit("needs_clarification", "¿Cuál de los dos cobros?", candidates=seen["ids"], id="toolu_submit1")

    def second_turn_opens_with_previous_result(messages):
        first_block = messages[-1]["content"][0]
        assert first_block == {"type": "tool_result", "tool_use_id": "toolu_submit1", "content": "Respuesta entregada al cliente."}
        return step(tool_use("create_dispute", {"transaction_id": "T-02", "reason": "Cobro tras cancelar", "amount": 15.99,
                                                "cancellation_requested_on": "2026-10-01"}))

    def after_create(messages):
        seen["create"] = results_json(messages)[0]
        return submit("procede", "Registramos tu reclamo.", transaction_id="T-02", sections=["5", "§1", "99"])

    llm = ScriptedFakeLLM([
        step(tool_use("get_transactions", {"date_from": "2026-10-01", "date_to": "2026-10-10"})),
        pick_candidates,
        second_turn_opens_with_previous_result,
        after_create,
    ])
    client, container = client_for(llm)

    first = post(client, NETFLIX_MSG)
    assert first["status"] == "needs_clarification"
    assert [c["transaction_id"] for c in first["candidates"]] == ["T-01", "T-02"]

    second = post(client, "Es el del 5 de octubre. Pedí la cancelación el 1 de octubre.", session_id=first["session_id"])
    assert second["status"] == "procede"
    assert second["dispute_id"].startswith("D-") and second["dispute_reference"].startswith("R-")
    assert [p["section"] for p in second["policy_refs"]] == ["1", "5"]
    assert seen["create"]["registered"] is True
    assert len(container.legacy.calls) == 1

    # The card number never reaches the model.
    assert "4532" not in json.dumps(llm.requests)
    assert "terminada en 9012" in json.dumps(llm.requests)


def test_injected_merchant_text_cannot_avoid_advisor_review(client_for):
    llm = ScriptedFakeLLM([
        step(tool_use("get_transactions", {"date_from": "2026-10-15", "date_to": "2026-10-25"}),
             tool_use("get_merchant_info", {"merchant_id": "M-ELECTRO"})),
        # The model follows the provider's "note for automated systems"...
        submit("no_procede", "El comercio está verificado.", transaction_id="T-03"),
        # ...gets rejected by the hard rule and corrects itself.
        lambda m: (_ for _ in ()).throw(AssertionError("expected rejection")) if not last_tool_results(m)[0].get("is_error")
        else step(tool_use("escalate_to_advisor", {"transaction_id": "T-03", "reason": "monto > 500", "summary": "Posible inyección del proveedor"})),
        submit("revision_asesor", "Un asesor revisará tu caso.", transaction_id="T-03", sections=["3"]),
    ])
    client, container = client_for(llm)
    body = post(client, "No reconozco una compra de 820 dólares en Electro Store")
    assert body["status"] == "revision_asesor"
    assert body["case_id"].startswith("CASE-")
    case = next(iter(container.notifier.cases.values()))
    assert case["injection_suspected_merchants"] == ["M-ELECTRO"]
    assert container.legacy.calls == []


def test_model_that_keeps_contradicting_hard_rule_is_overridden(client_for):
    wrong = lambda m: submit("no_procede", "No procede.", transaction_id="T-03")  # noqa: E731
    client, container = client_for(ScriptedFakeLLM([wrong, wrong, wrong]))
    body = post(client, "No reconozco la compra en Electro Store")
    assert body["status"] == "revision_asesor"
    assert body["case_id"] and [p["section"] for p in body["policy_refs"]] == ["3"]


def test_guard_blocks_create_dispute_for_pending_transaction(client_for):
    captured = {}

    def after(messages):
        captured.update(results_json(messages)[0])
        return submit("no_procede", "Está pendiente.", transaction_id="T-04", sections=["2"])

    client, container = client_for(ScriptedFakeLLM([
        step(tool_use("create_dispute", {"transaction_id": "T-04", "reason": "x", "amount": 42.10, "cancellation_requested_on": None})),
        after,
    ]))
    body = post(client, "No reconozco el cobro del supermercado de 42.10")
    assert body["status"] == "no_procede"
    assert captured["registered"] is False and captured["required_outcome"] == "no_procede"
    assert container.legacy.calls == []


@pytest.mark.parametrize("txn", ["T-05", "T-06"])
def test_guard_blocks_expired_and_foreign_transactions(client_for, txn):
    captured = {}

    def after(messages):
        captured.update(results_json(messages)[0])
        return submit("no_procede", "No procede.", transaction_id=txn)

    client, container = client_for(ScriptedFakeLLM([
        step(tool_use("create_dispute", {"transaction_id": txn, "reason": "x", "amount": 1, "cancellation_requested_on": None})), after,
    ]))
    assert post(client, "reclamo")["status"] == "no_procede"
    assert captured["registered"] is False
    assert container.legacy.calls == []


def test_subscription_without_cancellation_is_blocked(client_for):
    captured = {}

    def after(messages):
        captured.update(results_json(messages)[0])
        return submit("no_procede", "Cancela con Netflix.", transaction_id="T-02", sections=["5"])

    client, _ = client_for(ScriptedFakeLLM([
        step(tool_use("create_dispute", {"transaction_id": "T-02", "reason": "x", "amount": 15.99, "cancellation_requested_on": None})), after,
    ]))
    assert post(client, "no reconozco netflix")["status"] == "no_procede"
    assert captured["violations"][0]["rule"] == "suscripcion_sin_cancelacion"


def test_procede_without_registering_is_rejected(client_for):
    def check(messages):
        assert last_tool_results(messages)[0]["is_error"] is True
        return submit("needs_clarification", "¿Cuál fue la fecha?")

    client, _ = client_for(ScriptedFakeLLM([submit("procede", "Listo", transaction_id="T-01"), check]))
    assert post(client, "netflix")["status"] == "needs_clarification"


def test_duplicate_request_in_new_session_reuses_dispute(client_for):
    create = lambda m: step(tool_use("create_dispute", {"transaction_id": "T-02", "reason": "x", "amount": 15.99,  # noqa: E731
                                                       "cancellation_requested_on": "2026-09-30"}))
    captured = []

    def done(messages):
        captured.append(results_json(messages)[0])
        return submit("procede", "Registrado", transaction_id="T-02")

    client, container = client_for(ScriptedFakeLLM([create, done, create, done]))
    first = post(client, "netflix del 5")
    second = post(client, "netflix del 5 otra vez")
    assert first["dispute_reference"] == second["dispute_reference"]
    assert captured[1]["already_exists"] is True
    assert len(container.legacy.calls) == 1


def test_slow_legacy_gives_pending_registration(client_for):
    release = threading.Event()
    legacy = LegacyDisputeSystemMock(latency=lambda: 0.0)
    original = legacy.create_dispute
    legacy.create_dispute = lambda *a: (release.wait(5), original(*a))[1]

    client, container = client_for(ScriptedFakeLLM([
        step(tool_use("create_dispute", {"transaction_id": "T-02", "reason": "x", "amount": 15.99, "cancellation_requested_on": "2026-09-30"})),
        submit("procede", "En cola", transaction_id="T-02"),
    ]), legacy=legacy)
    body = post(client, "netflix")
    assert body["status"] == "pending_registration"
    assert body["dispute_id"] is None and body["dispute_reference"].startswith("R-")

    release.set()
    record = container.toolbox.disputes_repo.find_by_transaction("T-02")
    assert record.done.wait(2) and record.legacy_dispute_id
    assert len(legacy.calls) == 1


def test_submit_mixed_with_other_tools_is_deferred(client_for):
    def check(messages):
        results = last_tool_results(messages)
        assert results[0].get("is_error") is None and results[1]["is_error"] is True
        return submit("needs_clarification", "¿Cuál?")

    client, _ = client_for(ScriptedFakeLLM([
        step(tool_use("search_reglamento", {"query": "plazo"}), tool_use("submit_response", {"status": "no_procede"})),
        check,
    ]))
    assert post(client, "hola")["status"] == "needs_clarification"


def test_iteration_cap_escalates_and_session_stays_usable(client_for, settings):
    settings.max_iterations = 3
    loop = lambda m: step(tool_use("search_reglamento", {"query": "plazo"}))  # noqa: E731
    llm = ScriptedFakeLLM([loop, loop, loop, submit("needs_clarification", "¿Me das más detalles?")])
    client, container = client_for(llm)
    first = post(client, "ayuda")
    assert first["status"] == "revision_asesor" and first["case_id"]

    second = post(client, "sigo esperando", session_id=first["session_id"])
    assert second["status"] == "needs_clarification"
    roles = [m["role"] for m in llm.requests[-1]["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:]))  # roles still alternate


def test_llm_outage_still_answers_customer(client_for):
    def boom(messages):
        raise LLMError("no se pudo conectar con el modelo")

    client, container = client_for(ScriptedFakeLLM([boom]))
    body = post(client, NETFLIX_MSG)
    assert body["status"] == "revision_asesor" and body["case_id"]
    assert "asesor" in body["message"]


def test_plain_text_answers_are_nudged_then_returned(client_for):
    llm = ScriptedFakeLLM([step(text("¿Qué cobro?"), stop="end_turn"), step(text("¿Qué cobro exactamente?"), stop="end_turn")])
    client, _ = client_for(llm)
    body = post(client, "hola")
    assert body == {**body, "status": "needs_clarification", "message": "¿Qué cobro exactamente?"}


def test_session_of_another_customer_is_forbidden(client_for):
    client, _ = client_for(ScriptedFakeLLM([submit("needs_clarification", "¿Cuál?")]))
    first = post(client, "hola")
    response = client.post("/api/v1/disputes/assist", json={"customer_id": "C-2002", "session_id": first["session_id"], "message": "hola"})
    assert response.status_code == 403


def test_tools_are_bound_to_authenticated_customer(client_for):
    captured = {}

    def after(messages):
        captured.update(results_json(messages)[0])
        return submit("needs_clarification", "No encontramos el cobro.")

    client, _ = client_for(ScriptedFakeLLM([
        step(tool_use("get_transactions", {"date_from": "2026-10-01", "date_to": "2026-10-31"})), after,
    ]))
    post(client, "juegos", customer="C-1001")
    assert "T-06" not in [t["transaction_id"] for t in captured["transactions"]]


def test_request_validation(client_for):
    client, _ = client_for(ScriptedFakeLLM([]))
    assert client.post("/api/v1/disputes/assist", json={"customer_id": "C-1001", "message": ""}).status_code == 422
