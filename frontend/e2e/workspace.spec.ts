import type { ResearchJob } from '../src/types';
import { test, expect, type Page } from '@playwright/test';

const user = { id: 'owner-a', email: 'researcher@example.org' };
const initial: ResearchJob = { job_id: 'job-1', session_id: 'session-1', query: 'How many samples were measured?', status: 'running',
  current_stage: 'planner', progress: .1, attempt: 1, created_at: 1, started_at: 1, completed_at: null,
  error_code: null, error_message: null, last_sequence: 2, result: null,
  stages: { planner: { status: 'completed', output: { notes: 'Plan preserved across refresh' } } } };

async function setup(page: Page) {
  let job = structuredClone(initial);
  let submissions = 0;
  let cancellations = 0;
  let failSubmission = false;
  const keys: string[] = [];
  const bodies: Record<string, unknown>[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const respond = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path.endsWith('/auth/organizations')) return respond([{ id: 'org-1', name: 'Research team' }]);
    if (path.endsWith('/auth/me') || path.endsWith('/auth/login')) return respond(user);
    if (path.endsWith('/auth/logout')) return respond({ status: 'signed_out' });
    if (path === '/api/v1/research' && request.method() === 'POST') {
      submissions++; keys.push(request.headers()['idempotency-key']); bodies.push(request.postDataJSON());
      if (failSubmission) { failSubmission = false; return route.abort('failed'); }
      return respond(job, 202);
    }
    if (path === '/api/v1/research') return respond(submissions ? [job] : []);
    if (path.endsWith('/cancel')) { cancellations++; job = { ...job, status: 'cancelled', last_sequence: 3 }; return respond(job); }
    if (path.endsWith('/events')) {
      await new Promise(resolve => setTimeout(resolve, 100));
      return route.fulfill({ contentType: 'text/event-stream', body: ': heartbeat\n\n' });
    }
    if (path.endsWith('/job-1')) return respond(job);
    return respond([]);
  });
  return { submissions: () => submissions, cancellations: () => cancellations, keys, bodies,
    addEvidence: () => { job.stages.researcher_a = { status: 'completed', output: { findings: [{ id: 'c1', claim: 'The experiment measured 42 samples.', status: 'uncertain', reasoning: '', evidence: [{ source_id: 's1', passage: 'The experiment measured 42 samples.' }] }] } }; job.stages.retrieval = { status: 'completed', output: { source_index: [{ id: 's1', url: 'https://example.org/study', title: 'Primary report', publisher: 'example.org' }] } }; }, failNext: () => { failSubmission = true; } };
}

test('submission survives page refresh and closing its stream is not cancellation', async ({ page }) => {
  const state = await setup(page);
  await page.goto('/');
  await page.getByLabel('Research question').fill(initial.query);
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await expect(page.getByText('Plan preserved across refresh')).toBeVisible();
  await page.reload();
  await expect(page.getByText('Plan preserved across refresh')).toBeVisible();
  expect(state.submissions()).toBe(1);
  expect(state.cancellations()).toBe(0);
  await page.screenshot({ path: 'test-results/workspace.png', fullPage: true });
});

test('explicit cancellation reaches the API and updates the workspace', async ({ page }) => {
  const state = await setup(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await page.getByRole('button', { name: 'Cancel research' }).click();
  await expect(page.getByRole('status')).toHaveText('cancelled');
  expect(state.cancellations()).toBe(1);
});

test('ambiguous submission retries reuse the idempotency key', async ({ page }) => {
  const state = await setup(page); state.failNext();
  await page.goto('/');
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByText('Plan preserved across refresh')).toBeVisible();
  expect(state.keys).toHaveLength(2);
  expect(state.keys[0]).toBe(state.keys[1]);
});

test('pending submission recovers after refresh with the same key', async ({ page }) => {
  const state = await setup(page); state.failNext();
  await page.goto('/');
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Plan preserved across refresh')).toBeVisible();
  expect(state.keys[0]).toBe(state.keys[1]);
});


test('partial evidence and belief nodes appear before final verification', async ({ page }) => {
  const state = await setup(page); state.addEvidence();
  await page.goto('/');
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await page.getByRole('tab', { name: 'evidence', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'The experiment measured 42 samples.' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Primary report' })).toHaveAttribute('href', 'https://example.org/study');
  await page.getByRole('tab', { name: 'beliefs', exact: true }).click();
  await expect(page.getByText('uncertain', { exact: true })).toBeVisible();
});

test('organization sharing requires an explicit audience choice', async ({ page }) => {
  const state = await setup(page);
  await page.goto('/');
  await expect(page.getByLabel('Remember supported findings for')).toHaveValue('user');
  await page.getByLabel('Remember supported findings for').selectOption('org:org-1');
  await page.getByRole('button', { name: 'Run MARS' }).click();
  await expect(page.getByText('Plan preserved across refresh')).toBeVisible();
  expect(state.bodies[0].memory_scope).toBe('organization');
  expect(state.bodies[0].organization_id).toBe('org-1');
});
