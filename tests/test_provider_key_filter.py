"""
Tests for has_key_for_model — the env-based provider availability filter.
"""
from __future__ import annotations

from dataclasses import dataclass
import pytest

from core.orchestrator import has_key_for_model


@dataclass
class FakeModel:
    """Minimal stand-in: only `provider` is consulted by the filter."""
    provider: str
    model_id: str = "x/y"


class TestHasKeyForModel:
    def test_unknown_provider_returns_true(self, monkeypatch):
        """A provider with no entry in the env map is treated as 'no auth'."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        m = FakeModel(provider="self-hosted-llm")
        assert has_key_for_model(m) is True

    def test_known_provider_with_key_returns_true(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        m = FakeModel(provider="gemini")
        assert has_key_for_model(m) is True

    def test_known_provider_without_key_returns_false(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        m = FakeModel(provider="gemini")
        assert has_key_for_model(m) is False

    def test_known_provider_with_empty_key_returns_false(self, monkeypatch):
        """An empty env var counts as 'no key' — empty string is falsy."""
        monkeypatch.setenv("GEMINI_API_KEY", "   ")
        m = FakeModel(provider="gemini")
        assert has_key_for_model(m) is False

    def test_google_and_gemini_share_gemini_key(self, monkeypatch):
        """OpenRouter routes google/* through GEMINI_API_KEY."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        google = FakeModel(provider="google")
        openrouter = FakeModel(provider="openrouter")
        assert has_key_for_model(google) is True
        assert has_key_for_model(openrouter) is False

    def test_openrouter_requires_its_own_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        or_model = FakeModel(provider="openrouter")
        ds_model = FakeModel(provider="deepseek")
        assert has_key_for_model(or_model) is True
        assert has_key_for_model(ds_model) is False
