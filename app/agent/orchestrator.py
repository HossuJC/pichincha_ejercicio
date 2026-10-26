import json
import logging
from concurrent.futures import ThreadPoolExecutor

from app.agent.llm import LLMClient, LLMError
from app.agent.prompts import build_system_prompt, wrap_customer_message
from app.agent.tool_specs import TOOL_SPECS
from app.agent.toolbox import Toolbox, ToolInputError, TurnState
from app.policy.guard import PolicyGuard
from app.rag.loader import Reglamento
from app.schemas import AssistRequest, AssistResponse, Decision, PolicyRef, Transaction
from app.security.pii import mask_pan
from app.sessions.store import Session, SessionStore, new_session
from app.tools.disputes import PENDING

log = logging.getLogger(__name__)

SUBMIT = "submit_response"
MAX_SUBMIT_REJECTIONS = 2


class SessionForbidden(Exception):
    pass


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        toolbox: Toolbox,
        sessions: SessionStore,
        guard: PolicyGuard,
        reglamento: Reglamento,
        max_iterations: int,
    ):
        self._llm = llm
        self._toolbox = toolbox
        self._sessions = sessions
        self._guard = guard
        self._reglamento = reglamento
        self._max_iterations = max_iterations
        self._system = build_system_prompt(toolbox.clock.today(), reglamento.version)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

    # --- entry point -----------------------------------------------------------

    def handle(self, request: AssistRequest) -> AssistResponse:
        session = self._load_session(request)
        masked, last4 = mask_pan(request.message)
        if last4:
            session.card_last4 = last4

        user_content: list[dict] = list(session.pending_tool_results)
        session.pending_tool_results = []
        user_content.append({"type": "text", "text": wrap_customer_message(masked, session.card_last4)})
        if session.messages and session.messages[-1]["role"] == "user":
            # Previous turn ended before the model answered (e.g. LLM outage): keep roles alternating.
            session.messages[-1]["content"].extend(user_content)
        else:
            session.messages.append({"role": "user", "content": user_content})

        state = TurnState(customer_id=session.customer_id, session_id=session.session_id)
        try:
            response = self._run_loop(session, state)
        except LLMError as exc:
            log.error("LLM unavailable for session %s: %s", session.session_id, exc)
            response = self._fallback(session, state, "El asistente no estuvo disponible", str(exc))

        session.last_status = response.status
        session.candidates = [c.transaction_id for c in response.candidates]
        self._sessions.save(session)
        return response

    # --- loop --------------------------------------------------------------------

    def _run_loop(self, session: Session, state: TurnState) -> AssistResponse:
        rejections = 0
        nudged = False
        for _ in range(self._max_iterations):
            reply = self._llm.create(self._system, TOOL_SPECS, session.messages)
            session.messages.append({"role": "assistant", "content": reply.content})

            if reply.stop_reason == "refusal":
                return self._fallback(session, state, "El modelo rechazó la solicitud", "stop_reason=refusal")

            tool_uses = [b for b in reply.content if b.get("type") == "tool_use"]
            if not tool_uses:
                if nudged:
                    return self._from_plain_text(session, reply.content)
                nudged = True
                session.messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Finaliza llamando a submit_response con la respuesta para el cliente."}
                ]})
                continue

            submits = [b for b in tool_uses if b["name"] == SUBMIT]
            if submits and len(tool_uses) == 1:
                call = submits[0]
                problem = self._validate_submit(call["input"], state)
                if problem is None:
                    return self._finish(session, state, call)
                rejections += 1
                log.warning("submit_response rejected (%d): %s", rejections, problem)
                if rejections > MAX_SUBMIT_REJECTIONS:
                    return self._enforce(session, state, call)
                session.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call["id"], "is_error": True, "content": problem}
                ]})
                continue

            session.messages.append({"role": "user", "content": self._run_tools(tool_uses, state)})

        return self._fallback(session, state, "El caso superó el número máximo de pasos", "max_iterations")

    def _run_tools(self, tool_uses: list[dict], state: TurnState) -> list[dict]:
        def run(block: dict) -> dict:
            if block["name"] == SUBMIT:
                return _tool_result(block["id"], "Llama a submit_response sola, después de revisar los resultados de las demás herramientas.", True)
            try:
                result = self._toolbox.execute(block["name"], block["input"], state)
                return _tool_result(block["id"], json.dumps(result, ensure_ascii=False, default=str))
            except ToolInputError as exc:
                return _tool_result(block["id"], str(exc), True)
            except Exception:
                log.exception("tool %s failed", block["name"])
                return _tool_result(block["id"], "Error interno de la herramienta. Intenta otra vía o deriva a un asesor.", True)

        # All results go back in a single user message, in the original order.
        return list(self._pool.map(run, tool_uses))

    # --- final answer ------------------------------------------------------------

    def _validate_submit(self, args: dict, state: TurnState) -> str | None:
        status = args.get("status")
        txn_id = args.get("transaction_id")

        if status == Decision.NEEDS_CLARIFICATION:
            return None
        if status == Decision.PROCEDE:
            if not txn_id or txn_id not in state.disputes:
                return "Para responder 'procede' primero debes registrar el reclamo con create_dispute y obtener su referencia."
            return None
        if txn_id and txn_id in state.disputes and state.disputes[txn_id].customer_id == state.customer_id:
            return f"Ya existe un reclamo registrado para {txn_id}; la respuesta debe ser 'procede'."
        if txn_id:
            required = self._guard.evaluate(state.customer_id, txn_id, check_subscription=False).required_outcome
            if required is not None and required != status:
                return (f"Según las reglas obligatorias del reglamento vigente, el resultado para {txn_id} debe ser "
                        f"'{required}'. Revisa el reglamento y corrige la respuesta.")
        return None

    def _finish(self, session: Session, state: TurnState, call: dict) -> AssistResponse:
        args = call["input"]
        status = Decision(args["status"])
        txn_id = args.get("transaction_id")
        session.pending_tool_results = [_tool_result(call["id"], "Respuesta entregada al cliente.")]

        dispute = state.disputes.get(txn_id) if txn_id else None
        case_id = state.escalations.get(txn_id)
        if status == Decision.REVISION_ASESOR and case_id is None:
            # The model decided on a review but did not escalate: do it so no case gets lost.
            case_id = self._toolbox.escalate(state, txn_id, "revision_asesor", args.get("message", ""))
        if status == Decision.PROCEDE and dispute and dispute.status == PENDING:
            status = Decision.PENDING_REGISTRATION

        return AssistResponse(
            session_id=session.session_id,
            status=status,
            message=args.get("message", ""),
            transaction_id=txn_id,
            dispute_id=dispute.legacy_dispute_id if dispute else None,
            dispute_reference=dispute.reference if dispute else None,
            case_id=case_id,
            candidates=self._candidates(state, args.get("candidate_transaction_ids") or []),
            policy_refs=self._policy_refs(args.get("policy_sections") or [], state),
            next_steps=args.get("next_steps") or [],
        )

    def _enforce(self, session: Session, state: TurnState, call: dict) -> AssistResponse:
        """The model kept contradicting a hard rule: answer from the rule itself."""
        txn_id = call["input"].get("transaction_id")
        verdict = self._guard.evaluate(state.customer_id, txn_id, check_subscription=False) if txn_id else None
        if verdict is None or verdict.allowed:
            return self._fallback(session, state, "Respuesta del asistente inconsistente", json.dumps(call["input"], ensure_ascii=False))
        violation = verdict.violations[0]
        session.pending_tool_results = [_tool_result(call["id"], "Respuesta reemplazada por la regla obligatoria.", True)]
        if violation.outcome == Decision.REVISION_ASESOR:
            case_id = self._toolbox.escalate(state, txn_id, violation.rule, violation.detail)
            message = ("Recibimos tu solicitud. Por el tipo de caso, un asesor revisará tu reclamo y te contactará "
                       "con el resultado.")
            steps = ["Un asesor revisará el caso y te contactará."]
        else:
            case_id = None
            message = f"Revisamos tu solicitud y el reclamo no procede: {violation.detail}"
            steps = ["Si tienes dudas, puedes comunicarte con un asesor."]
        return AssistResponse(
            session_id=session.session_id, status=violation.outcome, message=message, transaction_id=txn_id,
            case_id=case_id, policy_refs=self._policy_refs([violation.section], state), next_steps=steps,
        )

    def _fallback(self, session: Session, state: TurnState, reason: str, detail: str) -> AssistResponse:
        """Never leave the customer without an answer: hand the case to an advisor."""
        session.pending_tool_results = _close_dangling_tool_uses(session.messages)
        case_id = self._toolbox.escalate(state, None, reason, detail)
        return AssistResponse(
            session_id=session.session_id,
            status=Decision.REVISION_ASESOR,
            message=("Recibimos tu solicitud, pero no pudimos completarla en este momento. Un asesor revisará tu caso "
                     "y te contactará. No necesitas volver a enviarla."),
            case_id=case_id,
            next_steps=["Un asesor revisará tu caso y te contactará."],
        )

    def _from_plain_text(self, session: Session, content: list[dict]) -> AssistResponse:
        text = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
        return AssistResponse(
            session_id=session.session_id,
            status=Decision.NEEDS_CLARIFICATION,
            message=text or "¿Podrías darnos más detalles del consumo que no reconoces (fecha, monto o comercio)?",
        )

    # --- helpers ----------------------------------------------------------------

    def _load_session(self, request: AssistRequest) -> Session:
        if request.session_id:
            session = self._sessions.get(request.session_id)
            if session is not None:
                if session.customer_id != request.customer_id:
                    raise SessionForbidden("La sesión no pertenece al cliente")
                return session
        return new_session(request.customer_id)

    def _candidates(self, state: TurnState, ids: list[str]) -> list[Transaction]:
        txns = (self._toolbox.transactions.get_for_customer(state.customer_id, i) for i in ids)
        return [Transaction(**t) for t in txns if t]

    def _policy_refs(self, sections: list[str], state: TurnState) -> list[PolicyRef]:
        valid = {s.section for s in self._reglamento.sections}
        cleaned = sorted({str(s).strip().lstrip("§").strip() for s in sections} & valid, key=int)
        return [PolicyRef(section=s, version=self._reglamento.version) for s in cleaned]


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _close_dangling_tool_uses(messages: list[dict]) -> list[dict]:
    """If the conversation stopped right after tool calls, answer them on the next turn."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    return [
        _tool_result(b["id"], "No se ejecutó: el turno se interrumpió.", True)
        for b in messages[-1]["content"]
        if b.get("type") == "tool_use"
    ]
