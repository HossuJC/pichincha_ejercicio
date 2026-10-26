import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable

from app.security.injection import looks_like_injection
from app.tools.mock_data import MERCHANTS

log = logging.getLogger(__name__)


class MerchantNotFound(Exception):
    pass


class ProviderUnavailable(Exception):
    pass


class MerchantProviderMock:
    """External merchant-data provider (simulated)."""

    def __init__(
        self,
        catalogue: dict[str, dict] | None = None,
        failure_probability: float = 0.0,
        latency: Callable[[], float] = lambda: random.uniform(0.05, 0.3),
        rng: random.Random | None = None,
    ):
        self._catalogue = catalogue if catalogue is not None else MERCHANTS
        self._failure_probability = failure_probability
        self._latency = latency
        self._rng = rng or random.Random()

    def fetch(self, merchant_id: str) -> dict:
        time.sleep(self._latency())
        if self._rng.random() < self._failure_probability:
            raise ProviderUnavailable("proveedor de comercios no disponible")
        if merchant_id not in self._catalogue:
            raise MerchantNotFound(merchant_id)
        return dict(self._catalogue[merchant_id])


class MerchantService:
    """Wraps the provider with timeout, TTL cache and a degraded fallback.

    The provider's free-text description is returned as explicitly untrusted data
    and screened for instruction-like content.
    """

    def __init__(self, provider: MerchantProviderMock, timeout_s: float, cache_ttl_s: float):
        self._provider = provider
        self._timeout_s = timeout_s
        self._ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="merchant")

    def get_merchant_info(self, merchant_id: str) -> dict:
        with self._lock:
            cached = self._cache.get(merchant_id)
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]

        future = self._pool.submit(self._provider.fetch, merchant_id)
        try:
            raw = future.result(timeout=self._timeout_s)
        except MerchantNotFound:
            return {
                "merchant_id": merchant_id,
                "available": False,
                "reason": "El proveedor no tiene información de este comercio.",
            }
        except (FutureTimeout, ProviderUnavailable) as exc:
            log.warning("merchant provider degraded for %s: %r", merchant_id, exc)
            return {
                "merchant_id": merchant_id,
                "available": False,
                "reason": "El proveedor de comercios no respondió a tiempo. Continúa con los datos de la transacción.",
            }

        description = raw.get("description")
        suspicious = looks_like_injection(description)
        if suspicious:
            log.warning("possible prompt injection in provider description for %s", merchant_id)
        result = {
            "merchant_id": raw["merchant_id"],
            "available": True,
            "name": raw["name"],
            "category": raw["category"],
            "risk_score": raw["risk_score"],
            "description": {
                "untrusted_provider_text": description,
                "injection_suspected": suspicious,
            },
        }
        with self._lock:
            self._cache[merchant_id] = (time.monotonic(), result)
        return result
