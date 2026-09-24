# Estado de alta y reserva — 2026-09-24

`quotamax status` en el checkout local informó Redis disponible, **0 modelos
verificados, 0 proveedores aptos y 0 de reserva** para Journal, Simón y Cactus.
Faltan `var/verified-models.json` y los picos medidos de las tres cargas. El
router no está habilitado como ruta principal ni se inició un piloto con APIs.

| Proveedor | Evidencia revisada | Falta para contar capacidad |
| --- | --- | --- |
| Gemini Free | La sesión de Google de MaatWork muestra un proyecto existente en nivel Free. Para Gemini 3.1 Flash Lite mostró 15 RPM, 250.000 TPM y 500 RPD. | Crear una clave dedicada sin facturación, confirmar precio y límites del proyecto que vaya a usar el router, hacer llamada real y evaluación editorial. El intento de crear un proyecto exclusivo fue rechazado por AI Studio como solicitud sospechosa; no se repitió ni se creó una clave. |
| Groq Free | [Cuotas oficiales](https://console.groq.com/docs/rate-limits) y nivel gratuito documentados. | Acceso de MaatWork, clave, cuota de la cuenta, precio, smoke y calidad por carga. |
| Cloudflare Workers AI Free | [10.000 Neurons diarios en Workers Free](https://developers.cloudflare.com/workers-ai/platform/pricing/); se corta al llegar al cupo. | Acceso de MaatWork, token acotado, modelo utilizable en Free, tasa de Neurons, smoke y calidad. |
| SiliconFlow Free | [Modelos de precio cero y límites por cuenta](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation). | Verificación de identidad exigida por el proveedor, acceso de MaatWork, cuota exacta y modelo de calidad suficiente; sin esos datos no es reserva. |
| OpenRouter `:free` | [Variantes gratuitas](https://openrouter.ai/docs/guides/routing/model-variants/free) publicadas. | Acceso, precio y cuota de la cuenta; sólo capacidad extra, no proveedor independiente. |

[Cerebras](https://www.cerebras.ai/inference) anuncia crédito inicial de prueba:
se quitó del registro de backends admitidos. [Z.ai](https://chat.z.ai/legal-agreement/terms-of-service)
restringe noticias y finanzas; [Mistral Free](https://docs.mistral.ai/admin/billing-usage/subscriptions)
incluye créditos contra tarifas por modelo. Tampoco cuentan para esta política
de tarifa estrictamente cero. [Freebuff](https://freebuff.com/terms-of-service)
queda para uso humano en su app.

## Próxima comprobación necesaria

Para cada carga hacen falta cuatro operadores independientes con autor y revisor
aptos, tres activos y uno de reserva, y al menos el doble del pico diario medido.
Los tres proyectos deben terminar la comparación editorial de siete días sin
publicación automática, comprobar $0 facturados y medir la reducción de GPU.
Ninguna de estas pruebas de cuenta o piloto figura como aprobada en este estado.
