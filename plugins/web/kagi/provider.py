"""Kagi web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Two
capabilities advertised:

- ``supports_search()``  -> True (Kagi v1 ``/search``)
- ``supports_extract()`` -> True (Kagi v1 ``/extract``)

Both are sync — the underlying calls are ``httpx.post(...)``.

Config keys this provider responds to::

    web:
      search_backend: "kagi"     # explicit per-capability
      extract_backend: "kagi"    # explicit per-capability
      backend: "kagi"            # shared fallback for both

Env vars::

    KAGI_API_KEY=...             # https://kagi.com/api/keys (required)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterable, List

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_KAGI_API_BASE = "https://kagi.com/api/v1"
_KAGI_SEARCH_TIMEOUT_SECONDS = 30.0
_KAGI_EXTRACT_TIMEOUT_SECONDS = 60.0
_KAGI_EXTRACT_BATCH_SIZE = 10
_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _kagi_api_key() -> str:
    api_key = os.getenv("KAGI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "KAGI_API_KEY environment variable not set. "
            "Create an API key at https://kagi.com/api/keys"
        )
    return api_key


def _kagi_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_kagi_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _coerce_kagi_error(response: httpx.Response) -> str:
    """Return a concise Kagi error message from an HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    details: list[str] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, list):
            for item in error:
                if isinstance(item, dict):
                    msg = item.get("message") or item.get("code")
                    if msg:
                        details.append(str(msg))
                elif item:
                    details.append(str(item))
        elif error:
            details.append(str(error))

    detail = "; ".join(details) or response.text.strip()
    if response.status_code in {401, 403}:
        prefix = "Kagi authentication failed"
    elif response.status_code == 429:
        prefix = "Kagi rate limit or API credit limit reached"
    else:
        prefix = "Kagi API request failed"

    if detail:
        return f"{prefix} ({response.status_code}): {detail}"
    return f"{prefix} ({response.status_code})"


def _kagi_post(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    timeout: float,
) -> Dict[str, Any]:
    url = f"{_KAGI_API_BASE}/{endpoint.lstrip('/')}"
    response = httpx.post(url, json=payload, headers=_kagi_headers(), timeout=timeout)
    if response.status_code >= 400:
        raise ValueError(_coerce_kagi_error(response))
    return response.json()


def _iter_search_results(raw: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield Kagi web search result dictionaries from v1 and legacy shapes."""
    data = raw.get("data")
    if isinstance(data, dict):
        results = data.get("search", [])
        if isinstance(results, list):
            yield from (r for r in results if isinstance(r, dict))
        return

    # Legacy / kagiapi 0.x shape used a flat data list with type markers.
    if isinstance(data, list):
        for result in data:
            if isinstance(result, dict) and result.get("t") in {0, None}:
                yield result


def _normalize_kagi_search_results(raw: Dict[str, Any], limit: int = 5) -> Dict[str, Any]:
    """Map Kagi ``/search`` response to ``{success, data: {web: [...]}}``."""
    web_results: list[dict[str, Any]] = []
    for result in _iter_search_results(raw):
        url = str(result.get("url") or "").strip()
        title = str(result.get("title") or "").strip()
        snippet = str(result.get("snippet") or result.get("description") or "").strip()
        if not url and not title and not snippet:
            continue
        web_results.append(
            {
                "title": title,
                "url": url,
                "description": snippet,
                "position": len(web_results) + 1,
            }
        )
        if len(web_results) >= limit:
            break
    return {"success": True, "data": {"web": web_results}}


def _title_from_markdown(markdown: str) -> str:
    match = _HEADING_RE.search(markdown or "")
    return match.group(1).strip() if match else ""


def _kagi_error_messages(raw: Dict[str, Any]) -> list[str]:
    """Return concise messages from Kagi's optional top-level error list."""
    errors = raw.get("errors")
    if not isinstance(errors, list):
        return []

    messages: list[str] = []
    for error in errors:
        if isinstance(error, dict):
            message = (
                error.get("message")
                or error.get("detail")
                or error.get("error")
                or error.get("code")
            )
            if not message:
                continue
            location = error.get("location") or error.get("loc") or error.get("path")
            if isinstance(location, list):
                location_text = ".".join(str(part) for part in location)
            elif location:
                location_text = str(location)
            else:
                location_text = ""
            if location_text:
                messages.append(f"{location_text}: {message}")
            else:
                messages.append(str(message))
        elif error:
            messages.append(str(error))
    return messages


def _normalize_kagi_extract_result(result: Dict[str, Any], fallback_url: str = "") -> Dict[str, Any]:
    """Map a single Kagi extract page result to a Hermes document entry."""
    url = str(result.get("url") or fallback_url)
    markdown = result.get("markdown")
    if markdown is None:
        markdown = result.get("content", "")
    markdown = str(markdown or "")
    title = str(result.get("title") or "").strip() or _title_from_markdown(markdown)
    document = {
        "url": url,
        "title": title,
        "content": markdown,
        "raw_content": markdown,
        "metadata": {"sourceURL": url, "provider": "kagi"},
    }
    error = result.get("error")
    if error:
        document["error"] = str(error)
    return document


def _normalize_kagi_extract_results(
    raw: Dict[str, Any],
    fallback_urls: list[str] | None = None,
) -> List[Dict[str, Any]]:
    """Map Kagi ``/extract`` response to standard document entries."""
    fallback_urls = fallback_urls or []
    top_level_errors = _kagi_error_messages(raw)
    data = raw.get("data", [])
    if not isinstance(data, list):
        error = "Kagi extract response did not contain a data list"
        if top_level_errors:
            error = f"{error}: {'; '.join(top_level_errors)}"
        return [
            {
                "url": fallback_urls[0] if fallback_urls else "",
                "title": "",
                "content": "",
                "raw_content": "",
                "error": error,
                "metadata": {"provider": "kagi"},
            }
        ]

    documents: list[dict[str, Any]] = []
    for i, result in enumerate(data):
        fallback_url = fallback_urls[i] if i < len(fallback_urls) else ""
        if isinstance(result, dict):
            documents.append(_normalize_kagi_extract_result(result, fallback_url))
        else:
            documents.append(
                {
                    "url": fallback_url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": f"Unexpected Kagi extract result: {result!r}",
                    "metadata": {"sourceURL": fallback_url, "provider": "kagi"},
                }
            )

    if not documents and top_level_errors:
        for fallback_url in fallback_urls:
            documents.append(
                {
                    "url": fallback_url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": "; ".join(top_level_errors),
                    "metadata": {"sourceURL": fallback_url, "provider": "kagi"},
                }
            )

    seen = {doc.get("url") for doc in documents}
    for url in fallback_urls:
        if url not in seen:
            documents.append(
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": "Kagi extract response did not include this URL",
                    "metadata": {"sourceURL": url, "provider": "kagi"},
                }
            )
    return documents


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class KagiWebSearchProvider(WebSearchProvider):
    """Kagi search + extract provider."""

    @property
    def name(self) -> str:
        return "kagi"

    @property
    def display_name(self) -> str:
        return "Kagi"

    def is_available(self) -> bool:
        """Return True when ``KAGI_API_KEY`` is set to a non-empty value."""
        return bool(os.getenv("KAGI_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Kagi search."""
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            limit = min(max(int(limit), 1), 100)
            logger.info("Kagi search: '%s' (limit=%d)", query, limit)
            raw = _kagi_post(
                "search",
                {
                    "query": query,
                    "workflow": "search",
                    "limit": limit,
                    "format": "json",
                },
                timeout=_KAGI_SEARCH_TIMEOUT_SECONDS,
            )
            return _normalize_kagi_search_results(raw, limit)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — including httpx errors
            logger.warning("Kagi search error: %s", exc)
            return {"success": False, "error": f"Kagi search failed: {exc}"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract markdown content from one or more URLs via Kagi."""
        del kwargs
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "title": "", "content": "", "error": "Interrupted"}
                    for u in urls
                ]

            # Validate key once so missing credentials produce per-URL entries.
            _kagi_api_key()
            documents: list[dict[str, Any]] = []
            logger.info("Kagi extract: %d URL(s)", len(urls))
            for batch in _chunks(list(urls), _KAGI_EXTRACT_BATCH_SIZE):
                raw = _kagi_post(
                    "extract",
                    {
                        "pages": [{"url": url} for url in batch],
                        # Hermes' format kwarg describes our output preference;
                        # Kagi must return JSON so we can preserve per-URL errors.
                        "format": "json",
                    },
                    timeout=_KAGI_EXTRACT_TIMEOUT_SECONDS,
                )
                documents.extend(_normalize_kagi_extract_results(raw, batch))
            return documents
        except ValueError as exc:
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": str(exc),
                    "metadata": {"sourceURL": u, "provider": "kagi"},
                }
                for u in urls
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kagi extract error: %s", exc)
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": f"Kagi extract failed: {exc}",
                    "metadata": {"sourceURL": u, "provider": "kagi"},
                }
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Kagi",
            "badge": "paid",
            "tag": "Premium search + markdown extraction.",
            "env_vars": [
                {
                    "key": "KAGI_API_KEY",
                    "prompt": "Kagi API key",
                    "url": "https://kagi.com/api/keys",
                },
            ],
        }
