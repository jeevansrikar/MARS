import asyncio
import socket
import time

import pytest

from app.core.errors import AppError
from app.evaluation.grounding import build_result
from app.providers.search import PublicResolver, canonicalize, extract_content, validate_url
from app.schemas.research import Claim, Evidence, Source


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "http://[::1]/",
        "http://user:pass@example.com",
        "http://example.com:22",
        "http://internal.local",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_ssrf_urls_rejected(url):
    with pytest.raises(AppError):
        validate_url(url)


async def test_dns_rebinding_blocked_at_connector(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(AppError):
        await PublicResolver().resolve("public-looking.example.com", 80)


def test_normalization_and_extraction():
    assert canonicalize("https://example.com/path?utm_source=x&a=1#anchor") == "https://example.com/path?a=1"
    assert extract_content("<p>Useful evidence</p><script>evil()</script><nav>menu</nav>") == "Useful evidence"


def test_forged_citations_and_metrics_fail_closed():
    source = Source(
        id="s1",
        url="https://example.com",
        canonical_url="https://example.com/",
        title="Source",
        publisher="example.com",
        content="The experiment measured 42 samples in total.",
        content_hash="abc",
        retrieved_at=time.time(),
    )
    supported = Claim(
        id="c1",
        claim="There were 42 samples.",
        status="supported",
        evidence=[Evidence(source_id="s1", passage="measured 42 samples in total")],
    )
    forged = Claim(
        id="c2",
        claim="A fabricated claim.",
        status="supported",
        evidence=[Evidence(source_id="s1", passage="This was never in the source")],
    )
    unknown = Claim(
        id="c3",
        claim="Another fabrication.",
        status="supported",
        evidence=[Evidence(source_id="missing", passage="measured 42 samples in total")],
    )
    result = build_result([supported, forged, unknown], [source], ["c1", "c2", "c3"], [], {})
    assert result.quality_metrics.citation_coverage == pytest.approx(1 / 3)
    assert result.quality_metrics.unsupported_claim_rate == pytest.approx(2 / 3)
    assert "fabricat" not in result.answer
    assert len(result.citations) == 1


def test_empty_evidence_is_not_perfect_quality():
    result = build_result([], [], [], [], {})
    assert result.quality_metrics.citation_coverage is None
    assert "Insufficient evidence" in result.answer
