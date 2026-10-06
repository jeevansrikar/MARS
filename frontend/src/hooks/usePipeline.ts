import { useState, useCallback, useRef, useEffect } from 'react';
import { API, api, ApiError } from '../api';
import { readEvents } from '../sse';
import { isTerminal } from '../types';
import type { ResearchJob, ResearchEvent, JobSummary, UIStatus, JobStatus } from '../types';

export function usePipeline(userId: string) {
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [status, setStatus] = useState<UIStatus>('idle');
  const [error, setError] = useState('');
  const [history, setHistory] = useState<JobSummary[]>([]);
  const controller = useRef<AbortController | null>(null);
  const pending = useRef<{ query: string; key: string; memory_scope?: string; organization_id?: string; session_id?: string } | null>(null);
  const storageKey = `mars:job:${userId}`;
  const pendingKey = `mars:pending:${userId}`;
  const refreshHistory = useCallback(async () => { setHistory(await api<JobSummary[]>('/api/v1/research')); }, []);

  const open = useCallback(async (id: string) => {
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    localStorage.setItem(storageKey, id); setError('');
    let cursor = 0; let delay = 1000;
    const apply = (event: ResearchEvent) => {
      if (active.signal.aborted || event.job_id !== id || event.sequence <= cursor) return;
      cursor = event.sequence;
      const nextStatus = typeof event.payload.status === 'string' && ['queued', 'running', 'cancelling', 'cancelled', 'completed', 'completed_with_warnings', 'failed'].includes(event.payload.status) ? event.payload.status as JobStatus : undefined;
      if (nextStatus) setStatus(nextStatus);
      setJob(previous => {
        if (!previous || previous.job_id !== id) return previous;
        const stages = { ...previous.stages };
        if (event.agent && ['agent_started', 'agent_completed', 'agent_failed', 'agent_cancelled'].includes(event.type)) {
          stages[event.agent] = { status: event.type === 'agent_started' ? 'running' : event.type.slice(6), output: event.type === 'agent_started' ? null : event.payload };
        }
        return { ...previous, stages, last_sequence: cursor, status: nextStatus || previous.status,
          attempt: typeof event.payload.attempt === 'number' ? event.payload.attempt : previous.attempt,
          current_stage: event.agent || previous.current_stage,
          progress: typeof event.payload.progress === 'number' ? event.payload.progress : previous.progress };
      });
    };
    while (!active.signal.aborted) {
      try {
        const snapshot = await api<ResearchJob>(`/api/v1/research/${id}`, { signal: active.signal });
        if (active.signal.aborted) return;
        setJob(snapshot); setStatus(snapshot.status); setError(''); cursor = snapshot.last_sequence;
        if (isTerminal(snapshot.status)) { void refreshHistory().catch(() => undefined); return; }
        // Hydration and cursor come from one durable job snapshot; replay covers subsequent events.
        const connection = new AbortController();
        const abortConnection = () => connection.abort();
        active.signal.addEventListener('abort', abortConnection, { once: true });
        const connectTimer = setTimeout(abortConnection, 20000);
        try {
          const stream = await fetch(`${API}/api/v1/research/${id}/events`, { credentials: 'include', signal: connection.signal, headers: { 'Last-Event-ID': String(cursor) } });
          clearTimeout(connectTimer);
          if (!stream.ok || !stream.body) throw new ApiError('Research stream unavailable', stream.status);
          await readEvents(stream.body, apply, active.signal);
        } finally { clearTimeout(connectTimer); active.signal.removeEventListener('abort', abortConnection); connection.abort(); }
        delay = 1000;
      } catch (e) {
        if (active.signal.aborted) return;
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) { setError(e.message); setStatus('failed'); return; }
        setStatus('reconnecting'); setError('Connection interrupted. Research continues in the background. Reconnecting…');
        await new Promise<void>(resolve => {
          const done = () => { clearTimeout(timer); active.signal.removeEventListener('abort', done); resolve(); };
          const timer = setTimeout(done, delay); active.signal.addEventListener('abort', done, { once: true });
        });
        delay = Math.min(delay * 2, 10000);
      }
    }
  }, [refreshHistory, storageKey]);

  const run = useCallback(async (query: string, retry = false, memory: { memory_scope: string; organization_id?: string; session_id?: string } = { memory_scope: 'user' }) => {
    controller.current?.abort(); setError(''); setJob(null); setStatus('submitting'); localStorage.removeItem(storageKey);
    const submission = new AbortController(); controller.current = submission;
    if (retry && !pending.current) {
      try { pending.current = JSON.parse(sessionStorage.getItem(pendingKey) || 'null'); } catch { pending.current = null; }
    }
    if (!retry || pending.current?.query !== query) pending.current = { query, key: crypto.randomUUID(), ...memory };
    sessionStorage.setItem(pendingKey, JSON.stringify(pending.current));
    try {
      const created = await api<ResearchJob>('/api/v1/research', { method: 'POST', signal: submission.signal, headers: { 'Idempotency-Key': pending.current!.key }, body: JSON.stringify({ query, memory_scope: pending.current!.memory_scope || 'user', organization_id: pending.current!.organization_id, session_id: pending.current!.session_id }) });
      if (submission.signal.aborted) return;
      pending.current = null; sessionStorage.removeItem(pendingKey); void refreshHistory().catch(() => undefined); void open(created.job_id);
    } catch (e) { if (!submission.signal.aborted) { setError(e instanceof Error ? e.message : 'Submission failed'); setStatus('failed'); } }
  }, [open, refreshHistory, pendingKey, storageKey]);
  const cancel = useCallback(async () => {
    if (!job) return;
    setStatus('cancelling');
    try { await api(`/api/v1/research/${job.job_id}/cancel`, { method: 'POST' }); }
    catch (e) { setError(e instanceof Error ? e.message : 'Cancellation failed'); setStatus(job.status); }
  }, [job]);
  const reset = useCallback(() => { controller.current?.abort(); localStorage.removeItem(storageKey); sessionStorage.removeItem(pendingKey); pending.current = null; setJob(null); setStatus('idle'); setError(''); }, [storageKey, pendingKey]);
  useEffect(() => {
    void refreshHistory().catch(e => setError(String(e)));
    const id = localStorage.getItem(storageKey); if (id) void open(id);
    else {
      try {
        const saved = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
        if (saved?.query && saved?.key) void run(saved.query, true);
      } catch { sessionStorage.removeItem(pendingKey); }
    }
    return () => controller.current?.abort();
  }, [open, refreshHistory, storageKey, pendingKey, run]);
  return { job, status, error, history, run, open, cancel, reset };
}
