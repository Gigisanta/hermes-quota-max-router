# Estado de alta y reserva — 2026-09-24

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
| Gemini Free | La sesión de Google de MaatWork muestra el proyecto `maatworkyoutube` en nivel Free. Para Gemini 3.1 Flash Lite mostró 15 RPM, 250.000 TPM y 500 RPD. Una clave preexistente etiquetada para iStock devolvió HTTP 200 al listar modelos, sin probar generación ni vinculación con ese proyecto Free. | Crear una clave dedicada sin facturación, confirmar precio y límites del proyecto que vaya a usar el router, hacer llamada real y evaluación editorial. Tras la autorización del usuario, el intento de crear `MaatWork Free Editorial Router` en el proyecto existente fue rechazado por AI Studio con «The request is suspicious». No se creó ninguna clave ni se intentó eludir el bloqueo. |
| Groq Free | MaatWork (`maatwork.comercial@gmail.com`) pudo entrar en su organización Personal y proyecto Default. Facturación muestra **Free, $0, plan actual**. Los límites de la cuenta para `openai/gpt-oss-120b`, `openai/gpt-oss-20b` y `qwen/qwen3.8-27b` muestran 30 RPM, 1.000 RPD, 8.000 TPM y 200.000 TPD por modelo. En Playground, `openai/gpt-oss-120b` respondió una prueba sintética de dos movimientos de mercado con cifras y direcciones correctas; esto no acredita la evaluación editorial. Ya existe una clave denominada `iStock production fallback verified`; una consulta de sólo lectura con la credencial preexistente devolvió HTTP 403. No se mostró su valor ni se reutilizó para generar. [Límites de Groq](https://console.groq.com/docs/rate-limits). | Clave dedicada para el router, llamada real con esa clave y evaluación editorial por carga. Estos límites no se cuentan como capacidad apta hasta completar esas pruebas. |
| Novita AI | La [página oficial de Ling 3.0 Flash Fin](https://novita.ai/models/model-detail/inclusionai-ling-3.0-flash-fin) muestra $0 por millón de tokens de entrada y salida y API compatible con OpenAI. Sin embargo, el [catálogo actual](https://novita.ai/models) marca **TIME LIMITED FREE** para Ling 3.0 Flash Fin y Sante; no pueden contar como reserva permanente. Ling 3.0 Flash VL aparece con [precio positivo](https://novita.ai/pricing/). La [guía oficial](https://blogs.novita.ai/es/ling-3-0-flash-fin-on-novita-ai/) menciona 30 RPM como límite de catálogo, sujeto a la cuenta. Su [AUP §5(6)](https://novita.ai/legal/acceptable-use-policy) restringe usos para competir con Novita: sólo se evalúa como cliente interno editorial, nunca como servicio de enrutamiento ofrecido a terceros. | Alta de MaatWork pendiente de consentimiento de términos. Puede servir para evaluación temporal, pero no se suma a las cuatro rutas permanentes mientras la gratuidad tenga plazo; también faltan cuotas de cuenta, clave, llamada real y calidad. |
| Cloudflare Workers AI Free | [10.000 Neurons diarios en Workers Free](https://developers.cloudflare.com/workers-ai/platform/pricing/); se corta al llegar al cupo. La misma tabla identifica GLM 5.3 Flash y otros modelos como exclusivos de Workers Paid o créditos prepagos; el catálogo del router los rechaza. | Acceso de MaatWork, token acotado, modelo utilizable en Free, tasa de Neurons, smoke y calidad. |
| Cerebras Free Trial | La [página comercial vigente](https://www.cerebras.ai/inference) ofrece **USD 5 de crédito inicial**, no una tarifa cero estable; la [documentación de precios antigua](https://inference-docs.cerebras.ai/support/pricing) todavía describe un nivel Free $0 y no se usa como prueba de capacidad actual. Los [términos](https://www.cerebras.ai/terms-of-service) restringen productos competidores y benchmarking. | Excluido de las cuatro rutas permanentes. No vale la pena aceptar términos ni crear una clave para capacidad que desaparece al agotar el crédito. |
| SiliconFlow Free | [Modelos de precio cero y límites por cuenta](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation). La pantalla de alta ofrece teléfono, correo con código y WeChat; no apareció inicio con Google. La documentación exige verificación de identidad real para usar todos los modelos gratuitos. | Acceso de MaatWork por correo, consentimiento de términos, verificación de identidad por el titular, cuota exacta y modelo de calidad suficiente; sin esos datos no es reserva. |
| SimpleLLM Free | El [catálogo público oficial](https://simplellm.eu/docs/models.html) permite comprobar precio y estado sin clave. El 24 de septiembre, su API marcó `gemma-4-E4B` disponible con entrada/salida a 0 SC; Llama 8B, Mistral 7B y Kode 14B figuraban a 0 SC pero `planned`, por lo que no se cuentan. [Los límites documentados](https://simplellm.eu/docs/limits.html) incluyen ventanas por minuto, hora y día para solicitudes y tokens gratuitos. El router exige los límites horarios `rph` y `tph` de la cuenta y los reserva en Redis antes de cada llamada. Su API publica comprobaciones de [uso por cuenta y clave](https://simplellm.eu/docs/usage.html). | Su formulario de alta sólo ofreció nombre, correo y contraseña, sin inicio con Google. La creación de contraseña requiere intervención del titular. Falta cuenta MaatWork, cuota exacta, clave dedicada, llamada real y evaluación editorial; el modelo gratuito activo puede ser insuficiente para autor/revisor. Está implementado como candidato deshabilitado hasta que pase todas esas pruebas. |
| Mistral Free | La cuenta Google de MaatWork ya es reconocida por AI Studio. Su [modo Free](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key) permite API sin tarjeta y su [plan actual](https://mistral.ai/pricing/) anuncia USD 10/mes de créditos, consumidos contra precios por modelo. [Mistral lo destina a evaluación y prototipado](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them). La consola exige aceptar sus términos antes de mostrar límites. | Consentimiento pendiente; no cuenta como tarifa cero por modelo ni como reserva productiva sostenida. |
| OpenRouter `:free` | Sus [términos, §7(4)](https://openrouter.ai/terms) prohíben usar el servicio para desarrollar uno competidor. | Excluido del registro y de la auditoría automática; no se abrió cuenta. Sólo reconsiderar con autorización escrita específica del operador. |

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
Experiments](https://docs.hetzner.com/general/company-and-policy/experiments/openclaw/)
es gratis sólo durante su fase experimental y desaconseja producción.
[Vikasit Nova](https://vikasit.ai/inference) anuncia 2 millones de tokens
gratuitos diarios y acceso por GitHub, pero su propia página lo presenta para
prototipos, scripts y proyectos personales, y sus [términos](https://vikasit.ai/terms)
se declaran plantilla pendiente de revisión jurídica. Queda en investigación,
sin alta ni cupo contabilizado para estas publicaciones.
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

## Próxima comprobación necesaria

Para cada carga hacen falta cuatro operadores independientes con autor y revisor
aptos, tres activos y uno de reserva, y al menos el doble del pico diario medido.
Los tres proyectos deben terminar la comparación editorial de siete días sin
publicación automática, comprobar $0 facturados y medir la reducción de GPU.
Ninguna de estas pruebas de cuenta o piloto figura como aprobada en este estado.
