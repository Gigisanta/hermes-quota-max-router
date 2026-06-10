"""Tests for the Heuristic Task Analyzer."""
import pytest

from core.task_analyzer import HeuristicTaskAnalyzer
from core.schemas import TaskAnalysis


@pytest.fixture
def a() -> HeuristicTaskAnalyzer:
    return HeuristicTaskAnalyzer()


def test_code_task_detects_coding_tags(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Refactor this Python function and add pytest coverage")
    assert "coding_sota" in r.required_tags
    assert "refactoring_god" in r.required_tags
    assert "test_generation" in r.required_tags
    assert r.task_type == "code"


def test_writing_task_detects_writing_tags(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Write a 5000-word essay on the history of Rome")
    assert "writing_master" in r.required_tags
    assert r.task_type == "writing"
    # Writing produces more output than input
    assert r.estimated_output_tokens > r.estimated_input_tokens


def test_vision_task_detects_multimodal(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Analyze this screenshot of the dashboard")
    assert r.needs_multimodal is True
    assert "vision_master" in r.required_tags


def test_long_context_detected(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Read the entire 200k token codebase and summarize it")
    assert r.needs_long_context is True
    assert "200k_plus" in r.required_tags


def test_spanish_language_detected(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Hola, por favor ayúdame con este código en Python")
    assert r.language == "es"


def test_chinese_language_detected(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("请用中文解释一下这段代码的意思")
    assert r.language == "zh"


def test_tool_use_detected(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Use the weather API tool to call and orchestrate the response")
    assert r.needs_tools is True
    assert "tool_master" in r.required_tags


def test_math_detected(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Prove this mathematical theorem step by step")
    assert "math_expert" in r.required_tags
    assert "proof_capable" in r.required_tags


def test_critical_quality_escalates(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("This is critical production code, must be production-grade")
    assert r.min_quality == "exceptional"


def test_normal_request_keeps_default_quality(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("Help me write a quick draft email")
    assert r.min_quality == "high"


def test_empty_message_does_not_crash(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("")
    assert isinstance(r, TaskAnalysis)
    assert r.task_type == "chat"


def test_token_estimates_non_negative(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("test")
    assert r.estimated_input_tokens >= 50  # floor
    assert r.estimated_output_tokens >= 100  # floor


def test_no_duplicate_tags(a: HeuristicTaskAnalyzer) -> None:
    r = a.analyze("code code code python python refactor")
    assert len(r.required_tags) == len(set(r.required_tags))
