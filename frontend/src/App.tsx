import { useEffect, useState } from 'react';
import { api, safeUrl, ApiError } from './api';
import { usePipeline } from './hooks/usePipeline';
import { MemoryPanel } from './components/MemoryPanel';
import { isTerminal } from './types';
import type { Stage, Claim, Source, ResearchResult } from './types';

const AGENTS = [ ['planner', 'Planner', 'Decomposition'], ['researcher_a', 'Researcher A', 'Primary evidence'], ['researcher_b', 'Researcher B', 'Domain specialist'], ['critic', 'Critic', 'Gaps and contradictions'], ['skeptic', 'Skeptic', 'Counter-evidence'], ['synthesizer', 'Synthesizer', 'Final synthesis'], ['verifier', 'Verifier', 'Evidence assessment'] ];
const PRESETS = ['Compare RAG and fine-tuning for enterprise deployment: cost, accuracy and latency', 'What recent quantum computing results have independent experimental support?'];
type User = { id: string; email: string };
type Tab = 'agents' | 'debate' | 'beliefs' | 'evidence' | 'evaluation' | 'memory';
function stageText(stage?: Stage) {
  if (stage?.error_code) return `${stage.status}: ${stage.error_code}`;
  if (!stage?.output) return stage?.status === 'running' ? 'Working…' : 'Waiting for upstream stages';
  const output = stage.output;
  if (Array.isArray(output.findings)) return output.findings.map((f: { claim: string }) => f.claim).join('\n\n');
  if (typeof output.notes === 'string') return output.notes;
  if (Array.isArray(output.queries)) return output.queries.join('\n');
  if (Array.isArray(output.verdicts)) return `${output.verdicts.length} claim assessments completed`;
  return 'Stage completed';
}
function Workspace({ user, logout }: { user: User; logout: () => void }) {
  const { job, status, error, history, run, open, cancel, reset } = usePipeline(user.id);
  const [query, setQuery] = useState(PRESETS[0]); const [tab, setTab] = useState<Tab>('agents');
  useEffect(() => { if (job) setQuery(job.query); }, [job?.job_id, job?.query]);
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [memoryAudience, setMemoryAudience] = useState('user');
  useEffect(() => { void api<{ id: string; name: string }[]>('/api/v1/auth/organizations').then(setOrganizations).catch(() => undefined); }, []);
  useEffect(() => { if (job?.memory_scope) setMemoryAudience(job.organization_id ? `org:${job.organization_id}` : job.memory_scope); }, [job?.job_id, job?.memory_scope, job?.organization_id]);
  const memoryChoice = memoryAudience.startsWith('org:') ? { memory_scope: 'organization', organization_id: memoryAudience.slice(4), session_id: job?.session_id } : { memory_scope: memoryAudience, session_id: job?.session_id };
  const active = !['idle', 'failed', 'cancelled', 'completed', 'completed_with_warnings'].includes(status);
  const result = job?.result;
  const liveClaims = ['researcher_a', 'researcher_b'].flatMap(agent => {
    const findings = job?.stages[agent]?.output?.findings;
    return Array.isArray(findings) ? findings as Claim[] : [];
  });
  const visibleClaims = result?.claims || liveClaims;
  const sourceIndex = job?.stages.retrieval?.output?.source_index;
  const visibleSources = result?.sources || (Array.isArray(sourceIndex) ? sourceIndex as Source[] : []);
  const checkpointGraph = ['beliefs', 'conflict_graph', 'evidence_fusion'].map(stage => job?.stages[stage]?.output?.belief_graph).find(Boolean) as ResearchResult['belief_graph'] | undefined;
  const visibleGraph = result?.belief_graph || checkpointGraph || {
    nodes: liveClaims.map(claim => ({ id: claim.id, label: claim.claim, status: claim.status, source_ids: claim.evidence.map(e => e.source_id) })), edges: []
  };
  const metric = (value: number | null) => value === null ? 'N/A' : `${(value * 100).toFixed(1)}%`;
  return <div className="layout"><aside><div className="brand"><b>M</b><div>MARS<small>Research workspace</small></div></div>
    <p className={`status ${status}`} role="status">{status.replaceAll('_', ' ')}</p>
    <button onClick={reset}>New research</button>
    <h3>Recent research</h3><nav aria-label="Research history">{history.map(item => <button key={item.job_id} onClick={() => { void open(item.job_id); setQuery(item.query); }} className={job?.job_id === item.job_id ? 'selected' : ''}>{item.query}<small>{item.status.replaceAll('_', ' ')}</small></button>)}</nav>
    <div className="account"><small>{user.email}</small><button onClick={logout}>Sign out</button></div>
  </aside><main><header><div><h1>Research pipeline</h1><p>Specialist agents · structured debate · evidence you can inspect</p></div><span className="badge">MARS 2</span></header>
    <form className="query" onSubmit={e => { e.preventDefault(); if (!active) { setTab('agents'); void run(query.trim(), false, memoryChoice); } }}>
      <label htmlFor="query">Research question</label><textarea id="query" rows={3} maxLength={2000} minLength={3} value={query} onChange={e => setQuery(e.target.value)} disabled={active} required />
      <label htmlFor="memory-audience">Remember supported findings for</label><select id="memory-audience" value={memoryAudience} disabled={active} onChange={e => setMemoryAudience(e.target.value)}><option value="user">My future research</option><option value="session">This session only</option>{organizations.map(org => <option key={org.id} value={`org:${org.id}`}>{org.name} members</option>)}</select>
      <div className="actions">{PRESETS.map(p => <button type="button" className="preset" disabled={active} key={p} onClick={() => setQuery(p)}>{p.slice(0, 42)}…</button>)}
        <button className="primary" disabled={active || query.trim().length < 3}>{active ? status.replaceAll('_', ' ') : 'Run MARS →'}</button>
        {job && !isTerminal(job.status) && <button type="button" onClick={() => void cancel()} disabled={status === 'cancelling'}>Cancel research</button>}</div>
    </form>
    {error && <div className="notice" role="alert">{error} {status === 'failed' && <button onClick={() => void run(query.trim(), !job)}>Retry</button>}</div>}
    {job?.error_message && <div className="notice" role="alert">{job.error_code}: {job.error_message}<button onClick={() => void run(job.query)}>Start a new attempt</button></div>}
    {job && <div className="progress"><progress max={1} value={job.progress} aria-label="Research progress" /><small>{job.current_stage || 'queued'} · attempt {job.attempt} · {job.job_id}</small></div>}
    <div className="tabs" role="tablist">{(['agents', 'debate', 'beliefs', 'evidence', 'evaluation', 'memory'] as Tab[]).map(t => <button role="tab" aria-selected={t === tab} key={t} className={t === tab ? 'selected' : ''} onClick={() => setTab(t)}>{t}</button>)}</div>
    <div className="content" role="tabpanel">
      {tab === 'agents' && <><div className="grid">{AGENTS.map(([id, name, role]) => <article className={`card agent ${job?.stages[id]?.status || 'idle'}`} key={id}><div className="card-title"><h2>{name}</h2><span className="badge">{job?.stages[id]?.status || 'idle'}</span></div><small>{role}</small><p className="prewrap">{stageText(job?.stages[id])}</p></article>)}</div>
        {result && <article className="card answer"><h2>Evidence-backed answer</h2><p className="prewrap">{result.answer}</p>{result.warnings.map(w => <p className="warning" key={w}>{w}</p>)}</article>}</>}
      {tab === 'debate' && <>{Object.keys(job?.stages || {}).filter(k => k.startsWith('debate_')).map(k => <article className="card" key={k}><h2>{k.replace('_', ' ')}</h2><p>{stageText(job?.stages[k])}</p></article>)}<p>Debate runs only when the critic identifies conflicting claims. No consensus percentage is inferred from model confidence.</p></>}
      {tab === 'beliefs' && <><h2>Belief and evidence relationships</h2><p>Assessments evolve as evidence is collected and reviewed.</p>{visibleGraph.nodes.length ? visibleGraph.nodes.map(n => <article key={n.id} className="card"><span className="badge">{n.status}</span><p>{n.label}</p><small>{n.source_ids.length} linked source(s)</small></article>) : <p>Claim nodes appear as researchers finish, then update through critique and verification.</p>}{visibleGraph.edges.map((e, i) => <p className="notice" key={i}>Contradiction: {e.reason}</p>)}</>}
      {tab === 'evidence' && <><h2>Claims and citations</h2>{!visibleClaims.length && <p>Evidence appears as researchers finish. Support assessments remain provisional until verification.</p>}{visibleClaims.map(c => <article className="card" key={c.id}><span className="badge">{c.status.replaceAll('_', ' ')}</span><h3>{c.claim}</h3><p>{c.reasoning}</p>{c.evidence.map(e => { const source = visibleSources.find(s => s.id === e.source_id); return <blockquote key={e.source_id}>{e.passage}<footer>{source && <a href={safeUrl(source.url)} target="_blank" rel="noopener noreferrer">{source.title || source.publisher}</a>}</footer></blockquote>; })}</article>)}</>}
      {tab === 'evaluation' && <>{result ? <><h2>Measured evidence coverage</h2><p>{result.quality_metrics.method}</p><div className="grid">{[['Citation coverage', result.quality_metrics.citation_coverage], ['Evidence coverage', result.quality_metrics.evidence_coverage], ['Unsupported claim rate', result.quality_metrics.unsupported_claim_rate], ['Contradiction rate', result.quality_metrics.contradiction_rate]].map(([label, value]) => <article className="card" key={String(label)}><h2>{String(label)}</h2><strong className="metric">{metric(value as number | null)}</strong></article>)}</div><p>{result.quality_metrics.supported_claims} supported / {result.quality_metrics.factual_claims} proposed factual claims. {result.quality_metrics.unique_documents} distinct document contents from {result.quality_metrics.unique_publishers} publisher domains. Domains may share an owner; this is not a count of independent organizations.</p></> : <p>Evaluation is available after verification.</p>}</>}
      {tab === 'memory' && <MemoryPanel key={job?.job_id || "personal"} organizationId={job?.organization_id || undefined} sessionId={job?.session_id} />}
    </div></main></div>;
}
export default function App() {
  const [user, setUser] = useState<User | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  useEffect(() => { void api<User>('/api/v1/auth/me').then(setUser).catch(() => undefined).finally(() => setLoading(false)); }, []);
  async function logout() { try { await api('/api/v1/auth/logout', { method: 'POST' }); setUser(null); } catch (e) { if (e instanceof ApiError && e.status === 401) setUser(null); else setError(String(e)); } }
  if (loading) return <div className="login"><p role="status">Loading your workspace…</p></div>;
  if (user) return <><Workspace key={user.id} user={user} logout={() => void logout()} />{error && <p role="alert">{error}</p>}</>;
  return <div className="login"><form className="card" onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); setError(''); void api<User>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ email: data.get('email'), password: data.get('password') }) }).then(setUser).catch(e => setError(e.message)); }}><div className="brand"><b>M</b>MARS</div><h1>Your research workspace</h1><p>Sign in to run research and return to your results later.</p><label>Email<input name="email" type="email" autoComplete="username" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required maxLength={256} /></label><button className="primary">Sign in</button>{error && <p role="alert">{error}</p>}<small>Accounts are provisioned by your workspace administrator.</small></form></div>;
}
