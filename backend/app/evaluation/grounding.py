from app.schemas.research import Citation, Claim, QualityMetrics, ResearchResult, Source


def validate_evidence(claims: list[Claim], sources: list[Source]) -> list[Claim]:
    by_id = {s.id: s for s in sources}
    for claim in claims:
        valid = []
        for evidence in claim.evidence:
            source = by_id.get(evidence.source_id)
            quote = " ".join(evidence.passage.split())
            if source and len(quote) >= 10 and quote in " ".join(source.content.split()):
                valid.append(evidence)
        claim.evidence = list({e.source_id: e for e in valid}.values())
        if not claim.evidence:
            claim.status = "insufficient_evidence"
            claim.reasoning = "No exact supporting passage found in retrieved sources."
    return claims


def build_result(
    claims: list[Claim], sources: list[Source], selected: list[str], warnings: list[str], graph: dict
) -> ResearchResult:
    claims = validate_evidence(claims, sources)
    total = len(claims)
    supported = sum(c.status == "supported" for c in claims)
    by_source = {s.id: s for s in sources}
    citations = [
        Citation(claim_id=c.id, source_id=e.source_id, url=by_source[e.source_id].url, passage=e.passage)
        for c in claims
        for e in c.evidence
    ]
    metrics = QualityMetrics(
        factual_claims=total,
        supported_claims=supported,
        citation_coverage=supported / total if total else None,
        evidence_coverage=sum(bool(c.evidence) for c in claims) / total if total else None,
        contradiction_rate=sum(c.status == "conflicting" for c in claims) / total if total else None,
        unsupported_claim_rate=sum(c.status != "supported" for c in claims) / total if total else None,
        unique_publishers=len({s.publisher for s in sources}),
        unique_documents=len({s.content_hash for s in sources}),
    )
    by_id = {c.id: c for c in claims}
    ordered = [by_id[c] for c in dict.fromkeys(selected) if c in by_id]
    if not ordered:
        ordered = claims
    lines = []
    for claim in ordered:
        if claim.status == "supported":
            refs = " ".join(f"[{e.source_id}]" for e in claim.evidence)
            lines.append(f"{claim.claim} {refs}")
    if supported != total or not total:
        warnings.append("Some proposed claims lack sufficient support. Review the claim assessments below.")
    if not lines:
        lines = ["Insufficient evidence to provide a supported answer to this question."]
    # The answer contains only assessed atomic claims; no unverified summary is introduced afterward.
    return ResearchResult(
        summary=lines[0],
        answer="\n\n".join(lines),
        claims=claims,
        sources=sources,
        citations=citations,
        quality_metrics=metrics,
        warnings=list(dict.fromkeys(warnings)),
        belief_graph=graph,
    )
