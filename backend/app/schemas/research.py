from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JobStatus = Literal["queued", "running", "cancelling", "cancelled", "completed", "completed_with_warnings", "failed"]
TERMINAL = {"cancelled", "completed", "completed_with_warnings", "failed"}


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=3, max_length=2000)
    session_id: UUID | None = None
    memory_scope: Literal["session", "user", "organization"] = "user"
    organization_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.memory_scope == "organization") != bool(self.organization_id):
            raise ValueError("Organization sharing requires an organization ID and organization scope")
        return self

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Query must contain at least three characters")
        return value


class Evidence(BaseModel):
    source_id: str
    passage: str = Field(min_length=10, max_length=1500)


class Claim(BaseModel):
    id: str
    claim: str = Field(min_length=3, max_length=1500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    status: Literal["supported", "uncertain", "conflicting", "insufficient_evidence"] = "uncertain"
    reasoning: str = ""


class AgentResult(BaseModel):
    findings: list[Claim] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=4000)


class Source(BaseModel):
    id: str
    url: str
    canonical_url: str
    title: str
    publisher: str
    content: str
    content_hash: str
    retrieved_at: float
    http_status: int = 200
    source_type: str = "web"


class Citation(BaseModel):
    claim_id: str
    source_id: str
    url: str
    passage: str


class QualityMetrics(BaseModel):
    factual_claims: int
    supported_claims: int
    citation_coverage: float | None
    evidence_coverage: float | None
    contradiction_rate: float | None
    unsupported_claim_rate: float | None
    unique_publishers: int
    unique_documents: int
    method: str = "LLM entailment assessment with exact passage validation; not independently measured factual accuracy"


class ResearchResult(BaseModel):
    summary: str
    answer: str
    claims: list[Claim]
    citations: list[Citation]
    sources: list[Source]
    quality_metrics: QualityMetrics
    warnings: list[str] = Field(default_factory=list)
    belief_graph: dict[str, Any] = Field(default_factory=dict)


class ResearchEvent(BaseModel):
    event_id: str
    sequence: int
    job_id: str
    type: str
    agent: str | None = None
    timestamp: float
    payload: dict[str, Any]


class ResearchJob(BaseModel):
    job_id: str
    session_id: str
    query: str
    memory_scope: Literal["session", "user", "organization"] = "user"
    organization_id: str | None = None
    status: JobStatus
    current_stage: str | None
    progress: float
    attempt: int
    created_at: float
    started_at: float | None
    completed_at: float | None
    error_code: str | None
    error_message: str | None
    last_sequence: int
    result: ResearchResult | None = None
    stages: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str | None = None
