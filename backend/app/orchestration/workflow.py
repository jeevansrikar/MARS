import asyncio
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from app.agents import prompts
from app.core.errors import AppError
from app.core.telemetry import QUALITY, RESEARCH_COUNTS, safe_span
from app.evaluation.beliefs import assessment_graph
from app.evaluation.grounding import build_result, validate_evidence
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import RankedList, ReciprocalRankFusion
from app.schemas.research import AgentResult, Claim, Source


class Workflow:
    def __init__(self, repo, job, llm, search, embedder=None):
        self.repo, self.job, self.llm, self.search = repo, job, llm, search
        self.embedder = embedder
        self.cfg = repo.settings

    async def stage(self, name, progress, action):
        existing = await self.repo.stage_start(self.job.id, self.job.lease_token, name, progress)
        if existing is not None:
            return existing
        with safe_span("agent.execute", attributes={"agent": name, "job.id": self.job.id}):
            output = await action()
        await self.repo.stage_complete(self.job.id, self.job.lease_token, name, output)
        return output

    async def generate(self, prompt, data, model):
        generation = await self.llm.generate(
            prompt, json.dumps(data, ensure_ascii=False), model, self.cfg.max_output_tokens
        )
        try:
            output = json.loads(generation.text)
            if not isinstance(output, dict):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise AppError(
                "INVALID_PROVIDER_OUTPUT", "Provider did not return the required structured response", 503, True
            ) from exc
        return {
            **output,
            "model": generation.model,
            "input_tokens": generation.input_tokens,
            "output_tokens": generation.output_tokens,
        }

    async def run(self):
        if self.job.memory_scope == "organization" and not self.job.organization_id:
            raise AppError("FORBIDDEN", "Organization context is no longer available", 403)

        async def plan():
            memories = await self.repo.memory(
                self.job.user_id, self.job.session_id, organization_id=self.job.organization_id
            )
            index = BM25Index()
            index.add_documents([(m["id"], m["content"]) for m in memories])
            ranked_memory = [RankedList("memory_bm25", [r.doc_id for r in index.search(self.job.query, top_k=10)])]
            if self.embedder:
                vector = (await self.embedder.encode([self.job.query]))[0]
                semantic = await self.repo.semantic_memory(
                    self.job.user_id,
                    vector,
                    self.embedder.model_id,
                    self.job.session_id,
                    organization_id=self.job.organization_id,
                )
                ranked_memory.append(RankedList("memory_dense", [row["id"] for row in semantic]))
                memories = list({row["id"]: row for row in memories + semantic}.values())
            wanted = {r.doc_id for r in ReciprocalRankFusion().fuse(ranked_memory)[:3]}
            return await self.generate(
                prompts.PLANNER,
                {
                    "query": self.job.query,
                    "prior_hypotheses_to_recheck": [m["content"] for m in memories if m["id"] in wanted],
                },
                self.cfg.fast_model,
            )

        plan_result = await self.stage("planner", 0.05, plan)

        async def gather():
            queries = [q[:500] for q in plan_result.get("queries", []) if isinstance(q, str) and q.strip()][:3]
            queries = list(dict.fromkeys([self.job.query] + queries))[: self.cfg.max_search_calls]
            results = []
            for query in queries:
                results.extend(await self.search.search(query))
            unique: dict[str, dict] = {}
            from app.providers.search import canonicalize

            for item in results:
                try:
                    unique.setdefault(canonicalize(item["url"]), item)
                except (AppError, KeyError, TypeError):
                    continue
            semaphore = asyncio.Semaphore(4)

            async def fetch(item):
                async with semaphore:
                    try:
                        return await self.search.fetch_url(item["url"], item.get("title", ""))
                    except AppError:
                        return None

            fetched = await asyncio.gather(*(fetch(item) for item in list(unique.values())[: self.cfg.max_sources]))
            sources = list({s.content_hash: s for s in fetched if s}.values())
            if not sources:
                raise AppError("NO_EVIDENCE", "No usable public source content was retrieved", 503)
            chunks = {}
            for source in sources:
                for offset in range(0, len(source.content), 1200):
                    chunks[f"{source.id}:{offset}"] = {
                        "source_id": source.id,
                        "passage": source.content[offset : offset + 1500],
                    }
            index = BM25Index()
            index.add_documents([(key, value["passage"]) for key, value in chunks.items()])
            ranked = [
                RankedList(f"bm25_{i}", [r.doc_id for r in index.search(q, top_k=20)]) for i, q in enumerate(queries)
            ]
            fused = ReciprocalRankFusion().fuse(ranked)
            if self.embedder:
                candidates = {r.doc_id: chunks[r.doc_id] for r in fused[:64]} or dict(list(chunks.items())[:64])
                ranked.append(RankedList("dense", await self.embedder.rank(self.job.query, candidates)))
                fused = ReciprocalRankFusion().fuse(ranked)
            passages = [chunks[r.doc_id] for r in fused[:16]]
            if not passages:
                passages = list(chunks.values())[:16]
            return {
                "sources": [s.model_dump() for s in sources],
                "source_index": [s.model_dump(exclude={"content"}) for s in sources],
                "rankers": [r.ranker_name for r in ranked],
                "passages": passages,
                "notes": f"Fetched {len(sources)} unique source documents.",
            }

        evidence = await self.stage("retrieval", 0.15, gather)
        sources = [Source.model_validate(s) for s in evidence["sources"]]

        async def rerank():
            response = await self.generate(
                'Rank supplied passage indices by relevance to the question. Return JSON {"indices":[0,1,...]}. Include at most 12 distinct valid indices.',
                {"query": self.job.query, "passages": evidence["passages"]},
                self.cfg.fast_model,
            )
            indices = [i for i in response.get("indices", []) if type(i) is int and 0 <= i < len(evidence["passages"])]
            response["passages"] = [evidence["passages"][i] for i in dict.fromkeys(indices)][:12] or evidence[
                "passages"
            ][:12]
            return response

        ranked = await self.stage("reranker", 0.25, rerank)
        context = {"query": self.job.query, "passages": ranked["passages"]}

        async def research(name, prompt):
            async def action():
                output = await self.generate(prompt + prompts.FINDINGS, context, self.cfg.research_model)
                try:
                    parsed = AgentResult.model_validate(output)
                except ValidationError as exc:
                    raise AppError("INVALID_PROVIDER_OUTPUT", "Research response failed validation", 503, True) from exc
                for index, claim in enumerate(parsed.findings):
                    claim.id = f"{self.job.id}:{name}:{index}"
                    claim.status = "uncertain"
                validate_evidence(parsed.findings, sources)
                return {**output, **parsed.model_dump()}

            return await self.stage(name, 0.35, action)

        # TaskGroup cancels the sibling on failure; no orphan provider calls continue.
        async with asyncio.TaskGroup() as group:
            a = group.create_task(research("researcher_a", prompts.RESEARCHER_A))
            b = group.create_task(research("researcher_b", prompts.RESEARCHER_B))
        unique_claims: dict[str, Claim] = {}
        for item in a.result()["findings"] + b.result()["findings"]:
            claim = Claim.model_validate(item)
            key = hashlib.sha256(claim.claim.strip().lower().encode()).hexdigest()
            unique_claims.setdefault(key, claim)
        claims = list(unique_claims.values())

        async def graph_checkpoint(conflicts):
            return {"belief_graph": assessment_graph(self.job.id, claims, conflicts)}

        await self.stage("evidence_fusion", 0.5, lambda: graph_checkpoint([]))
        context["claims"] = [c.model_dump() for c in claims]
        critic = await self.stage(
            "critic", 0.55, lambda: self.generate(prompts.CRITIC, context, self.cfg.research_model)
        )
        skeptic = await self.stage(
            "skeptic",
            0.65,
            lambda: self.generate(prompts.SKEPTIC, {**context, "critique": critic}, self.cfg.research_model),
        )
        known = {c.id for c in claims}
        conflicts = [
            c
            for c in critic.get("conflicts", [])
            if isinstance(c, dict) and isinstance(c.get("claim_ids"), list) and len(set(c["claim_ids"]) & known) >= 2
        ]
        debate: list[dict[str, Any]] = []
        await self.stage("conflict_graph", 0.66, lambda: graph_checkpoint(conflicts))
        for i in range(self.cfg.max_debate_rounds if conflicts else 0):
            result = await self.stage(
                f"debate_{i + 1}",
                0.7,
                lambda: self.generate(
                    prompts.DEBATE,
                    {
                        **context,
                        "conflicts": conflicts,
                        "skeptic": skeptic,
                        "previous_rounds": debate,
                        "stance": "Advocate" if i % 2 == 0 else "Challenger",
                    },
                    self.cfg.research_model,
                ),
            )
            debate.append(result)
        synthesis = await self.stage(
            "synthesizer",
            0.8,
            lambda: self.generate(
                prompts.SYNTHESIZER,
                {**context, "critique": critic, "skeptic": skeptic, "debate": debate},
                self.cfg.synthesis_model,
            ),
        )
        verification = await self.stage(
            "verifier",
            0.9,
            lambda: self.generate(prompts.VERIFIER, {**context, "conflicts": conflicts}, self.cfg.verification_model),
        )
        verdicts = {v.get("id"): v for v in verification.get("verdicts", []) if isinstance(v, dict)}
        for claim in claims:
            verdict = verdicts.get(claim.id, {})
            status = verdict.get("status", "insufficient_evidence")
            claim.status = (
                status
                if status in {"supported", "uncertain", "conflicting", "insufficient_evidence"}
                else "insufficient_evidence"
            )
            claim.reasoning = str(verdict.get("reasoning", "No verification verdict returned"))[:2000]
        validate_evidence(claims, sources)
        graph = (await self.stage("beliefs", 0.92, lambda: graph_checkpoint(conflicts)))["belief_graph"]
        warnings = ["Source support is assessed by an LLM and is not a guarantee of factual correctness."]
        if conflicts:
            warnings.append("Conflicting claims were identified; inspect individual assessments.")
        result = build_result(claims, sources, synthesis.get("claim_ids", []), warnings, graph)
        embeddings = None
        if self.embedder:

            async def embed_memory():
                supported = [c for c in result.claims if c.status == "supported"]
                vectors = await self.embedder.encode([c.claim for c in supported])
                return {
                    "embeddings": {c.id: vector for c, vector in zip(supported, vectors, strict=True)},
                    "embedding_model": self.embedder.model_id,
                }

            memory = await self.stage("memory", 0.95, embed_memory)
            embeddings = memory["embeddings"]
        await self.repo.finish(
            self.job.id, self.job.lease_token, result, embeddings, self.embedder.model_id if self.embedder else None
        )
        RESEARCH_COUNTS.labels("sources").observe(len(result.sources))
        RESEARCH_COUNTS.labels("claims").observe(len(result.claims))
        for name in ("citation_coverage", "evidence_coverage", "contradiction_rate", "unsupported_claim_rate"):
            value = getattr(result.quality_metrics, name)
            if value is not None:
                QUALITY.labels(name).observe(value)
        return result
