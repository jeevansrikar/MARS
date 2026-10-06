import type { ResearchEvent } from './types';
export function parseFrame(frame: string): ResearchEvent | null {
  const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
  if (!data) return null;
  const event = JSON.parse(data) as ResearchEvent;
  if (!Number.isSafeInteger(event.sequence) || event.sequence < 1 || !event.job_id || !event.type) throw new Error('Invalid research event');
  return event;
}
export async function readEvents(body: ReadableStream<Uint8Array>, receive: (event: ResearchEvent) => void, signal: AbortSignal) {
  const reader = body.getReader(); const decoder = new TextDecoder(); let buffer = '';
  const abort = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    while (!signal.aborted) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const { done, value } = await Promise.race([reader.read(), new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('Stream heartbeat timed out')), 30000);
      })]).finally(() => clearTimeout(timer));
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 1_000_000) throw new Error('Event frame exceeds limit');
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
        const event = parseFrame(frame); if (event) receive(event);
      }
    }
  } finally { signal.removeEventListener('abort', abort); await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}
