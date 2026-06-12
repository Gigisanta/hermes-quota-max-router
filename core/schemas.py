"""Shared Pydantic schemas for the routing pipeline.

TaskAnalysis is the output of the Task Analyzer.
RoutingDecision is the output of the Orchestrator (matches spec §6 exactly).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TaskAnalysis(BaseModel):
    """Structured task requirements (output of task_analyzer.md)."""

    required_tags: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    needs_tools: bool = False
    needs_multimodal: bool = False
    needs_long_context: bool = False
    min_quality: Literal["high", "very_high", "exceptional"] = "high"
    language: Literal["es", "en", "zh", "mixed"] = "en"
    task_type: Literal[
        "code",
        "research",
        "writing",
        "analysis",
        "planning",
        "extraction",
        "chat",
    ] = "chat"
    notes: str = ""

    @field_validator("estimated_input_tokens", "estimated_output_tokens")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        return max(0, int(v))


class RoutingDecision(BaseModel):
    """Structured routing decision (output of orchestrator_system.md).

    Exact field names from spec §6.
    """

    chosen_strategy: Literal["direct", "moa", "critique", "multi_step", "fallback"] = "direct"
    primary_model: str
    fallback_model: str | None = None
    models_to_use: list[str] = Field(default_factory=list)
    reasoning: str
    estimated_tokens: int = 0
    quality_expectation: Literal["high", "very_high", "exceptional"] = "high"
    preserve_paid_quota: bool = True
    tags_matched: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
