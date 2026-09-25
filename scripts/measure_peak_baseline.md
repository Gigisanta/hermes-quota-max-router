# Medición basal de pico

`measure_peak_baseline.py` genera un reporte de evidencia para una ventana de
siete días completos UTC. Sólo abre explícitamente los SQLite de Journal y
Simón, en modo `mode=ro`, activa `query_only` y autoriza consultas de lectura.
No busca bases automáticamente, no abre secretos y no lee prompts, respuestas,
payloads editoriales, titulares ni identificadores de filas.

## Fuentes y límites

- **Journal:** cuenta filas de `model_runs` con `role` `author` o `reviewer`,
  agrupadas por el epoch `created_ts` en UTC. También reporta el máximo observado
  diario de requests como **límite inferior**, el máximo observado de
  `total_tokens` por fila y la proporción de filas presentes con ese campo
  registrado. El writer actual registra telemetría en best effort, por lo que
  siete días con filas no prueban captura completa de intentos: Journal siempre
  queda `insufficient_evidence`, `peak_comparable=false` y `peak_requests=null`.
- **Simón:** usa el nuevo `model_attempt` persistido antes de cada clasificación
  o redacción, incluso si la llamada falla o se interrumpe. Una clasificación
  con techo de 200 tokens indica una llamada prevista; una redacción con techo
  acumulado de 3500 indica autor y con 4500 indica autor y revisor. Una fila de redacción
  con techo cero aún no llegó al modelo. `candidate.attempted_at` se informa
  sólo como marcador histórico y nunca se suma a la cuenta nueva. En bases
  anteriores sin `model_attempt`, la herramienta vuelve al contador heredado,
  que omite los fallos previos a `write_draft`. Los techos se escriben antes de
  invocar el modelo: el cálculo nuevo representa **llamadas planificadas**, no
  llamadas completadas ni una cota matemática de las llamadas reales. Tampoco
  demuestra la cobertura diaria del scheduler ni guarda tokens consumidos o
  bytes de entrada; por eso todavía no produce un pico promocionable.
- **Cactus:** el generador mantiene un audit por edición semanal en bundles de
  archivos. La herramienta no lee esos archivos ni distribuye una auditoría
  semanal entre siete fechas. No se inspeccionó ninguna base de datos de Cactus;
  el reporte deja su medición diaria y de tokens como evidencia insuficiente.

Los días sin filas no se convierten en ceros demostrados: se reportan como
ausentes porque estos logs no prueban por sí solos que el flujo estuvo cubierto
y sin actividad. El máximo diario de Journal cuenta sólo eventos persistidos y
es una cota inferior, nunca un pico promocionable. `total_tokens` observado
describe uso real reportado, no la reserva requerida para el router. No se emite
`tokens_per_request`: faltan los bytes UTF-8 de cada mensaje, el margen de 64
bytes por mensaje y `max_tokens` solicitado en cada request.

## Uso

El rango es inclusivo/exclusivo y debe contener exactamente siete días UTC ya
finalizados. Se pasan rutas manualmente para evitar acceder a datos vivos por
descubrimiento implícito.

```sh
python3 scripts/measure_peak_baseline.py \
  --journal-db /path/to/read-only-journal-copy.sqlite3 \
  --simon-db /path/to/read-only-simon-copy.sqlite3 \
  --start-date 2026-09-14 \
  --end-date 2026-09-21
```

La salida es JSON en stdout. Exit code `0` significa evidencia válida para las
tres cargas; `2` significa `insufficient_evidence`. `--write-daily-peak` intenta
escribir el esquema entero existente en `var/daily-peak.json` sólo cuando los
tres picos diarios pasan cobertura, comparabilidad y semántica. El reporte de
evidencia insuficiente no crea el directorio ni el archivo. Con las fuentes
actuales, Journal no supera la compuerta porque `model_runs` es best effort, y
por tanto ningún `daily-peak.json` se produce. El reporte de tokens observados
nunca se copia a ese archivo.

Las filas SQLite usadas en los tests son fixtures sintéticos para comprobar
agregación, límites UTC y rechazo seguro. No describen actividad real de
Journal, Simón o Cactus.
