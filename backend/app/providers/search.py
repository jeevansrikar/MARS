import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import httpx
import tldextract
from aiohttp.abc import AbstractResolver
from bs4 import BeautifulSoup
from redis.exceptions import RedisError

from app.core.errors import AppError
from app.core.telemetry import safe_span
from app.schemas.research import Source

publisher_domain = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise ValueError()
        if parts.port not in {None, 80, 443} or "\\" in url or any(ord(c) < 32 for c in url):
            raise ValueError()
        if (
            host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".home", ".test"))
            or "." not in host
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if (
            address is not None
            and address.version == 6
            and (address.ipv4_mapped or address.sixtofour or address.teredo)
        ):
            raise ValueError()
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    except (ValueError, TypeError) as exc:
        raise AppError("UNSAFE_URL", "Source URL is not public HTTP(S)") from exc


def canonicalize(url: str) -> str:
    parts = urlsplit(validate_url(url))
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))


class PublicResolver(AbstractResolver):
    """Validate the exact DNS answers used by the connector, preventing a second lookup/rebind."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        output = []
        for fam, _, proto, _, address in answers:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
                raise AppError("UNSAFE_URL", "Source resolves to a non-public address")
            output.append(
                {
                    "hostname": host,
                    "host": address[0],
                    "port": port,
                    "family": fam,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not output:
            raise AppError("SOURCE_UNAVAILABLE", "Source DNS resolution failed")
        return output

    async def close(self):
        return None


def extract_content(document: str) -> str:
    soup = BeautifulSoup(document, "html.parser")
    for element in soup(["script", "style", "noscript", "nav", "footer", "iframe", "form"]):
        element.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


class SearchProvider:
    def __init__(self, settings, redis, owner: str, job_id: str, reserve):
        self.settings, self.redis, self.owner, self.job_id, self.reserve = settings, redis, owner, job_id, reserve
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), trust_env=False)
        self.fetcher = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=4, use_dns_cache=False),
            timeout=aiohttp.ClientTimeout(total=20, connect=5),
            trust_env=False,
            auto_decompress=False,
            headers={"User-Agent": "MARS-Research/2.0", "Accept-Encoding": "identity"},
        )

    async def close(self):
        await self.http.aclose()
        await self.fetcher.close()

    async def search(self, query: str) -> list[dict]:
        if not self.settings.search_api_key.get_secret_value():
            raise AppError("SEARCH_UNCONFIGURED", "Search provider is not configured", 503)
        cache_key = f"search:{self.owner}:" + hashlib.sha256(query.encode()).hexdigest()
        try:
            cached = await self.redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except RedisError:
            cached = None
        await self.reserve()
        with safe_span("search"):
            try:
                async with self.http.stream(
                    "POST",
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {self.settings.search_api_key.get_secret_value()}"},
                    json={
                        "query": query,
                        "max_results": self.settings.max_sources,
                        "include_answer": False,
                        "search_depth": "basic",
                    },
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise AppError("SEARCH_UNAVAILABLE", "Search temporarily unavailable", 503, True)
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            raise AppError("SEARCH_RESPONSE_TOO_LARGE", "Search returned excessive data")
                    document = json.loads(body)
                    if not isinstance(document, dict) or not isinstance(document.get("results"), list):
                        raise ValueError("Invalid search payload")
                    results = [
                        item
                        for item in document["results"]
                        if isinstance(item, dict) and isinstance(item.get("url"), str)
                    ][: self.settings.max_sources]
            except httpx.TimeoutException as exc:
                raise AppError("SEARCH_TIMEOUT", "Search timed out", 503, True) from exc
            except httpx.HTTPError as exc:
                raise AppError("SEARCH_FAILED", "Search request failed", 503) from exc
            except (ValueError, TypeError) as exc:
                raise AppError("SEARCH_FAILED", "Search returned an invalid response", 503, True) from exc
        try:
            # Queries are private: cache always scoped to authenticated owner.
            ttl = 60 if any(x in query.lower() for x in ("today", "latest", "news", "current")) else 900
            await self.redis.set(cache_key, json.dumps(results), ex=ttl)
        except RedisError:
            pass
        return results

    async def fetch_url(self, url: str, title: str = "") -> Source:
        url = validate_url(url)
        cache_key = f"source:{self.owner}:" + hashlib.sha256(canonicalize(url).encode()).hexdigest()
        try:
            cached = await self.redis.get(cache_key)
            if cached:
                source = Source.model_validate_json(cached)
                source.id = self.job_id + ":" + hashlib.sha256(source.canonical_url.encode()).hexdigest()[:20]
                return source
        except (RedisError, ValueError):
            pass
        with safe_span("source.fetch"):
            try:
                async with asyncio.timeout(25):
                    for _ in range(4):
                        async with self.fetcher.get(url, allow_redirects=False) as response:
                            if response.status in {301, 302, 303, 307, 308}:
                                url = validate_url(urljoin(url, response.headers.get("Location", "")))
                                continue
                            if response.status != 200:
                                raise AppError("SOURCE_UNAVAILABLE", "Source could not be retrieved")
                            if response.headers.get("Content-Encoding", "identity") != "identity":
                                raise AppError("SOURCE_UNAVAILABLE", "Compressed source rejected")
                            mime = response.headers.get("Content-Type", "").split(";")[0]
                            if mime not in {"text/html", "text/plain", "application/xhtml+xml"}:
                                raise AppError("SOURCE_UNAVAILABLE", "Unsupported source document type")
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(16384):
                                body.extend(chunk)
                                if len(body) > self.settings.max_source_bytes:
                                    raise AppError("SOURCE_TOO_LARGE", "Source exceeds download limit")
                            text = await asyncio.to_thread(extract_content, body.decode("utf-8", errors="replace"))
                            text = text[: self.settings.max_document_chars]
                            if len(text) < 40:
                                raise AppError("SOURCE_UNAVAILABLE", "Source has no usable text")
                            canonical = canonicalize(url)
                            host = urlsplit(url).hostname or ""
                            source = Source(
                                id=self.job_id + ":" + hashlib.sha256(canonical.encode()).hexdigest()[:20],
                                url=url,
                                canonical_url=canonical,
                                title=title[:500],
                                publisher=publisher_domain(host).top_domain_under_public_suffix or host,
                                content=text,
                                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                                retrieved_at=time.time(),
                            )
                            try:
                                await self.redis.set(cache_key, source.model_dump_json(), ex=60)
                            except RedisError:
                                pass
                            return source
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                raise AppError("SOURCE_UNAVAILABLE", "Source request failed") from exc
        raise AppError("SOURCE_UNAVAILABLE", "Too many source redirects")
