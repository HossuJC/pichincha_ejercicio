"""Builds the object graph from settings. Tests build their own with fakes."""

import importlib
from dataclasses import dataclass

from app.agent.llm import AnthropicLLM, LLMClient
from app.agent.orchestrator import Orchestrator
from app.agent.toolbox import Toolbox
from app.clock import FixedClock
from app.config import Settings
from app.policy.guard import PolicyGuard
from app.rag.loader import load_active_reglamento
from app.rag.retriever import BM25Retriever
from app.sessions.store import InMemorySessionStore, SQLiteSessionStore
from app.tools.disputes import DisputeRepository, IdempotentDisputeService, LegacyDisputeSystemMock
from app.tools.merchants import MerchantProviderMock, MerchantService
from app.tools.notifications import OutboxNotifier
from app.tools.transactions import TransactionRepository


@dataclass
class Container:
    orchestrator: Orchestrator
    toolbox: Toolbox
    legacy: LegacyDisputeSystemMock
    notifier: OutboxNotifier


def build_container(settings: Settings, llm: LLMClient | None = None,
                    legacy: LegacyDisputeSystemMock | None = None,
                    merchant_provider: MerchantProviderMock | None = None) -> Container:
    clock = FixedClock(settings.today)
    reglamento = load_active_reglamento(settings.reglamento_dir, clock.today())

    transactions = TransactionRepository()
    disputes_repo = DisputeRepository()
    legacy = legacy or LegacyDisputeSystemMock.with_random_latency(
        slow_probability=settings.dispute_slow_probability,
        slow_range=(settings.dispute_slow_min_s, settings.dispute_slow_max_s),
        fast_max=settings.dispute_fast_max_s,
    )
    merchant_provider = merchant_provider or MerchantProviderMock(failure_probability=settings.merchant_failure_probability)
    notifier = OutboxNotifier()

    toolbox = Toolbox(
        transactions=transactions,
        merchants=MerchantService(merchant_provider, settings.merchant_timeout_s, settings.merchant_cache_ttl_s),
        disputes_repo=disputes_repo,
        disputes=IdempotentDisputeService(disputes_repo, legacy, clock, settings.dispute_timeout_s, settings.dispute_max_attempts),
        guard=PolicyGuard(reglamento, transactions, disputes_repo, clock),
        retriever=BM25Retriever(reglamento),
        notifier=notifier,
        clock=clock,
    )
    if llm is None:
        llm = _build_llm(settings)

    sessions = SQLiteSessionStore(settings.sqlite_path) if settings.session_backend == "sqlite" else InMemorySessionStore()
    orchestrator = Orchestrator(llm, toolbox, sessions, toolbox.guard, reglamento, settings.max_iterations)
    return Container(orchestrator=orchestrator, toolbox=toolbox, legacy=legacy, notifier=notifier)


def _build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "custom":
        if not settings.llm_factory or ":" not in settings.llm_factory:
            raise ValueError("APP_LLM_PROVIDER=custom requiere APP_LLM_FACTORY=paquete.modulo:fabrica")
        module_name, attr = settings.llm_factory.split(":", 1)
        return getattr(importlib.import_module(module_name), attr)(settings)
    if settings.llm_provider != "anthropic":
        raise ValueError("APP_LLM_PROVIDER=fake solo se usa en tests (inyecta el LLM)")
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return AnthropicLLM(settings.model, settings.effort, settings.max_tokens, settings.enable_fallbacks,
                        settings.llm_timeout_s, api_key=api_key)
