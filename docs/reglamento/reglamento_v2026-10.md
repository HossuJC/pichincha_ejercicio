---
version: "2026-10"
effective_date: "2026-10-01"
owner: "Área Legal"
# Parámetros que el sistema aplica de forma determinística. Deben coincidir con el texto.
thresholds:
  max_days_to_dispute: 60
  review_amount_usd: 500
  recent_disputes_window_days: 30
  max_recent_disputes: 3
---

# Reglamento de reclamos de tarjeta de crédito

## 1. Plazo máximo para reclamar

El plazo máximo para presentar un reclamo es de 60 días calendario contados desde la fecha del consumo.
Si han transcurrido más de 60 días, el reclamo no procede.

## 2. Transacciones pendientes

No proceden reclamos de transacciones en estado pending (pendiente). El cliente puede volver a
reclamar cuando la transacción se haya registrado (estado posted), siempre dentro del plazo.

## 3. Montos mayores a USD 500

Los reclamos por montos mayores a USD 500 requieren revisión de un asesor antes de registrarse.

## 4. Frecuencia de reclamos

Un cliente con más de 3 reclamos en los últimos 30 días requiere revisión de un asesor.

## 5. Cargos recurrentes de suscripciones

En cargos recurrentes de suscripciones, el cliente debe haber solicitado la cancelación al comercio
antes del cobro. Si no hay evidencia de la solicitud de cancelación, el reclamo no procede y se orienta
al cliente a cancelar la suscripción directamente con el comercio.
