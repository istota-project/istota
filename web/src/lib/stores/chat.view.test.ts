/**
 * Web chat cross-room views (All / Unread / Starred) + per-message starring —
 * store behaviour.
 *
 * Covers: selectView clears the active room and loads via the aggregate
 * endpoint, selectRoom resets view to room mode, toggleStar's optimistic
 * flip / revert-on-failure / removal in the Starred view, view-aware
 * loadOlder paging, notif-poll suppression while a view is active, and
 * markAllRead zeroing every room badge.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({
	getChatConfig: vi.fn(),
	getChatRooms: vi.fn(),
	getRoomMessages: vi.fn(),
	getChatMessagesView: vi.fn(),
	setChatMessageStarred: vi.fn(),
	markAllRoomsRead: vi.fn(),
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

function aggMsg(
	msgId: number, text: string,
	opts: Partial<ChatHistory['messages'][number]> = {},
): ChatHistory['messages'][number] {
	return {
		role: 'assistant', text, msg_id: msgId, starred: false,
		task_id: msgId, status: 'completed',
		room_token: 't1', room_name: 'Room 1',
		created_at: '2026-07-10T12:00:00Z',
		segments: [{ kind: 'text', text }],
		...opts,
	};
}

const emptyHistory = { messages: [], active_task: null, active_tasks: [] };

async function freshSession() {
	vi.resetModules();
	const mod = await import('./chat');
	return mod.getChatSession();
}

describe('chat store — cross-room views + starring', () => {
	beforeEach(() => {
		Object.values(api).forEach((v) => { if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset(); });
		api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
		api.getChatRooms.mockResolvedValue({ rooms: [room(1, 2), room(2, 3)] });
		api.getRoomMessages.mockResolvedValue(emptyHistory);
		api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
		api.setChatMessageStarred.mockResolvedValue({ ok: true, starred: true });
		api.markAllRoomsRead.mockResolvedValue({ ok: true, updated: 2 });
		Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
	});
	afterEach(() => { vi.useRealTimers(); });

	it('selectView clears the active room and loads the aggregate page', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'hello from room 1')],
			has_more: true, oldest_cursor: { ts: '2026-07-10 12:00:00', id: 10 },
		});
		const s = await freshSession();
		await s.init();
		expect(get(s.activeRoomId)).toBe(1);
		await s.selectView('all');
		expect(get(s.view)).toBe('all');
		expect(get(s.activeRoomId)).toBeNull();
		expect(api.getChatMessagesView).toHaveBeenCalledWith('all');
		const msgs = get(s.messages);
		expect(msgs).toHaveLength(1);
		expect(msgs[0].msgId).toBe(10);
		expect(msgs[0].roomToken).toBe('t1');
		expect(msgs[0].roomName).toBe('Room 1');
		expect(get(s.hasMore)).toBe(true);
	});

	it('selectRoom resets view to room mode', async () => {
		api.getChatMessagesView.mockResolvedValue({ messages: [], has_more: false, oldest_cursor: null });
		const s = await freshSession();
		await s.init();
		await s.selectView('starred');
		expect(get(s.view)).toBe('starred');
		await s.selectRoom(2);
		expect(get(s.view)).toBe('room');
		expect(get(s.activeRoomId)).toBe(2);
	});

	it('toggleStar flips optimistically and calls the API', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'starrable')], has_more: false, oldest_cursor: null,
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('all');
		const cid = get(s.messages)[0].cid;
		let resolveApi: (v: unknown) => void;
		api.setChatMessageStarred.mockReturnValue(new Promise((r) => { resolveApi = r; }));
		const p = s.toggleStar(cid);
		// Optimistic: flipped before the API resolves.
		expect(get(s.messages)[0].starred).toBe(true);
		resolveApi!({ ok: true, starred: true });
		await p;
		expect(api.setChatMessageStarred).toHaveBeenCalledWith(10, true);
		expect(get(s.messages)[0].starred).toBe(true);
	});

	it('toggleStar reverts on API failure', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'starrable')], has_more: false, oldest_cursor: null,
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('all');
		const cid = get(s.messages)[0].cid;
		api.setChatMessageStarred.mockRejectedValue(new Error('boom'));
		await s.toggleStar(cid);
		expect(get(s.messages)[0].starred).toBe(false);
		expect(get(s.error)).toBeTruthy();
	});

	it('toggleStar is a no-op for a message without msgId', async () => {
		api.getRoomMessages.mockResolvedValue({
			...emptyHistory,
			messages: [{ role: 'assistant', text: 'aux only', task_id: 9, status: 'failed', created_at: '2026-07-10T12:00:00Z' }],
		});
		const s = await freshSession();
		await s.init();
		const cid = get(s.messages)[0].cid;
		await s.toggleStar(cid);
		expect(api.setChatMessageStarred).not.toHaveBeenCalled();
	});

	it('unstar in the Starred view removes the message after the call resolves', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'starred msg', { starred: true })],
			has_more: false, oldest_cursor: null,
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('starred');
		const cid = get(s.messages)[0].cid;
		api.setChatMessageStarred.mockResolvedValue({ ok: true, starred: false });
		await s.toggleStar(cid);
		expect(api.setChatMessageStarred).toHaveBeenCalledWith(10, false);
		expect(get(s.messages)).toHaveLength(0);
	});

	it('unstar failure in the Starred view keeps the message (reverted in place)', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'starred msg', { starred: true })],
			has_more: false, oldest_cursor: null,
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('starred');
		const cid = get(s.messages)[0].cid;
		api.setChatMessageStarred.mockRejectedValue(new Error('boom'));
		await s.toggleStar(cid);
		expect(get(s.messages)).toHaveLength(1);
		expect(get(s.messages)[0].starred).toBe(true);
	});

	it('loadOlder pages the aggregate endpoint with the view cursor', async () => {
		api.getChatMessagesView.mockResolvedValueOnce({
			messages: [aggMsg(10, 'newest')],
			has_more: true, oldest_cursor: { ts: '2026-07-10 12:00:00', id: 10 },
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('unread');
		api.getChatMessagesView.mockResolvedValueOnce({
			messages: [aggMsg(9, 'older')],
			has_more: false, oldest_cursor: { ts: '2026-07-10 11:00:00', id: 9 },
		});
		await s.loadOlder();
		expect(api.getChatMessagesView).toHaveBeenLastCalledWith('unread', {
			before: { ts: '2026-07-10 12:00:00', id: 10 },
		});
		expect(get(s.messages).map((m) => m.msgId)).toEqual([9, 10]);
		expect(get(s.hasMore)).toBe(false);
		// The room paging path was never touched.
		expect(api.getRoomMessages).toHaveBeenCalledTimes(1); // init only
	});

	it('suppresses the notif poll while a view is active', async () => {
		vi.useFakeTimers();
		api.getChatMessagesView.mockResolvedValue({ messages: [], has_more: false, oldest_cursor: null });
		const s = await freshSession();
		await s.init();
		await s.selectView('all');
		api.getRoomMessages.mockClear();
		await vi.advanceTimersByTimeAsync(11_000);
		expect(api.getRoomMessages).not.toHaveBeenCalled();
		s.teardown();
	});

	it('markAllRead calls the API and zeroes every room badge', async () => {
		const s = await freshSession();
		await s.init();
		expect(get(s.rooms).find((r) => r.id === 2)?.unread_count).toBe(3);
		await s.markAllRead();
		expect(api.markAllRoomsRead).toHaveBeenCalled();
		expect(get(s.rooms).every((r) => (r.unread_count ?? 0) === 0)).toBe(true);
	});

	it('markAllRead reloads the Unread view when it is active', async () => {
		api.getChatMessagesView.mockResolvedValue({
			messages: [aggMsg(10, 'unread thing')],
			has_more: false, oldest_cursor: null,
		});
		const s = await freshSession();
		await s.init();
		await s.selectView('unread');
		expect(get(s.messages)).toHaveLength(1);
		api.getChatMessagesView.mockResolvedValue({ messages: [], has_more: false, oldest_cursor: null });
		await s.markAllRead();
		expect(get(s.messages)).toHaveLength(0);
	});
});
