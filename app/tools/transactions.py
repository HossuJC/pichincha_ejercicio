from datetime import date

from app.tools.mock_data import TRANSACTIONS

_PUBLIC_FIELDS = ("transaction_id", "date", "amount", "currency", "merchant_id", "status")


class TransactionRepository:
    """Mock of the core-banking transactions API."""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = [dict(r) for r in (rows if rows is not None else TRANSACTIONS)]

    def get_transactions(self, customer_id: str, date_from: date, date_to: date) -> list[dict]:
        return [
            {k: row[k] for k in _PUBLIC_FIELDS}
            for row in self._rows
            if row["customer_id"] == customer_id and date_from <= date.fromisoformat(row["date"]) <= date_to
        ]

    def get_for_customer(self, customer_id: str, transaction_id: str) -> dict | None:
        """Returns the transaction only if it belongs to the customer."""
        for row in self._rows:
            if row["transaction_id"] == transaction_id and row["customer_id"] == customer_id:
                return {k: row[k] for k in _PUBLIC_FIELDS}
        return None
