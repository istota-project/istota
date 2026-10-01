/**
 * Phone rooms in the store (room-surface-model Stage 24).
 *
 * The phone-room backfill writes a room's earlier history with new ids and old
 * stamps. A room open during that pass receives those rows over the room
 * stream after newer messages are already on screen, so the store places a
 * streamed row by its server stamp rather than at the bottom. A texted turn's
 * surface rides onto the row.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

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

function room(id: number): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name: 'SMS',
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'sms',
    phone_surface: 'sms',
    read_only: true,
    unread_count: 0,
  };
}

type Row = ChatHistory['messages'][number] & { room_token: string };

function row(msgId: number, over: Partial<Row> = {}): Row {
  return {
    role: 'assistant',
    text: `msg ${msgId}`,
    created_at: '2026-09-20T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: 't1',
    room_name: 'SMS',
    ...over,
  } as Row;
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

function installFakeEventSource(): {
  current: FakeEventSource | null;
  instances: FakeEventSource[];
  opened: number;
} {
  const ref: {
    current: FakeEventSource | null;
    instances: FakeEventSource[];
    opened: number;
  } = {
    current: null,
    instances: [],
    opened: 0,
  };
  class FakeEventSource {
    url: string;
    listeners = new Map<string, ((e: any) => void)[]>();
    onerror: (() => void) | null = null;
    onopen: (() => void) | null = null;
    closed = false;
    // 0 = CONNECTING (the browser is retrying on its own), 1 = OPEN,
    // 2 = CLOSED. Left undefined by default so the pre-existing tests keep
    // exercising the "fatal error → poll" branch.
    readyState: number | undefined = undefined;
    constructor(url: string) {
      this.url = url;
      ref.current = this as unknown as FakeEventSource;
      ref.instances.push(this as unknown as FakeEventSource);
      ref.opened += 1;
    }
    addEventListener(kind: string, fn: (e: any) => void) {
      const cur = this.listeners.get(kind) ?? [];
      cur.push(fn);
      this.listeners.set(kind, cur);
    }
    close() {
      this.closed = true;
    }
    emit(kind: string, payload: unknown, lastEventId = '') {
      for (const fn of this.listeners.get(kind) ?? []) {
        fn({ data: JSON.stringify(payload), lastEventId });
      }
    }
    fail() {
      this.onerror?.();
    }
  }
  (globalThis as any).EventSource = FakeEventSource;
  return ref;
}
type FakeEventSource = {
  url: string;
  emit: (kind: string, payload: unknown, lastEventId?: string) => void;
  fail: () => void;
  onerror: (() => void) | null;
  onopen: (() => void) | null;
  closed: boolean;
  readyState: number | undefined;
};

describe('chat store — phone rooms', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.chatStreamUrl.mockReturnValue('/task-stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getRoomMessages.mockResolvedValue({
      messages: [
        row(5, {
          role: 'user',
          text: 'first text',
          task_id: 7,
          created_at: '2026-09-20T10:00:00Z',
          via: 'sms',
        }),
        row(6, { text: 'reply', task_id: 7, created_at: '2026-09-20T10:00:05Z' }),
      ],
      active_task: null,
      active_tasks: [],
      has_more: false,
      oldest_cursor: null,
    });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('places a backfilled row above the newer history it came before', async () => {
    const es = installFakeEventSource();
    const s = await freshSession();
    await s.init();
    es.current!.emit(
      'message',
      row(20, {
        role: 'user',
        text: 'old question',
        task_id: 2,
        created_at: '2026-09-01T09:00:00Z',
      }),
      '20',
    );
    es.current!.emit(
      'message',
      row(21, { text: 'old answer', task_id: 2, created_at: '2026-09-01T09:00:03Z' }),
      '21',
    );
    expect(get(s.messages).map((m) => m.text)).toEqual([
      'old question',
      'old answer',
      'first text',
      'reply',
    ]);
    s.teardown();
  });

  it('still appends a new row at the bottom', async () => {
    const es = installFakeEventSource();
    const s = await freshSession();
    await s.init();
    es.current!.emit(
      'message',
      row(22, { text: 'later', created_at: '2026-09-20T11:00:00Z' }),
      '22',
    );
    expect(get(s.messages).map((m) => m.text)).toEqual(['first text', 'reply', 'later']);
    s.teardown();
  });

  it('carries a group flag through the room stream (ISSUE-585)', async () => {
    // The group wording and a group's confirmation buttons read `phone_group`;
    // a key the frame merge omits reads as absent, so it has to follow frames.
    const es = installFakeEventSource();
    const group = { ...room(1), origin: 'whatsapp', phone_surface: 'whatsapp' as const };
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...group, phone_group: true }] });
    const s = await freshSession();
    await s.init();
    expect(get(s.rooms)[0].phone_group).toBe(true);
    const frame = (phone_group: boolean) => ({
      action: 'upsert',
      room: { ...group, name: 'Family', phone_group },
    });
    es.current!.emit('room', frame(true));
    expect(get(s.rooms)[0].phone_group).toBe(true);
    expect(get(s.rooms)[0].name).toBe('Family');
    es.current!.emit('room', frame(false));
    expect(get(s.rooms)[0].phone_group).toBe(false);
    s.teardown();
  });

  it('carries the texted surface and the read-only flag', async () => {
    installFakeEventSource();
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).find((m) => m.role === 'user')!.via).toBe('sms');
    expect(get(s.rooms)[0].read_only).toBe(true);
    s.teardown();
  });
});
