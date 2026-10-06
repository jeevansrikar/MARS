"""Optional owner-scoped pgvector memory library.

Not enabled by the durable runtime. Confidence fields contain caller assessments;
recency decay is a retrieval ranking heuristic, not a factual accuracy metric.
No HNSW index is installed by default: benchmark workload recall/latency first.
Repeated provenance does not manufacture confidence.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

log = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    pass


EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension
HNSW_M = 16  # Production value: m=16-24 for <1024 dims
HNSW_EF_CONSTRUCTION = 96  # Production value: 96-128 for good recall/build tradeoff
DEFAULT_SIMILARITY_THRESHOLD = 0.75  # Cosine similarity floor for "relevant"
RECENCY_HALF_LIFE_DAYS = 90  # Retrieval recency weight half-life; not an accuracy estimate
CONSOLIDATION_MIN_CORROBORATION = 2  # Need 2+ independent episodes to consolidate a fact


# ── ORM models ─────────────────────────────────────────────────────────────────


class SemanticMemoryEntry(Base):
    """
    A single unit of durable, cross-session knowledge.

    Each row represents one distilled fact/conclusion, backed by an
    embedding for similarity retrieval and a corroboration count that
    tracks how many independent episodes support it.
    """

    __tablename__ = "semantic_memory"
    __table_args__ = (UniqueConstraint("user_id", "content_hash"),)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    content: Mapped[str] = mapped_column(Text, nullable=False)  # The distilled fact, in natural language
    content_hash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # Dedup key
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)

    topics: Mapped[list[Any]] = mapped_column(ARRAY(String), default=list, index=False)
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.6)
    corroboration_count: Mapped[int] = mapped_column(Integer, default=1)  # How many episodes support this
    contradiction_count: Mapped[int] = mapped_column(Integer, default=0)  # How many episodes conflict with this

    source_episode_ids: Mapped[list[Any]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), default=list)

    is_contested: Mapped[bool] = mapped_column(Boolean, default=False)  # Flagged for Critic review
    contested_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_reinforced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    reinforcement_count: Mapped[int] = mapped_column(Integer, default=1)

    def to_dict(self, include_embedding: bool = False) -> dict[str, Any]:
        d = {
            "id": str(self.id),
            "content": self.content,
            "topics": self.topics or [],
            "domain": self.domain,
            "confidence": round(self.confidence, 4),
            "corroboration_count": self.corroboration_count,
            "contradiction_count": self.contradiction_count,
            "is_contested": self.is_contested,
            "contested_reason": self.contested_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_reinforced_at": self.last_reinforced_at.isoformat() if self.last_reinforced_at else None,
            "reinforcement_count": self.reinforcement_count,
            "source_episode_count": len(self.source_episode_ids or []),
        }
        if include_embedding:
            d["embedding"] = list(self.embedding) if self.embedding is not None else None
        return d


class SemanticConflict(Base):
    """
    Records a detected contradiction between a new episode's conclusion
    and existing semantic memory. This table feeds the Critic agent's
    contradiction-resolution queue — semantic memory itself never silently
    resolves conflicts, it surfaces them.
    """

    __tablename__ = "semantic_conflicts"
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    semantic_entry_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("semantic_memory.id", ondelete="CASCADE")
    )
    conflicting_episode_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conflicting_claim: Mapped[str] = mapped_column(Text, nullable=False)
    existing_claim: Mapped[str] = mapped_column(Text, nullable=False)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── DDL for HNSW index (run once via migration) ───────────────────────────────

HNSW_INDEX_DDL = f"""
CREATE INDEX IF NOT EXISTS idx_semantic_memory_embedding
ON semantic_memory
USING hnsw (embedding vector_cosine_ops)
WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION});
"""

# At query time, ef_search controls the recall/latency tradeoff for THIS query.
# Higher = better recall, more latency. 40 is a reasonable default for
# sub-100ms lookups against a corpus in the tens-of-thousands of rows.
SET_EF_SEARCH_SQL = "SELECT set_config('hnsw.ef_search', :ef_search, true)"


# ── Data transfer objects ──────────────────────────────────────────────────────


@dataclass
class SemanticSearchResult:
    entry: dict[str, Any]
    similarity: float


@dataclass
class ConsolidationCandidate:
    """One episode's conclusion, pending consolidation into semantic memory."""

    episode_id: UUID
    content: str
    embedding: list[float]
    topics: list[str]
    domain: str
    confidence: float


class SemanticMemoryError(Exception):
    pass


def _content_hash(content: str) -> str:
    """Normalize and hash content for exact-dedup detection before
    falling back to embedding similarity for near-dup detection."""
    normalized = " ".join(content.strip().lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()


def _recency_weighted_score(base_score: float, last_reinforced: datetime) -> float:
    """Recency-weighted retrieval score, never a factual-confidence estimate."""
    now = datetime.now(timezone.utc)
    if last_reinforced.tzinfo is None:
        last_reinforced = last_reinforced.replace(tzinfo=timezone.utc)
    days_stale = (now - last_reinforced).days
    if days_stale <= 0:
        return base_score
    decay_factor = 0.5 ** (days_stale / RECENCY_HALF_LIFE_DAYS)
    # Floor at 15% of original — a fact never fully disappears, it just
    # stops dominating retrieval rankings
    return max(base_score * decay_factor, base_score * 0.15)


class SemanticMemoryStore:
    """
    pgvector-backed semantic memory. Handles similarity search,
    consolidation from episodic memory, and contradiction detection.
    """

    def __init__(self, db_session: AsyncSession, user_id: UUID):
        if user_id is None:
            raise ValueError("Memory owner is required")
        self.db = db_session
        self.user_id = user_id

    def embed(self, text_input: str) -> list[float]:
        from app.core.config import get_settings
        from app.providers.embeddings import encode_local

        return encode_local([text_input[:2000]], get_settings())[0]

    async def search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        domain: str | None = None,
        ef_search: int = 40,
        exclude_contested: bool = False,
    ) -> list[SemanticSearchResult]:
        """
        Find semantically relevant memory entries for a query.

        Sets hnsw.ef_search per-query (not globally) so concurrent requests
        with different latency/recall needs don't interfere with each other —
        this is a session-local SET, scoped to this transaction.
        """
        import asyncio

        query_embedding = await asyncio.to_thread(self.embed, query)

        await self.db.execute(text(SET_EF_SEARCH_SQL), {"ef_search": str(ef_search)})

        # Cosine distance: pgvector's <=> operator. Similarity = 1 - distance.
        distance_expr = SemanticMemoryEntry.embedding.cosine_distance(query_embedding)

        stmt = (
            select(SemanticMemoryEntry, distance_expr.label("distance"))
            .where(SemanticMemoryEntry.user_id == self.user_id)
            .order_by(distance_expr)
            .limit(top_k * 2)  # Over-fetch before threshold filtering
        )
        if domain:
            stmt = stmt.where(SemanticMemoryEntry.domain == domain)
        if exclude_contested:
            stmt = stmt.where(SemanticMemoryEntry.is_contested.is_(False))

        result = await self.db.execute(stmt)
        rows = result.all()

        results = []
        for entry, distance in rows:
            similarity = 1.0 - float(distance)
            if similarity < similarity_threshold:
                continue

            recency_weighted_relevance = _recency_weighted_score(similarity, entry.last_reinforced_at)
            entry_dict = entry.to_dict()
            entry_dict["recency_weighted_relevance"] = round(recency_weighted_relevance, 4)

            results.append(SemanticSearchResult(entry=entry_dict, similarity=round(similarity, 4)))

            if len(results) >= top_k:
                break

        log.info("semantic_memory.search", query=query[:60], n_results=len(results), threshold=similarity_threshold)
        return results

    async def format_for_agent_context(self, query: str, top_k: int = 5, domain: str | None = None) -> str:
        """
        The primary integration point: called by the Planner agent before
        research begins. Returns a ready-to-inject context block, or an
        empty string if nothing relevant exists — callers don't need to
        branch on "did we find anything."
        """
        results = await self.search(query, top_k=top_k, domain=domain)
        if not results:
            return ""

        lines = ["PRIOR KNOWLEDGE FROM PAST RESEARCH (verify if load-bearing to your conclusion):"]
        for r in results:
            entry = r.entry
            flag = " [CONTESTED]" if entry["is_contested"] else ""
            lines.append(
                f"- [{entry['recency_weighted_relevance']:.2f} retrieval relevance, "
                f"corroborated {entry['corroboration_count']}x{flag}] {entry['content']}"
            )
        return "\n".join(lines)

    # ── Consolidation (episodic → semantic) ─────────────────────────────────

    async def consolidate(self, candidate: ConsolidationCandidate) -> tuple[SemanticMemoryEntry, str]:
        """
        Attempt to fold one episode's conclusion into semantic memory.

        Returns (entry, action) where action is one of:
          "created"       — no similar entry existed, new fact recorded
          "reinforced"     — matched an existing entry, corroboration incremented
          "conflict"       — matched an existing entry but claim CONTRADICTS it

        This is called by a background consolidation worker after each
        session completes, NOT inline in the request path — consolidation
        involves an LLM call to check for semantic contradiction, which is
        too slow for the critical path.
        """
        content_hash = _content_hash(candidate.content)

        exact_match_stmt = (
            select(SemanticMemoryEntry)
            .where(SemanticMemoryEntry.user_id == self.user_id)
            .where(SemanticMemoryEntry.content_hash == content_hash)
        )
        exact_match = (await self.db.execute(exact_match_stmt)).scalar_one_or_none()

        if exact_match:
            return await self._reinforce(exact_match, candidate.episode_id), "reinforced"

        similar = await self.search(candidate.content, top_k=1, similarity_threshold=0.85)

        if not similar:
            entry = await self._create_entry(candidate)
            return entry, "created"

        existing_entry_id = UUID(similar[0].entry["id"])
        stmt = (
            select(SemanticMemoryEntry)
            .where(SemanticMemoryEntry.user_id == self.user_id)
            .where(SemanticMemoryEntry.id == existing_entry_id)
        )
        existing = (await self.db.execute(stmt)).scalar_one()

        is_contradiction = await self._check_contradiction(existing.content, candidate.content)

        if is_contradiction:
            await self._flag_conflict(existing, candidate)
            return existing, "conflict"
        else:
            return await self._reinforce(existing, candidate.episode_id), "reinforced"

    async def _create_entry(self, candidate: ConsolidationCandidate) -> SemanticMemoryEntry:
        entry = SemanticMemoryEntry(
            user_id=self.user_id,
            content=candidate.content,
            content_hash=_content_hash(candidate.content),
            embedding=candidate.embedding,
            topics=candidate.topics,
            domain=candidate.domain,
            confidence=candidate.confidence,
            corroboration_count=1,
            source_episode_ids=[candidate.episode_id],
        )
        self.db.add(entry)
        await self.db.commit()
        log.info("semantic_memory.entry_created", content=candidate.content[:80], domain=candidate.domain)
        return entry

    async def _reinforce(self, entry: SemanticMemoryEntry, episode_id: UUID) -> SemanticMemoryEntry:
        """Record new episode provenance and refresh recency without changing confidence."""
        if episode_id in (entry.source_episode_ids or []):
            return entry
        entry.corroboration_count += 1
        entry.reinforcement_count += 1
        entry.last_reinforced_at = datetime.now(timezone.utc)

        if episode_id not in (entry.source_episode_ids or []):
            entry.source_episode_ids = list(entry.source_episode_ids or []) + [episode_id]

        # Corroboration count is provenance, not a calibrated accuracy probability.

        await self.db.commit()
        log.info(
            "semantic_memory.reinforced",
            entry_id=str(entry.id),
            new_confidence=entry.confidence,
            corroboration=entry.corroboration_count,
        )
        return entry

    async def _check_contradiction(self, existing_claim: str, new_claim: str) -> bool:
        """
        LLM-based contradiction check between two semantically similar
        claims. This is intentionally conservative — high similarity alone
        doesn't mean contradiction (two claims can be near-duplicates in
        embedding space while being fully consistent, e.g. paraphrases).
        """
        import json as _json

        from app.core.llm_client import LLMClient, TokenBudget

        llm = LLMClient("critic", TokenBudget(max_tokens=5000))
        system = "Determine if two claims genuinely contradict each other. Return ONLY valid JSON."
        prompt = f"""Claim A (existing): {existing_claim}
Claim B (new): {new_claim}

Do these claims genuinely CONTRADICT each other (not just discuss different aspects)?
Return JSON: {{"contradicts": true/false, "reason": "one sentence"}}"""

        try:
            response = await llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=system,
                max_tokens=128,
                temperature=0.0,
            )
            data = _json.loads(response.strip())
            return bool(data.get("contradicts", False))
        except Exception as exc:
            log.warning("semantic_memory.contradiction_check_failed", error=str(exc))
            raise SemanticMemoryError("Contradiction assessment unavailable") from exc

    async def _flag_conflict(self, existing: SemanticMemoryEntry, candidate: ConsolidationCandidate) -> None:
        """
        Record the conflict for Critic review, and mark the existing entry
        as contested. Confidence is NOT overwritten — both claims persist
        until a human or the Critic agent explicitly resolves the conflict.
        """
        conflict = SemanticConflict(
            user_id=self.user_id,
            semantic_entry_id=existing.id,
            conflicting_episode_id=candidate.episode_id,
            conflicting_claim=candidate.content,
            existing_claim=existing.content,
        )
        self.db.add(conflict)

        existing.is_contested = True
        existing.contested_reason = f"Conflicts with newer claim: {candidate.content[:200]}"
        existing.contradiction_count += 1

        await self.db.commit()
        log.warning(
            "semantic_memory.conflict_flagged",
            entry_id=str(existing.id),
            existing_claim=existing.content[:80],
            new_claim=candidate.content[:80],
        )

    # ── Conflict resolution (called by Critic agent) ────────────────────────

    async def resolve_conflict(self, conflict_id: UUID, resolution: str, winning_claim: str | None = None) -> None:
        stmt = (
            select(SemanticConflict)
            .where(SemanticConflict.user_id == self.user_id)
            .where(SemanticConflict.id == conflict_id)
        )
        conflict = (await self.db.execute(stmt)).scalar_one_or_none()
        if not conflict:
            raise SemanticMemoryError(f"Conflict {conflict_id} not found")

        conflict.resolved = True
        conflict.resolution = resolution

        if winning_claim:
            entry_stmt = (
                select(SemanticMemoryEntry)
                .where(SemanticMemoryEntry.user_id == self.user_id)
                .where(SemanticMemoryEntry.id == conflict.semantic_entry_id)
            )
            entry = (await self.db.execute(entry_stmt)).scalar_one()
            entry.content = winning_claim
            entry.content_hash = _content_hash(winning_claim)
            entry.embedding = await asyncio.to_thread(self.embed, winning_claim)
            entry.is_contested = False
            entry.contested_reason = None

        await self.db.commit()

    async def get_pending_conflicts(self, limit: int = 20) -> list[dict[str, Any]]:
        stmt = (
            select(SemanticConflict)
            .where(SemanticConflict.user_id == self.user_id)
            .where(SemanticConflict.resolved.is_(False))
            .order_by(SemanticConflict.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [
            {
                "id": str(c.id),
                "existing_claim": c.existing_claim,
                "conflicting_claim": c.conflicting_claim,
                "created_at": c.created_at.isoformat(),
            }
            for c in result.scalars().all()
        ]

    # ── Maintenance ──────────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        stmt = select(
            func.count(SemanticMemoryEntry.id),
            func.avg(SemanticMemoryEntry.confidence),
            func.avg(SemanticMemoryEntry.corroboration_count),
            func.count(SemanticMemoryEntry.id).filter(SemanticMemoryEntry.is_contested.is_(True)),
        )
        stmt = stmt.where(SemanticMemoryEntry.user_id == self.user_id)
        result = await self.db.execute(stmt)
        row = result.one()
        return {
            "total_entries": row[0] or 0,
            "avg_confidence": round(float(row[1]), 4) if row[1] else None,
            "avg_corroboration": round(float(row[2]), 2) if row[2] else None,
            "contested_entries": row[3] or 0,
        }
