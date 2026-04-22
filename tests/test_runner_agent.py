from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.runner_agent import detect_provider, get_api_key_for_provider, load_pricing


def test_detect_provider_uses_model_prefix():
    assert detect_provider("gemini-3-flash-preview") == "gemini"
    assert detect_provider("claude-sonnet-4-5") == "anthropic"


def test_get_api_key_for_provider_prefers_google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert get_api_key_for_provider("gemini") == "google-key"


def test_get_api_key_for_provider_falls_back_to_gemini_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert get_api_key_for_provider("gemini") == "gemini-key"


def test_load_pricing_defaults_to_zero_for_gemini_without_file():
    pricing = load_pricing(None, "gemini")
    assert pricing == {
        "input_base_per_1m": 0.0,
        "cache_write_per_1m": 0.0,
        "cache_read_per_1m": 0.0,
        "output_per_1m": 0.0,
    }


def test_load_pricing_file_overrides_provider_defaults(tmp_path):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(
        '{"input_base_per_1m": 1.5, "cache_write_per_1m": 0.25, "cache_read_per_1m": 0.1, "output_per_1m": 4.0}',
        encoding="utf-8",
    )
    pricing = load_pricing(pricing_file, "gemini")
    assert pricing == {
        "input_base_per_1m": 1.5,
        "cache_write_per_1m": 0.25,
        "cache_read_per_1m": 0.1,
        "output_per_1m": 4.0,
    }