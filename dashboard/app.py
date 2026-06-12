"""Gradio Dashboard — Phase 5 (visual layer).

Zero-config: `python dashboard/app.py` and a browser tab opens at the
printed URL. The dashboard exposes:

  1. **Chat**: type a request → see the orchestrator's routing decision,
     the real model reply (via /v1/chat/completions), and the live
     tokens / latency / cost (always $0 for free-tier routes).
  2. **Registry tab**: live snapshot of all models + their quotas.
  3. **Updater tab**: trigger a manual registry refresh from a local feed.

The chat tab calls the router over HTTP. If the router is not running
or is in stub mode, the response is clearly marked as such.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.model_registry import ModelRegistry
from core.quota_manager import QuotaManager
from core.task_analyzer import HeuristicTaskAnalyzer
from core.orchestrator import RuleBasedOrchestrator
from core.auto_updater import LocalFeedProvider, RegistryUpdater

log = logging.getLogger(__name__)


# --- Helpers (no Gradio imports here so this module is testable headless) ---

def build_state() -> dict:
    """Build the runtime state shared across all UI tabs."""
    reg = ModelRegistry()
    qm = QuotaManager()
    qm.sync_from_registry(reg)
    return {
        "registry": reg,
        "quota": qm,
        "analyzer": HeuristicTaskAnalyzer(),
        "orchestrator": RuleBasedOrchestrator(),
    }


def format_decision(decision) -> str:
    return (
        f"**Strategy:** `{decision.chosen_strategy}`\n\n"
        f"**Primary:** `{decision.primary_model}`\n\n"
        f"**Fallback:** `{decision.fallback_model or '(none)'}`\n\n"
        f"**Confidence:** `{decision.confidence:.2f}`  |  "
        f"**Preserve paid:** `{decision.preserve_paid_quota}`  |  "
        f"**Quality:** `{decision.quality_expectation}`\n\n"
        f"**Tags matched:** `{', '.join(decision.tags_matched) or '(none)'}`\n\n"
        f"**Reasoning:** {decision.reasoning}"
    )


def format_quota_table(qm: QuotaManager) -> str:
    rows = ["| Model | Remaining | Total | % |", "|---|---|---|---|"]
    for s in qm.all_snapshots():
        if s.has_quota() and s.total:
            rows.append(
                f"| `{s.model_id}` | {s.remaining:,} | {s.total:,} | "
                f"{s.pct_remaining:.1%} |"
            )
        else:
            rows.append(f"| `{s.model_id}` | ∞ | — | paid |")
    return "\n".join(rows)


def _get_router_base_url() -> str:
    """Where is the router listening? Defaults to 127.0.0.1:8088."""
    return os.environ.get("QUOTAMAX_BASE_URL", "http://127.0.0.1:8088/v1").rstrip("/")


def _is_stub_response(body: dict) -> bool:
    """Stub-mode responses are flagged so the UI can mark them clearly."""
    content = (body.get("choices", [{}])[0].get("message", {}).get("content") or "")
    return content.startswith("[stub:") or content.startswith("[Stub mode")


def _fetch_live_models() -> dict:
    """Update the model dropdown from the live router's /v1/models.

    Returns a dict suitable for gr.Dropdown.update(). Always includes
    "auto" as the first option so the user can still ask the router to
    decide even if the list is empty.
    """
    import httpx

    base = _get_router_base_url()
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{base}/models")
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        return {
            "choices": ["auto"],
            "value": "auto",
            "info": f"Router unreachable: {exc}",
        }

    items = data if isinstance(data, list) else data.get("data", [])
    ids = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
    return {
        "choices": ["auto"] + ids,
        "value": "auto",
        "info": f"{len(ids)} models available",
    }


def run_chat(
    state: dict,
    user_message: str,
    model: str = "auto",
) -> tuple[str, str, str, str]:
    """Process a chat turn. Returns (decision_md, response_text, metrics_md, status)."""
    if not user_message.strip():
        return "_Type a request above and hit Send._", "", "", ""

    # First, get the orchestrator's routing decision (read-only, local).
    analysis = state["analyzer"].analyze(user_message)
    decision = state["orchestrator"].route(analysis, state["registry"], state["quota"])
    decision_md = format_decision(decision)

    # Then actually call the router over HTTP for a real response.
    import time
    import httpx

    base = _get_router_base_url()
    url = f"{base}/chat/completions"
    payload = {
        "model": model or "auto",
        "messages": [{"role": "user", "content": user_message}],
        "max_tokens": 256,
    }
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as exc:
        return (
            decision_md,
            f"_(Router unreachable: `{exc}`)_",
            "",
            f"❌ Failed to call {url}",
        )
    dt = time.monotonic() - t0

    used_model = body.get("model", "<unknown>")
    content = (body["choices"][0]["message"]["content"] or "").strip()
    u = body.get("usage", {}) or {}
    is_stub = _is_stub_response(body)
    cost = 0.0  # free tier always

    if is_stub:
        response = (
            f"⚠️ **STUB MODE** — router has no live API keys.\n\n"
            f"Reply (placeholder): {content}\n\n"
            f"Set `GEMINI_API_KEY` (or another upstream key) on the router "
            f"and restart it for real responses."
        )
    else:
        response = content

    metrics_md = (
        f"**Model used:** `{used_model}`\n\n"
        f"**HTTP latency:** `{dt:.2f}s`\n\n"
        f"**Tokens:** prompt=`{u.get('prompt_tokens', 0)}` "
        f"completion=`{u.get('completion_tokens', 0)}` "
        f"total=`{u.get('total_tokens', 0)}`\n\n"
        f"**Cost:** `${cost:.6f}` {'(free tier)' if cost == 0.0 else '(paid)'}\n\n"
        f"**Status:** {'🟢 real response' if not is_stub else '🟡 stub'}"
    )
    status = f"Last call: {dt:.2f}s, {u.get('total_tokens', 0)} tokens, ${cost:.4f}"
    return decision_md, response, metrics_md, status


def run_updater(feed_path: str) -> str:
    """Trigger a registry refresh from a local feed file.

    Uses the module-level state via _get_state() so the registry and
    quota manager are shared with the chat tab.
    """
    state = _get_state()
    path = Path(feed_path).expanduser()
    if not path.exists():
        return f"❌ Feed not found: {path}"
    try:
        feed_models = LocalFeedProvider(path).fetch()
        updater = RegistryUpdater(state["registry"], REPO_ROOT / "registry" / "models.json")
        result = updater.apply_feed(feed_models)
        # Re-sync quota store (new models need entries)
        state["quota"].sync_from_registry(state["registry"])
    except Exception as e:  # noqa: BLE001
        return f"❌ Update failed: {e}"

    lines = [
        f"✅ Version: `{result.old_version}` → `{result.new_version}`",
        f"Added: {len(result.added)} | Updated: {len(result.updated)} | "
        f"Removed: {len(result.removed)} | Unchanged: {len(result.unchanged)}",
    ]
    for c in result.changes:
        lines.append(f"  - {c}")
    if result.errors:
        lines.append(f"Errors: {result.errors}")
    return "\n".join(lines)


# --- Gradio app ---

# Module-level state to avoid the gr.State deepcopy restriction
# (ModelRegistry/QuotaManager contain non-deepcopyable bits like fakeredis locks).
_MODULE_STATE: dict | None = None


def _get_state() -> dict:
    global _MODULE_STATE
    if _MODULE_STATE is None:
        _MODULE_STATE = build_state()
    return _MODULE_STATE


def _run_chat_for_gradio(user_message: str, model: str) -> tuple[str, str, str, str]:
    """Gradio callback wrapper: pulls state from module global."""
    return run_chat(_get_state(), user_message, model)


def _refresh_quota_table() -> str:
    return format_quota_table(_get_state()["quota"])


def build_gradio_app():
    import gradio as gr

    # Build the state once and reuse — Gradio will deep-copy it on every
    # click if we pass it via gr.State, which fails for objects like
    # fakeredis that hold locks. Calling _get_state() inside the handler
    # gives us the same instance back without copying.
    state = _get_state()

    with gr.Blocks(title="Hermes QuotaMax Router", theme=gr.themes.Soft()) as app:
        gr.Markdown(
            "# Hermes QuotaMax Router\n"
            "Free-tier-first LLM routing. Decisions are deterministic and explainable."
        )

        with gr.Tab("Chat"):
            with gr.Row():
                with gr.Column(scale=2):
                    msg = gr.Textbox(
                        label="Your request",
                        placeholder="e.g. Refactor this Python function and add pytest coverage",
                        lines=3,
                    )
                    model_choice = gr.Dropdown(
                        label="Model (router decides when set to 'auto')",
                        choices=["auto"],
                        value="auto",
                        info=(
                            "Click 'Refresh models' to populate the list "
                            "from the live router's /v1/models."
                        ),
                    )
                    refresh_models_btn = gr.Button("Refresh models", size="sm")
                    send = gr.Button("Send", variant="primary")
                with gr.Column(scale=3):
                    decision_box = gr.Markdown(label="Routing decision")
                    response_box = gr.Textbox(label="Model response", lines=8)
                    metrics_box = gr.Markdown(label="Live metrics")
                    status_box = gr.Markdown()
            send.click(
                _run_chat_for_gradio, inputs=[msg, model_choice],
                outputs=[decision_box, response_box, metrics_box, status_box],
            )
            refresh_models_btn.click(
                _fetch_live_models, inputs=[],
                outputs=[model_choice],
            )

        with gr.Tab("Registry"):
            quota_md = gr.Markdown(format_quota_table(state["quota"]))
            refresh = gr.Button("Refresh")
            refresh.click(
                _refresh_quota_table, inputs=[],
                outputs=[quota_md],
            )

        with gr.Tab("Updater"):
            feed_path = gr.Textbox(
                label="Feed JSON path",
                value=str(REPO_ROOT / "registry" / "feed_sample.json"),
            )
            update_btn = gr.Button("Apply feed", variant="primary")
            update_out = gr.Markdown()
            update_btn.click(
                run_updater, inputs=[feed_path], outputs=update_out,
            )

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    port = int(os.environ.get("DASHBOARD_PORT", "7860"))
    app = build_gradio_app()
    app.launch(server_name="127.0.0.1", server_port=port, share=False, show_error=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
