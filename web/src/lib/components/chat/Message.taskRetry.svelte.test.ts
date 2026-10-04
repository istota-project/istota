/**
 * Retry and Continue on a failed or cancelled turn (ISSUE-631).
 *
 * The control sits on the assistant turn whose task failed, at rest rather
 * than in the hover row, and is not gated on why it failed: a task that used up
 * its automatic retries is the commonest case.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { applyEvent, type ChatMessage } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(cleanup);

const noop = () => {};

function failedTurn(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    cid: 7,
    role: 'assistant',
    text: 'Something went wrong.',
    segments: [{ kind: 'text', id: 's1', text: 'Something went wrong.', settled: true }],
    taskId: 41,
    status: 'failed',
    streaming: false,
    createdAt: '2026-10-04T12:00:00Z',
    ...over,
  };
}

type Props = {
  onRetryTask?: (cid: number, mode: 'retry' | 'resume') => void;
  retryBusy?: boolean;
  aggregate?: boolean;
};

function mount(message: ChatMessage, props: Props = {}) {
  return render(Message, { message, onConfirm: noop, onReject: noop, ...props });
}

function buttons(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.task-retry button')].map((b) =>
    (b.textContent ?? '').trim(),
  );
}

describe('a failed turn', () => {
  it('offers Retry and calls back with the row and the mode', async () => {
    const seen: [number, string][] = [];
    const { container } = mount(failedTurn(), {
      onRetryTask: (cid, mode) => seen.push([cid, mode]),
    });
    expect(buttons(container)).toEqual(['Retry']);
    await fireEvent.click(container.querySelector<HTMLButtonElement>('.task-retry button.btn')!);
    expect(seen).toEqual([[7, 'retry']]);
  });

  it('offers it on a failure loaded from history, which carries only the status', () => {
    const { container } = mount(failedTurn({ error: undefined, status: 'failed' }), {
      onRetryTask: noop,
    });
    expect(buttons(container)).toEqual(['Retry']);
  });

  it('offers it on a live failure, which the error event marks', () => {
    const live: ChatMessage = {
      cid: 7,
      role: 'assistant',
      text: '',
      segments: [],
      taskId: 41,
      status: 'running',
      streaming: true,
    };
    applyEvent(live, 'error', { message: 'Task failed after 3 attempts' });
    const { container } = mount(live, { onRetryTask: noop });
    expect(buttons(container)).toEqual(['Retry']);
  });

  it('offers it on a cancelled turn', () => {
    const live: ChatMessage = {
      cid: 7,
      role: 'assistant',
      text: '',
      segments: [],
      taskId: 41,
      status: 'running',
      streaming: true,
    };
    applyEvent(live, 'cancelled', {});
    const { container } = mount(live, { onRetryTask: noop });
    expect(buttons(container)).toEqual(['Retry']);
  });

  it('offers Continue only when the turn took steps', async () => {
    const seen: string[] = [];
    const withSteps = failedTurn({
      segments: [
        {
          kind: 'tool',
          id: 's2',
          tool: { id: 'c1', name: 'Bash', description: 'ran a thing', running: false },
        },
        { kind: 'text', id: 's3', text: 'boom', settled: true },
      ],
    });
    const { container } = mount(withSteps, { onRetryTask: (_cid, mode) => seen.push(mode) });
    expect(buttons(container)).toEqual(['Retry', 'Continue']);
    await fireEvent.click(container.querySelectorAll<HTMLButtonElement>('.task-retry button')[1]);
    expect(seen).toEqual(['resume']);
  });

  it('is disabled while the room has a turn in flight', () => {
    const { container } = mount(failedTurn(), { onRetryTask: noop, retryBusy: true });
    const btn = container.querySelector<HTMLButtonElement>('.task-retry button')!;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe('Wait for the current turn to finish');
  });

  it('says where the task went once it has been retried', () => {
    const { container } = mount(failedTurn({ retriedAs: 52 }), { onRetryTask: noop });
    expect(buttons(container)).toEqual([]);
    expect(container.querySelector('.task-retry')?.textContent).toContain('Retried as #52');
  });
});

describe('no Retry', () => {
  it('on a completed turn', () => {
    const { container } = mount(failedTurn({ status: 'completed' }), { onRetryTask: noop });
    expect(container.querySelector('.task-retry')).toBeNull();
  });

  it('on a turn still streaming', () => {
    const { container } = mount(failedTurn({ status: 'running', streaming: true }), {
      onRetryTask: noop,
    });
    expect(container.querySelector('.task-retry')).toBeNull();
  });

  it('on a turn with no task', () => {
    const { container } = mount(failedTurn({ taskId: undefined }), { onRetryTask: noop });
    expect(container.querySelector('.task-retry')).toBeNull();
  });

  it('in an aggregate view', () => {
    const { container } = mount(failedTurn(), { onRetryTask: noop, aggregate: true });
    expect(container.querySelector('.task-retry')).toBeNull();
  });

  it('where the surface passes no handler', () => {
    const { container } = mount(failedTurn());
    expect(container.querySelector('.task-retry')).toBeNull();
  });
});
