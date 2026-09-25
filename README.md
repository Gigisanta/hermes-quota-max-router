<!-- maatwork-brand:maatwork-mw-20260901 -->
<p align="center"><img src="docs/brand/hermes-quota-max-router-cover.png" alt="hermes-quota-max-router · MaatWork" width="1200"></p>

> internal platform de MaatWork

# Hermes QuotaMax Router 1.0

Router editorial de costo **$0** para MaatWork. Sólo envía fuentes públicas y borradores editoriales a modelos de API gratuitos **verificados en la cuenta**. Si no hay capacidad, guarda el trabajo en SQLite y responde `202`; un worker lo reintenta. No hay respuestas simuladas, pago de emergencia ni fallback a la GPU local.

## Estado y alcance

El servicio arranca en `127.0.0.1:8123`. Arrancar no lo vuelve productivo: cada carga requiere adopción explícita tras el piloto y, en su primera petición productiva, **cuatro proveedores independientes** (tres de rotación y uno de reserva), dos opciones para autor/revisor y una cuota diaria total de al menos 2× el pico medido. La activación se registra en Redis persistente. Si después baja la reserva, el router avisa y sigue probando los proveedores gratuitos todavía aptos; encola al agotarse todos. Cada modelo necesita evidencia vigente de costo facturable cero en esa cuenta (tarifa cero o cupo Free con corte duro), ausencia de facturación, cuotas reales, smoke y evaluación editorial. Una entrada inválida se muestra en `/v1/router/status` pero no enruta. Ningún operador externo puede garantizar acceso gratuito perpetuo: la garantía local es cola duradera y reanudación cuando vuelve una ruta apta.

Al **25 de septiembre de 2026** hay cuatro altas MaatWork con credencial: Gemini,
SimpleLLM, Groq y Final Router. Las tres primeras pasaron pruebas de acceso
gratuito; Final Router permanece aislado por una restricción de modelos que no
se aplicó. **Todavía hay 0 de 4 proveedores admitidos por carga.** El
[inventario ordenado de modelos, cuotas y evidencia](docs/PROVIDER-STATUS.md#cuentas-de-maatwork-ordenadas-por-evidencia-editorial)
distingue lo probado de lo anunciado y de los candidatos sin alta. El orden
productivo se calculará por calidad editorial de cada carga y etapa cuando
exista una comparación suficiente; no se infiere del tamaño del modelo.

Freebuff **no** es un backend: [sus términos](https://freebuff.com/terms-of-service) limitan el uso gratuito a interacciones humanas en la app. GLM-5.3-Flash es [pago vía API](https://docs.z.ai/guides/overview/pricing). El catálogo de [Free-LLM](https://github.com/nejib1/Free-LLM) sirve para descubrir candidatos, nunca para autorizar precios o cuotas.

## Instalación local

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
# Exportar variables del almacén del proyecto antes de servir; no usar keys de otros proyectos.
.venv/bin/quotamax status
.venv/bin/quotamax serve
```

La aplicación no lee archivos `.env` por sí sola. Usá `~/.hermes/project-env/HerMaatOS/work/hermes-quota-max-router/` para las claves propias y exportalas al proceso de forma segura. `.env.example` enumera **nombres**, nunca valores. Necesita Redis local con AOF activado, `appendfsync always` y `maxmemory-policy noeviction`, una sola instancia de Uvicorn y espacio para `var/queue.sqlite3` (modo `0600`). Si Redis cae o pierde esas garantías, la cola sigue aceptando trabajos pero no hay inferencia: un reinicio no puede borrar reservas de cuota y habilitar consumo de más. No expongas `:8123` fuera de loopback.

### Servicio persistente en este Mac

Con los cambios revisados y confirmados en un commit, `scripts/install_service.py` instala un snapshot de ese commit en `~/.hermes/services/free-router/releases/<sha>`, crea un entorno Python propio y registra `com.hermaat.free-router` en launchd. Guarda la cola y el catálogo fuera del checkout. En la primera instalación crea tres tokens distintos de cliente y las rutas de estado en `~/.hermes/project-env/HerMaatOS/work/hermes-quota-max-router/.env` con modo `0600`; las claves de proveedores se agregan allí sólo después de cada alta legítima. La lectura del archivo no ejecuta shell y el plist no contiene secretos.

```sh
.venv/bin/python scripts/install_service.py install
curl -fsS http://127.0.0.1:8123/health
curl -fsS http://127.0.0.1:8123/v1/router/status
launchctl print gui/$(id -u)/com.hermaat.free-router
```

El instalador requiere Python 3.11.4 o posterior, Redis sano y el puerto libre. Comprueba que el proceso arrancó desde el commit instalado con los tres tokens de cliente y rutas de estado absolutas; revierte el plist si falla. Al actualizar conserva las claves existentes y agrega las rutas o tokens faltantes. Si un archivo privado anterior usa rutas relativas, se detiene para migrarlas explícitamente antes de mover la cola. En la primera instalación también rechaza un archivo previo que ya tenga cargas activas o modo piloto, para revisar esa adopción antes de arrancar el servicio. La instalación inicial escribe `ROUTER_PRODUCTION_WORKLOADS=` y `ROUTER_PILOT_MODE=0`: el proceso puede servir salud y déficit de reserva, pero ninguna carga editorial se considera adoptada. Tras superar **todas** las compuertas del piloto, actualizá la lista de cargas en el archivo privado y reiniciá con `launchctl kickstart -k gui/$(id -u)/com.hermaat.free-router`. El mismo comando de instalación despliega otro commit sin rotar los tokens existentes.

## Admisión de proveedores

1. Crear una única cuenta legítima por proveedor con MaatWork; no añadir tarjeta, crédito ni recarga automática. Verificar que las condiciones permiten API automatizada para contenido editorial público. [Gemini Free](https://ai.google.dev/gemini-api/terms) puede usar entradas y respuestas para mejorar productos y prohíbe enviar información sensible o confidencial: no se le envía chat, información personal ni borradores bajo embargo o confidenciales.
2. Guardar la clave en el almacén del proyecto. Registrar en `var/verified-models.json` cada modelo con costo facturable cero en la cuenta y URL oficial, cuota exacta por minuto/día, contexto, fecha de comprobación, smoke real, evaluación editorial y cargas autorizadas. Para cada carga y etapa autorizada, `quality_evaluations` exige casos aprobados/totales, fecha y SHA-256 del conjunto de evaluación; la muestra mínima es 60 casos de Journal, 50 de Simón y 10 de Cactus. Todos los modelos comparados en una carga y etapa deben usar el mismo conjunto **y la misma cantidad de casos**. La evidencia de calidad vence a los 30 días. El archivo de ejemplo es intencionalmente inválido. Para SimpleLLM, registrar además `rph` y `tph` comprobados en la cuenta: el router reserva atómicamente esas ventanas horarias y las incluye al calcular la capacidad diaria. Coordina una sola solicitud SimpleLLM en curso entre procesos mediante Redis, una cota conservadora mientras el límite efectivo de concurrencia de la cuenta no esté verificado. Para Cloudflare, añadir `daily_neurons` y las tasas oficiales `neurons_per_million_input/output`; no seleccionar modelos que requieran Workers Paid.
3. Registrar en `var/daily-peak.json` los picos diarios y la máxima reserva de tokens por petición medidos en el piloto (ver [docs/PILOT.md](docs/PILOT.md)). `quotamax status` muestra el margen de capacidad y los motivos que impiden activar cada carga. Las atestaciones vencen a los siete días: el servicio falla cerrado hasta renovarlas.
4. `quotamax audit` extrae nombres del directorio Free-LLM como entradas **sin verificar**, identifica nombres nuevos incluso después de una caída del directorio y muestra la lista curada en `config/provider-watchlist.json`. Esa lista indica el próximo paso, fuentes oficiales y revisiones vencidas; no suma capacidad. La auditoría reserva cuota para hacer una petición real mínima a cada modelo admitido y comprueba una afirmación editorial simple. El servicio la repite cada 24 horas. El reporte guarda sólo resultados, sin texto. Esa prueba de humo **no** sustituye la evaluación editorial ni confirma precio, cuota o facturación. Esos datos deben verificarse en las fuentes oficiales y en la cuenta antes de registrar cada modelo; la atestación vence a los siete días. El directorio Free-LLM nunca autoriza modelos por sí mismo, aun si muestra un precio gratuito.
5. Tras superar los siete días, la revisión editorial, USD 0 facturados y la reducción de GPU, agregar sólo las cargas aprobadas a `ROUTER_PRODUCTION_WORKLOADS` (separadas por comas) y reiniciar el servicio. La primera petición no piloto aún exige la reserva completa y registra su activación duradera. Quitar una carga de esa variable la desactiva sin borrar su cola. `/v1/router/status` distingue adopción/activación de salud actual de la reserva.

La cuota diaria se divide según los picos medidos de los tres proyectos, con al menos una cuarta parte reservada a Cactus por su plazo. La disponibilidad descuenta tanto solicitudes como tokens y, en Cloudflare, Neurons. Ante agotamiento, el próximo intento se programa para el reinicio de la ventana de cuota o el `Retry-After` aplicable.

Entre modelos admitidos, el router intenta primero el de mejor límite inferior de calidad editorial (Wilson 95 %) para la carga y etapa concretas; después usa la fracción de cuota restante para desempatar. Mantiene las rutas de reserva detrás de las activas y excluye el proveedor usado por el autor al elegir revisor. `/v1/router/status` muestra el orden de preferencia sin cuota en vivo, la brecha numérica de reserva y el backlog de candidatos. El orden de un intento puede cambiar por contexto, cuota, cooldown o exclusión del autor. Un puntaje alto no reemplaza los gates de calidad ni habilita por sí mismo el modelo.

El [inventario de proveedores](docs/PROVIDER-STATUS.md) separa las cuentas
registradas de candidatos sin acceso verificado. La cuarta ruta independiente
más prometedora es [Cloudflare Workers AI Free](https://developers.cloudflare.com/workers-ai/platform/pricing/), pendiente de alta MaatWork, token limitado y prueba real de sus 10.000 Neurons diarios sin facturación. [SiliconFlow](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation) requiere verificación de identidad; [Vikasit Nova](https://vikasit.ai/inference) aún tiene condiciones legales provisionales y acceso no probado. Final Router tiene cuenta y cupo gratuito, pero queda fuera del servicio mientras su guardrail permita un modelo excluido. Una clave guardada no equivale a admisión. [OpenRouter](https://openrouter.ai/terms) queda excluido por su cláusula 7(4) sobre desarrollar un servicio competidor; sólo se reconsidera con autorización escrita del operador. Créditos temporales, modelos con tarifa positiva fuera de un límite duro de plan Free y anuncios sin cuota de cuenta no suman reserva.

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

`200` incluye `router.provider`, `router.model`, `router.job_id`, `usage` y contenido real. Para la etapa `reviewer`, enviar `X-Maat-Author-Provider` y `X-Maat-Author-Job` con los valores efectivos de la respuesta del autor; el router consulta su trabajo completado, comprueba esa identidad y excluye el proveedor. `202` incluye `id`, `Retry-After` y `Location`; consultar `GET /v1/jobs/{id}` con el mismo token/carga. El resultado completado del trabajo también incluye `router.job_id`. `GET /health`, `/v1/models` y `/v1/router/status` muestran salud, modelos admitidos y déficit de reserva. `GET /v1/router/metrics` informa eventos agregados, cola y trabajos únicos aceptados por día UTC y carga (`daily_requests`), incluidos pendientes; cada fila informa `planned_token_samples` y `max_planned_tokens` sin contenido ni secretos. El servidor rechaza patrones evidentes de datos personales y peticiones mayores a 128 KiB, y los adaptadores deben admitir sólo fuentes públicas y borradores editoriales. La cola no registra contenido en logs, conserva pendientes hasta completarlos y purga resultados completados después de siete días.

Para una calibración no publicable con menos de cuatro proveedores, iniciar con `ROUTER_PILOT_MODE=1` y mandar `X-Maat-Pilot: true`. Nunca activar ese modo en una ruta de publicación. Al desactivar la variable, los trabajos piloto pendientes quedan suspendidos en cola aunque esa carga esté adoptada en producción. La publicación de cada proyecto mantiene sus propios gates.

## Verificación

El procedimiento de comparación de siete días y sus criterios de adopción están en [docs/PILOT.md](docs/PILOT.md). El estado de altas, modelos y cuotas comprobadas al 2026-09-25 está en [docs/PROVIDER-STATUS.md](docs/PROVIDER-STATUS.md).

```sh
make lint type-check test
```

Las pruebas usan Redis falso con Lua y HTTP simulado; no consumen cuota ni tocan producción. El código anterior se conserva sólo en historial Git. No usar sus scripts, catálogos, LiteLLM, Gradio ni endpoints de `:8088`.
