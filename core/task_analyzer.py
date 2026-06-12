"""Task Analyzer — extracts semantic requirements from a user message.

Two backends behind the same interface:
  - HeuristicTaskAnalyzer: deterministic keyword/regex based, zero dependencies,
    suitable for tests, dev, and cold-start fallback.
  - LLMTaskAnalyzer: uses LiteLLM with prompts/task_analyzer.md for real
    semantic understanding in production.

Usage:
  analyzer = HeuristicTaskAnalyzer()
  analysis = analyzer.analyze("Refactor this Python function and add tests")
  # -> TaskAnalysis(required_tags=["coding_sota", "test_generation", "refactoring_god"],
  #                 task_type="code", needs_tools=False, ...)
"""
from __future__ import annotations

import logging
import re
from typing import Protocol

from .schemas import TaskAnalysis

log = logging.getLogger(__name__)


# Keyword -> tag mapping. Conservative: only fires on word boundaries
# (or quoted phrases for multi-word) to keep false positives down.
TAG_KEYWORDS: dict[str, list[str]] = {
    # Coding
    r"\b(code|coding|python|javascript|typescript|rust|go|java|c\+\+)\b": ["coding_sota"],
    r"\b(debug|bug|fix|traceback|error in)\b": ["debugging_expert", "coding_sota"],
    r"\b(refactor|restructure|cleanup|simplify)\b": ["refactoring_god", "coding_sota"],
    r"\b(test|pytest|unittest|test suite|coverage)\b": ["test_generation", "coding_sota"],

    # Reasoning / agentic
    r"\b(reason|chain of thought|step by step|analyze deeply)\b": ["deep_reasoning", "long_chain_of_thought"],
    r"\b(agent|tool.?call|function.?call|use api|orchestrate)\b": ["tool_master", "parallel_tool_use", "agentic_god"],
    r"\b(critique|review|self.?reflect|grade)\b": ["self_reflection", "critique_master"],

    # Math
    r"\b(math|mathematical|prove|proof|olympiad|integral|equation)\b": ["math_expert", "proof_capable"],

    # Long context
    r"\b(long|extensive|200k|book|novel|entire document|whole codebase)\b": ["long_context_king", "long_coherence", "200k_plus"],

    # Speed / volume
    r"\b(fast|quick|rapid|brief|draft)\b": ["ultra_fast", "high_throughput"],
    r"\b(many|massive|hundreds|bulk|thousands of)\b": ["high_volume", "cheap_parallel"],

    # Multimodal
    r"\b(image|photo|picture|screenshot|vision|visual)\b": ["vision_master", "image_analysis", "multimodal"],
    r"\b(video|clip|footage)\b": ["video_understanding", "multimodal"],

    # Language hints
    r"(?i)\b(chinese|mandarin|cantonese|中文)\b": ["chinese_strong", "bilingual_perfect"],
    r"(?i)\b(in english|respond in english|en inglés)\b": ["bilingual_perfect"],

    # Writing
    r"\b(write|essay|story|narrative|novel|blog post|article)\b": ["writing_master", "narrative_coherence"],
    r"\b(roleplay|character|dialogue|persona)\b": ["roleplay_god", "writing_master"],

    # Output format
    r"\b(json|structured|schema|valid output)\b": ["json_mode_perfect", "structured_output"],
    r"\b(summarize|summary|tldr|recap)\b": ["summarization_expert"],
    r"\b(research|investigate|survey|deep dive)\b": ["research_master"],
}

LANG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\u4e00-\u9fff]"), "zh"),
    (re.compile(r"\b(hola|gracias|por favor|cómo|qué|cuál)\b", re.IGNORECASE), "es"),
]

# Tokens-per-word rough heuristic by language
WORD_TOKEN_RATIO = {
    "en": 1.3,
    "es": 1.4,  # Spanish runs ~10% longer
    "zh": 1.8,  # Chinese is denser; tokens ≈ chars
    "mixed": 1.4,
}


class TaskAnalyzer(Protocol):
    def analyze(self, message: str, history: list[dict] | None = None) -> TaskAnalysis: ...


class HeuristicTaskAnalyzer:
    """Deterministic, dependency-free analyzer.

    Sufficient for unit tests, dev without LLM keys, and the spec's
    'fast pre-filter' use case. The LLM analyzer is used when keys
    are available and the request warrants the extra latency.
    """

    def analyze(self, message: str, history: list[dict] | None = None) -> TaskAnalysis:
        text = (message or "").strip()
        lower = text.lower()

        # --- tags ---
        tags: list[str] = []
        for pattern, mapped in TAG_KEYWORDS.items():
            if re.search(pattern, lower):
                for t in mapped:
                    if t not in tags:
                        tags.append(t)

        # --- task type (first match wins) ---
        # iter 15: removed the dead `task_type: TaskAnalysis.model_fields[...]`
        # line that was immediately overwritten below.
        task_type = "chat"  # default
        type_rules: list[tuple[str, str]] = [
            ("code", r"\b(code|debug|refactor|test|implement|function|class|compile|build)\b"),
            ("research", r"\b(research|investigate|survey|deep dive|compare|benchmark)\b"),
            ("writing", r"\b(write|essay|story|narrative|article|blog|draft a)\b"),
            ("analysis", r"\b(analy[sz]e|breakdown|examine|interpret|review the)\b"),
            ("planning", r"\b(plan|roadmap|strategy|design|architect)\b"),
            ("extraction", r"\b(extract|parse|pull out|list all|find all)\b"),
        ]
        for ttype, pat in type_rules:
            if re.search(pat, lower):
                task_type = ttype  # type: ignore[assignment]
                break

        # --- needs ---
        needs_tools = any(t in tags for t in ("tool_master", "parallel_tool_use", "agentic_god"))
        needs_multimodal = any(t in tags for t in ("vision_master", "image_analysis", "video_understanding", "multimodal"))
        needs_long_context = any(t in tags for t in ("long_context_king", "long_coherence", "200k_plus"))

        # --- quality ---
        min_quality: str = "high"
        if re.search(r"\b(critical|production|core|essential|mission.?critical)\b", lower):
            min_quality = "exceptional"
        elif re.search(r"\b(important|high.?quality|careful|thorough)\b", lower):
            min_quality = "very_high"

        # --- language ---
        language = "en"
        for pat, code in LANG_PATTERNS:
            if pat.search(text):
                language = code
                break
        if re.search(r"(?i)\b(in english|responde en inglés)\b", text):
            language = "en"
        if re.search(r"(?i)\b(en español|responde en español)\b", text):
            language = "es"

        # --- token estimates ---
        words = len(text.split())
        ratio = WORD_TOKEN_RATIO.get(language, 1.4)
        est_in = max(50, int(words * ratio))
        # Output rough default: 3x input for reasoning/coding, 2x for writing, 1.5x for chat
        mult = 3.0 if any(t in tags for t in ("deep_reasoning", "long_chain_of_thought", "coding_sota")) else \
               2.0 if task_type in ("writing", "research") else 1.5
        est_out = max(100, int(est_in * mult))

        # --- notes ---
        notes = (text[:120] + "…") if len(text) > 120 else text

        return TaskAnalysis(
            required_tags=tags,
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
            needs_tools=needs_tools,
            needs_multimodal=needs_multimodal,
            needs_long_context=needs_long_context,
            min_quality=min_quality,  # type: ignore[arg-type]
            language=language,  # type: ignore[arg-type]
            task_type=task_type,  # type: ignore[arg-type]
            notes=notes,
        )


class LLMTaskAnalyzer:
    """LiteLLM-backed analyzer using prompts/task_analyzer.md.

    Requires the orchestrator LLM API keys to be available. Used in
    production where the extra semantic accuracy matters.
    """

    DEFAULT_MODEL = "gemini/gemini-2.5-flash"  # free, fast, good extraction
    PROMPT_PATH = "prompts/task_analyzer.md"

    def __init__(self, model: str | None = None, prompt_path: str | None = None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self.prompt_path = prompt_path or self.PROMPT_PATH

    def analyze(self, message: str, history: list[dict] | None = None) -> TaskAnalysis:
        from pathlib import Path
        import json

        from litellm import completion

        system_prompt = Path(self.prompt_path).read_text()
        user_payload = {
            "message": message,
            "history": history or [],
        }
        resp = completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        text = resp["choices"][0]["message"]["content"]
        data = json.loads(text)
        return TaskAnalysis(**data)


if __name__ == "__main__":
    a = HeuristicTaskAnalyzer()
    for msg in [
        "Refactor this Python function and add pytest coverage",
        "Escribe un ensayo de 5000 palabras sobre la historia de Roma",
        "Analyze this screenshot of the dashboard and summarize the issues",
        "Prove the Riemann hypothesis step by step using chain of thought",
    ]:
        r = a.analyze(msg)
        print(f"\n>>> {msg[:60]}")
        print(f"    type={r.task_type} lang={r.language} quality={r.min_quality}")
        print(f"    tags={r.required_tags}")
        print(f"    est_in={r.estimated_input_tokens} est_out={r.estimated_output_tokens}")
