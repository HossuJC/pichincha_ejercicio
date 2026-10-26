"""Deterministic checks that protect regulated actions.

The LLM decides with the reglamento it retrieves; this guard does not replace it.
It only (a) refuses to register a dispute that breaks a hard rule and (b) lets the
orchestrator reject a final answer that contradicts one. Thresholds come from the
front-matter of the same reglamento version the LLM reads, so both stay in sync.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.clock import Clock
from app.rag.loader import Reglamento
from app.schemas import Decision
from app.tools.disputes import DisputeRepository
from app.tools.transactions import TransactionRepository

SUBSCRIPTION_MARKERS = ("suscripcion", "subscription", "streaming")


@dataclass(frozen=True)
class Violation:
    rule: str
    section: str
    outcome: Decision
    detail: str


@dataclass
class GuardResult:
    transaction: dict | None
    version: str
    violations: list[Violation] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.violations

    @property
    def required_outcome(self) -> Decision | None:
        return self.violations[0].outcome if self.violations else None

    def as_tool_payload(self) -> dict:
        return {
            "allowed": self.allowed,
            "required_outcome": self.required_outcome,
            "reglamento_version": self.version,
            "violations": [v.__dict__ for v in self.violations],
        }


def is_subscription(category: str | None) -> bool:
    return bool(category) and any(m in category.lower() for m in SUBSCRIPTION_MARKERS)


class PolicyGuard:
    def __init__(self, reglamento: Reglamento, transactions: TransactionRepository, disputes: DisputeRepository, clock: Clock):
        self._reglamento = reglamento
        self._transactions = transactions
        self._disputes = disputes
        self._clock = clock

    def evaluate(
        self,
        customer_id: str,
        transaction_id: str,
        *,
        merchant_category: str | None = None,
        cancellation_requested_on: date | None = None,
        check_subscription: bool = True,
    ) -> GuardResult:
        t = self._reglamento.thresholds
        today = self._clock.today()
        result = GuardResult(transaction=None, version=self._reglamento.version)

        txn = self._transactions.get_for_customer(customer_id, transaction_id)
        if txn is None:
            result.violations.append(Violation("transaccion_no_encontrada", "-", Decision.NO_PROCEDE,
                                               f"La transacción {transaction_id} no existe o no pertenece al cliente."))
            return result
        result.transaction = txn
        txn_date = date.fromisoformat(txn["date"])

        # 1) Ineligible: these end the evaluation as "no procede".
        if txn["status"] == "pending":
            result.violations.append(Violation("transaccion_pendiente", "2", Decision.NO_PROCEDE,
                                               "La transacción está en estado pending."))
        age = (today - txn_date).days
        if age > t.max_days_to_dispute:
            result.violations.append(Violation("plazo_vencido", "1", Decision.NO_PROCEDE,
                                               f"Han pasado {age} días desde el consumo (máximo {t.max_days_to_dispute})."))
        if result.violations:
            return result

        # 2) Eligible but needs a human.
        if txn["amount"] > t.review_amount_usd:
            result.violations.append(Violation("monto_mayor_umbral", "3", Decision.REVISION_ASESOR,
                                               f"Monto {txn['amount']:.2f} {txn['currency']} supera USD {t.review_amount_usd:g}."))
        since = today - timedelta(days=t.recent_disputes_window_days)
        recent = [r for r in self._disputes.recent_for_customer(customer_id, since) if r.transaction_id != transaction_id]
        if len(recent) > t.max_recent_disputes:
            result.violations.append(Violation("frecuencia_reclamos", "4", Decision.REVISION_ASESOR,
                                               f"El cliente tiene {len(recent)} reclamos en los últimos {t.recent_disputes_window_days} días."))
        if result.violations:
            return result

        # 3) Recurring subscription charges need a cancellation requested before the charge.
        if check_subscription and is_subscription(merchant_category):
            if cancellation_requested_on is None or cancellation_requested_on >= txn_date:
                result.violations.append(Violation("suscripcion_sin_cancelacion", "5", Decision.NO_PROCEDE,
                                                   "Cargo de suscripción sin evidencia de cancelación solicitada antes del cobro."))
        return result
