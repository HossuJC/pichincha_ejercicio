import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.clock import Clock
from app.policy.guard import PolicyGuard
from app.rag.retriever import Retriever
from app.tools.disputes import PENDING, DisputeRecord, DisputeRepository, IdempotentDisputeService
from app.tools.merchants import MerchantService
from app.tools.notifications import Notifier
from app.tools.transactions import TransactionRepository

log = logging.getLogger(__name__)

MAX_RANGE_DAYS = 366


class ToolInputError(Exception):
    pass


@dataclass
class TurnState:
    """What the agent did during one request; used to validate and build the final answer."""

    customer_id: str
    session_id: str
    disputes: dict[str, DisputeRecord] = field(default_factory=dict)
    escalations: dict[str | None, str] = field(default_factory=dict)
    sections_read: set[str] = field(default_factory=set)
    injection_flags: set[str] = field(default_factory=set)


@dataclass
class Toolbox:
    transactions: TransactionRepository
    merchants: MerchantService
    disputes_repo: DisputeRepository
    disputes: IdempotentDisputeService
    guard: PolicyGuard
    retriever: Retriever
    notifier: Notifier
    clock: Clock

    def execute(self, name: str, args: dict, state: TurnState) -> dict | list:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ToolInputError(f"Herramienta desconocida: {name}")
        return handler(args, state)

    # --- tools -----------------------------------------------------------------

    def _tool_get_transactions(self, args: dict, state: TurnState) -> dict:
        date_from, date_to = _parse_date(args.get("date_from")), _parse_date(args.get("date_to"))
        if date_from > date_to:
            raise ToolInputError("date_from debe ser anterior o igual a date_to")
        if (date_to - date_from).days > MAX_RANGE_DAYS:
            raise ToolInputError(f"El rango máximo es de {MAX_RANGE_DAYS} días")
        rows = self.transactions.get_transactions(state.customer_id, date_from, date_to)
        return {"transactions": rows, "count": len(rows)}

    def _tool_get_merchant_info(self, args: dict, state: TurnState) -> dict:
        info = self.merchants.get_merchant_info(str(args.get("merchant_id", "")))
        if info.get("description", {}).get("injection_suspected"):
            state.injection_flags.add(info["merchant_id"])
        return info

    def _tool_get_dispute_history(self, args: dict, state: TurnState) -> dict:
        days = max(1, min(int(args.get("days") or 30), 365))
        since = self.clock.today() - timedelta(days=days)
        records = self.disputes_repo.recent_for_customer(state.customer_id, since)
        return {
            "window_days": days,
            "count": len(records),
            "disputes": [
                {"transaction_id": r.transaction_id, "created_on": r.created_on.isoformat(), "amount": r.amount, "status": r.status}
                for r in records
            ],
        }

    def _tool_search_reglamento(self, args: dict, state: TurnState) -> dict:
        hits = self.retriever.search(str(args.get("query", "")), top_k=3)
        state.sections_read.update(h.section for h in hits)
        return {"results": [{"section": h.section, "title": h.title, "text": h.text, "version": h.version} for h in hits]}

    def _tool_create_dispute(self, args: dict, state: TurnState) -> dict:
        transaction_id = str(args.get("transaction_id", ""))
        reason = str(args.get("reason") or "Consumo no reconocido")
        cancellation = args.get("cancellation_requested_on")
        cancellation_date = _parse_date(cancellation) if cancellation else None

        txn = self.transactions.get_for_customer(state.customer_id, transaction_id)
        existing = self.disputes_repo.find_by_transaction(transaction_id) if txn else None
        if existing and existing.customer_id == state.customer_id:
            state.disputes[transaction_id] = existing
            return {"registered": True, "already_exists": True, **_dispute_view(existing),
                    "note": "Ya existía un reclamo para esta transacción; no se creó otro."}

        category = None
        if txn:
            merchant = self.merchants.get_merchant_info(txn["merchant_id"])
            category = merchant.get("category") if merchant.get("available") else None
        verdict = self.guard.evaluate(state.customer_id, transaction_id, merchant_category=category,
                                      cancellation_requested_on=cancellation_date)
        if not verdict.allowed:
            log.info("guard blocked create_dispute for %s: %s", transaction_id, [v.rule for v in verdict.violations])
            return {"registered": False, **verdict.as_tool_payload(),
                    "note": "No se registró el reclamo. Responde al cliente según required_outcome."}

        # The amount always comes from the ledger, never from the model.
        if abs(float(args.get("amount") or 0) - txn["amount"]) > 0.005:
            log.warning("create_dispute amount mismatch for %s: model=%s ledger=%s", transaction_id, args.get("amount"), txn["amount"])
        record, _ = self.disputes.create(state.customer_id, transaction_id, reason, txn["amount"])
        state.disputes[transaction_id] = record
        payload = {"registered": True, "already_exists": False, **_dispute_view(record)}
        if record.status == PENDING:
            payload["note"] = ("El sistema de reclamos está tardando en responder. El reclamo quedó en cola con la "
                               "referencia indicada y se completará automáticamente. No vuelvas a llamar create_dispute.")
        return payload

    def _tool_escalate_to_advisor(self, args: dict, state: TurnState) -> dict:
        transaction_id = args.get("transaction_id") or None
        if transaction_id and self.transactions.get_for_customer(state.customer_id, transaction_id) is None:
            raise ToolInputError("La transacción no pertenece al cliente")
        case_id = self.escalate(state, transaction_id, str(args.get("reason", "")), str(args.get("summary", "")))
        return {"case_id": case_id, "status": "derivado_a_asesor"}

    # --- helpers used by the orchestrator too ----------------------------------

    def escalate(self, state: TurnState, transaction_id: str | None, reason: str, summary: str) -> str:
        case_id = self.notifier.notify_advisor({
            "customer_id": state.customer_id,
            "session_id": state.session_id,
            "transaction_id": transaction_id,
            "reason": reason,
            "summary": summary,
            "injection_suspected_merchants": sorted(state.injection_flags),
            "created_on": self.clock.today().isoformat(),
        })
        state.escalations[transaction_id] = case_id
        return case_id


def _parse_date(value) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolInputError(f"Fecha inválida: {value!r}; usa YYYY-MM-DD") from exc


def _dispute_view(record: DisputeRecord) -> dict:
    return {
        "transaction_id": record.transaction_id,
        "reference": record.reference,
        "dispute_id": record.legacy_dispute_id,
        "status": record.status,
        "amount": record.amount,
    }
