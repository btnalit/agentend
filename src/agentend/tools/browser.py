from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urljoin

import httpx

from agentend.core.evidence import record_browser_extract_evidence, record_browser_screenshot_evidence
from agentend.core.paths import safe_artifact_path
from agentend.tools.base import ToolContext, ToolResult


@dataclass(frozen=True)
class PlaywrightStatus:
    package_available: bool
    browser_available: bool
    message: str


class BrowserOpenTool:
    name = "browser.open"
    description = "Open a URL in a browser backend and return page metadata."
    input_schema = {"type": "object", "required": ["url"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        page = _load_page(str(input_data["url"]))
        data = {
            "url": page["url"],
            "title": page["title"],
            "text": page["text"][:1000],
            "dom_excerpt": page["text"][:1000],
            "backend": page["backend"],
            "fallback": page.get("fallback", False),
            "fallback_reason": page.get("fallback_reason"),
        }
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class BrowserExtractTool:
    name = "browser.extract"
    description = "Extract text and links from a URL using the browser backend when available."
    input_schema = {"type": "object", "required": ["url"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        page = _load_page(str(input_data["url"]))
        source = record_browser_extract_evidence(
            context.session,
            context.home,
            run_id=context.run_id,
            url=page["url"],
            title=page["title"],
            text=page["text"],
        )
        data = {
            "url": page["url"],
            "title": page["title"],
            "text": page["text"],
            "links": page["links"],
            "backend": page["backend"],
            "dom_excerpt": page["text"][:1000],
            "fallback": page.get("fallback", False),
            "fallback_reason": page.get("fallback_reason"),
            "source_id": source.id,
        }
        return ToolResult(content=json.dumps(data, ensure_ascii=False, indent=2), data=data)


class BrowserScreenshotTool:
    name = "browser.screenshot"
    description = "Capture a browser screenshot artifact for a URL."
    input_schema = {"type": "object", "required": ["url"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        output = _artifact_path(context, str(input_data.get("path", "browser-screenshot.png")))
        output.parent.mkdir(parents=True, exist_ok=True)
        url = str(input_data["url"])
        data, error = _try_playwright_screenshot(url, output)
        if data is None:
            page = _load_page(url)
            output.write_bytes(_placeholder_png())
            data = {
                "url": url,
                "title": page["title"],
                "path": str(output),
                "backend": page["backend"],
                "fallback": True,
                "fallback_reason": error or page.get("fallback_reason") or "playwright_unavailable",
                "dom_excerpt": page["text"][:1000],
                "size_bytes": output.stat().st_size,
            }
        digest = sha256(output.read_bytes()).hexdigest()
        source = record_browser_screenshot_evidence(
            context.session,
            context.home,
            run_id=context.run_id,
            url=str(data.get("url") or url),
            title=data.get("title") if data.get("title") is not None else None,
            path=output,
            dom_excerpt=str(data.get("dom_excerpt") or ""),
            content_hash=digest,
        )
        data = data | {"sha256": digest, "source_id": source.id}
        return ToolResult(content=str(output), data=data, artifact_path=output)


class BrowserClickTool:
    name = "browser.click"
    description = "Click a selector on a URL; uses Playwright when available and static verification otherwise."
    input_schema = {"type": "object", "required": ["url", "selector"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        url = str(input_data["url"])
        selector = str(input_data["selector"])
        output = _artifact_path(context, str(input_data.get("screenshot_path", "browser-click.png")))
        output.parent.mkdir(parents=True, exist_ok=True)
        result, error = _try_playwright_action(url, selector, action="click", screenshot_path=output)
        if result is None:
            page = _load_page(url)
            if not _selector_exists(page["html"], selector):
                raise ValueError(f"Selector not found: {selector}")
            output.write_bytes(_placeholder_png())
            result = {
                "url": url,
                "title": page["title"],
                "selector": selector,
                "action": "click",
                "backend": page["backend"],
                "fallback": True,
                "fallback_reason": error or page.get("fallback_reason") or "playwright_unavailable",
                "dom_excerpt": page["text"][:1000],
                "screenshot_path": str(output),
                "size_bytes": output.stat().st_size,
            }
        return ToolResult(content=json.dumps(result, ensure_ascii=False, sort_keys=True), data=result, artifact_path=output)


class BrowserTypeTool:
    name = "browser.type"
    description = "Type text into a selector on a URL; uses Playwright when available and static verification otherwise."
    input_schema = {"type": "object", "required": ["url", "selector", "text"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        url = str(input_data["url"])
        selector = str(input_data["selector"])
        text = str(input_data["text"])
        output = _artifact_path(context, str(input_data.get("screenshot_path", "browser-type.png")))
        output.parent.mkdir(parents=True, exist_ok=True)
        result, error = _try_playwright_action(url, selector, action="type", text=text, screenshot_path=output)
        if result is None:
            page = _load_page(url)
            if not _selector_exists(page["html"], selector):
                raise ValueError(f"Selector not found: {selector}")
            output.write_bytes(_placeholder_png())
            result = {
                "url": url,
                "title": page["title"],
                "selector": selector,
                "action": "type",
                "text": text,
                "backend": page["backend"],
                "fallback": True,
                "fallback_reason": error or page.get("fallback_reason") or "playwright_unavailable",
                "dom_excerpt": page["text"][:1000],
                "screenshot_path": str(output),
                "size_bytes": output.stat().st_size,
            }
        return ToolResult(content=json.dumps(result, ensure_ascii=False, sort_keys=True), data=result, artifact_path=output)


def _load_page(url: str) -> dict:
    playwright_page, error = _try_playwright_page(url)
    if playwright_page is not None:
        return playwright_page
    response = httpx.get(url, timeout=20)
    title, text, links = _parse_html(response.text, url)
    return {
        "url": str(response.url),
        "title": title,
        "text": text,
        "links": links,
        "html": response.text,
        "backend": "httpx_fallback",
        "fallback": True,
        "fallback_reason": error or "playwright_unavailable",
    }


def playwright_status() -> PlaywrightStatus:
    if find_spec("playwright") is None:
        return PlaywrightStatus(
            package_available=False,
            browser_available=False,
            message="playwright package is not installed",
        )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            browser.close()
        return PlaywrightStatus(package_available=True, browser_available=True, message="chromium browser is available")
    except Exception as exc:
        return PlaywrightStatus(package_available=True, browser_available=False, message=_short_error(exc))


def _try_playwright_page(url: str) -> tuple[dict | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            title = page.title()
            text = page.locator("body").inner_text(timeout=3000)
            links = [
                {"text": item.get("text", ""), "href": item.get("href", "")}
                for item in page.eval_on_selector_all(
                    "a",
                    "els => els.map(a => ({text: a.innerText, href: a.href}))",
                )
            ]
            html = page.content()
            current_url = page.url
            browser.close()
            return {"url": current_url, "title": title, "text": text, "links": links, "html": html, "backend": "playwright", "fallback": False}, None
    except Exception as exc:
        return None, _short_error(exc)


def _try_playwright_screenshot(url: str, output: Path) -> tuple[dict | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            title = page.title()
            text = page.locator("body").inner_text(timeout=3000)
            page.screenshot(path=str(output), full_page=True)
            current_url = page.url
            browser.close()
            return {
                "url": current_url,
                "title": title,
                "path": str(output),
                "backend": "playwright",
                "fallback": False,
                "dom_excerpt": text[:1000],
                "size_bytes": output.stat().st_size,
            }, None
    except Exception as exc:
        return None, _short_error(exc)


def _try_playwright_action(url: str, selector: str, *, action: str, text: str = "", screenshot_path: Path) -> tuple[dict | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            if action == "click":
                page.click(selector, timeout=3000)
            elif action == "type":
                page.fill(selector, text, timeout=3000)
            dom_excerpt = page.locator("body").inner_text(timeout=3000)[:1000]
            page.screenshot(path=str(screenshot_path), full_page=True)
            result = {
                "url": page.url,
                "title": page.title(),
                "selector": selector,
                "action": action,
                "backend": "playwright",
                "fallback": False,
                "dom_excerpt": dom_excerpt,
                "screenshot_path": str(screenshot_path),
                "size_bytes": screenshot_path.stat().st_size,
            }
            browser.close()
            return result, None
    except Exception as exc:
        return None, _short_error(exc)


def _parse_html(html: str, base_url: str) -> tuple[str | None, str, list[dict[str, str]]]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None
    links = [
        {"text": re.sub(r"<[^>]+>", " ", body).strip(), "href": urljoin(base_url, href)}
        for href, body in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.IGNORECASE | re.DOTALL)
    ]
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text, links


def _selector_exists(html: str, selector: str) -> bool:
    if selector.startswith("#"):
        return f'id="{selector[1:]}"' in html or f"id='{selector[1:]}'" in html
    if selector.startswith("."):
        return selector[1:] in html
    return re.search(rf"<\s*{re.escape(selector)}(\s|>|/)", html, flags=re.IGNORECASE) is not None


def _artifact_path(context: ToolContext, requested: str) -> Path:
    return safe_artifact_path(context.home, context.run_id, requested)


def _placeholder_png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c4944415408d7636060600000000400010d0a2db40000000049454e44ae426082"
    )


def _short_error(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip()[:500] or exc.__class__.__name__


BROWSER_TOOLS = [
    BrowserOpenTool(),
    BrowserClickTool(),
    BrowserTypeTool(),
    BrowserScreenshotTool(),
    BrowserExtractTool(),
]
