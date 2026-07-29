import asyncio
import hashlib
import re
import urllib.robotparser
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.services.normalizer import RawSetPayload, normalize_raw_payload
from app.services.provider import (
    ProviderBlockedError,
    ProviderPayloadError,
    ProviderTemporaryError,
    ProviderValidationError,
)


_ALLOWED_HOSTS = {"freeteknomusic.org", "www.freeteknomusic.org"}
_SET_PAGE_PATH = re.compile(r"^/sets/[A-Za-z0-9][A-Za-z0-9_-]*$")
_ISO_DURATION = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def validate_ftm_url(url: str) -> str:
    """Accept only one safe FTM set-page route without redirect inputs."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ProviderValidationError("ftm_invalid_url") from error
    if (
        parsed.scheme.casefold() != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or _SET_PAGE_PATH.fullmatch(parsed.path) is None
    ):
        raise ProviderValidationError("ftm_invalid_url")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _source_id(canonical_url: str) -> str:
    path = urlsplit(canonical_url).path.strip("/")
    return path.replace("/", "-") or hashlib.sha256(
        canonical_url.encode()
    ).hexdigest()[:24]


def _duration_seconds(soup: BeautifulSoup) -> int | None:
    candidates: list[str] = []
    for attribute, value in (
        ("property", "music:duration"),
        ("property", "video:duration"),
        ("name", "duration"),
        ("itemprop", "duration"),
    ):
        tag = soup.find("meta", attrs={attribute: value})
        if tag and isinstance(tag.get("content"), str):
            candidates.append(tag["content"].strip())
    for value in candidates:
        if value.isdigit():
            return int(value)
        match = _ISO_DURATION.fullmatch(value)
        if match:
            return (
                int(match.group("hours") or 0) * 3600
                + int(match.group("minutes") or 0) * 60
                + int(match.group("seconds") or 0)
            )
    return None


def _meta(soup: BeautifulSoup, key: str) -> str | None:
    tag = soup.find("meta", attrs={"property": key})
    if tag and isinstance(tag.get("content"), str):
        value = tag["content"].strip()
        return value or None
    return None


class FTMAdapter:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        scraper_user_agent: str | None = None,
        scraper_request_delay_ms: int | None = None,
        ftm_max_pages_per_run: int | None = None,
        timeout: float | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        settings = get_settings()
        self.enabled = settings.ftm_scraper_enabled if enabled is None else enabled
        self.transport = transport
        self.scraper_user_agent = (
            settings.scraper_user_agent
            if scraper_user_agent is None
            else scraper_user_agent
        )
        self.scraper_request_delay_ms = (
            settings.scraper_request_delay_ms
            if scraper_request_delay_ms is None
            else scraper_request_delay_ms
        )
        self.ftm_max_pages_per_run = (
            settings.ftm_max_pages_per_run
            if ftm_max_pages_per_run is None
            else ftm_max_pages_per_run
        )
        self.timeout = (
            settings.provider_request_timeout_seconds
            if timeout is None
            else timeout
        )
        self.sleep = asyncio.sleep if sleep is None else sleep
        self._request_count = 0

    async def fetch(self, url: str) -> RawSetPayload:
        if not self.enabled:
            raise ProviderBlockedError("ftm_disabled")
        page_url = validate_ftm_url(url)
        async with self._client() as client:
            await self._ensure_robots(client, page_url)
            return await self._fetch_page(client, page_url)

    async def crawl(
        self,
        start_url: str,
        max_pages: int | None = None,
    ) -> list[RawSetPayload]:
        if not self.enabled:
            raise ProviderBlockedError("ftm_disabled")
        first_url = validate_ftm_url(start_url)
        requested = self.ftm_max_pages_per_run if max_pages is None else max_pages
        page_limit = min(max(0, requested), self.ftm_max_pages_per_run, 25)
        if page_limit == 0:
            return []
        queue = [first_url]
        seen_urls: set[str] = set()
        seen_content: set[str] = set()
        payloads: list[RawSetPayload] = []
        async with self._client() as client:
            while queue and len(seen_urls) < page_limit:
                page_url = queue.pop(0)
                if page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                await self._ensure_robots(client, page_url)
                payload = await self._fetch_page(client, page_url)
                raw_html = str(payload.raw_payload["raw_html"])
                content_hash = str(payload.raw_payload["content_hash"])
                if content_hash in seen_content:
                    continue
                seen_content.add(content_hash)
                payloads.append(payload)
                for candidate in self._page_links(raw_html, page_url):
                    if candidate not in seen_urls and candidate not in queue:
                        queue.append(candidate)
        return payloads

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            headers={"User-Agent": self.scraper_user_agent},
            follow_redirects=False,
        )

    async def _ensure_robots(
        self,
        client: httpx.AsyncClient,
        page_url: str,
    ) -> None:
        robots_url = urlunsplit(
            ("https", urlsplit(page_url).hostname or "", "/robots.txt", "", "")
        )
        try:
            response = await self._request(client, robots_url)
        except ProviderTemporaryError as error:
            raise ProviderBlockedError("ftm_robots_denied") from error
        if response.status_code != 200:
            raise ProviderBlockedError("ftm_robots_denied")
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        if not parser.can_fetch(self.scraper_user_agent, page_url):
            raise ProviderBlockedError("ftm_robots_denied")

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        page_url: str,
    ) -> RawSetPayload:
        response = await self._request(client, page_url)
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderTemporaryError("ftm_temporary_error")
        if response.is_error:
            raise ProviderPayloadError("ftm_provider_error")
        raw_html = response.text
        soup = BeautifulSoup(raw_html, "html.parser")
        canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_value = (
            canonical_tag.get("href")
            if canonical_tag is not None
            else None
        )
        canonical_url = validate_ftm_url(
            urljoin(page_url, canonical_value or page_url)
        )
        title = _meta(soup, "og:title")
        if title is None:
            heading = soup.find("h1")
            title = heading.get_text(" ", strip=True) if heading else None
        if not title:
            raise ProviderPayloadError("ftm_invalid_response")
        raw: dict[str, Any] = {
            "id": _source_id(canonical_url),
            "webpage_url": canonical_url,
            "title": title,
            "description": _meta(soup, "og:description"),
            "duration_seconds": _duration_seconds(soup),
            "thumbnail": _meta(soup, "og:image"),
            "raw_html": raw_html,
            "content_hash": hashlib.sha256(raw_html.encode()).hexdigest(),
        }
        try:
            return normalize_raw_payload("freeteknomusic", raw)
        except (TypeError, ValueError) as error:
            raise ProviderPayloadError("ftm_invalid_response") from error

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> httpx.Response:
        if self._request_count:
            await self.sleep(self.scraper_request_delay_ms / 1000)
        self._request_count += 1
        try:
            return await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise ProviderTemporaryError("ftm_temporary_error") from error

    @staticmethod
    def _page_links(raw_html: str, page_url: str) -> list[str]:
        links: list[str] = []
        for tag in BeautifulSoup(raw_html, "html.parser").find_all("a", href=True):
            href = str(tag["href"])
            candidate = urljoin(page_url, href)
            try:
                normalized = validate_ftm_url(candidate)
            except ProviderValidationError:
                continue
            if urlsplit(normalized).hostname != urlsplit(page_url).hostname:
                continue
            links.append(normalized)
        return links
