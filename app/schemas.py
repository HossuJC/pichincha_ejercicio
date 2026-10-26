from enum import StrEnum

from pydantic import BaseModel, Field


class Decision(StrEnum):
    PROCEDE = "procede"
    NO_PROCEDE = "no_procede"
    REVISION_ASESOR = "revision_asesor"
    NEEDS_CLARIFICATION = "needs_clarification"
    PENDING_REGISTRATION = "pending_registration"


class AssistRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64, examples=["C-1001"])
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class Transaction(BaseModel):
    transaction_id: str
    date: str
    amount: float
    currency: str
    merchant_id: str
    status: str


class PolicyRef(BaseModel):
    section: str
    version: str


class AssistResponse(BaseModel):
    session_id: str
    status: Decision
    message: str
    transaction_id: str | None = None
    dispute_id: str | None = Field(default=None, description="ID asignado por el sistema de reclamos legado")
    dispute_reference: str | None = Field(default=None, description="Referencia interna, disponible aunque el legado no haya respondido")
    case_id: str | None = Field(default=None, description="Caso derivado a un asesor")
    candidates: list[Transaction] = []
    policy_refs: list[PolicyRef] = []
    next_steps: list[str] = []
