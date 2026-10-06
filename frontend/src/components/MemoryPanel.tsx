import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
interface Memory { id: string; content: string; job_id: string; expires_at: number; scope: string; can_delete: boolean }
export function MemoryPanel({ organizationId, sessionId }: { organizationId?: string; sessionId?: string }) {
  const [entries, setEntries] = useState<Memory[]>([]); const [query, setQuery] = useState(''); const [error, setError] = useState('');
  const scope = `${organizationId ? `&organization_id=${encodeURIComponent(organizationId)}` : ''}${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ''}`;
  const load = useCallback(async (search = '') => { try { setEntries(await api<Memory[]>(`/api/v1/memory?query=${encodeURIComponent(search)}${scope}`)); setError(''); } catch (e) { setError(String(e)); } }, [scope]);
  useEffect(() => { setEntries([]); void load(); }, [load]);
  return <section><h2>Your research memory</h2><p>Supported findings visible in this research context, retained for a limited time. Rechecked against fresh evidence in future jobs.</p>
    <form onSubmit={e => { e.preventDefault(); void load(query); }}><input aria-label="Search memory" value={query} onChange={e => setQuery(e.target.value)} /><button>Search memory</button></form>
    {error && <p role="alert">{error}</p>}{entries.length === 0 && <p>No matching memory entries.</p>}
    {entries.map(entry => <article className="card" key={entry.id}><p>{entry.content}</p><small>Expires {new Date(entry.expires_at * 1000).toLocaleDateString()}</small>
      {entry.can_delete && <button onClick={() => { void api(`/api/v1/memory/${entry.id}`, { method: 'DELETE' }).then(() => load(query)).catch(e => setError(String(e))); }}>Forget</button>}</article>)}
  </section>;
}
