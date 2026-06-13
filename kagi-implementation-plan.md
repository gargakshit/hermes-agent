# Kagi Web Search and Extract Implementation Plan

## Goal

Add Kagi as a first-class Hermes web backend for both `web_search` and
`web_extract`.

The implementation should expose Kagi through the existing web-provider plugin
surface:

```yaml
web:
  search_backend: kagi
  extract_backend: kagi
```

Secrets stay in `~/.hermes/.env`:

```bash
KAGI_API_KEY=...
```

This should not add a new model tool. Hermes already has `web_search` and
`web_extract`; Kagi is a backend behind those tools.

## Current Repo Shape

Hermes already has the right abstraction:

- `agent/web_search_provider.py` defines `WebSearchProvider`.
- Bundled providers live under `plugins/web/<name>/`.
- Providers register via `ctx.register_web_search_provider(...)` in
  `plugins/web/<name>/__init__.py`.
- `tools/web_tools.py` selects the active backend from:
  - `web.search_backend`
  - `web.extract_backend`
  - `web.backend`
  - env-key autodetection
- `hermes_cli/tools_config.py` discovers provider setup rows from
  `provider.get_setup_schema()`.

Existing examples to mirror:

- `plugins/web/tavily/provider.py` for a compact search + extract provider.
- `plugins/web/exa/provider.py` for provider-specific normalizers and client
  helpers.
- `tests/tools/test_web_tools_tavily.py` for dispatch and response-shape tests.
- `tests/plugins/web/test_web_search_provider_plugins.py` for plugin discovery.

## External API Facts To Respect

Source references checked on 2026-06-13:

- Kagi API Portal: https://help.kagi.com/kagi/api/overview.html
- Kagi OpenAPI spec: https://kagi.com/api/docs/_spec/openapi.yaml
- Official Kagi MCP server: https://github.com/kagisearch/kagimcp

Relevant current facts:

- Kagi's API portal manages API keys, usage, billing, and API credit.
- The current OpenAPI spec uses `https://kagi.com/api/v1` as the server and
  Bearer authentication:

  ```http
  Authorization: Bearer <KAGI_API_KEY>
  ```

- Search is `POST /search` with JSON body fields including required `query`
  and optional `workflow`, `format`, `limit`, and `extract`.
- Extract is `POST /extract` with JSON body:

  ```json
  {
    "pages": [{"url": "https://example.com"}],
    "format": "json"
  }
  ```

  It accepts 1-10 HTTP(S) URLs per request and returns per-page markdown
  entries plus optional errors.
- The API portal and official MCP server support the new Search and Extract
  APIs.
- The official MCP README describes:
  - `kagi_search_fetch`: web/news/video/podcast/image search, optional extracts.
  - `kagi_extract`: fetch a page's full content as markdown.
- The old Universal Summarizer is a separate API. It summarizes content and is
  not a drop-in replacement for Hermes `web_extract`, which expects page
  content. Do not silently implement `web_extract` with the summarizer unless
  the user explicitly accepts "summarized extract" semantics.

## Design Choice

Implement `kagi` as a bundled web provider:

```text
plugins/web/kagi/
  plugin.yaml
  __init__.py
  provider.py
```

Why:

- This matches the current provider architecture.
- It adds zero model-tool footprint.
- It appears automatically in `hermes tools` through `get_setup_schema()`.
- It preserves prompt caching because the tool schema does not change.

No new dependency should be needed. Use `httpx`, already present in Hermes.
Avoid depending on `kagiapi` unless direct REST access is not viable; adding an
SDK would require lazy dependency wiring and more lockfile churn.

## User-Facing Behavior

Setup options:

```bash
hermes config set web.search_backend kagi
hermes config set web.extract_backend kagi
```

or via YAML:

```yaml
web:
  search_backend: kagi
  extract_backend: kagi
```

Required secret:

```bash
KAGI_API_KEY=...
```

Expected `hermes doctor` / tool availability behavior:

- With `KAGI_API_KEY` set and either backend explicitly configured, web tools
  should be available.
- Without `KAGI_API_KEY`, an explicit `kagi` backend should fail clearly with
  guidance to configure `KAGI_API_KEY`.
- Without explicit config, `KAGI_API_KEY` should allow autodetect to select
  Kagi before keyless/free fallback backends.

## Files To Add

### `plugins/web/kagi/plugin.yaml`

Use the same backend-plugin shape as other bundled web providers:

```yaml
name: web-kagi
version: 1.0.0
kind: backend
provides_web_providers:
  - kagi
```

Keep metadata consistent with neighboring providers.

### `plugins/web/kagi/__init__.py`

Register the provider:

```python
from .provider import KagiWebSearchProvider


def register(ctx):
    ctx.register_web_search_provider(KagiWebSearchProvider())
```

### `plugins/web/kagi/provider.py`

Implement:

- `KagiWebSearchProvider.name -> "kagi"`
- `display_name -> "Kagi"`
- `is_available() -> bool(os.getenv("KAGI_API_KEY", "").strip())`
- `supports_search() -> True`
- `supports_extract() -> True`
- `search(query, limit=5)`
- `extract(urls, **kwargs)`
- `get_setup_schema()`

Helper functions:

- `_kagi_api_key() -> str`
- `_kagi_headers() -> dict[str, str]`
- `_kagi_post(endpoint, payload, timeout) -> dict`
- `_normalize_kagi_search_results(raw, limit) -> dict`
- `_normalize_kagi_extract_document(raw, fallback_url) -> dict`
- `_normalize_kagi_extract_results(raw, fallback_url) -> list[dict]`

Suggested constants:

```python
_KAGI_API_BASE = "https://kagi.com/api/v1"
_KAGI_SEARCH_TIMEOUT_SECONDS = 30.0
_KAGI_EXTRACT_TIMEOUT_SECONDS = 60.0
```

Do not add `KAGI_API_BASE` as a user-facing env var in the first pass. If tests
need a base override, patch the module constant.

## Search Implementation

Request:

```http
POST https://kagi.com/api/v1/search
Authorization: Bearer <KAGI_API_KEY>
Content-Type: application/json
```

Payload:

```json
{
  "query": "<query>",
  "workflow": "search",
  "limit": 5,
  "format": "json"
}
```

- keep the Hermes surface small
- only pass `query`, `workflow`, `limit`, and `format`
- trim Hermes output to `limit`
- do not expose Kagi lenses, include/exclude domains, file type, dates, or
  workflow in Hermes config yet

Normalization:

The current v1 response exposes web results at `response["data"]["search"]`.
Keep legacy flat-list support too, because older Kagi helpers and MCP examples
used `response["data"]` entries with `t == 0`. Read:

- `title`
- `url`
- `snippet`
- optional `published`

Map to Hermes:

```python
{
    "success": True,
    "data": {
        "web": [
            {
                "title": title,
                "url": url,
                "description": snippet,
                "position": i + 1,
                "metadata": {"published": published}  # only if useful/supported
            }
        ]
    },
}
```

Keep the public response shape exactly compatible with
`WebSearchProvider.search()`. If adding `metadata`, verify downstream code
preserves or ignores it safely. If not, omit it and keep only the legacy four
fields.

Error handling:

- Missing key: return `{"success": False, "error": "...KAGI_API_KEY..."}`
- HTTP 401/403: mention Kagi API key/auth.
- HTTP 429: mention Kagi rate/credit limits.
- Other HTTP errors: include status code and concise response text.
- Interrupt: follow Tavily's pattern and return `"Interrupted"`.

## Extract Implementation

Target behavior:

`web_extract(["https://example.com"])` should return a list of document entries:

```python
[
    {
        "url": "https://example.com",
        "title": "...",
        "content": "...markdown...",
        "raw_content": "...markdown...",
        "metadata": {"sourceURL": "https://example.com", "provider": "kagi"},
    }
]
```

API plan:

1. Use Kagi's new v1 Extract API, not the Universal Summarizer.
2. Call `POST /extract` with `pages: [{"url": ...}]` in batches of up to 10.
3. Request `format: "json"` so Hermes can preserve per-URL errors and return
   markdown in its standard extract envelope.
4. Implement direct REST calls with `httpx`.

Important caveat:

Shape to support:

- batch up to 10 URLs per request
- preserve order of input URLs
- normalize every returned page into one Hermes document
- add an error document for any requested URL missing from Kagi's response

Do not fall back to the summarizer silently:

- Summarizer output is an LLM summary, not source-page content.
- Hermes' `web_extract` is commonly used to inspect source content.
- A fallback would hide semantic differences from the agent.

Error handling:

- Per-URL failures should become entries with `error`, not a total exception
  unless every request fails before per-URL processing.
- Missing key should return one error entry per requested URL.
- HTTP 401/403/429 should be clear and actionable.
- Respect `kwargs` such as `format`, `include_raw`, and `max_chars` if they are
  easy to apply locally. Ignore unknown kwargs.

## Files To Modify

### `tools/web_tools.py`

Add `kagi` to known backends:

```python
_KNOWN_WEB_BACKENDS = frozenset({... "kagi" ...})
```

Do not add `kagi` to `_SEARCH_ONLY_BACKENDS` if v1 Extract is implemented.

Add autodetect before keyless/free fallbacks without changing existing users'
priority unexpectedly. Conservative placement:

```python
backend_candidates = (
    ("tavily", _has_env("TAVILY_API_KEY")),
    ("exa", _has_env("EXA_API_KEY")),
    ("parallel", _has_env("PARALLEL_API_KEY")),
    ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
    ("firecrawl", _is_tool_gateway_ready()),
    ("kagi", _has_env("KAGI_API_KEY")),
    ...
)
```

This means:

- Existing users with existing keys keep their old autodetect result.
- Users who only add `KAGI_API_KEY` get Kagi.
- Explicit `web.search_backend: kagi` always wins.

Add availability:

```python
if backend == "kagi":
    return _has_env("KAGI_API_KEY")
```

Add tool metadata env:

```python
"KAGI_API_KEY",
```

Add backward-compatible helper re-exports only if tests or callers need direct
imports from `tools.web_tools`, for example:

```python
from plugins.web.kagi.provider import (
    _kagi_request,
    _normalize_kagi_search_results,
    _normalize_kagi_extract_results,
)
```

Prefer not to grow `tools.web_tools.py` more than necessary.

### `hermes_cli/nous_subscription.py`

Add Kagi to direct web availability/status checks. Kagi is direct-only and
should not be wired into Nous managed gateway routing.

### Docs

Update:

- `website/docs/reference/environment-variables.md`
- Chinese translation file if it mirrors provider env vars:
  `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/environment-variables.md`
- Relevant web tools docs if there is a provider list.

Mention:

```markdown
| `KAGI_API_KEY` | Kagi Search/Extract API |
```

Also add a short configuration example if there is a web backend section:

```yaml
web:
  search_backend: kagi
  extract_backend: kagi
```

## Tests To Add Or Update

### Provider unit tests

Add `tests/tools/test_web_tools_kagi.py`.

Coverage:

- missing `KAGI_API_KEY` returns helpful error
- request uses `Authorization: Bearer <key>`
- search endpoint uses `https://kagi.com/api/v1/search`
- search normalizer:
  - handles current `data.search[]`
  - filters `t == 0`
  - maps `title`, `url`, `snippet`
  - respects `limit`
  - handles missing optional fields
- extract normalizer:
  - maps full markdown/content to `content` and `raw_content`
  - includes source URL metadata
  - preserves per-URL failures
- `web_search_tool` dispatches to Kagi when selected
- `web_extract_tool` dispatches to Kagi when selected

Mock `httpx` responses; no live Kagi calls in unit tests.

### Plugin discovery tests

Update or rely on existing plugin discovery tests so `kagi` appears in
`list_providers()` after plugin discovery.

If there is a hardcoded provider count, do not write a change-detector test.
Assert by name instead:

```python
assert "kagi" in {provider.name for provider in list_providers()}
```

### Backend selection/config tests

Update `tests/tools/test_web_tools_config.py` or `tests/tools/test_web_providers.py`:

- explicit `web.search_backend: kagi` is honored even if key missing
- explicit `web.extract_backend: kagi` is honored even if key missing
- `KAGI_API_KEY` autodetect chooses `kagi` when no higher-priority direct
  provider key is present
- `check_web_api_key()` includes `kagi` in capability checks
- typo behavior remains unchanged: unrecognized explicit backend does not
  silently reroute

Update terminal/tool metadata tests if they enumerate `_web_requires_env()`.

### Setup UI tests

If `hermes_cli/tools_config.py` has tests for provider picker rows, assert Kagi
is shown with:

- name: `Kagi`
- badge: `paid`
- env var: `KAGI_API_KEY`
- URL: `https://kagi.com/api`

## Manual Verification

With a real key:

```bash
hermes config set web.search_backend kagi
hermes config set web.extract_backend kagi
hermes doctor
```

Then in Hermes:

```text
search the web for "Kagi Search API"
extract https://help.kagi.com/kagi/api/search.html
```

Expected:

- `web_search` returns Kagi results.
- `web_extract` returns markdown/source content, not just a summary.
- Missing credit or rate-limit errors are readable.

Without a key:

```bash
hermes config set web.search_backend kagi
hermes config set web.extract_backend kagi
hermes doctor
```

Expected:

- tool availability clearly points at `KAGI_API_KEY`.
- no fallback to Parallel hides the explicit Kagi misconfiguration.

## Verification Commands

Focused test pass:

```bash
scripts/run_tests.sh \
  tests/tools/test_web_tools_kagi.py \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_providers.py \
  tests/plugins/web/test_web_search_provider_plugins.py \
  -- -q
```

Style:

```bash
.venv/bin/python -m ruff check \
  plugins/web/kagi \
  tools/web_tools.py \
  tests/tools/test_web_tools_kagi.py
```

Repository hygiene:

```bash
git diff --check
```

If docs are touched and the repo has a docs check for env-var tables, run that
target too.

## Rollout Notes

- This should be one commit.
- Do not make Kagi the default backend.
- Do not use the Universal Summarizer for `web_extract` unless the feature is
  explicitly named as summarized extraction.
- Do not add a new `HERMES_*` env var.
- Do not add a new core model tool.
- Keep any Kagi-specific advanced search filters out of the first PR; they can
  be follow-ups once the basic provider is stable.

## Resolved Questions

1. Extract endpoint: `POST https://kagi.com/api/v1/extract`.
2. Extract batches: yes, 1-10 URLs via `pages`.
3. Search limit: current spec includes `limit`; Hermes also trims defensively.
4. Search result shapes: current `data.search[]` plus legacy flat `data[]`
   with `t == 0`.
5. Product-scope errors: handled through Kagi's HTTP/error envelope and surfaced
   as readable tool errors.
