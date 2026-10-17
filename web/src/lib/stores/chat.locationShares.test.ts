/**
 * Location share cards on an answer: the server's `location_shares` becomes
 * `locationShares` on the row, from history, from a streamed row, and when a
 * stored row is adopted into the placeholder of a followed turn. A malformed
 * element is dropped rather than drawn.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory, LocationShare } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  chatStreamUrl: vi.fn(() => '/task-stream'),
});

vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

const room: ChatRoom = {
  id: 1,
  token: 't1',
  name: 'Hiking',
  archived: false,
  created_at: '',
  updated_at: '',
  origin: 'web',
  unread_count: 0,
};

type Row = ChatHistory['messages'][number] & { room_token: string };

function row(msgId: number, over: Partial<Row>): Row {
  return {
    role: 'assistant',
    text: '',
    created_at: '2026-10-16T14:03:00Z',
    msg_id: msgId,
    starred: false,
    room_token: 't1',
    room_name: 'Hiking',
    ...over,
  } as Row;
}

const SHARE: LocationShare = {
  card_id: 4,
  owner_user_id: 'bob',
  owner_label: 'Bob',
  precision: 'exact',
  lat: 52.52,
  lon: 13.405,
  accuracy_m: 40,
  observed_at: '2026-10-16T14:02:31+00:00',
  freshness: 'fresh',
  map_url: 'https://www.openstreetmap.org/?mlat=52.52&mlon=13.405',
};

const EXPECTED = {
  cardId: 4,
  ownerLabel: 'Bob',
  mapUrl: SHARE.map_url,
  observation: {
    precision: 'exact',
    lat: 52.52,
    lon: 13.405,
    accuracy_m: 40,
    observed_at: '2026-10-16T14:02:31+00:00',
    freshness: 'fresh',
    owner_user_id: 'bob',
  },
};

function queueRoomEvents(events: Row[], cursor: number) {
  api.getRoomEvents.mockResolvedValueOnce({ events, cursor, gap: false });
  api.getRoomEvents.mockResolvedValue({ events: [], cursor, gap: false });
}

function taskEvents(events: { kind: string; payload: Record<string, unknown> }[]) {
  const seqd = events.map((e, i) => ({ seq: i + 1, ...e }));
  api.getTaskEvents.mockImplementation(async (_id: number, since: number) => ({
    events: seqd.filter((e) => e.seq > since),
  }));
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — location share cards', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.chatStreamUrl.mockReturnValue('/task-stream');
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    (globalThis as unknown as { EventSource: unknown }).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('maps a history row, dropping elements it cannot draw', async () => {
    api.getRoomMessages.mockResolvedValue({
      messages: [
        row(11, {
          text: 'Bob is at the trailhead.',
          task_id: 77,
          status: 'completed',
          location_shares: [
            SHARE,
            { ...SHARE, card_id: 5, lat: 'north' },
            { ...SHARE, card_id: 6, precision: 'street' },
            { ...SHARE, card_id: 7, map_url: 'javascript:alert(1)' },
            'junk',
          ],
        } as unknown as Partial<Row>),
      ],
      active_task: null,
      active_tasks: [],
    });
    const s = await freshSession();
    await s.init();
    const answer = get(s.messages).find((m) => m.msgId === 11);
    expect(answer?.locationShares).toEqual([EXPECTED]);
    s.teardown();
  });

  it('leaves a row with no shares without the field', async () => {
    api.getRoomMessages.mockResolvedValue({
      messages: [row(11, { text: 'Hello.', task_id: 77, status: 'completed' })],
      active_task: null,
      active_tasks: [],
    });
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).find((m) => m.msgId === 11)?.locationShares).toBeUndefined();
    s.teardown();
  });

  it('maps a streamed row', async () => {
    const s = await freshSession();
    await s.init();
    queueRoomEvents(
      [
        row(12, {
          text: 'Bob is at the trailhead.',
          task_id: 78,
          status: 'completed',
          location_shares: [SHARE],
        }),
      ],
      12,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).find((m) => m.msgId === 12)?.locationShares).toEqual([EXPECTED]);
    s.teardown();
  });

  it('carries the cards onto a followed turn when its stored row lands', async () => {
    const s = await freshSession();
    await s.init();
    queueRoomEvents(
      [row(10, { role: 'user', text: 'Where is Bob?', task_id: 77, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    taskEvents([{ kind: 'text_delta', payload: { text: 'Bob is at the trailhead.' } }]);
    await vi.advanceTimersByTimeAsync(2000);
    queueRoomEvents(
      [
        row(11, {
          text: 'Bob is at the trailhead.',
          task_id: 77,
          status: 'completed',
          location_shares: [SHARE],
        }),
      ],
      11,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const turns = get(s.messages).filter((m) => m.role === 'assistant' && m.taskId === 77);
    expect(turns).toHaveLength(1);
    expect(turns[0].locationShares).toEqual([EXPECTED]);
    s.teardown();
  });
});
