# Piloto editorial sin publicación

El router y los tres adaptadores arrancan desactivados. Este piloto dura **siete días consecutivos** y no publica contenido. Si hay menos de cuatro proveedores independientes aptos para una carga, la prueba puede usar `ROUTER_PILOT_MODE=1` para medirla sin cambiar su ruta principal; sus resultados no satisfacen la compuerta de producción.

## Preparación

1. Verificá una sola cuenta MaatWork por proveedor, sin tarjeta ni facturación, y registrá tarifa cero, condiciones de API, cuotas exactas, fecha, modelo y cargas autorizadas en el catálogo local. Ningún crédito de prueba cuenta como capacidad estable. Guardá claves sólo en el almacén del proyecto.
2. Medí el pico diario de solicitudes de Journal, Simón y Cactus durante siete días de ejecución actual. Registrá los tres números positivos en `var/daily-peak.json`. Durante el piloto, `/v1/router/metrics` expone `daily_requests`: trabajos únicos aceptados por día UTC y carga, incluidos los que siguen en cola; los reintentos idempotentes no suman otra vez. Compará esa serie con el pico del flujo actual antes de actualizarlo. `quotamax status` debe mostrar tres proveedores activos, uno de reserva y capacidad gratuita de al menos el doble del pico de cada carga y del total compartido.
3. Ejecutá `quotamax audit` y conservá sus reportes `var/discovery.json` y `var/access-audit.json`. El smoke diario prueba acceso y una afirmación mínima; no acredita calidad general ni facturación. Revisá precio y cuotas en las consolas oficiales cada día y renová las atestaciones sólo con evidencia real.

## Casos y medidas

| Carga | Muestra sin publicación | Control editorial |
| --- | --- | --- |
| Journal | Los 60 casos de `bin/newsblog/frozen_cases.json` del repo HerMaatOS, más corridas de autor sobre fuentes públicas | Citas exactas, esquema, grounding y decisión del revisor frente a `expected_approved`; ningún error nuevo |
| Simón | 50 temas reales distintos de la cola de ciencia, sin selección favorable | Evidencia y revisión clínica humana; cero errores graves, afirmaciones sustentadas y comparación a ciegas conforme al gate de Simón |
| Cactus | Briefs históricos con fixtures de mercado y cotizaciones vigentes de prueba | Cifras, procedencia y vigencia; ninguna omisión de historias/temas por el revisor; fallback determinista sólo tras su QA |

Guardá por día y carga: fecha UTC, cantidad de casos, aprobados/rechazados, errores editoriales, solicitudes en cola/reanudadas, proveedor/modelo efectivos, segundos de GPU del flujo actual y del flujo con router, y monto facturado en cada cuenta. No guardes prompts, respuestas, secretos ni datos personales en métricas del router. Los artefactos editoriales del proyecto permanecen bajo sus propios controles.

## Criterio de salida

El piloto pasa sólo si durante los siete días no hay regresión editorial, los extractos de facturación de **todas** las cuentas verifican **USD 0**, y el tiempo de GPU de estas síntesis baja al menos **50 %** frente al período comparable. Además, `quotamax status` debe mostrar cuatro proveedores independientes aptos por carga (tres activos y uno de reserva), cuota conjunta de al menos 2× el pico medido, y autor/revisor capaces de usar proveedores distintos. Si una condición falla, no agregues esa carga a `ROUTER_PRODUCTION_WORKLOADS`; sus trabajos permanecen en cola. Tras la primera activación con reserva completa, los déficits posteriores generan aviso y el router sigue usando las rutas gratuitas aptas. Si ninguna queda, encola hasta recuperarse. No se habilita fallback pago ni local.
