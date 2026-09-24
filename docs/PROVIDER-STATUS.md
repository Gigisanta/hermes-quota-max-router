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
| Gemini Free | La sesión de Google de MaatWork muestra un proyecto existente en nivel Free. Para Gemini 3.1 Flash Lite mostró 15 RPM, 250.000 TPM y 500 RPD. Una clave preexistente etiquetada para iStock devolvió HTTP 200 al listar modelos, sin probar generación ni vinculación con ese proyecto Free. | Crear una clave dedicada sin facturación, confirmar precio y límites del proyecto que vaya a usar el router, hacer llamada real y evaluación editorial. El intento de crear un proyecto exclusivo fue rechazado por AI Studio como solicitud sospechosa; no se repitió ni se creó una clave. |
| Groq Free | MaatWork (`maatwork.comercial@gmail.com`) pudo entrar en su organización Personal y proyecto Default. Facturación muestra **Free, $0, plan actual**. Los límites de la cuenta para `openai/gpt-oss-120b`, `openai/gpt-oss-20b` y `qwen/qwen3.8-27b` muestran 30 RPM, 1.000 RPD, 8.000 TPM y 200.000 TPD por modelo. En Playground, `openai/gpt-oss-120b` respondió una prueba sintética de dos movimientos de mercado con cifras y direcciones correctas; esto no acredita la evaluación editorial. Ya existe una clave denominada `iStock production fallback verified`; una consulta de sólo lectura con la credencial preexistente devolvió HTTP 403. No se mostró su valor ni se reutilizó para generar. [Límites de Groq](https://console.groq.com/docs/rate-limits). | Clave dedicada para el router, llamada real con esa clave y evaluación editorial por carga. Estos límites no se cuentan como capacidad apta hasta completar esas pruebas. |
| Cloudflare Workers AI Free | [10.000 Neurons diarios en Workers Free](https://developers.cloudflare.com/workers-ai/platform/pricing/); se corta al llegar al cupo. | Acceso de MaatWork, token acotado, modelo utilizable en Free, tasa de Neurons, smoke y calidad. |
| SiliconFlow Free | [Modelos de precio cero y límites por cuenta](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation). La pantalla de alta ofrece teléfono, correo con código y WeChat; no apareció inicio con Google. La documentación exige verificación de identidad real para usar todos los modelos gratuitos. | Acceso de MaatWork por correo, consentimiento de términos, verificación de identidad por el titular, cuota exacta y modelo de calidad suficiente; sin esos datos no es reserva. |
| OpenRouter `:free` | [Variantes gratuitas](https://openrouter.ai/docs/guides/routing/model-variants/free) publicadas. | Acceso, precio y cuota de la cuenta; sólo capacidad extra, no proveedor independiente. |

[Cerebras](https://www.cerebras.ai/inference) anuncia crédito inicial de prueba:
se quitó del registro de backends admitidos. [Z.ai](https://chat.z.ai/legal-agreement/terms-of-service)
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

## Próxima comprobación necesaria

Para cada carga hacen falta cuatro operadores independientes con autor y revisor
aptos, tres activos y uno de reserva, y al menos el doble del pico diario medido.
Los tres proyectos deben terminar la comparación editorial de siete días sin
publicación automática, comprobar $0 facturados y medir la reducción de GPU.
Ninguna de estas pruebas de cuenta o piloto figura como aprobada en este estado.
