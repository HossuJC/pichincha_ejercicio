import logging

from fastapi import FastAPI, HTTPException

from app.agent.orchestrator import SessionForbidden
from app.config import get_settings
from app.container import Container, build_container
from app.schemas import AssistRequest, AssistResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="Asistente de reclamos de tarjeta de crédito", version="0.1.0")
    app.state.container = container or build_container(get_settings())

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # Sync handler: FastAPI runs it in a worker thread, the slow legacy call does not block the loop.
    @app.post("/api/v1/disputes/assist", response_model=AssistResponse)
    def assist(request: AssistRequest) -> AssistResponse:
        try:
            return app.state.container.orchestrator.handle(request)
        except SessionForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the real app lazily, so importing this module in tests needs no API key.
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
