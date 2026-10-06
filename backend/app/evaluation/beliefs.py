from itertools import combinations

from app.evaluation.live_belief_graph import LiveBeliefGraph
from app.schemas.research import Claim


def assessment_graph(job_id: str, claims: list[Claim], conflicts: list[dict]) -> dict:
    events = []
    graph = LiveBeliefGraph(job_id, on_event=lambda event: events.append(event.to_sse_payload()))
    for claim in claims:
        graph.add_claim_node(
            claim.claim,
            agent_id="evidence_fusion",
            node_id=claim.id,
            metadata={"status": claim.status, "source_ids": [e.source_id for e in claim.evidence]},
        )
    for conflict in conflicts:
        ids = list(dict.fromkeys(i for i in conflict.get("claim_ids", []) if i in graph.nodes))
        for one, two in combinations(ids, 2):
            graph.flag_contradiction(one, two, str(conflict.get("reason", "")))
    return {
        "nodes": [{"id": n.id, "label": n.label, **n.metadata} for n in graph.nodes.values()],
        "edges": [
            {"type": e.edge_type.value, "claim_ids": [e.source_id, e.target_id], "reason": e.explanation}
            for e in graph.edges.values()
        ],
        "mutations": events,
    }
