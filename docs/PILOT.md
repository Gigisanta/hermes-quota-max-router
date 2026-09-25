# Piloto editorial sin publicación

El router y los tres adaptadores arrancan desactivados. Este piloto dura **siete días consecutivos** y no publica contenido. Si hay menos de cuatro proveedores independientes aptos para una carga, la prueba puede usar `ROUTER_PILOT_MODE=1` para medirla sin cambiar su ruta principal; sus resultados no satisfacen la compuerta de producción.

## Preparación

1. Verificá una sola cuenta MaatWork por proveedor, sin tarjeta ni facturación, y registrá tarifa cero, condiciones de API, cuotas exactas, fecha, modelo y cargas autorizadas en el catálogo local. Ningún crédito de prueba cuenta como capacidad estable. Guardá claves sólo en el almacén del proyecto.
2. Medí el pico diario de solicitudes de Journal, Simón y Cactus durante siete días de ejecución actual. Para cada carga, registrá en `var/daily-peak.json` `{"requests": <pico diario>, "tokens_per_request": <máxima reserva observada>}`. La reserva es la suma de bytes UTF-8 de los mensajes más 64 por mensaje más `max_tokens` solicitado; `usage.total_tokens` solo no la sustituye. No inventes esta cifra si el flujo actual no la registra: instrumentá el piloto y esperá a reunir la muestra. Durante el piloto, `/v1/router/metrics` expone `daily_requests`: trabajos únicos aceptados por día UTC y carga, incluidos los que siguen en cola, con `planned_token_samples` y `max_planned_tokens`; los reintentos idempotentes no suman otra vez. Exigí que `planned_token_samples` cubra todas las solicitudes antes de utilizar el máximo, y compará la serie con el pico del flujo actual. `quotamax status` debe mostrar tres proveedores activos, uno de reserva y capacidad gratuita de al menos el doble del pico de cada carga y del total compartido. Un entero antiguo para el pico sigue siendo aceptado, pero calcula capacidad con el contexto completo del modelo y puede subestimar las peticiones realmente posibles; no es sustituto del presupuesto medido para promoción.

   La herramienta de [medición basal](../scripts/measure_peak_baseline.md) consulta los SQLite actuales en modo lectura y muestra los eventos observados. Journal registra intentos en best effort, Simón anota algunos intentos después de generar y Cactus mantiene una auditoría semanal; por eso esos contadores históricos son límites inferiores y la herramienta no emite `daily-peak.json` ni los trata como siete días completos de demanda.
3. Ejecutá `quotamax audit` y conservá sus reportes `var/discovery.json` y `var/access-audit.json`. El smoke diario prueba acceso y una afirmación mínima; no acredita calidad general ni facturación. Revisá precio y cuotas en las consolas oficiales cada día y renová las atestaciones sólo con evidencia real.

   Para preparar una reserva nueva, revisá `config/provider-watchlist.json` y `candidates`/`new_directory_entries` en `var/discovery.json`. Priorizá operadores independientes con alta y uso editorial permitidos. Confirmá en fuentes oficiales y en la cuenta tarifa cero, límites, ausencia de facturación y modelo exacto; probá acceso con una petición pública mínima y completá los casos editoriales. Sólo entonces agregá el modelo a `var/verified-models.json`. Las entradas del directorio y la lista de candidatos no se promueven automáticamente. El router recarga las atestaciones admitidas en cada intento, sin reinicio, y retira al vencerlas. Actualizá la lista curada tras cada investigación para que el próximo déficit muestre un paso concreto.

## Casos y medidas

| Carga | Muestra sin publicación | Control editorial |
| --- | --- | --- |
| Journal | Los 60 casos de `bin/newsblog/frozen_cases.json` del repo HerMaatOS, más corridas de autor sobre fuentes públicas | Citas exactas, esquema, grounding y decisión del revisor frente a `expected_approved`; ningún error nuevo |
| Simón | 50 temas reales distintos de la cola de ciencia, sin selección favorable | Evidencia y revisión clínica humana; cero errores graves, afirmaciones sustentadas y comparación a ciegas conforme al gate de Simón |
| Cactus | Briefs históricos con fixtures de mercado y cotizaciones vigentes de prueba | Cifras, procedencia y vigencia; ninguna omisión de historias/temas por el revisor; fallback determinista sólo tras su QA |

Las evaluaciones de cada modelo usan el mismo conjunto y cantidad de casos por carga y etapa; registrá su SHA-256, aprobados/totales y fecha en `quality_evaluations`. El router compara modelos con un límite inferior conservador de calidad y deja la reserva detrás de los activos. Para Cactus se exige al menos 10 briefs de calibración, además de sus controles de cifras, procedencia y vigencia. Estos campos no sustituyen la revisión humana ni el período de siete días.

Para la comparación sintética del revisor de Journal, cargá la clave verificada
en el entorno del proceso y ejecutá, desde este repo:

```sh
.venv/bin/python scripts/evaluate_free_reviewer.py \
  --provider gemini \
  --cases /Users/gigi/HerMaatOS/bin/newsblog/frozen_cases.json \
  --output var/evals/journal-gemini-YYYY-MM-DD.json
```

`--provider simplellm` usa su modelo gratuito exacto. El script fija el hash de
los 60 casos sintéticos, conserva un checkpoint sin contenido ni claves, y
rechaza reutilizar un reporte sin `--resume`. Esta opción vuelve a intentar los
casos con error y sustituye su fila anterior. Para investigar una respuesta
incompleta, `--case-id <ID>` selecciona únicamente ese caso y permite
`--max-tokens 1024` en otro reporte. SimpleLLM consulta su cuota vigente antes
de cada petición. Ambos proveedores usan un lock local por cuenta entre
evaluaciones del mismo Mac. Otras
aplicaciones de la misma cuenta podrían consumir cuota entre la consulta y la
petición; esta comparación se ejecuta sin tráfico productivo concurrente. Sus
resultados son sólo un filtro
de candidatos: también hacen falta autor, citas, esquemas, artículos reales,
calibración por carga y el período completo de siete días.

Guardá por día y carga: fecha UTC, cantidad de casos, aprobados/rechazados, errores editoriales, solicitudes en cola/reanudadas, proveedor/modelo efectivos, segundos de GPU del flujo actual y del flujo con router, y monto facturado en cada cuenta. No guardes prompts, respuestas, secretos ni datos personales en métricas del router. Los artefactos editoriales del proyecto permanecen bajo sus propios controles.

## Criterio de salida

El piloto pasa sólo si durante los siete días no hay regresión editorial, los extractos de facturación de **todas** las cuentas verifican **USD 0**, y el tiempo de GPU de estas síntesis baja al menos **50 %** frente al período comparable. Además, `quotamax status` debe mostrar cuatro proveedores independientes aptos por carga (tres activos y uno de reserva), cuota conjunta de al menos 2× el pico medido, y autor/revisor capaces de usar proveedores distintos. Si una condición falla, no agregues esa carga a `ROUTER_PRODUCTION_WORKLOADS`; sus trabajos permanecen en cola. Tras la primera activación con reserva completa, los déficits posteriores generan aviso y el router sigue usando las rutas gratuitas aptas. Si ninguna queda, encola hasta recuperarse. No se habilita fallback pago ni local.
