from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.providers.search import SearchProvider, publisher_domain


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "server_error", "malformed"])
async def test_search_failure_never_becomes_fake_results(failure):
    provider = SearchProvider(
        Settings(search_api_key="test"), fakeredis.aioredis.FakeRedis(), "owner", "job", AsyncMock()
    )
    await provider.http.aclose()

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret query", request=request)
        if failure == "malformed":
            return httpx.Response(200, content=b"not JSON")
        return httpx.Response(429 if failure == "rate_limit" else 503)

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as caught:
        await provider.search("private research query")
    assert "secret query" not in caught.value.message
    await provider.close()


async def test_redirect_to_metadata_and_oversized_documents_are_rejected():
    provider = SearchProvider(
        Settings(search_api_key="test", max_source_bytes=1024),
        fakeredis.aioredis.FakeRedis(),
        "owner",
        "job",
        AsyncMock(),
    )
    await provider.fetcher.close()

    class Response:
        status = 302
        headers = {"Location": "http://169.254.169.254/latest/meta-data"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"x" * 1025

    class Fetcher:
        def get(self, *args, **kwargs):
            return Response()

        async def close(self):
            return None

    provider.fetcher = Fetcher()
    with pytest.raises(AppError) as caught:
        await provider.fetch_url("https://example.org")
    assert caught.value.code == "UNSAFE_URL"
    Response.status, Response.headers = 200, {"Content-Type": "text/plain"}
    with pytest.raises(AppError) as caught:
        await provider.fetch_url("https://example.org")
    assert caught.value.code == "SOURCE_TOO_LARGE"
    await provider.close()


def test_subdomains_do_not_inflate_publisher_diversity():
    assert publisher_domain("news.example.co.uk").top_domain_under_public_suffix == "example.co.uk"
    assert publisher_domain("science.example.co.uk").top_domain_under_public_suffix == "example.co.uk"
