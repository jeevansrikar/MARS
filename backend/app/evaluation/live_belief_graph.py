"""Evidence graph mutations. Confidence is an explicitly supplied assessment, never
automatically increased or decreased by graph topology. Persist graph snapshots in
job checkpoints when using this library across worker attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

import structlog

log = structlog.get_logger(__name__)


class NodeType(str, Enum):
    CLAIM = "claim"
    SOURCE = "source"
    SYNTHESIS = "synthesis"


class EdgeType(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"


class GraphEventType(str, Enum):
    NODE_ADDED = "node_added"
    NODE_CONFIDENCE_CHANGED = "node_confidence_changed"
    EDGE_ADDED = "edge_added"
    CONTRADICTION_DETECTED = "contradiction_detected"
    CONTRADICTION_RESOLVED = "contradiction_resolved"
    NODE_REMOVED = "node_removed"


@dataclass
class BeliefNode:
    id: str
    node_type: NodeType
    label: str
    confidence: float | None
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.node_type.value,
            "label": self.label,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "agent_id": self.agent_id,
            "metadata": self.metadata,
        }


@dataclass
class BeliefEdge:
    id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float | None = None
    explanation: str = ""
    resolved: bool = False  # For contradiction edges — has this been addressed?

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source_id,
            "target": self.target_id,
            "type": self.edge_type.value,
            "weight": round(self.weight, 4) if self.weight is not None else None,
            "explanation": self.explanation,
            "resolved": self.resolved,
        }


@dataclass
class GraphEvent:
    """A single, typed, serializable mutation event — this IS the live stream."""

    event_type: GraphEventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    node: dict[str, Any] | None = None
    edge: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_sse_payload(self) -> dict[str, Any]:
        """Directly consumable by an SSE `data:` field — no further
        transformation needed by the caller."""
        payload: dict[str, Any] = {
            "event": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.node is not None:
            payload["node"] = self.node
        if self.edge is not None:
            payload["edge"] = self.edge
        if self.extra:
            payload.update(self.extra)
        return payload


class LiveBeliefGraphError(Exception):
    pass


class LiveBeliefGraph:
    """
    The WOW feature's backend. One instance per pipeline session.

    Usage (called inline by agents during pipeline execution):
        graph = LiveBeliefGraph(session_id, on_event=sse_queue.put_nowait)

        node = graph.add_claim_node("IBM Condor has 1121 qubits", agent_id="researcher_a", confidence=0.85)
        graph.add_relationship(node.id, other_node.id, EdgeType.SUPPORTS)

        # When Critic finds a contradiction:
        graph.flag_contradiction(node_a.id, node_b.id, explanation="Conflicting qubit counts")

        # When debate resolves it:
        graph.resolve_contradiction(edge_id)
    """

    def __init__(
        self,
        session_id: str,
        on_event: Callable[[GraphEvent], None] | None = None,
    ):
        self.session_id = session_id
        self._on_event = on_event
        self.nodes: dict[str, BeliefNode] = {}
        self.edges: dict[str, BeliefEdge] = {}

    def _emit(self, event: GraphEvent) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception as exc:
                # Live-stream emission is best-effort — a broken SSE
                # connection or full queue must never break graph state
                # mutation, which is the actual source of truth.
                log.warning("live_belief_graph.emit_failed", session_id=self.session_id, error=str(exc))

    def add_claim_node(
        self,
        label: str,
        agent_id: str,
        confidence: float | None = None,
        node_type: NodeType = NodeType.CLAIM,
        metadata: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> BeliefNode:
        node = BeliefNode(
            id=node_id or str(uuid4()),
            node_type=node_type,
            label=label,
            confidence=confidence,
            agent_id=agent_id,
            metadata=metadata or {},
        )
        self.nodes[node.id] = node
        self._emit(GraphEvent(event_type=GraphEventType.NODE_ADDED, node=node.to_dict()))
        log.info("live_belief_graph.node_added", session_id=self.session_id, node_id=node.id, agent=agent_id)
        return node

    def add_relationship(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        weight: float | None = None,
        explanation: str = "",
    ) -> BeliefEdge:
        if source_id not in self.nodes or target_id not in self.nodes:
            raise LiveBeliefGraphError(f"Cannot add edge: source={source_id} or target={target_id} not in graph")

        edge = BeliefEdge(
            id=str(uuid4()),
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            explanation=explanation,
        )
        self.edges[edge.id] = edge

        event_type = (
            GraphEventType.CONTRADICTION_DETECTED if edge_type == EdgeType.CONTRADICTS else GraphEventType.EDGE_ADDED
        )
        self._emit(GraphEvent(event_type=event_type, edge=edge.to_dict()))

        return edge

    def flag_contradiction(self, node_a_id: str, node_b_id: str, explanation: str = "") -> BeliefEdge:
        """Convenience wrapper — the common case of the Critic detecting
        a contradiction between two existing claim nodes."""
        return self.add_relationship(
            node_a_id,
            node_b_id,
            EdgeType.CONTRADICTS,
            explanation=explanation,
        )

    def resolve_contradiction(self, edge_id: str, resolution_note: str = "") -> BeliefEdge:
        """Record a resolution without inventing a numerical confidence change."""
        edge = self.edges.get(edge_id)
        if edge is None:
            raise LiveBeliefGraphError(f"Edge not found: {edge_id}")
        if edge.edge_type != EdgeType.CONTRADICTS:
            raise LiveBeliefGraphError(f"Edge {edge_id} is not a contradiction edge")

        edge.resolved = True
        edge.explanation = resolution_note or edge.explanation

        self._emit(GraphEvent(event_type=GraphEventType.CONTRADICTION_RESOLVED, edge=edge.to_dict()))
        log.info("live_belief_graph.contradiction_resolved", session_id=self.session_id, edge_id=edge_id)
        return edge

    def update_node_confidence(self, node_id: str, new_confidence: float, reason: str = "") -> BeliefNode:
        node = self.nodes.get(node_id)
        if node is None:
            raise LiveBeliefGraphError(f"Node not found: {node_id}")

        old_confidence = node.confidence
        node.confidence = max(0.0, min(1.0, new_confidence))
        self._emit(
            GraphEvent(
                event_type=GraphEventType.NODE_CONFIDENCE_CHANGED,
                node=node.to_dict(),
                extra={
                    "previous_confidence": round(old_confidence, 4) if old_confidence is not None else None,
                    "reason": reason,
                },
            )
        )
        return node

    def get_unresolved_contradictions(self) -> list[BeliefEdge]:
        return [e for e in self.edges.values() if e.edge_type == EdgeType.CONTRADICTS and not e.resolved]

    def stats(self) -> dict[str, Any]:
        contradictions = [e for e in self.edges.values() if e.edge_type == EdgeType.CONTRADICTS]
        assessments = [n.confidence for n in self.nodes.values() if n.confidence is not None]
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "contradictions_total": len(contradictions),
            "contradictions_unresolved": len(self.get_unresolved_contradictions()),
            "avg_confidence": round(sum(assessments) / len(assessments), 4) if assessments else None,
        }

    def to_static_graph(self) -> dict[str, Any]:
        """
        Export the final graph state for handoff to Week 1's post-hoc
        EvidenceGraph analysis, or for archival in episodic memory. This
        is the bridge between "live during execution" and "durable
        record after completion."
        """
        return {
            "session_id": self.session_id,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
            "stats": self.stats(),
        }
