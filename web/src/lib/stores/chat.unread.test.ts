/**
 * Web chat unread-room indicators — store behaviour (spec stage 3).
 *
 * Covers: the rooms-list payload's `unread_count` lands in the store, the
 * active room is held at 0, selecting a room optimistically clears it and
 * persists via markRoomRead, and the periodic rooms refresh merges fresh
 * counts for non-active rooms while keeping the active room clear.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';

const api = vi.hoisted(() => ({
	getChatConfig: vi.fn(),
	getChatRooms: vi.fn(),
	getRoomMessages: vi.fn(),
	markRoomRead: vi.fn(),
	getTaskEvents: vi.fn(),
	sendChatMessage: vi.fn(),
	createChatRoom: vi.fn(),
	updateChatRoom: vi.fn(),
	deleteChatRoom: vi.fn(),
	promoteChatRoom: vi.fn(),
	cancelChatTask: vi.fn(),
	confirmChatTask: vi.fn(),
	chatStreamUrl: vi.fn(),
	ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/persisted', () => ({
	loadSetting: vi.fn(() => null),
	saveSetting: vi.fn(),
}));

function room(id: number, unread = 0, name = `Room ${id}`): ChatRoom {
	return {
		id, token: `t${id}`, name, archived: false,
		created_at: '', updated_at: '', origin: 'web', unread_count: unread,
	};
}

async function freshSession() {
	vi.resetModules();
	const mod = await import('./chat');
	return mod.getChatSession();
}

describe('chat store — unread indicators', () => {
	beforeEach(() => {
		Object.values(api).forEach((v) => { if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset(); });
		api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
		api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
		api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
		Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
	});

	afterEach(() => { vi.useRealTimers(); });

	it('keeps non-active room counts and zeroes the active room on init', async () => {
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 4), room(2, 3)] });
		const s = await freshSession();
		await s.init();
		const list = get(s.rooms);
		const byId = Object.fromEntries(list.map((r) => [r.id, r]));
		// room 1 auto-selected → forced to 0; room 2 keeps its 3
		expect(get(s.activeRoomId)).toBe(1);
		expect(byId[1].unread_count).toBe(0);
		expect(byId[2].unread_count).toBe(3);
		// the open room was persisted read
		expect(api.markRoomRead).toHaveBeenCalledWith(1);
	});

	it('selectRoom optimistically clears the chip and persists', async () => {
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0), room(2, 3)] });
		const s = await freshSession();
		await s.init();
		api.markRoomRead.mockClear();
		await s.selectRoom(2);
		expect(get(s.activeRoomId)).toBe(2);
		expect(get(s.rooms).find((r) => r.id === 2)?.unread_count).toBe(0);
		expect(api.markRoomRead).toHaveBeenCalledWith(2);
	});

	it('refresh merges fresh counts for non-active rooms, holds active at 0', async () => {
		vi.useFakeTimers();
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0), room(2, 0)] });
		const s = await freshSession();
		await s.init(); // selects room 1, starts the refresh timer
		// next poll: room 2 gained 5 unread; room 1 (active) reports a stale 9
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 9), room(2, 5)] });
		await vi.advanceTimersByTimeAsync(5000);
		const byId = Object.fromEntries(get(s.rooms).map((r) => [r.id, r]));
		expect(byId[2].unread_count).toBe(5);
		expect(byId[1].unread_count).toBe(0); // active room forced clear
		s.teardown();
	});

	it('refresh appends a newly-surfaced room', async () => {
		vi.useFakeTimers();
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0)] });
		const s = await freshSession();
		await s.init();
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0), room(2, 2)] });
		await vi.advanceTimersByTimeAsync(5000);
		expect(get(s.rooms).map((r) => r.id)).toContain(2);
		expect(get(s.rooms).find((r) => r.id === 2)?.unread_count).toBe(2);
		s.teardown();
	});
});
