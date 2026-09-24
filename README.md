<!-- maatwork-brand:maatwork-mw-20260901 -->
<p align="center"><img src="docs/brand/hermes-quota-max-router-cover.png" alt="hermes-quota-max-router · MaatWork" width="1200"></p>

> internal platform de MaatWork

# Hermes QuotaMax Router 1.0

Router editorial de costo **$0** para MaatWork. Sólo envía fuentes públicas y borradores editoriales a modelos de API gratuitos **verificados en la cuenta**. Si no hay capacidad, guarda el trabajo en SQLite y responde `202`; un worker lo reintenta. No hay respuestas simuladas, pago de emergencia ni fallback a la GPU local.

## Estado y alcance

El servicio arranca en `127.0.0.1:8123`. Arrancar no lo vuelve productivo: cada carga queda en cola hasta que tenga **cuatro proveedores independientes** (tres de rotación y uno de reserva), dos opciones para autor/revisor y una cuota diaria total de al menos 2× el pico medido. Cada modelo necesita evidencia vigente de precio cero, cuenta sin facturación, cuotas reales, smoke y evaluación editorial. Una entrada inválida se muestra en `/v1/router/status` pero no enruta.

Freebuff **no** es un backend: [sus términos](https://freebuff.com/terms-of-service) limitan el uso gratuito a interacciones humanas en la app. GLM-5.3-Flash es [pago vía API](https://docs.z.ai/guides/overview/pricing). El catálogo de [Free-LLM](https://github.com/nejib1/Free-LLM) sirve para descubrir candidatos, nunca para autorizar precios o cuotas.

## Instalación local

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
# Exportar variables del almacén del proyecto antes de servir; no usar keys de otros proyectos.
.venv/bin/quotamax status
.venv/bin/quotamax serve
```

La aplicación no lee archivos `.env` por sí sola. Usá `~/.hermes/project-env/HerMaatOS/work/hermes-quota-max-router/` para las claves propias y exportalas al proceso de forma segura. `.env.example` enumera **nombres**, nunca valores. Necesita Redis local, una sola instancia de Uvicorn y espacio para `var/queue.sqlite3` (modo `0600`). La cola puede seguir aceptando trabajos si Redis cae; no hace inferencia hasta que vuelva. No expongas `:8123` fuera de loopback.

## Admisión de proveedores

1. Crear una única cuenta legítima por proveedor con MaatWork; no añadir tarjeta, crédito ni recarga automática. Verificar que las condiciones permiten API automatizada para contenido editorial público. Gemini Free puede usar datos de los prompts para mejorar productos de Google; por eso no se envía chat ni información personal.
2. Guardar la clave en el almacén del proyecto. Registrar en `var/verified-models.json` cada modelo con precio cero y URL oficial, cuota exacta de esa cuenta por minuto/día, contexto, fecha de comprobación, smoke real, evaluación editorial y cargas autorizadas. El archivo de ejemplo es intencionalmente inválido. Para Cloudflare, añadir `daily_neurons` y las tasas oficiales `neurons_per_million_input/output`; no seleccionar modelos que requieran Workers Paid.
3. Registrar en `var/daily-peak.json` los picos diarios medidos de los pilotos. `quotamax status` muestra el margen de capacidad y los motivos que impiden activar cada carga. Las atestaciones vencen a los siete días: el servicio falla cerrado hasta renovarlas.
4. `quotamax audit` consulta el catálogo oficial de OpenRouter, registra nuevos `:free` a evaluar y retira los que dejan de figurar con precios cero. También reserva cuota y hace una petición real mínima a cada modelo admitido para comprobar acceso y una afirmación editorial simple; el reporte guarda sólo resultados, sin texto. El servicio repite la auditoría cada 24 horas. Esa prueba de humo **no** sustituye la evaluación de 60/50 casos ni confirma la facturación. Los demás proveedores necesitan una revisión de precio, cuota y cuenta por su consola si no exponen una señal verificable por API; la atestación vence a los siete días. El directorio Free-LLM queda como referencia, no como fuente de verdad.

La cuota diaria se divide según los picos medidos de los tres proyectos, con al menos una cuarta parte reservada a Cactus por su plazo. La disponibilidad descuenta tanto solicitudes como tokens y, en Cloudflare, Neurons. Ante agotamiento, el próximo intento se programa para el reinicio de la ventana de cuota o el `Retry-After` aplicable.

Proveedores candidatos independientes para las cargas editoriales: [Gemini Free](https://ai.google.dev/gemini-api/docs/pricing), [Groq Free](https://console.groq.com/docs/rate-limits), [Cloudflare Workers AI Free](https://developers.cloudflare.com/workers-ai/platform/pricing/) y [SiliconFlow Free](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation). SiliconFlow documenta modelos de tarifa cero y cuota fija por cuenta, pero exige verificación de identidad; su modelo, acceso y aptitud editorial siguen pendientes de comprobar con MaatWork. [OpenRouter `:free`](https://openrouter.ai/docs/guides/routing/model-variants/free) agrega capacidad, pero el gate no lo cuenta como proveedor independiente porque agrega operadores ajenos. Cada candidato debe pasar la verificación de cuenta, cuota y calidad por carga; figurar aquí no lo habilita. [Cerebras](https://www.cerebras.ai/inference) anuncia un crédito inicial de prueba y por eso no cuenta como cuarto proveedor. En Cloudflare el modelo tiene precio por Neuron fuera del cupo, pero el plan Free se detiene al agotarse: sólo cuenta el cupo de 10.000 Neurons diarios sin facturación y la cuota real que confirme la cuenta. La auditoría debe comprobar también que el modelo sigue disponible en Workers Free. Créditos temporales no cuentan como capacidad permanente.

[Z.ai](https://docs.z.ai/legal-agreement/terms-of-use) queda excluido de estas cargas porque sus términos incluyen una restricción sobre *news reporting* e inversión. [Mistral Free](https://docs.mistral.ai/admin/billing-usage/subscriptions) proporciona un uso mensual incluido que se consume contra precios por modelo; no se admite como tarifa cero. [SambaNova Free](https://cloud.sambanova.ai/plans) exige método de pago y compra de créditos para las primeras peticiones, así que tampoco cuenta como reserva gratuita. Si cambian los términos o el precio, se vuelve a evaluar antes de habilitarlos.

## API

Cada proyecto tiene su token de servidor `ROUTER_TOKEN_JOURNAL`, `ROUTER_TOKEN_SIMON_NEWS` o `ROUTER_TOKEN_CACTUS_BRIEF`. El cliente usa el mismo valor como Bearer. Sólo se acepta `model: "auto"`, texto y respuestas no streaming.

```sh
curl http://127.0.0.1:8123/v1/chat/completions \
  -H "Authorization: Bearer $MAAT_FREE_ROUTER_TOKEN" \
  -H 'X-Maat-Workload: journal' \
  -H 'X-Maat-Stage: author' \
  -H 'X-Maat-Data-Class: public_editorial' \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Resumí esta fuente pública: ..."}],"max_tokens":500}'
```

`200` incluye `router.provider`, `router.model`, `router.job_id`, `usage` y contenido real. Para la etapa `reviewer`, enviar `X-Maat-Author-Provider` y `X-Maat-Author-Job` con los valores efectivos de la respuesta del autor; el router consulta su trabajo completado, comprueba esa identidad y excluye el proveedor. `202` incluye `id`, `Retry-After` y `Location`; consultar `GET /v1/jobs/{id}` con el mismo token/carga. El resultado completado del trabajo también incluye `router.job_id`. `GET /health`, `/v1/models` y `/v1/router/status` muestran salud, modelos admitidos y déficit de reserva. El servidor rechaza patrones evidentes de datos personales y peticiones mayores a 128 KiB, y los adaptadores deben admitir sólo fuentes públicas y borradores editoriales. La cola no registra contenido en logs, conserva pendientes hasta completarlos y purga resultados completados después de siete días.

Para una calibración no publicable con menos de cuatro proveedores, iniciar con `ROUTER_PILOT_MODE=1` y mandar `X-Maat-Pilot: true`. Nunca activar ese modo en una ruta de publicación. La publicación de cada proyecto mantiene sus propios gates.

## Verificación

El procedimiento de comparación de siete días y sus criterios de adopción están en [docs/PILOT.md](docs/PILOT.md). El estado de altas y reserva al 2026-09-24 está en [docs/PROVIDER-STATUS.md](docs/PROVIDER-STATUS.md).

```sh
make lint type-check test
```

Las pruebas usan Redis falso con Lua y HTTP simulado; no consumen cuota ni tocan producción. El código anterior se conserva sólo en historial Git. No usar sus scripts, catálogos, LiteLLM, Gradio ni endpoints de `:8088`.
