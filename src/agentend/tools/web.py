from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from agentend.config import load_config
from agentend.core.evidence import record_web_fetch_evidence, record_web_search_evidence
from agentend.tools.base import ToolContext, ToolResult


def _html_text(html: str) -> tuple[str | None, str]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text


class WebFetchTool:
    name = "web.fetch"
    description = "Fetch a web page and record source evidence."
    input_schema = {"type": "object", "required": ["url"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        url = str(input_data["url"])
        if url.startswith("https://example.com/search/"):
            title = "Fake search result"
            text = f"Offline fixture content for {url}."
            status_code = 200
        else:
            response = httpx.get(url, timeout=int(input_data.get("timeout_seconds", 20)))
            response.raise_for_status()
            title, text = _html_text(response.text)
            status_code = response.status_code
        source = record_web_fetch_evidence(
            context.session,
            context.home,
            run_id=context.run_id,
            url=url,
            title=title,
            text=text,
        )
        data = {"url": url, "title": title, "text": text, "status_code": status_code, "source_id": source.id}
        return ToolResult(content=text, data=data)


class WebSearchTool:
    name = "web.search"
    description = "Search the web through a configured provider."
    input_schema = {"type": "object", "required": ["query"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        query = str(input_data["query"])
        limit = _limit(input_data.get("limit", 5))
        config = load_config(context.home)
        provider = str(input_data.get("provider") or config.search.provider or "fake")
        if provider == "fake":
            results = [
                {"title": f"{query} result {index + 1}", "url": f"https://example.com/search/{index + 1}", "snippet": query}
                for index in range(limit)
            ]
        elif provider == "brave":
            results = _search_brave(context, input_data, query=query, limit=limit)
        else:
            raise ValueError(f"web.search provider is not configured: {provider}")
        recorded = record_web_search_evidence(
            context.session,
            context.home,
            run_id=context.run_id,
            query=query,
            provider=provider,
            results=results,
        )
        return ToolResult(
            content=json.dumps(recorded, ensure_ascii=False, indent=2),
            data={"query": query, "provider": provider, "results": recorded},
        )


def _search_brave(context: ToolContext, input_data: dict, *, query: str, limit: int) -> list[dict[str, str]]:
    config = load_config(context.home)
    provider_config = config.search.providers.get("brave")
    base_url = str(input_data.get("base_url") or (provider_config.base_url if provider_config else "")).strip()
    api_key_env = str(input_data.get("api_key_env") or (provider_config.api_key_env if provider_config else "BRAVE_SEARCH_API_KEY")).strip()
    if not base_url:
        raise ValueError("web.search provider is not configured: brave.base_url")
    token = os.environ.get(api_key_env)
    if not token:
        raise RuntimeError(f"Search provider secret is not set: {api_key_env}")
    params: dict[str, Any] = {
        "q": query,
        "count": min(limit, 20),
    }
    for key in ["country", "search_lang", "ui_lang", "safesearch", "freshness", "offset"]:
        if key in input_data:
            params[key] = input_data[key]
    response = httpx.get(
        base_url,
        params=params,
        headers={"Accept": "application/json", "X-Subscription-Token": token},
        timeout=int(input_data.get("timeout_seconds", 20)),
    )
    response.raise_for_status()
    payload = response.json()
    web = payload.get("web", {}) if isinstance(payload, dict) else {}
    raw_results = web.get("results", []) if isinstance(web, dict) else []
    results = []
    for item in raw_results[:limit]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "snippet": str(item.get("description") or item.get("snippet") or ""),
            }
        )
    return results


def _limit(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(parsed, 20))
