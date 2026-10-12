import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configRequest, configView } from '$lib/test/adminConfig';
import type { ConfigRequest } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import RequestProgress from './RequestProgress.svelte';
import { NOT_PICKED_UP_MS, POLL_MS, RESTART_DEADLINE_MS } from './progress';

const FINISHED = '2026-10-12T08:06:00Z';

function mount() {
  const props = { requestId: 17, onApplied: vi.fn(), onDismiss: vi.fn() };
  render(RequestProgress, { props });
  return props;
}

/** Answer status polls with these rows in turn, the last one for good. */
function statuses(...rows: Partial<ConfigRequest>[]) {
  for (const row of rows.slice(0, -1)) {
    api.getConfigRequest.mockResolvedValueOnce(configRequest(row));
  }
  api.getConfigRequest.mockResolvedValue(configRequest(rows[rows.length - 1]));
}

async function advance(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  vi.setSystemTime(new Date('2026-10-12T08:05:00Z'));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('following a request', () => {
  it('walks pending, taken and applied, then waits for the restart', async () => {
    statuses(
      { status: 'pending' },
      { status: 'taken' },
      { status: 'applied', finished_at: FINISHED },
    );
    // The first answer is the process from before the restart; then the web
    // is down; then a process that started after the change answers.
    api.getAdminConfig
      .mockResolvedValueOnce(configView([], { started_at: '2026-10-12T08:00:00Z' }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(configView([], { started_at: '2026-10-12T08:06:30Z' }));
    const props = mount();

    await advance(0);
    expect(screen.getByText(/Waiting for the host/)).toBeInTheDocument();
    await advance(POLL_MS);
    expect(screen.getByText(/Applying…/)).toBeInTheDocument();
    await advance(POLL_MS);
    expect(screen.getByText(/Waiting for Istota to restart/)).toBeInTheDocument();
    expect(props.onApplied).not.toHaveBeenCalled();

    await advance(POLL_MS);
    expect(props.onApplied).not.toHaveBeenCalled();
    await advance(10_000);
    expect(props.onApplied).toHaveBeenCalledTimes(1);
    expect(props.onApplied.mock.calls[0][0].started_at).toBe('2026-10-12T08:06:30Z');
    expect(screen.getByText(/Applied. Istota restarted/)).toBeInTheDocument();
  });

  it('never reports the pre-restart state as the result', async () => {
    statuses({ status: 'applied', finished_at: FINISHED });
    api.getAdminConfig.mockResolvedValue(configView([], { started_at: '2026-10-12T08:00:00Z' }));
    const props = mount();
    await advance(RESTART_DEADLINE_MS - 1_000);
    expect(props.onApplied).not.toHaveBeenCalled();
    expect(screen.getByText(/Waiting for Istota to restart/)).toBeInTheDocument();
  });

  it('names the host recovery when the web does not come back', async () => {
    statuses({ status: 'applied', finished_at: FINISHED });
    api.getAdminConfig.mockRejectedValue(new TypeError('Failed to fetch'));
    mount();
    await advance(RESTART_DEADLINE_MS + 20_000);
    expect(screen.getByText(/has not come back three minutes after/)).toBeInTheDocument();
    expect(screen.getByText(/config\.toml\.bak/)).toBeInTheDocument();
  });

  it('keeps polling through a failed status read', async () => {
    api.getConfigRequest
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue(configRequest({ status: 'refused', output_text: 'apply: refused' }));
    mount();
    await advance(0);
    expect(screen.getByText(/still trying/)).toBeInTheDocument();
    await advance(2_000);
    expect(screen.getByText(/The host refused the change/)).toBeInTheDocument();
    expect(screen.getByText('apply: refused')).toBeInTheDocument();
  });
});

describe('a request the host does not take', () => {
  it('says so after two minutes and can be cancelled', async () => {
    statuses({ status: 'pending' });
    api.cancelConfigRequest.mockResolvedValue({ id: 17, status: 'cancelled' });
    mount();
    await advance(NOT_PICKED_UP_MS - POLL_MS);
    expect(screen.queryByText(/has not picked this up/)).toBeNull();
    await advance(POLL_MS);
    expect(screen.getByText(/has not picked this up after two minutes/)).toBeInTheDocument();

    api.getConfigRequest.mockResolvedValue(configRequest({ status: 'cancelled' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel change' }));
    expect(api.cancelConfigRequest).toHaveBeenCalledWith(17);
    await advance(POLL_MS);
    expect(screen.getByText(/Cancelled. Nothing was changed/)).toBeInTheDocument();
  });
});
