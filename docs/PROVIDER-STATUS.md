# Estado de alta y reserva — 2026-09-25

`quotamax status` en el checkout local informó Redis disponible y persistente,
**0 modelos
verificados, 0 proveedores aptos y 0 de reserva** para Journal, Simón y Cactus.
Faltan `var/verified-models.json` y los picos medidos de las tres cargas. El
router no está habilitado como ruta principal ni se inició un piloto con APIs.
Redis está iniciado como servicio de usuario con AOF, `appendfsync always` y
`noeviction`; el router vuelve a comprobar esas garantías antes y después de cada
reserva. Si alguna desaparece, cierra todas las rutas remotas.

| Proveedor | Evidencia revisada | Falta para contar capacidad |
| --- | --- | --- |
| Gemini Free | MaatWork activó la verificación en dos pasos y autorizó una clave dedicada. El 25 de septiembre, AI Studio creó `MaatWork Free Editorial Router` en `maatworkyoutube`, nivel **Free**, sin facturación; Google Cloud la muestra vinculada a una cuenta de servicio y restringida a **Gemini API**. Se guardó sólo en el almacén local. La [tabla oficial de precios](https://ai.google.dev/gemini-api/docs/pricing) marca entrada y salida de `gemini-3.5-flash-lite` a tarifa cero en Free. La API listó el modelo y dos pruebas sintéticas por la ruta compatible con OpenAI devolvieron HTTP 200, modelo efectivo exacto, frase fiel y `finish_reason=stop`, incluso con el presupuesto de auditoría de 48 tokens. El tablero de esa cuenta mostró **15 RPM, 250.000 TPM y 500 RPD** para ese modelo; «Spend» indicó que el proyecto no tiene facturación configurada. `gemini-2.5-flash-lite` aparece todavía en el listado pero rechazó generación para nuevos usuarios (404); `gemini-3.1-flash-lite` devolvió 503 por demanda y no se considera disponible. `gemini-3.5-flash` no completó el smoke con 48 tokens y después respondió 503. | Evaluación editorial por carga, observación de disponibilidad y reinicio de cuotas, y piloto de siete días. Los límites son por proyecto, no por clave. El smoke y la ausencia de facturación no acreditan calidad ni capacidad diaria para las tres cargas. Sigue deshabilitado para producción y no suma a los cuatro proveedores aptos. |
| Groq Free | MaatWork (`maatwork.comercial@gmail.com`) usa la organización Personal y el proyecto Default. El 25 de septiembre, la consola mostró **Free, $0, plan actual**. «Usage» mostró USD 0,00 y aclara que cualquier costo calculado es hipotético mientras no se cambie al plan pago. Para `openai/gpt-oss-120b`, `openai/gpt-oss-20b` y `qwen/qwen3.8-27b` mostró 30 RPM, 1.000 RPD, 8.000 TPM y 200.000 TPD; los límites son por organización y modelo, no por clave. La clave provista por el titular se guardó sólo en el almacén local. Con el cliente HTTP del router (`httpx`, sin proxy), `GET /models` listó los tres modelos y una generación pública mínima con `openai/gpt-oss-120b` devolvió HTTP 200, modelo exacto, `finish_reason=stop` y el hecho esperado; las cabeceras confirmaron 1.000 RPD y 8.000 TPM. Una prueba inicial con `urllib` devolvió 403 de Cloudflare (1010) por la firma del cliente; no se usó para juzgar la clave. [Límites de Groq](https://console.groq.com/docs/rate-limits). | Evaluar autor y revisor por carga y medir siete días de capacidad, calidad y facturación antes de admitirlo. El smoke no acredita calidad editorial ni reserva productiva. |
| Novita AI | La [página oficial de Ling 3.0 Flash Fin](https://novita.ai/models/model-detail/inclusionai-ling-3.0-flash-fin) muestra $0 por millón de tokens de entrada y salida y API compatible con OpenAI. Sin embargo, el [catálogo actual](https://novita.ai/models) marca **TIME LIMITED FREE** para Ling 3.0 Flash Fin y Sante; no pueden contar como reserva permanente. Ling 3.0 Flash VL aparece con [precio positivo](https://novita.ai/pricing/). La [guía oficial](https://blogs.novita.ai/es/ling-3-0-flash-fin-on-novita-ai/) menciona 30 RPM como límite de catálogo, sujeto a la cuenta. Su [AUP §5(6)](https://novita.ai/legal/acceptable-use-policy) restringe usos para competir con Novita: sólo se evalúa como cliente interno editorial, nunca como servicio de enrutamiento ofrecido a terceros. | Alta de MaatWork pendiente de consentimiento de términos. Puede servir para evaluación temporal, pero no se suma a las cuatro rutas permanentes mientras la gratuidad tenga plazo; también faltan cuotas de cuenta, clave, llamada real y calidad. |
| Cloudflare Workers AI Free | [10.000 Neurons diarios en Workers Free](https://developers.cloudflare.com/workers-ai/platform/pricing/); se corta al llegar al cupo. La misma tabla identifica GLM 5.3 Flash y otros modelos como exclusivos de Workers Paid o créditos prepagos; el catálogo del router los rechaza. El [changelog oficial](https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/) identifica `@cf/zai-org/glm-4.7-flash` y `@cf/google/gemma-4-26b-a4b-it` como disponibles en Workers Free. | Acceso de MaatWork, token acotado, tasa de Neurons, smoke real y calidad editorial de esos modelos. |
| Cerebras Free Trial | La [página comercial vigente](https://www.cerebras.ai/inference) ofrece **USD 5 de crédito inicial**, no una tarifa cero estable; la [documentación de precios antigua](https://inference-docs.cerebras.ai/support/pricing) todavía describe un nivel Free $0 y no se usa como prueba de capacidad actual. Los [términos](https://www.cerebras.ai/terms-of-service) restringen productos competidores y benchmarking. | Excluido de las cuatro rutas permanentes. No vale la pena aceptar términos ni crear una clave para capacidad que desaparece al agotar el crédito. |
| SiliconFlow Free | [Modelos de precio cero y límites por cuenta](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation). La pantalla de alta ofrece teléfono, correo con código y WeChat; no apareció inicio con Google. Su [política de privacidad](https://docs.siliconflow.cn/docs/legals/privacy-policy) exige verificación de identidad real para usar la API: para personas, nombre, documento y validación facial por Alipay; para empresas, datos del representante y validación facial o transferencia bancaria empresarial. | El titular tendría que evaluar y completar esa verificación; no se transmitirán documentos ni datos biométricos mediante este trabajo. Después faltarían la cuota exacta y un modelo de calidad suficiente. No se cuenta como reserva. |
| SimpleLLM Free | MaatWork completó el alta con contraseña propia. El 25 de septiembre, el [catálogo público oficial](https://simplellm.eu/docs/models.html) y su API marcaron `gemma-4-E4B` disponible, con entrada y salida a **0 SC** y contexto de 131.072 tokens. Llama 8B, Mistral 7B y Kode 14B aparecieron a 0 SC pero `planned`, y no se cuentan. Hay una clave dedicada guardada sólo en el almacén local del router. `GET /v1/rate-limit` devolvió 1.000 RPM generales, 100 solicitudes/hora y 1.000/día, 50.000 tokens/hora y 500.000/día para modelos gratuitos. Tres llamadas sintéticas a `gemma-4-E4B` devolvieron HTTP 200; una con 120 tokens de salida no produjo texto, y otra con 512 produjo un resumen fiel. `GET /v1/usage` y el uso por clave registraron 3 solicitudes, **0 SC gastados** y saldo 0 SC. El router reserva las ventanas horarias y limita a una solicitud SimpleLLM simultánea. [Documentación de cuotas](https://simplellm.eu/docs/limits.html). | Falta comprobar vigencia y reinicio real de ventanas, evaluar los casos editoriales de cada carga y verificar que el modelo pequeño puede servir como autor o revisor. El smoke sintético y el saldo 0 SC no acreditan calidad ni el piloto de siete días. Sigue deshabilitado para producción y no suma a los cuatro proveedores aptos. |
| Mistral Free | La cuenta Google de MaatWork ya es reconocida por AI Studio. Su [modo Free](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key) permite API sin tarjeta y su [plan actual](https://mistral.ai/pricing/) anuncia USD 10/mes de créditos, consumidos contra precios por modelo. [Mistral lo destina a evaluación y prototipado](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them). La consola exige aceptar sus términos antes de mostrar límites. | Consentimiento pendiente; no cuenta como tarifa cero por modelo ni como reserva productiva sostenida. |
| OpenRouter `:free` | Sus [términos, §7(4)](https://openrouter.ai/terms) prohíben usar el servicio para desarrollar uno competidor. | Excluido del registro y de la auditoría automática; no se abrió cuenta. Sólo reconsiderar con autorización escrita específica del operador. |
| Final Router Free | La [página oficial](https://finalrouter.com/free-models) anuncia 25 solicitudes diarias renovables, sin tarjeta, para `openai/gpt-5-mini` y `deepseek/deepseek-v4-flash`; la [tabla de precios](https://finalrouter.com/pricing) indica 20 RPM y HTTP 402 al agotar el cupo. El 25 de septiembre se guardó una clave de evaluación sólo en el almacén local: una generación pública mínima con `openai/gpt-5-mini` devolvió HTTP 200, modelo efectivo exacto, `finish_reason=stop`, costo reportado de 0 centavos y sin fallback. La consola mostraba saldo USD 0. Se creó un guardrail asignado a esa misma clave Default con presupuesto USD 0/mes, lista cerrada que sólo permite GPT-5 mini, exclusión de proveedores que entrenan con prompts y bloqueo de datos sensibles. **La restricción no funcionó en la API:** dos llamadas sintéticas explícitas a `deepseek/deepseek-v4-flash` devolvieron HTTP 200, modelo efectivo DeepSeek; una figura como exitosa en el registro de la consola con la misma clave. Después de crear el guardrail, `GET /api/v1/models` con esa clave siguió listando ambos modelos, pese a que la [referencia de API](https://finalrouter.com/api-reference) indica que una solicitud bloqueada debe devolver 403. Quedaban 22 solicitudes gratuitas, sin crédito pago. No se envió ningún borrador editorial. Sus [términos](https://finalrouter.com/terms) prohíben revender acceso bruto y obtener por scraping un catálogo competidor. | **No usar para datos editoriales ni contar como reserva.** Investigar el fallo del guardrail y exigir una prueba negativa HTTP 403 para DeepSeek antes de considerar el acceso seguro. Luego verificar el proveedor efectivo, la calidad y si 25 solicitudes diarias cubren la reserva de 2× el pico medido. El saldo cero no demuestra que el guardrail de gasto funcione. |

## Comparación sintética de revisión de Journal

El 25 de septiembre se ejecutó el mismo prompt de revisor de Journal contra los
60 casos ficticios de `bin/newsblog/frozen_cases.json` (SHA-256
`c5ba0b687443437f8fd73ba19d734a3af00a955924a6a80c3e4392bdbee0c65e`).
El cliente de evaluación fija modelo y endpoint, no permite fallback y sólo
guarda ID de caso, decisión, error, latencia y tokens en `var/evals/` local.

| Modelo | Casos correctos | Errores | Mediana por llamada | Tokens observados |
| --- | ---: | ---: | ---: | ---: |
| Gemini `gemini-3.5-flash-lite` | 60/60 | 0 | 1,29 s | 9.053 entrada + 1.972 salida |
| SimpleLLM `gemma-4-E4B` | 57/60 totales; 57/57 juzgados correctos | 3 respuestas incompletas con techo de 512 tokens | 7,61 s | 9.399 entrada + 24.010 salida en las 57 respuestas completas |

Los tres casos incompletos (`ia-004`, `tech-008`, `ia-007`) se repitieron por
separado con un techo de 1.024 tokens: **3/3 correctos, 0 errores**, 494 tokens
de entrada y 1.854 de salida. Entre ambas corridas, los 60 casos obtuvieron un
veredicto correcto, pero el modelo necesitó más margen de salida en tres y aún
no se probó el flujo completo de artículos reales. El evaluador comprueba las
cuotas disponibles de SimpleLLM antes de cada llamada y suspende la corrida si
no puede acreditar capacidad gratuita. Después de la repetición, `/v1/usage`
seguía mostrando saldo **0 SC** y gasto total **0 SC**. La cuota horaria tenía
32 solicitudes y 10.720 tokens disponibles; Gemini permanece en proyecto Free
sin facturación configurada.
Groq `openai/gpt-oss-120b` acertó **2/2** casos iniciales de la misma suite
(`tech-001` aceptado y `tech-002` rechazado), con JSON válido, modelo exacto y
`finish_reason=stop`. Es una muestra exploratoria: la evaluación de 60 casos,
autor y las otras cargas siguen pendientes.
Estos casos sintéticos prueban fidelidad de veredictos simples; no son borradores
reales, ni la calibración de Simón, ni los controles de cifras de Cactus. Ningún
modelo se promueve por esta medición aislada.

## Comprobación inicial de autor de Journal

Con el `SYSTEM` anterior de `bin/newsblog/draft.py`, el mismo brief público
`rss-etag` de `bin/newsblog/guide_briefs.json` y el techo productivo de 1.400
tokens de salida, ambos proveedores devolvieron HTTP 200 y `finish_reason=stop`.
Ni `gemini-3.5-flash-lite` ni `gemma-4-E4B` incluyeron el campo obligatorio
`schema: "newsblog.article.v2"`; `validate_document(..., allow_legacy=False)`
rechazó ambos borradores con `invalid_article_schema`. El ejemplo JSON dentro
del `SYSTEM` enumera `kind`, `title`, `standfirst`, `sections` y `tags`, pero
omite `schema`, lo que puede explicar el fallo. La prueba se repitió con el
prompt inicial exacto del flujo. Después de agregar ese campo al ejemplo en el
PR de Journal, ambos modelos devolvieron un documento válido en una repetición
del mismo caso. La corrección quedó en el prompt de usuario para conservar los
digests de checkpoints existentes. Esto comprueba el formato en un caso público,
sin evaluación editorial completa de los hechos ni aprobación de autor. **Ninguno está
aprobado como autor.** Hay que completar los gates de Journal y ampliar la
muestra antes de considerar cualquier promoción.

[Z.ai](https://chat.z.ai/legal-agreement/terms-of-service)
restringe noticias y finanzas; [Mistral Free](https://docs.mistral.ai/admin/billing-usage/subscriptions)
incluye créditos contra tarifas por modelo. Tampoco cuentan para esta política
de tarifa estrictamente cero. [Freebuff](https://freebuff.com/terms-of-service)
queda para uso humano en su app.

[SambaNova Cloud Free](https://cloud.sambanova.ai/plans) exige agregar un medio
de pago y comprar créditos antes de las primeras solicitudes; no sirve como
reserva a costo cero. [Together AI](https://support.together.ai/articles/4999040689-where-to-find-your-api-key)
no entrega claves de API en su nivel gratuito limitado. [GitHub Models](https://docs.github.com/en/github-models)
cerró el servicio anterior. Estos nombres del directorio de Free-LLM no se
promueven al registro de capacidad por aparecer allí.

[Cohere Trial](https://cohere.com/pricing) prohíbe producción o uso comercial;
[NVIDIA API Catalog](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)
permite sólo evaluación y créditos de prueba. [Hugging Face Inference Providers](https://huggingface.co/docs/inference-providers/en/pricing)
ofrece USD 0,10 de crédito mensual sobre tarifas pagas, no modelos de tarifa
cero. [FreeInference](https://freeinference.org/terms) es experimental,
registra entradas y salidas para investigación y no publica una cuota estable.
Ninguno acredita una cuarta reserva independiente permanente.

[Ollama Cloud Free](https://ollama.com/pricing) reinicia mensualmente un pequeño
saldo de uso, pero cobra cada modelo por token; [Aion Labs Free](https://www.aionlabs.ai/pricing/)
da un crédito diario contra modelos con [precio por token](https://www.aionlabs.ai/docs/models/).
Son límites de gasto incluidos, no modelos de tarifa cero. [ModelScope
API-Inference](https://community.modelscope.cn/675262372db35d1195183bdb.html)
advierte expresamente no usar su beta gratuita para producción. [Hetzner
Experiments](https://docs.hetzner.com/general/company-and-policy/experiments/inference/)
es gratis sólo durante su fase experimental y desaconseja producción.
[Vikasit Nova](https://vikasit.ai/inference) anuncia 2 millones de tokens
gratuitos diarios y acceso por GitHub, pero su propia página lo presenta para
prototipos, scripts y proyectos personales. El 25 de septiembre el enlace
oficial «Get an API key» devolvió HTTP 502 en `auth.vikasit.ai`. Sus
[términos](https://vikasit.ai/terms) se declaran plantilla pendiente de
revisión jurídica. El router reconoce únicamente `vikasit-nova` y topa la
atestación diaria en 2 millones de tokens; todos los demás modelos publicados
por Vikasit tienen precio positivo. Queda en investigación, sin alta, cuota de
cuenta ni aptitud editorial comprobada y sin cupo contabilizado.
[Nous Portal](https://portal.nousresearch.com/terms) limita el acceso por
competidores y tiene cuota gratuita ambigua entre plan y prueba: tampoco se
admite. [LLM7](https://github.com/chigwell/llm7.io/blob/main/TERMS.md) publica
cupo gratuito para investigación, pero sus términos prohíben usarlo como
*gateway* o integrarlo en productos o flujos para terceros sin permiso escrito,
y descartan expresamente producción. Se excluye; no se creó cuenta ni se probó
su API.

[OVHcloud AI Endpoints](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started)
exige un proyecto con medio de pago para claves de API; la modalidad anónima
tiene 2 RPM por IP y modelo, pero no acredita precio cero ni cuota diaria
garantizada. [Pollinations](https://enter.pollinations.ai/terms) consume Pollen
por petición; sus grants y recompensas gratuitos no son una tarifa cero estable.

[Requesty Free](https://www.requesty.ai/pricing) publica modelos a precio cero
y 200 solicitudes diarias sin tarjeta. Sin embargo, sus
[términos, §10(11)](https://www.requesty.ai/terms) prohíben acceder al servicio
con el fin de desarrollar uno competidor. Este proyecto construye un router
compartido, así que no se abrió cuenta ni se integra su API sin permiso escrito
de Requesty. La lista gratuita de un agregador tampoco equivale a operadores
independientes: varios modelos pueden depender del mismo proveedor subyacente.

[Plugsky](https://plugsky.com/free-ai-api) publicita 100.000 tokens gratis
diarios, pero sus [términos vigentes](https://plugsky.com/legal/terms) describen
el plan Free como una prueba de siete días. La afirmación promocional no
acredita capacidad permanente: se excluye de la reserva.

## Ocho operadores adicionales revisados el 25 de septiembre

La revisión de fuentes oficiales no encontró otra ruta apta de tarifa cero
permanente para generación editorial comercial:

| Operador | Resultado |
| --- | --- |
| [Baidu Qianfan](https://cloud.baidu.com/doc/qianfan/index.html) | La oferta actual también presenta cupones de bienvenida; el [acuerdo de experiencia](https://ai.baidu.com/ai-doc/WENXINWORKSHOP/Rlgujm1c6) impone restricciones sobre resultados y derivados. No se pudo verificar la cuota permanente de una cuenta MaatWork. |
| [IBM watsonx.ai](https://www.ibm.com/docs/en/watsonx/saas?topic=watsonx-faq) | Lite es evaluativo; los límites gratuitos publicados no son consistentes entre páginas y [el alta suele requerir tarjeta](https://cloud.ibm.com/docs/account?topic=account-accountfaqs). No acredita producción a costo cero. |
| [Nebius Token Factory](https://nebius.com/token-factory/prices) | Ofrece un crédito inicial, sujeto a [términos de Builder](https://nebius.com/builders-terms-and-conditions); no es tarifa cero sostenida. |
| [Fireworks AI](https://fireworks.ai/pricing) | Ofrece un crédito inicial y después cobra por uso. |
| [DeepInfra](https://deepinfra.com/) | Sus modelos tienen tarifas positivas y los [términos](https://deepinfra.com/terms) restringen usos competitivos. |
| [Jina AI](https://jina.ai/embeddings/) | La oferta citada es embeddings y reranking, no generación editorial de chat; su [página legal](https://jina.ai/legal) advierte cambios tras la adquisición por Elastic. |
| [Featherless AI](https://featherless.ai/pricing) | La API para automatización pertenece a planes pagos. |
| [Modal Shared Endpoints](https://modal.com/docs/guide/shared-endpoints) | Cobra por token; los créditos generales del plan no cubren esos endpoints. |

Todos figuran como excluidos en `config/provider-watchlist.json`, para que el
directorio Free-LLM no los presente repetidamente como reserva nueva. Se
reconsiderarán sólo si cambian las condiciones oficiales y se comprueba la
cuota de la cuenta. Ninguno recibió alta ni prueba con datos editoriales.

## Próxima comprobación necesaria

Para cada carga hacen falta cuatro operadores independientes con autor y revisor
aptos, tres activos y uno de reserva, y al menos el doble del pico diario medido.
Los tres proyectos deben terminar la comparación editorial de siete días sin
publicación automática, comprobar $0 facturados y medir la reducción de GPU.
Ninguna de estas pruebas de cuenta o piloto figura como aprobada en este estado.
