"""Gradio Dashboard — Phase 5 (visual layer).

Zero-config: `python dashboard/app.py` and a browser tab opens at the
printed URL. The dashboard exposes:

  1. **Chat**: type a request → see the orchestrator's routing decision
     and the model's response (or MoA synthesis).
  2. **Registry tab**: live snapshot of all models + their quotas.
  3. **Updater tab**: trigger a manual registry refresh from a local feed.

The dashboard is READ-ONLY by default against real LLM APIs — it only
calls models when the user actually sends a chat message. If no API
keys are present, the chat shows a friendly stub.
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


def run_chat(state: dict, user_message: str) -> tuple[str, str, str]:
    """Process a chat turn. Returns (decision_md, response_text, status)."""
    if not user_message.strip():
        return "_Type a request above and hit Send._", "", ""

    analysis = state["analyzer"].analyze(user_message)
    decision = state["orchestrator"].route(analysis, state["registry"], state["quota"])

    decision_md = format_decision(decision)
    response = (
        f"_(Demo mode — no live LLM call.)\n\n"
        f"Orchestrator chose **{decision.primary_model}** "
        f"with strategy **{decision.chosen_strategy}** "
        f"(confidence {decision.confidence:.2f}).\n\n"
        f"In production, this would invoke LiteLLM with that model and "
        f"return the response here._"
    )
    status = (
        f"Tokens reserved: {decision.estimated_tokens} (dry-run, "
        f"not actually consumed in demo mode)"
    )
    return decision_md, response, status


def run_updater(state: dict, feed_path: str) -> str:
    """Trigger a registry refresh from a local feed file."""
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

def build_gradio_app():
    import gradio as gr

    state = build_state()

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
                    send = gr.Button("Send", variant="primary")
                with gr.Column(scale=3):
                    decision_box = gr.Markdown(label="Routing decision")
                    response_box = gr.Textbox(label="Model response", lines=8)
                    status_box = gr.Markdown()
            send.click(
                run_chat, inputs=[gr.State(state), msg],
                outputs=[decision_box, response_box, status_box],
            )

        with gr.Tab("Registry"):
            quota_md = gr.Markdown(format_quota_table(state["quota"]))
            refresh = gr.Button("Refresh")
            refresh.click(
                lambda qm: format_quota_table(qm),
                inputs=gr.State(state["quota"]),
                outputs=quota_md,
            )

        with gr.Tab("Updater"):
            feed_path = gr.Textbox(
                label="Feed JSON path",
                value=str(REPO_ROOT / "registry" / "feed_sample.json"),
            )
            update_btn = gr.Button("Apply feed", variant="primary")
            update_out = gr.Markdown()
            update_btn.click(
                run_updater, inputs=[gr.State(state), feed_path], outputs=update_out,
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
