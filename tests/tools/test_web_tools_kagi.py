"""Tests for Kagi web backend integration.

Coverage:
  _kagi_post() — API key handling, endpoint construction, error propagation.
  _normalize_kagi_search_results() — current v1 and legacy response shapes.
  _normalize_kagi_extract_results() — markdown extraction and per-URL failures.
  web_search_tool / web_extract_tool — Kagi dispatch paths.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from tests.tools.conftest import register_all_web_providers


class TestKagiRequest:
    """Test suite for the _kagi_post helper."""

    def test_raises_without_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAGI_API_KEY", None)
            from plugins.web.kagi.provider import _kagi_post

            with pytest.raises(ValueError, match="KAGI_API_KEY"):
                _kagi_post("search", {"query": "test"}, timeout=1)

    def test_posts_with_bearer_auth(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"search": []}}

        with patch.dict(os.environ, {"KAGI_API_KEY": "kg-test-key"}):
            with patch("plugins.web.kagi.provider.httpx.post", return_value=mock_response) as mock_post:
                from plugins.web.kagi.provider import _kagi_post

                result = _kagi_post("search", {"query": "hello"}, timeout=1)

        assert result == {"data": {"search": []}}
        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.args[0] == "https://kagi.com/api/v1/search"
        assert call.kwargs["json"] == {"query": "hello"}
        assert call.kwargs["headers"]["Authorization"] == "Bearer kg-test-key"
        assert call.kwargs["timeout"] == 1

    def test_raises_clear_message_on_http_error(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = ""
        mock_response.json.return_value = {
            "error": [{"message": "API credit limit reached"}]
        }

        with patch.dict(os.environ, {"KAGI_API_KEY": "kg-test-key"}):
            with patch("plugins.web.kagi.provider.httpx.post", return_value=mock_response):
                from plugins.web.kagi.provider import _kagi_post

                with pytest.raises(ValueError, match="Kagi rate limit"):
                    _kagi_post("search", {"query": "hello"}, timeout=1)


class TestNormalizeKagiSearchResults:
    """Test Kagi search response normalization."""

    def test_v1_search_response(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_search_results

        raw = {
            "data": {
                "search": [
                    {
                        "title": "Python Docs",
                        "url": "https://docs.python.org",
                        "snippet": "Official Python documentation",
                    },
                    {
                        "title": "Tutorial",
                        "url": "https://example.com",
                        "snippet": "A tutorial",
                    },
                ]
            }
        }
        result = _normalize_kagi_search_results(raw, limit=1)

        assert result["success"] is True
        assert result["data"]["web"] == [
            {
                "title": "Python Docs",
                "url": "https://docs.python.org",
                "description": "Official Python documentation",
                "position": 1,
            }
        ]

    def test_legacy_flat_response_filters_non_web_results(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_search_results

        raw = {
            "data": [
                {
                    "t": 0,
                    "title": "Web result",
                    "url": "https://example.com",
                    "description": "Legacy description",
                },
                {"t": 1, "title": "Related search", "url": "https://ignore.test"},
            ]
        }
        result = _normalize_kagi_search_results(raw)

        assert result["success"] is True
        assert result["data"]["web"] == [
            {
                "title": "Web result",
                "url": "https://example.com",
                "description": "Legacy description",
                "position": 1,
            }
        ]

    def test_empty_response(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_search_results

        result = _normalize_kagi_search_results({"data": {"search": []}})

        assert result["success"] is True
        assert result["data"]["web"] == []


class TestNormalizeKagiExtractResults:
    """Test Kagi extract response normalization."""

    def test_markdown_document(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_extract_results

        docs = _normalize_kagi_extract_results(
            {
                "data": [
                    {
                        "url": "https://example.com",
                        "markdown": "# Example\n\nExtracted content",
                    }
                ]
            },
            ["https://example.com"],
        )

        assert len(docs) == 1
        assert docs[0]["url"] == "https://example.com"
        assert docs[0]["title"] == "Example"
        assert docs[0]["content"] == "# Example\n\nExtracted content"
        assert docs[0]["raw_content"] == "# Example\n\nExtracted content"
        assert docs[0]["metadata"]["provider"] == "kagi"

    def test_preserves_per_url_error(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_extract_results

        docs = _normalize_kagi_extract_results(
            {"data": [{"url": "https://bad.example", "error": "timeout"}]},
            ["https://bad.example"],
        )

        assert docs[0]["url"] == "https://bad.example"
        assert docs[0]["error"] == "timeout"
        assert docs[0]["content"] == ""

    def test_adds_missing_url_errors(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_extract_results

        docs = _normalize_kagi_extract_results(
            {"data": [{"url": "https://one.example", "markdown": "One"}]},
            ["https://one.example", "https://two.example"],
        )

        assert [doc["url"] for doc in docs] == [
            "https://one.example",
            "https://two.example",
        ]
        assert docs[1]["error"] == "Kagi extract response did not include this URL"

    def test_non_list_response_reports_error(self) -> None:
        from plugins.web.kagi.provider import _normalize_kagi_extract_results

        docs = _normalize_kagi_extract_results(
            {"data": {"unexpected": True}},
            ["https://example.com"],
        )

        assert docs == [
            {
                "url": "https://example.com",
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "Kagi extract response did not contain a data list",
                "metadata": {"provider": "kagi"},
            }
        ]


class TestWebSearchKagi:
    """Test web_search_tool dispatch to Kagi."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self) -> None:
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()

    def test_search_dispatches_to_kagi(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "search": [
                    {
                        "title": "Result",
                        "url": "https://r.example",
                        "snippet": "desc",
                    }
                ]
            }
        }

        with patch("tools.web_tools._get_search_backend", return_value="kagi"), \
             patch.dict(os.environ, {"KAGI_API_KEY": "kg-test"}), \
             patch("plugins.web.kagi.provider.httpx.post", return_value=mock_response), \
             patch("tools.interrupt.is_interrupted", return_value=False):
            from tools.web_tools import web_search_tool

            result = json.loads(web_search_tool("test query", limit=3))

        assert result["success"] is True
        assert result["data"]["web"] == [
            {
                "title": "Result",
                "url": "https://r.example",
                "description": "desc",
                "position": 1,
            }
        ]


class TestWebExtractKagi:
    """Test web_extract_tool dispatch to Kagi."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self) -> None:
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()

    def test_extract_dispatches_to_kagi(self) -> None:
        async def _safe_url(_url: str) -> bool:
            return True

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "url": "https://example.com",
                    "markdown": "# Page\n\nExtracted content",
                }
            ]
        }

        with patch("tools.web_tools._get_extract_backend", return_value="kagi"), \
             patch.dict(os.environ, {"KAGI_API_KEY": "kg-test"}), \
             patch("plugins.web.kagi.provider.httpx.post", return_value=mock_response), \
             patch("tools.web_tools.async_is_safe_url", side_effect=_safe_url), \
             patch("tools.web_tools.process_content_with_llm", return_value=None):
            from tools.web_tools import web_extract_tool

            result = json.loads(
                asyncio.run(
                    web_extract_tool(
                        ["https://example.com"],
                        use_llm_processing=False,
                    )
                )
            )

        assert result == {
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Page",
                    "content": "# Page\n\nExtracted content",
                    "error": None,
                }
            ]
        }

    def test_extract_missing_key_returns_per_url_error(self) -> None:
        from plugins.web.kagi.provider import KagiWebSearchProvider

        provider = KagiWebSearchProvider()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAGI_API_KEY", None)
            docs = provider.extract(["https://example.com"])

        assert len(docs) == 1
        assert docs[0]["url"] == "https://example.com"
        assert "KAGI_API_KEY" in docs[0]["error"]
