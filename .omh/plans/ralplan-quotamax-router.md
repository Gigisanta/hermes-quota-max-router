# Plan: Hermes QuotaMax Router → Production-Grade Free-Tier LLM Router + Hermes Plugin

> **Instance:** `quotamax-router`
> **Started:** 2026-06-10
> **Owner:** HerMaat (autopilot)
> **Goal:** Router LLM OpenAI-compatible que solo enruta a modelos **verificablemente gratis**, **auto-descubre** nuevos modelos, y se integra como **model-provider plugin de Hermes Agent** (`~/.hermes/plugins/model-providers/quotamax-router/`) para que Hermes pueda usarlo como `quotamax-router/...` en cualquier sub-agente.

---

## Estado actual (snapshot verificado 2026-06-10)

| Componente | Estado | Evidencia |
|---|---|---|
| HTTP server (FastAPI, OpenAI-compat) | ✅ Real | 8 endpoints, `server/app.py:100-388` |
| Routing heurístico (reglas) | ✅ Real | `core/orchestrator.py:147-288` (`RuleBasedOrchestrator`) |
| MoA engine | ⚠️ Implementado pero no wireado | `core/moa_engine.py` existe; `server/app.py:143-147` no lo instancia |
| LLM orchestrator (tier 2) | ⚠️ Implementado pero no wireado | `core/orchestrator.py:291-350` (`LLMOrchestrator`); server usa solo el rule-based |
| Quota manager | ✅ Real (fakeredis) | `core/quota_manager.py:64-188` |
| Auto-updater + remote feeds | ✅ Real (3 catalogs) | `core/remote_feeds.py` → 545 modelos, 233 gratis, 17 providers |
| Cost tracker | ✅ Real (in-memory) | `core/cost_tracker.py` |
| Dashboard | ⚠️ UI real, chat tab es stub | `dashboard/app.py:76-97` hardcoded |
| Tests | 189 pass, 11 fail | Falla cuando corre suite completa por env-leak |
| Keys reales en `.env` | ❌ **TODAS ROTAS** | Gemini 401, DeepSeek sin saldo, OpenRouter "User not found" |
| Plugin Hermes | ❌ No existe | hay que crearlo en `~/.hermes/plugins/model-providers/quotamax-router/` |

---

## Restricciones de hierro

1. **No requiero tarjeta de crédito.** Solo tier gratis permanente, sin trial que expire.
2. **Funciona en este Mac (macOS, Python 3.13, Hermes en `~/.hermes/`).**
3. **El plugin debe ser descubrible automáticamente** por el `providers/__init__.py._discover_providers()` de Hermes (sigue el contrato `ProviderProfile`).
4. **Sin fabrication**: ningún endpoint "stub" puede quedar en el camino crítico. Si una pieza no funciona, se remueve o se documenta.
5. **El plan se compromete y empuja al final de cada tarea importante** (perfil Gio en `USER.md`).

---

## Fases

### Phase 0: Stabilize (no live keys required)

**Iteración 1 — Fix 11 tests rotos por env-leak**
- **Qué:** agregar `tests/conftest.py` que limpia `ROUTER_MASTER_KEY`, `*_API_KEY`, `REDIS_URL` antes de cada test, y aislar las SQLite DB a un `tmp_path` por sesión.
- **Por qué:** el audit verificó que `pytest tests/test_server*.py` solo pasa 20/20; la suite completa rompe por env leak en `server/app.py:171`. Sin tests verdes, no puedo refactorear con confianza.
- **Done when:** `pytest tests/` → 200/200 pass, sin warnings de env leak.
- **Verificación:** output completo de `pytest -v` archivado en `tests/last_full_run.txt`.

### Phase 1: Make it real (live mode) — REQUIERE KEY VIVA

**Iteración 2 — Live key onboarding**
- **Bloqueante actual:** las 3 keys en `.env` no funcionan. Sin una key real, no puedo verificar end-to-end.
- **Plan:** agregar soporte para **Groq free tier** (100% gratis permanente, sin tarjeta, 15 modelos con límites claros según REPORT.md). Groq es OpenAI-compat en `https://api.groq.com/openai/v1`.
- **Acción inmediata:** pedir a Gio que pegue una `GROQ_API_KEY` real (signup en https://console.groq.com/keys, sin tarjeta, instantáneo) por el OOB. Mientras tanto, dejo la integración Groq codeada en el router y testeo contra el endpoint gratis.
- **Done when:** `litellm.completion(model="groq/llama-3.3-70b-versatile", ...)` devuelve contenido real, y el router lo enruta correctamente.

**Iteración 3 — Wire LLMOrchestrator + MoAEngine**
- **Qué:** instanciar `LLMOrchestrator(model="gemini/gemini-2.5-flash")` o `groq/llama-3.3-70b-versatile` en `build_app()`, y `MoAEngine(registry, quota)`. Default al rule-based, LLM gated por flag `ORCHESTRATOR_MODE=llm` o `ORCHESTRATOR_MODE=moa`.
- **Por qué:** el audit confirmó que ambos están implementados pero inertes.
- **Done when:** un test e2e que pega un prompt "refactor this Python function" → MoAEngine fanea-out a 3 modelos gratis → synthesizer devuelve respuesta consolidada.

**Iteración 4 — Quota tracking real con persistencia**
- **Qué:** instalar Redis (`brew install redis` o ya disponible, arrancar en background), cambiar `fakeredis` a Redis real. Agregar **scheduler in-process** (`asyncio.create_task`) que resetea cuotas a medianoche UTC.
- **Por qué:** `crontab.example` no es ejecutable, no hay scheduler real. Falsa sensación de reset.
- **Done when:** un test que setea `last_reset` a ayer, llama a `_maybe_reset()`, y verifica que `remaining = total`.

### Phase 2: Auto-Discovery (el "100% gratis automáticamente")

**Iteración 5 — Curated free-only feed**
- **Qué:** nuevo feed `core/catalogs.py:_parse_openrouter_free_only` que filtra `pricing.prompt == "0"` Y `pricing.completion == "0"` Y `id.endswith(":free")`. Aplica también a HF Inference.
- **Por qué:** REPORT.md descubrió que el router marca como "free" 233 modelos de los cuales ~30 son realmente `:free` en OpenRouter; el resto son pay-as-you-go con un default tier que pronto expira o que requiere top-up mínimo.
- **Done when:** `registry.discovered.json` solo contiene modelos con verificación en vivo (precio real == 0, endpoint responde 200 OK a `models` list).

**Iteración 6 — Live "is this still free?" probe**
- **Qué:** agregar `core/free_prober.py`: para cada modelo en el registry, hace un `GET {endpoint}/v1/models` (sin auth si es público, o con auth dummy) y verifica que (a) responde 200, (b) el modelo aparece, (c) `pricing.prompt == 0`. Marca como `is_verified_free=True` o lo expulsa a `unverified.json`.
- **Por qué:** la meta del usuario es **"automático y 100% gratis"**. Confiar en el campo `pricing` de un JSON que rotó hace 6 meses no es "automático". Hay que probar en vivo, en cada ciclo de discovery.
- **Cadencia:** corre como parte de `RegistryUpdater.apply_feed()` (cuando aplica, no cada request).
- **Done when:** un test que setea un modelo con `pricing.prompt = "0.001"`, corre el prober, y el modelo sale del registry curated.

**Iteración 7 — Provider coverage expansion**
- **Qué:** agregar catalogs faltantes: Together.ai (con su endpoint público `https://api.together.xyz/v1/models`), Fireworks.ai, Groq native, DeepSeek native. Cada uno con su parser en `core/catalogs.py`.
- **Done when:** `registry.discovered.json` lista ≥ 10 providers con ≥ 30 modelos `:free` verificados.

### Phase 3: Integración con Hermes (la parte que pediste explícitamente)

**Iteración 8 — Crear el plugin**
- **Path:** `~/.hermes/plugins/model-providers/quotamax-router/`
- **Archivos:**
  - `plugin.yaml` (manifest: name=quotamax-router, kind=model-provider)
  - `__init__.py` con `QuotaMaxRouterProfile(ProviderProfile)` que:
    - `base_url="http://127.0.0.1:8080/v1"` (configurable via `QUOTAMAX_BASE_URL`)
    - `env_vars=("QUOTAMAX_API_KEY",)`
    - `api_mode="chat_completions"`
    - `signup_url="https://github.com/tu-usuario/hermes-quota-max-router"` (o el repo real)
    - `display_name="QuotaMax Router (Free-Tier Aggregator)"`
    - `description="Routes only to verified 100% free LLM models. Self-discovering."`
    - `aliases=("quotamax", "qmr", "free-tier")`
    - Override `fetch_models()`: hace `GET {base_url}/v1/models` con header `Authorization: Bearer $QUOTAMAX_API_KEY`, parsea OpenAI `data[]` list, y agrega un sufijo `quotamax-router/{model_id}` para que el routing sea namespaced.
- **Por qué:** el contrato `ProviderProfile` está documentado y verificado en `providers/__init__.py:42-50` y `plugins/model-providers/README.md`.
- **Done when:** `python -c "from providers import get_provider_profile; print(get_provider_profile('quotamax-router'))"` devuelve un profile válido.

**Iteración 9 — End-to-end real: Hermes → Router → Modelo gratis**
- **Qué:** con el server del router corriendo en `127.0.0.1:8080`, hago una llamada real desde la API de Hermes:
  ```python
  from providers import get_provider_profile
  p = get_provider_profile("quotamax-router")
  # Then via the chat_completions transport, request:
  #   model="quotamax-router/auto"  →  router decide
  #   model="quotamax-router/groq/llama-3.3-70b-versatile"  →  direct
  ```
- **Por qué:** la integración está incompleta hasta que un sub-agente de Hermes puede pedirle al router una respuesta y recibirla.
- **Done when:** un test que simula exactamente la chain: Hermes → router → Groq → router → Hermes, con respuesta real y tokens contados.

**Iteración 10 — Sub-agent registration en `config.yaml`**
- **Qué:** agregar entry en `~/.hermes/config.yaml` bajo `model:` y/o `auxiliary:` para que aparezca en `hermes models` y pueda seleccionarse como main o como sub-agent provider. Ejemplo:
  ```yaml
  auxiliary:
    quotamax_subagent:
      provider: quotamax-router
      model: auto
      ...
  ```
- **Done when:** `hermes models` lista `quotamax-router/auto` y `quotamax-router/<cualquier modelo>` como seleccionables.

### Phase 4: Hardening, observability, delivery

**Iteración 11 — Dashboard funcional**
- **Qué:** reemplazar el chat tab stub (`dashboard/app.py:76-97`) con un form real que llame `/v1/chat/completions`. Mostrar: provider elegido, tokens consumidos, latencia, costo ($0 siempre para free tier).
- **Done when:** un screenshot del dashboard muestra un chat real con un modelo gratis respondiendo, y el `cost` siempre es $0.00.

**Iteración 12 — Self-test cron**
- **Qué:** un script `scripts/healthcheck.py` que cada 6h: (a) hace GET `/v1/router/health`, (b) hace POST `/v1/chat/completions` con un prompt trivial, (c) verifica que la respuesta sea real (no `[stub:...`), (d) alerta en `logs/alerts.jsonl` si falla. Registrar en `hermes cron`.
- **Done when:** `hermes cron list` muestra el healthcheck, y un run manual pasa.

**Iteración 13 — Streaming + tool calling**
- **Qué:** agregar `stream=true` support (`server/app.py:208` actualmente 400) y aceptar `tools` field passthrough.
- **Por qué:** los agentes de Hermes hacen streaming y llaman tools. Sin esto, la integración es solo decorativa.
- **Done when:** un test e2e con `stream=true` recibe chunks, y un test con `tools=[...]` recibe `tool_calls` en la respuesta.

**Iteración 14 — Documentación y primer commit/push**
- **Qué:** actualizar `README.md` con quickstart real (los pasos exactos para arrancar el server, plug-in en Hermes, y verificar). Actualizar `docs/RUNBOOK.md` con troubleshooting (qué hacer si una key muere, cómo forzar refresh, etc.). Corregir `docs/PROVIDERS.md` para reflejar los datos de REPORT.md.
- **Commit + push** (perfil Gio: "Gio wants completed repo changes committed and pushed automatically at the end of each task").
- **Done when:** `git log` muestra un commit limpio por fase, `git push` exitoso, README tiene un quickstart que un humano puede seguir sin ayuda.

---

## Riesgos identificados y mitigación

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Groq free tier también rompe (cuenta baneada, región, etc.) | Media | Bloqueante | Mantener fallback a stub mode documentado; tests no requieren live |
| Plugin en `~/.hermes/plugins/...` no se auto-descubre | Baja | Alto | Verificar con `hermes providers list` después de copiar; revisar `providers/__init__.py:_discover_providers()` |
| Auto-discovery alucina modelos que ya no son gratis | Alta (reporte lo confirmó) | Medio | Iter 6: live probe obligatorio, no confiar en `pricing` JSON |
| Tests flaky por env leak / estado compartido | Alta (ya pasó) | Medio | Iter 1: conftest.py con monkeypatch + tmp_path |
| Push falla porque repo es local-solo (no hay remote) | Alta | Bajo | Verificar `git remote -v`; si vacío, ofrecer `gh repo create` o push a `origin` |

---

## Cómo mido éxito

- **Cuantitativo:** 200/200 tests verdes, ≥ 30 modelos `:free` verificados en vivo, dashboard con respuesta real de un modelo gratis, plugin discoverable en `hermes providers list`.
- **Cualitativo:** un sub-agente de Hermes puede hacer `delegate_task(goal="...", provider="quotamax-router/auto")` y recibir respuesta útil de un modelo 100% gratis, en menos de 5 segundos.

---

## Estado de las fases

| Phase | Iter | Status | Verified by |
|---|---|---|---|
| 0 | 1 | pending | — |
| 1 | 2-4 | pending (bloqueado: necesita GROQ_API_KEY) | — |
| 2 | 5-7 | pending | — |
| 3 | 8-10 | pending | — |
| 4 | 11-14 | pending | — |
