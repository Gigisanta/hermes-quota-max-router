# Cambios

## 1.0.0 — reconstrucción editorial

- Enrutamiento estricto a proveedores gratuitos verificados por cuenta y carga editorial.
- Reserva atómica de cuotas en Redis, cola persistente en SQLite y respuesta `202` ante agotamiento.
- API local con autor y revisor en proveedores distintos; controles de clasificación pública.
- Auditoría de catálogo y candidatos sin alta automática de modelos sin evaluación.
- Eliminación del servidor, dashboard, cuotas supuestas y fallbacks del prototipo anterior. El historial Git conserva sus versiones previas.
