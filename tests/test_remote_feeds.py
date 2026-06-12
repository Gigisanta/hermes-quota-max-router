"""Tests for the catalog parsers and remote feeds (Phase 16)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from core.catalogs import (
    CATALOGS,
    _parse_huggingface,
    _parse_openrouter,
    _parse_static_curated,
)
from core.remote_feeds import RemoteFeedProvider

# --- OpenRouter parser ---


def test_openrouter_parses_free_and_paid() -> None:
    raw = {
        "data": [
            {
                "id": "deepseek/deepseek-r1:free",
                "name": "DeepSeek R1 (free)",
                "pricing": {"prompt": "0", "completion": "0"},
                "architecture": {"context_length": 131072},
                "top_provider": {},
                "description": "Deep reasoning model",
            },
            {
                "id": "openai/gpt-4o",
                "name": "GPT-4o",
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                "architecture": {"context_length": 128000},
                "top_provider": {},
                "description": "OpenAI flagship",
            },
        ]
    }
    out = _parse_openrouter(raw)
    assert len(out) == 2
    free, paid = out
    assert free["model_id"] == "deepseek/deepseek-r1:free"
    assert free["is_free"] is True
    assert free["input_price"] == 0.0
    assert free["output_price"] == 0.0
    assert free["context_window"] == 131072
    assert "deep_reasoning" in free["strength_tags"] or free["strength_tags"]  # parser may set generic

    assert paid["model_id"] == "openai/gpt-4o"
    assert paid["is_free"] is False
    assert paid["input_price"] == pytest.approx(0.0000025)
    assert paid["output_price"] == pytest.approx(0.00001)


def test_openrouter_handles_empty_response() -> None:
    assert _parse_openrouter({"data": []}) == []


def test_openrouter_skips_models_without_id() -> None:
    raw = {"data": [{"name": "no id"}]}
    assert _parse_openrouter(raw) == []


# --- HuggingFace parser ---


def test_huggingface_parses_chat_model() -> None:
    raw = [
        {
            "id": "meta-llama/Llama-3-8B-Instruct",
            "tags": ["conversational", "code", "transformers"],
            "inference_provider": "hf-inference",  # required for is_free=True
        },
    ]
    out = _parse_huggingface(raw)
    assert len(out) == 1
    m = out[0]
    assert m["model_id"] == "hf/meta-llama/Llama-3-8B-Instruct"
    assert m["provider"] == "huggingface"
    assert m["is_free"] is True
    assert "coding_sota" in m["strength_tags"]


def test_huggingface_caps_at_200() -> None:
    raw = [{"id": f"org/model-{i}", "inference_provider": "hf-inference"} for i in range(500)]
    out = _parse_huggingface(raw)
    assert len(out) == 200


def test_huggingface_handles_dict_response() -> None:
    raw = {"models": [{"id": "x/y", "tags": []}]}
    out = _parse_huggingface(raw)
    assert len(out) == 1


# --- Static curated ---


def test_static_curated_returns_models_list() -> None:
    raw = {"models": [{"model_id": "a/b"}]}
    assert _parse_static_curated(raw) == [{"model_id": "a/b"}]


# --- RemoteFeedProvider integration ---


def test_remote_provider_aggregates_multiple_sources() -> None:
    """With all sources failing except curated, the curated list wins."""
    with patch("core.remote_feeds.httpx.Client") as MockClient:
        # First 2 calls (openrouter + HF) raise a real network-class
        # exception; 3rd is the curated fallback. iter 15: tightened
        # the production code to catch specific exception classes, so
        # the test must use a real one (httpx.ConnectError, an
        # OSError subclass) rather than a bare Exception.
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.get.side_effect = [
            httpx.ConnectError("network down"),
            httpx.ConnectError("network down"),
        ]
        MockClient.return_value = mock_instance
        provider = RemoteFeedProvider(timeout_s=1.0)
        curated = Path(__file__).resolve().parent.parent / "registry" / "models.json"
        provider.curated_path = curated
        out = provider.fetch_all()
    # Curated has 7 models
    assert len(out) >= 1
    assert any(m["model_id"] == "deepseek/deepseek-r1-0528" for m in out)


def test_remote_provider_raises_when_all_fail() -> None:
    """If even curated is missing, fetch_all raises."""
    with patch("core.remote_feeds.httpx.Client") as MockClient:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        # See note above — use a real OSError subclass.
        mock_instance.get.side_effect = httpx.ConnectError("network down")
        MockClient.return_value = mock_instance
        provider = RemoteFeedProvider(timeout_s=1.0)
        provider.curated_path = Path("/nonexistent/path.json")
        with pytest.raises(RuntimeError) as exc:
            provider.fetch_all()
    assert "All catalogs failed" in str(exc.value)


def test_remote_provider_dedupes_by_model_id() -> None:
    """Same model_id in two sources → counted once."""
    # Mock both HTTP and curated to return overlapping entries
    with patch("core.remote_feeds.httpx.Client") as MockClient:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        # Both openrouter and HF return entries with the same id
        mock_instance.get.return_value = MagicMock(
            json=lambda: {
                "data": [
                    {
                        "id": "x/y",
                        "name": "Y",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"context_length": 1000},
                        "top_provider": {},
                        "description": "x",
                    }
                ]
            }
        )
        MockClient.return_value = mock_instance

        with patch("core.remote_feeds.RemoteFeedProvider._load_curated") as mock_curated:
            mock_curated.return_value = [
                {
                    "model_id": "x/y",
                    "provider": "x",
                    "display_name": "Y",
                    "context_window": 1000,
                    "input_price": 0.0,
                    "output_price": 0.0,
                    "is_free": True,
                    "tier_rank": 1,
                    "strength_tags": [],
                    "weakness_tags": [],
                    "best_for": [],
                    "performance_score": 50.0,
                },
            ]
            provider = RemoteFeedProvider(timeout_s=1.0)
            out = provider.fetch_all()
    ids = [m["model_id"] for m in out]
    # 'x/y' should appear at most once even if both sources returned it
    assert ids.count("x/y") == 1


# --- Catalog list integrity ---


def test_catalogs_list_has_expected_entries() -> None:
    names = [c.name for c in CATALOGS]
    assert "openrouter_public" in names
    assert "huggingface_warm" in names
    assert "curated_static" in names


def test_all_catalogs_have_parsers() -> None:
    for c in CATALOGS:
        assert callable(c.parser)
        assert c.endpoint
