# Cómo ejecutar

Requisitos: Python 3.12+ y [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env                  # y completar ANTHROPIC_API_KEY (o exportarla en la shell)
                                      # sin clave, el endpoint responde con el fallback (deriva a asesor)
uv run uvicorn app.main:app --reload
```

```bash
curl -s -X POST localhost:8000/api/v1/disputes/assist -H 'content-type: application/json' \
  -d '{"customer_id":"C-1001","message":"No reconozco un cobro de Netflix de principios de octubre, mi tarjeta es 4532 1234 5678 9012"}'

# continuar la conversación con el session_id devuelto
curl -s -X POST localhost:8000/api/v1/disputes/assist -H 'content-type: application/json' \
  -d '{"customer_id":"C-1001","session_id":"S-...","message":"Es el del 5 de octubre"}'
```

Docker: `docker build -t disputes . && docker run -p 8000:8000 --env-file .env disputes`

## Configuración (variables `APP_*`, ver `.env.example`)

| Variable | Default | Uso |
|---|---|---|
| `APP_TODAY` | 2026-10-26 | Fecha de negocio para plazos |
| `APP_MODEL` / `APP_EFFORT` | claude-opus-5-5 / medium | Modelo y nivel de esfuerzo |
| `APP_ENABLE_FALLBACKS` | true | Reintento server-side en otro modelo si el clasificador de seguridad rechaza |
| `APP_DISPUTE_TIMEOUT_S` | 5 | Espera máxima al legado antes de responder `pending_registration` |
| `APP_DISPUTE_SLOW_PROBABILITY` | 0.3 | Fracción de llamadas al legado que tardan 6–8 s |
| `APP_SESSION_BACKEND` | memory | `memory` o `sqlite` |

## Tests

```bash
uv run pytest                          # unitarios + orquestador con LLM simulado (sin clave)
uv run pytest tests/live -m live -v    # escenarios reales contra Claude (consume tokens; usa la clave de .env)
```

## Actualizar el reglamento

Agregar `docs/reglamento/reglamento_v<versión>.md` con front-matter `version`, `effective_date` y `thresholds`.
Se aplica la versión con la `effective_date` más reciente que no sea futura (al reiniciar). Los umbrales del
front-matter alimentan las validaciones obligatorias, así que deben coincidir con el texto.

## Pendiente para AWS

`infra/template.yaml` describe el despliegue (API Gateway, Lambda, DynamoDB, SQS, SNS, S3). Aún no existen los
adaptadores DynamoDB/S3 ni el worker `app.workers.dispute_worker`: en local, el hilo en segundo plano completa
los registros lentos; en Lambda ese trabajo debe ir a la cola SQS.
