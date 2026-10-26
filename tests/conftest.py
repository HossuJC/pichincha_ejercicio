import pytest

from app.config import Settings
from app.container import build_container
from app.tools.disputes import LegacyDisputeSystemMock
from app.tools.merchants import MerchantProviderMock


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_provider="fake",
        dispute_timeout_s=0.5,
        merchant_timeout_s=0.5,
        session_backend="memory",
        sqlite_path=tmp_path / "sessions.db",
    )


@pytest.fixture
def fast_legacy() -> LegacyDisputeSystemMock:
    return LegacyDisputeSystemMock(latency=lambda: 0.01)


@pytest.fixture
def fast_provider() -> MerchantProviderMock:
    return MerchantProviderMock(latency=lambda: 0.0)


@pytest.fixture
def make_container(settings, fast_legacy, fast_provider):
    def _make(llm, legacy=None, provider=None):
        return build_container(settings, llm=llm, legacy=legacy or fast_legacy, merchant_provider=provider or fast_provider)

    return _make
