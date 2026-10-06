export type JobStatus = 'queued' | 'running' | 'cancelling' | 'cancelled' | 'completed' | 'completed_with_warnings' | 'failed';
export type UIStatus = JobStatus | 'idle' | 'submitting' | 'reconnecting';
export interface Evidence { source_id: string; passage: string }
export interface Claim { id: string; claim: string; evidence: Evidence[]; status: string; reasoning: string }
export interface Source { id: string; url: string; title: string; publisher: string; content_hash: string; retrieved_at: number }
export interface ResearchResult {
  summary: string; answer: string; claims: Claim[]; sources: Source[];
  citations: { claim_id: string; source_id: string; url: string; passage: string }[];
  quality_metrics: { factual_claims: number; supported_claims: number; citation_coverage: number | null; evidence_coverage: number | null; contradiction_rate: number | null; unsupported_claim_rate: number | null; unique_publishers: number; unique_documents: number; method: string };
  warnings: string[];
  belief_graph: { nodes: { id: string; label: string; status: string; source_ids: string[] }[]; edges: { type: string; claim_ids: string[]; reason: string }[] };
}
export interface Stage { status: string; output: Record<string, unknown> | null; error_code?: string | null }
export interface ResearchJob {
  memory_scope?: 'session' | 'user' | 'organization'; organization_id?: string | null;
  job_id: string; session_id: string; query: string; status: JobStatus; current_stage: string | null;
  progress: number; attempt: number; created_at: number; started_at: number | null; completed_at: number | null;
  error_code: string | null; error_message: string | null; last_sequence: number;
  result: ResearchResult | null; stages: Record<string, Stage>;
}
export interface ResearchEvent { event_id: string; sequence: number; job_id: string; type: string; agent: string | null; timestamp: number; payload: Record<string, unknown> }
export interface JobSummary { job_id: string; query: string; status: JobStatus; created_at: number }
export const isTerminal = (status: string) => ['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(status);
