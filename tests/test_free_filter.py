"""
Tests for free-tier detection in the auto-discovery parsers.

These verify that:
  1. HuggingFace parser respects private/gated/inference_provider flags
  2. OpenRouter parser correctly identifies :free models
  3. The free-only filter helper does what it says
"""
from __future__ import annotations

from core.catalogs import _parse_huggingface, _parse_openrouter


class TestHuggingFaceFreeFilter:
    def test_private_models_marked_not_free(self):
        data = [{"id": "secret/model", "private": True, "inference_provider": "hf-inference"}]
        out = _parse_huggingface(data)
        assert len(out) == 1
        assert out[0]["is_free"] is False

    def test_gated_models_marked_not_free(self):
        data = [{"id": "meta-llama/Llama-3-70B", "gated": "manual", "inference_provider": "hf-inference"}]
        out = _parse_huggingface(data)
        assert len(out) == 1
        assert out[0]["is_free"] is False
        assert "gated" in out[0]["notes"]

    def test_no_inference_provider_marked_not_free(self):
        """Without an active inference provider, we can't reliably route to it."""
        data = [{"id": "some/model", "inference_provider": None}]
        out = _parse_huggingface(data)
        assert len(out) == 1
        assert out[0]["is_free"] is False

    def test_open_inference_marked_free(self):
        data = [{"id": "Qwen/Qwen2.5-7B-Instruct", "inference_provider": "hf-inference"}]
        out = _parse_huggingface(data)
        assert len(out) == 1
        assert out[0]["is_free"] is True
        assert out[0]["daily_quota_tokens"] == 1_000_000

    def test_empty_data_returns_empty(self):
        assert _parse_huggingface([]) == []
        assert _parse_huggingface({}) == []


class TestOpenRouterFreeFilter:
    def test_explicit_free_suffix_marked_free(self):
        data = {"data": [{
            "id": "qwen/qwen-2.5-72b:free",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 32768,
        }]}
        out = _parse_openrouter(data)
        assert len(out) == 1
        assert out[0]["is_free"] is True

    def test_paid_model_marked_not_free(self):
        data = {"data": [{
            "id": "anthropic/claude-3.5-sonnet",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "context_length": 200000,
        }]}
        out = _parse_openrouter(data)
        assert len(out) == 1
        assert out[0]["is_free"] is False
        assert out[0]["input_price"] == 0.000003

    def test_zero_pricing_without_free_suffix_still_marked_free(self):
        """Even without :free, a 0/0 pricing model is functionally free."""
        data = {"data": [{
            "id": "openai/gpt-oss-20b",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 8192,
        }]}
        out = _parse_openrouter(data)
        assert len(out) == 1
        assert out[0]["is_free"] is True

    def test_skips_models_without_id(self):
        data = {"data": [{"pricing": {"prompt": "0", "completion": "0"}}]}
        out = _parse_openrouter(data)
        assert out == []

    def test_empty_data_returns_empty(self):
        assert _parse_openrouter({"data": []}) == []
        assert _parse_openrouter({}) == []
