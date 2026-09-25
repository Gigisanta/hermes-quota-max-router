# Evaluación editorial Gemini Free: corte inicial del 25-09-2026

**Estado:** evaluación sin publicación ni admisión de modelos. La recomendación
de producción sigue **pendiente** del piloto de siete días, la comparación
editorial completa y cuatro proveedores independientes aptos por carga. El
router vivo conserva cero proveedores admitidos en Journal, Simón y Cactus; las
tres cargas figuran `ready=false`. `/v1/router/metrics` devuelve por ahora
`daily_requests=[]`: no hay una serie diaria real que permita estimar picos.

## Cuenta, condiciones y presupuesto

La consola de AI Studio de la cuenta MaatWork mostró el proyecto en nivel
**Free**, sin facturación configurada. Para `gemini-3.5-flash-lite` mostró 15
RPM, 250.000 TPM y 500 RPD; el tablero de 28 días registraba máximos de 14
RPM, 2.440 TPM y 66 RPD. Estos son datos de la cuenta observados hoy, no un
SLA ni una cuota universal. `gemini-3.1-flash-lite` aparecía con los mismos
límites nominales y compartía el proyecto; las peticiones a ambos modelos no
se cuentan como proveedores independientes. La [tabla oficial de
precios](https://ai.google.dev/gemini-api/docs/pricing) marca entrada y salida
Free para Flash-Lite; la búsqueda integrada no está disponible en Free. Las
[cuotas efectivas](https://ai.google.dev/gemini-api/docs/rate-limits) dependen
del proyecto. Según los [términos](https://ai.google.dev/gemini-api/terms), el
contenido del nivel Free puede usarse para mejorar productos de Google. Se
enviaron sólo fixtures ficticios y resúmenes de fuentes científicas públicas.

La clave se leyó del almacén privado del proyecto; ninguna clave figura en el
código ni en los reportes. Tras las pruebas se reemplazó la clave anterior del proyecto
por una nueva en el almacén privado (modo 0600). La nueva pasó `GET /v1beta/models`
con HTTP 200; AI Studio ya no muestra la anterior y Google Cloud Console
muestra la nueva restringida a **Gemini API**. No se comparó la cadena completa
del chat con la antigua.

## Conjuntos congelados y resultados

Las pruebas guardan sólo veredictos, latencia, uso y hashes; no publican ni
persisten los textos generados. Los reportes locales están en `var/evals/`
(directorio ignorado por Git). Los fallos HTTP, 429, 503 y timeout quedan en
la fila original y un `--resume` incrementa el historial de intentos. Ante
agotamiento o inestabilidad, el evaluador se detiene sin cambiar de modelo.

| Producto y etapa | Conjunto y hash SHA-256 | Gemini 3.5 Flash-Lite | Comparador | Interpretación |
| --- | --- | --- | --- | --- |
| Journal, revisor | 60 casos ficticios, `c5ba0b687443437f8fd73ba19d734a3af00a955924a6a80c3e4392bdbee0c65e` | 60/60 decisiones correctas, 0 errores API, mediana 1,29 s | SimpleLLM Gemma 4 E4B: 57/60 correctas con 3 respuestas incompletas a presupuesto inicial; esas 3 pasaron en otra corrida con 1.024 tokens, no es una comparación homogénea 60/60. Gemini 3.1 Lite: 3 correctas y 6 HTTP 503 en la corrida previa. | Candidato más sólido para **piloto de revisión ficticia**; no acredita textos reales ni sustitución productiva. |
| Journal, autor | Los mismos 60 temas ficticios | Primera corrida: 53/60 en esquema libre y 59/60 con JSON Schema, mediana 1,71 s. Esas cifras eran **sólo estructurales**. En repetición con gate de producto como criterio de aprobación: 13/14 respuestas estructurales, **0/13** pasaron `grounding`, dos intentos HTTP 503 (uno recuperado) y corrida detenida en el caso 14. | Gemma 4 E4B: 2/10 en un smoke, 8 sin `schema`; Gemini 3.1 Lite: primer caso HTTP 503. | **No apto**: el gate editorial falla en todas las respuestas válidas reexaminadas. La repetición completa de 60 queda pendiente por 503; no se suman éxitos de formato como aprobaciones editoriales. |
| Simón, autor | 50 DOI públicos distintos, intake `442f1c9ac0e64388dd4d9f4901998b0b46efb145d6b925d010aaec6b6b214ef9`; sólo 17 tenían al menos dos fuentes independientes verificadas y estudio principal | 17/17 respuestas API, **0/17** pasaron el gate del producto; 17 fallaron `uncertainty`, 2 además cita textual no presente, 1 cifra no sustentada. Mediana 3,18 s. | Sin corrida comparable de otro proveedor. | **No recomendar** para autor. Los otros 33 temas siguen sin evidencia suficiente, por lo que tampoco existe una muestra de 50 evaluable. |
| Simón, revisor técnico | Los mismos 17 temas con evidencia; cada entrada contiene una afirmación de cura deliberadamente falsa | Primera corrida: 17/17 rechazos, 0 errores API, mediana 1,36 s, pero el evaluador inicial contaba también rechazos sin motivo específico. Con la métrica corregida y fuentes públicas reconfirmadas en vivo, el primer caso obtuvo **dos HTTP 503** y la corrida quedó detenida. | Gemma 4 E4B: las primeras 3/17 respuestas fueron incompletas con el mismo límite de 1.000 tokens; se detuvo la prueba por fallos repetidos. | La cifra inicial **no** demuestra detección correcta de la afirmación falsa. Faltan controles positivos y revisión clínica; no hay aprobación clínica ni promoción. |
| Cactus, historial | 5 bundles completos en **4 semanas ISO distintas**: 02, 06, 13, 20 y 24-09-2026; inventario `55b3bfc07b8e937930762bc47e82e01ae175f04a4ea5523c83e3e2bcafdda4d6` | Aún no hay entradas históricas completas de noticias para repetir autor/revisor por modelo. | Verificador actual: 4/5 briefs pasaron, 17 noticias descartadas; el bundle publicado del 24-09 era determinista y falló con aviso crítico. | Sólo línea basal del producto; **no** atribuirla a Gemini. Faltan seis semanas reales para cumplir las diez históricas solicitadas. |
| Cactus, autor y corrector | Seis **escenarios ficticios** adicionales con snapshot y 12 noticias inventadas cada uno; hash `d5546f38f980666ae1110cce31795912734c188d6f7402c0c2bf69c68f5cce50` | Autor: 5/6 `verify()` válidos, 11 noticias descartadas y 23 avisos entre las cinco respuestas; el sexto caso acumuló **dos HTTP 503** y quedó pendiente. Corrector: **0/6** conservó todos los invariantes, 0 errores pendientes, un 503 recuperado; mediana 31,85 s. | Gemma 4 E4B: corrector 0/5 respuestas válidas; el sexto caso no se envió por cuota gratuita insuficiente. | **No apto** como par autor/revisor. Los casos ficticios amplían la calibración, pero **no** sustituyen las seis semanas reales faltantes ni constituyen una muestra homogénea de diez briefs históricos. |

Los fallos de `grounding` en Journal pueden depender de la brevedad de las
fuentes ficticias; el resultado sigue siendo un fallo real del gate usado. En
Simón, los 50 DOI se eligieron por orden de llegada y DOI, no por éxito del
modelo. La recolección de evidencia usa la función de producto `evidence_for`
con robots y límites por host. En Cactus, sólo se encontraron cinco pares
completos `snapshot.json`/`brief.json` en cuatro semanas ISO; otras páginas antiguas eran avisos de
retiro sin los datos de autor. No se generaron ediciones con fecha histórica
falsa.

## Decisión por etapa

| Carga | Autor | Revisor/corrector |
| --- | --- | --- |
| Journal | **No apto**: 0/13 respuestas válidas de la repetición pasaron `grounding`; el 59/60 inicial medía formato. | Flash-Lite es el mejor candidato medido para el **piloto ficticio** 60/60; falta comparación homogénea, fuentes reales y siete días. |
| Simón | **No apto**: 0/17 gate y 33/50 temas sin fuentes suficientes. | Resultado inicial de rechazo técnico insuficiente bajo el criterio corregido; **sin aprobación clínica**. |
| Cactus | Cinco respuestas válidas de autor pasan el gate con 11 noticias descartadas; falta sexta respuesta y reejecución histórica; **no apto**. | **No apto**: 0/6 conservaron los invariantes del router en los fixtures ficticios. |

Flash-Lite es el modelo Free de Gemini que mejor respondió **en estas pruebas**.
Gemini 3.1 Flash-Lite no mostró disponibilidad suficiente; `gemini-3.5-flash`
produjo dos HTTP 503 y un timeout en los smokes anteriores y no se presenta como
alternativa gratuita. Estas muestras no predicen disponibilidad continua.

## Repetir y continuar

Preparar el entorno de evaluación local con `httpx`, `pytest`, `ruff` y, para
los gates de producto, `feedparser`, `trafilatura`,
`lingua-language-detector`, `PyYAML` y `requests`. Obtener los secretos del
almacén privado, sin copiarlos a reportes. Ejecutar desde este repositorio:

```sh
.venv/bin/python scripts/prepare_editorial_suites.py --output var/evals/editorial-suite-YYYY-MM-DD.json
.venv/bin/python scripts/collect_simon_evidence.py --manifest var/evals/editorial-suite-YYYY-MM-DD.json --output var/evals/simon-evidence-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_simon_public.py --manifest var/evals/editorial-suite-YYYY-MM-DD.json --evidence var/evals/simon-evidence-YYYY-MM-DD.json --provider gemini --stage author --output var/evals/simon-author-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_simon_public.py --manifest var/evals/editorial-suite-YYYY-MM-DD.json --evidence var/evals/simon-evidence-YYYY-MM-DD.json --provider gemini --stage reviewer --output var/evals/simon-reviewer-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_editorial_author.py --provider gemini --structured --output var/evals/journal-author-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_free_reviewer.py --provider gemini --cases /Users/gigi/HerMaatOS/bin/newsblog/frozen_cases.json --output var/evals/journal-reviewer-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_cactus_synthetic.py --provider gemini --stage author --output var/evals/cactus-author-fictional-YYYY-MM-DD.json
.venv/bin/python scripts/evaluate_cactus_synthetic.py --provider gemini --stage reviewer --output var/evals/cactus-reviewer-fictional-YYYY-MM-DD.json
```

El verificador del historial de Cactus no llama modelos:
`.venv/bin/python scripts/check_cactus_historical.py --output ...`. Cada modelo debe usar el
mismo hash, mismo presupuesto de salida y misma etapa antes de comparar
calidad/latencia. Los smokes con 503 o un subconjunto de 10/17 casos **no** son
comparaciones completas.

El seguimiento diario del 26-09 al 02-10-2026 fue programado en esta tarea.
Si Flash-Lite recupera disponibilidad y la cuota observada lo permite, el
seguimiento reanudará sólo los casos pendientes de Journal autor v2, Cactus
autor v2 y Simón revisor v3, con el mismo modelo, prompt y hash. La corrida se
detendrá de nuevo ante 429, 503 o timeout y conservará todos los intentos.
El comando `.venv/bin/python scripts/record_editorial_pilot_day.py` guarda un
snapshot local por día y rechaza sobreescribirlo. Sus argumentos opcionales
`--daily-spend-usd` y `--gemini-used-rpd` sólo se pasan si se observó ese valor
en la consola ese día; la ausencia queda como `null`, nunca como cero. Debe
guardar fecha UTC, solicitudes previstas por carga, reserva máxima de
tokens, errores/reintentos, cuotas efectivas y gasto observado; si la consola
no está disponible, marcar `desconocido`. Tras siete días, contrastar uso real
con la cuota compartida y exigir USD 0 observado. La clave expuesta en el chat
ya se reemplazó y revocó. No tocar `ROUTER_PRODUCTION_WORKLOADS` ni relajar el gate de cuatro
proveedores independientes.
