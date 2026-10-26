from dataclasses import dataclass
from typing import Callable, Protocol

import anthropic


@dataclass
class LLMResponse:
    content: list[dict]  # content blocks as plain dicts, replayable as-is
    stop_reason: str | None


class LLMError(Exception):
    """The model could not be reached or answered with an unrecoverable error."""


class LLMClient(Protocol):
    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str, effort: str, max_tokens: int, enable_fallbacks: bool, timeout_s: float,
                 api_key: str | None = None):
        # Without an explicit key the SDK falls back to its own resolution (env var, `ant` profile...).
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=2)
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._enable_fallbacks = enable_fallbacks

    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse:
        extra: dict = {}
        if self._enable_fallbacks:
            # On a safety-classifier refusal the API re-runs the request on Anthropic's
            # recommended fallback model instead of returning the refusal.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                # Stable prefix (tools + system) is cached across turns and customers.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                tool_choice={"type": "auto"},
                messages=messages,
                **extra,
            )
        except anthropic.BadRequestError as exc:
            raise LLMError(f"solicitud inválida al modelo: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("credenciales de Anthropic inválidas") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("límite de uso del modelo alcanzado") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"error del modelo ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("no se pudo conectar con el modelo") from exc
        except (anthropic.AnthropicError, TypeError) as exc:
            # TypeError: the SDK raises it when no credentials are configured at all.
            raise LLMError(f"cliente del modelo mal configurado: {exc}") from exc

        return LLMResponse(
            content=[block.model_dump(mode="json", exclude_none=True) for block in response.content],
            stop_reason=response.stop_reason,
        )


class ScriptedFakeLLM:
    """Deterministic stand-in for tests: each call pops the next scripted step.

    A step is either an LLMResponse or a callable(messages) -> LLMResponse, so a
    test can react to tool results it receives.
    """

    def __init__(self, steps: list[LLMResponse | Callable[[list[dict]], LLMResponse]]):
        self._steps = list(steps)
        self.requests: list[dict] = []

    def create(self, system: str, tools: list[dict], messages: list[dict]) -> LLMResponse:
        self.requests.append({"system": system, "tools": tools, "messages": [dict(m) for m in messages]})
        if not self._steps:
            raise AssertionError("ScriptedFakeLLM: no more scripted steps")
        step = self._steps.pop(0)
        return step(messages) if callable(step) else step


def tool_use(name: str, input: dict, id: str | None = None) -> dict:
    import uuid

    return {"type": "tool_use", "id": id or f"toolu_{uuid.uuid4().hex[:12]}", "name": name, "input": input}


def text(value: str) -> dict:
    return {"type": "text", "text": value}
