import { describe, expect, it } from 'vitest';
import { parseFrame, readEvents } from './sse';
import { safeUrl } from './api';
describe('durable SSE parsing', () => {
  it('ignores heartbeat frames', () => { expect(parseFrame(': heartbeat')).toBeNull(); });
  it('supports CRLF and rejects invalid cursors', () => {
    expect(parseFrame('id: 2\r\ndata: {"sequence":2,"job_id":"j","type":"agent_completed"}')?.sequence).toBe(2);
    expect(() => parseFrame('data: {"sequence":-1}')).toThrow();
  });
  it('decodes split Unicode and multiple frames without loss', async () => {
    const text = 'id: 1\nevent: job_started\ndata: {"sequence":1,"job_id":"j","type":"job_started","payload":{"text":"研究"}}\n\n: heartbeat\n\nid: 2\ndata: {"sequence":2,"job_id":"j","type":"job_completed"}\n\n';
    const bytes = new TextEncoder().encode(text); const events: number[] = [];
    const body = new ReadableStream<Uint8Array>({ start(controller) { for (const b of bytes) controller.enqueue(new Uint8Array([b])); controller.close(); } });
    await readEvents(body, event => events.push(event.sequence), new AbortController().signal);
    expect(events).toEqual([1, 2]);
  });
  it('does not render executable citation schemes', () => { expect(safeUrl('javascript:alert(1)')).toBeUndefined(); expect(safeUrl('https://example.org')).toBe('https://example.org/'); });
});
