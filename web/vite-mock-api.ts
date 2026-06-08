import type { Plugin } from 'vite';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

interface MockReq {
	url: string;
	method: string;
	body: any;
}
type MockHandler = (req: MockReq) => unknown | undefined;

const __mockDir = dirname(fileURLToPath(import.meta.url));
const IMMUNIZATION_REFS: Array<{
	name: string;
	display_name: string;
	category: string;
	schedule: string;
	interval_days: number | null;
	primary_series_doses: number | null;
	aliases: string[];
	description: string | null;
	typical_age_range: string | null;
}> = (() => {
	try {
		const path = resolve(__mockDir, '../src/istota/health/data/immunization_refs.json');
		const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
		return raw.map((r) => ({
			name: r.name,
			display_name: r.display_name,
			category: r.category,
			schedule: r.schedule,
			interval_days: r.interval_days ?? null,
			primary_series_doses: r.primary_series_doses ?? null,
			aliases: r.aliases ?? [],
			description: r.description ?? null,
			typical_age_range: r.typical_age_range ?? null,
		}));
	} catch {
		return [];
	}
})();
const IMMUNIZATION_EXPLAINERS: Record<string, { summary: string; why_it_matters: string[] }> = (() => {
	try {
		const path = resolve(__mockDir, '../src/istota/health/data/immunization_explainers.json');
		const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
		const out: Record<string, { summary: string; why_it_matters: string[] }> = {};
		for (const e of raw) {
			if (!e || typeof e.name !== 'string') continue;
			out[e.name] = {
				summary: typeof e.summary === 'string' ? e.summary : '',
				why_it_matters: Array.isArray(e.why_it_matters)
					? e.why_it_matters.filter((w: unknown): w is string => typeof w === 'string' && w.trim().length > 0)
					: [],
			};
		}
		return out;
	} catch {
		return {};
	}
})();
const BIOMARKER_REFS: Array<{
	name: string;
	display_name: string;
	category: string;
	default_unit: string;
	ref_range_low: number | null;
	ref_range_high: number | null;
	ref_range_low_m: number | null;
	ref_range_high_m: number | null;
	ref_range_low_f: number | null;
	ref_range_high_f: number | null;
	aliases: string[];
	description: string | null;
}> = (() => {
	try {
		const path = resolve(__mockDir, '../src/istota/health/data/biomarker_refs.json');
		const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
		return raw.map((r) => ({
			name: r.name,
			display_name: r.display_name,
			category: r.category,
			default_unit: r.default_unit,
			ref_range_low: r.ref_range_low ?? null,
			ref_range_high: r.ref_range_high ?? null,
			ref_range_low_m: r.ref_range_low_m ?? null,
			ref_range_high_m: r.ref_range_high_m ?? null,
			ref_range_low_f: r.ref_range_low_f ?? null,
			ref_range_high_f: r.ref_range_high_f ?? null,
			aliases: r.aliases ?? [],
			description: r.description ?? null,
		}));
	} catch {
		return [];
	}
})();

const user = {
	username: 'stefan',
	display_name: 'Stefan',
	bot_name: 'Istota',
	is_admin: true,
	features: {
		chat: true,
		feeds: true,
		location: true,
		money: true,
		health: true,
		google_workspace: false,
		google_workspace_enabled: false,
		admin: true,
	},
};

// ---- Web chat mock state ----
interface MockChatRoom { id: number; token: string; name: string; archived: boolean; created_at: string; updated_at: string; }
interface MockChatTask { id: number; roomToken: string; prompt: string; createdAt: number; }
const mockChatRooms: MockChatRoom[] = [
	{ id: 1, token: 'web-stefan-general', name: 'general', archived: false, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
];
const mockChatTasks = new Map<number, MockChatTask>();
let mockChatRoomSeq = 1;
let mockChatTaskSeq = 1000;

// A canned event timeline for a mock task (ms offsets from creation). Models the
// target UX: the model's work (inter-tool narration + tool calls) collapses into
// the single ActivityTrace chip (its "current step" updates live), and the FINAL
// ANSWER streams token-by-token, prominent, after the last tool. Tweak the
// timings / chunking here to eyeball the streaming behaviour in the dev frontend
// (VITE_MOCK_API=1 npm run dev → /chat) without a live backend.
function mockTaskEvents(task: MockChatTask) {
	// A multi-paragraph markdown answer, chunked into small deltas so the live
	// prominent streaming (and incremental markdown) is visible.
	const reply =
		`Here are today's headlines for **${task.prompt.slice(0, 48)}**:\n\n` +
		'## Top stories\n\n' +
		'1. **Markets** rallied as inflation cooled for a third month.\n' +
		'2. **Tech** — a new on-device model shipped with `tool use` baked in.\n' +
		'3. **Sports** — the tournament bracket is set for the weekend.\n\n' +
		'> Streaming, tools, and `markdown` all render here in real time.\n\n' +
		'Ask a follow-up, or try `!help` for commands.';
	const answerChunks = reply.match(/.{1,14}/gs) ?? [reply];

	const events: { seq: number; kind: string; payload: Record<string, unknown>; at: number }[] = [
		{ seq: 1, kind: 'task_started', payload: { text: 'On it...' }, at: 0 },
		// Reasoning is still emitted by the brain (and exercised here), but the
		// web UI no longer renders it — the chip is tool-actions-only. These rows
		// verify the client correctly ignores `thinking`; they should NOT appear.
		{ seq: 2, kind: 'thinking', payload: { text: 'The user is asking for today\'s headlines. ' }, at: 250 },
		{ seq: 3, kind: 'thinking', payload: { text: 'I should search the web for recent news first.' }, at: 450 },
		{ seq: 4, kind: 'tool_start', payload: { tool_name: 'WebSearch', description: '🔎 web_search "today\'s news"', tool_call_id: 'c1' }, at: 800 },
		{ seq: 5, kind: 'tool_progress', payload: { tool_call_id: 'c1', text: '7 results' }, at: 1600 },
		{ seq: 6, kind: 'tool_end', payload: { tool_name: 'WebSearch', tool_call_id: 'c1', success: true, duration_ms: 1800 }, at: 2600 },
		// Reasoning between tools — also ignored by the UI.
		{ seq: 7, kind: 'thinking', payload: { text: 'Good results. Let me fetch the top source for detail.' }, at: 2900 },
		{ seq: 8, kind: 'tool_start', payload: { tool_name: 'WebFetch', description: '🌐 browse get justsecurity.org', tool_call_id: 'c2' }, at: 3200 },
		{ seq: 9, kind: 'tool_end', payload: { tool_name: 'WebFetch', tool_call_id: 'c2', success: true, duration_ms: 1900 }, at: 5100 },
		// A final beat of reasoning before the answer streams.
		{ seq: 10, kind: 'thinking', payload: { text: 'I have enough to summarize the top stories now.' }, at: 5250 },
	];

	// The final answer streams in, chunked, after the last tool — prominent and
	// live — then the canonical result reconciles it.
	let seq = 11;
	const answerStart = 5500;
	const perChunk = 70; // ms between chunks → visibly streaming markdown
	answerChunks.forEach((chunk, i) => {
		events.push({ seq: seq++, kind: 'text_delta', payload: { text: chunk }, at: answerStart + i * perChunk });
	});
	const answerEnd = answerStart + answerChunks.length * perChunk;
	events.push({ seq: seq++, kind: 'result', payload: { text: reply, truncated: false }, at: answerEnd + 100 });
	events.push({ seq: seq++, kind: 'done', payload: { stop_reason: 'completed', duration_seconds: (answerEnd + 200) / 1000 }, at: answerEnd + 200 });
	return events;
}
// Safely past the timeline's terminal `done` (answer streams ~5.5s→~7.5s); the
// history endpoint uses this to decide whether a task has finished streaming.
const MOCK_TASK_DONE_MS = 8000;

// Mock !command output so the command rendering (lists, code, tables) can be
// previewed without a live backend. Returns the inline markdown for a command,
// or null when the input is a `!model <alias> <prompt>` prefix that should
// create a real task instead (mirrors the server: unknown alias → usage).
const MOCK_MODEL_ALIASES = ['default', 'fast', 'general', 'smart', 'opus', 'opus-high', 'sonnet', 'haiku'];
const MOCK_HELP = [
	'**Available commands:**',
	'',
	'- `!check` -- Run Claude Code health check',
	'- `!cron` -- List/enable/disable scheduled jobs',
	'- `!export` -- Export conversation history to a file: `!export [markdown|text]`',
	'- `!help` -- List available commands',
	'- `!memory` -- Show memory: `!memory user`, `!memory channel`, `!memory facts`',
	'- `!models` -- List available model aliases (and what they resolve to)',
	'- `!more` -- Show execution trace for a task: `!more #123`',
	'- `!search` -- Search conversation history: `!search <query>`',
	'- `!skills` -- List available skills and their triggers',
	'- `!status` -- Show your running/pending tasks and system status',
	'- `!stop` -- Cancel your currently running task',
	'',
	'**Per-task model override:**',
	'',
	'- `!model <alias> <prompt>` — one-shot. Aliases: ' + MOCK_MODEL_ALIASES.map((a) => `\`${a}\``).join(', ') + '.',
].join('\n');
const MOCK_MODELS = [
	'**Model aliases**',
	'',
	'Use `!model <alias> <prompt>` to override the model for a single task.',
	'',
	'- `default` → (no override — use default)',
	'- `fast` → `claude-haiku-4-5`',
	'- `general` → `claude-sonnet-4-6`',
	'- `smart` → `claude-opus-4-8`',
	'- `opus` → `claude-opus-4-8`',
	'- `opus-high` → `claude-opus-4-8` + effort `high`',
].join('\n');

function mockCommandResult(text: string): string | null {
	const lower = text.toLowerCase();
	const name = lower.slice(1).split(/\s/)[0];
	if (name === 'model') {
		const alias = lower.split(/\s+/)[1] || '';
		if (alias && MOCK_MODEL_ALIASES.includes(alias) && text.split(/\s+/).length > 2) {
			return null; // valid prefix with a prompt → real task
		}
		return 'Usage: `!model <alias> <prompt>`. Aliases: ' +
			MOCK_MODEL_ALIASES.map((a) => `\`${a}\``).join(', ') + '.';
	}
	if (name === 'help') return MOCK_HELP;
	if (name === 'models') return MOCK_MODELS;
	if (name === 'status') return 'No active or pending tasks.\n\n**System:** 0 running, 0 queued';
	return `Mock command result for \`${text}\`.`;
}

const chatHandler: MockHandler = ({ url, method, body }) => {
	if (!url.startsWith('/istota/api/chat/')) return undefined;
	const path = url.split('?')[0];

	if (path === '/istota/api/chat/config') {
		return { max_prompt_chars: 32000, max_attachment_mb: 25, attachment_extensions: ['pdf', 'png', 'jpg'], client_poll_interval_ms: 600 };
	}

	if (path === '/istota/api/chat/rooms' && method === 'GET') {
		return { rooms: mockChatRooms.filter((r) => !r.archived) };
	}
	if (path === '/istota/api/chat/rooms' && method === 'POST') {
		const room: MockChatRoom = {
			id: ++mockChatRoomSeq, token: `web-stefan-${mockChatRoomSeq}`,
			name: (body?.name || 'room').slice(0, 80), archived: false,
			created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
		};
		mockChatRooms.push(room);
		return room;
	}
	const roomPatch = path.match(/^\/istota\/api\/chat\/rooms\/(\d+)$/);
	if (roomPatch && method === 'PATCH') {
		const room = mockChatRooms.find((r) => r.id === Number(roomPatch[1]));
		if (!room) return { error: 'room not found' };
		if (body?.name != null) room.name = String(body.name).slice(0, 80);
		if (body?.archived != null) room.archived = !!body.archived;
		return room;
	}
	if (roomPatch && method === 'DELETE') {
		const idx = mockChatRooms.findIndex((r) => r.id === Number(roomPatch[1]));
		if (idx < 0) return { error: 'room not found' };
		mockChatRooms.splice(idx, 1);
		return { status: 'ok' };
	}

	const msgMatch = path.match(/^\/istota\/api\/chat\/rooms\/(\d+)\/messages$/);
	if (msgMatch && method === 'GET') {
		const room = mockChatRooms.find((r) => r.id === Number(msgMatch[1]));
		if (!room) return { error: 'room not found' };
		const now = Date.now();
		const tasks = [...mockChatTasks.values()].filter((t) => t.roomToken === room.token).sort((a, b) => a.id - b.id);
		const messages: any[] = [];
		let active: any = null;
		for (const t of tasks) {
			messages.push({ role: 'user', text: t.prompt, task_id: t.id, created_at: new Date(t.createdAt).toISOString() });
			if (now - t.createdAt >= MOCK_TASK_DONE_MS) {
				const evs = mockTaskEvents(t);
				const ev = evs.find((e) => e.kind === 'result');
				const result = (ev?.payload as any).text as string;
				// Mirror the backend: a finished turn carries its tool trace +
				// duration so the action strip + timing persist on reload (ISSUE-122).
				const tools = evs
					.filter((e) => e.kind === 'tool_start')
					.map((e) => (e.payload as any).description as string);
				// Ordered segments rebuilt from the event timeline (mirrors the
				// backend `_trace_segments`): consecutive text_deltas collapse into
				// one text segment, tool_starts become tool segments, and the
				// canonical result reconciles the trailing answer.
				const segments: { kind: string; text: string }[] = [];
				for (const e of evs) {
					if (e.kind === 'text_delta') {
						const last = segments[segments.length - 1];
						if (last && last.kind === 'text') last.text += (e.payload as any).text;
						else segments.push({ kind: 'text', text: (e.payload as any).text });
					} else if (e.kind === 'thinking') {
						const last = segments[segments.length - 1];
						if (last && last.kind === 'thinking') last.text += (e.payload as any).text;
						else segments.push({ kind: 'thinking', text: (e.payload as any).text });
					} else if (e.kind === 'tool_start') {
						segments.push({ kind: 'tool', text: (e.payload as any).description });
					}
				}
				if (result) {
					const last = segments[segments.length - 1];
					if (last && last.kind === 'text') last.text = result;
					else segments.push({ kind: 'text', text: result });
				}
				const done = evs.find((e) => e.kind === 'done');
				messages.push({
					role: 'assistant', text: result, task_id: t.id,
					status: 'completed', created_at: new Date(t.createdAt).toISOString(),
					tools, segments, duration_seconds: (done?.payload as any)?.duration_seconds ?? null,
				});
			} else {
				active = { id: t.id, status: 'running' };
			}
		}
		return { messages, active_task: active };
	}
	if (msgMatch && method === 'POST') {
		const room = mockChatRooms.find((r) => r.id === Number(msgMatch[1]));
		if (!room) return { error: 'room not found' };
		const text = String(body?.text || '').trim();
		if (!text) return { error: 'text required' };
		if (text.startsWith('!')) {
			const inline = mockCommandResult(text);
			if (inline !== null) return { task_id: null, inline_result: inline };
			// !model <alias> <prompt> falls through to a real task (override carried).
		}
		const id = ++mockChatTaskSeq;
		mockChatTasks.set(id, { id, roomToken: room.token, prompt: text, createdAt: Date.now() });
		return { task_id: id, status: 'pending', stream_url: `/istota/api/chat/tasks/${id}/stream`, snapshot_url: `/istota/api/chat/tasks/${id}/events` };
	}

	const evMatch = path.match(/^\/istota\/api\/chat\/tasks\/(\d+)\/events$/);
	if (evMatch && method === 'GET') {
		const id = Number(evMatch[1]);
		const sinceSeq = Number(new URL(`http://x${url}`).searchParams.get('since_seq') || '0');
		const task = mockChatTasks.get(id);
		if (!task) return { events: [] };
		const elapsed = Date.now() - task.createdAt;
		const events = mockTaskEvents(task)
			.filter((e) => e.at <= elapsed && e.seq > sinceSeq)
			.map((e) => ({ seq: e.seq, kind: e.kind, payload: e.payload, created_at: new Date().toISOString() }));
		return { events };
	}

	if (path.match(/^\/istota\/api\/chat\/tasks\/\d+\/(confirm|cancel)$/) && method === 'POST') {
		return { status: 'ok' };
	}

	if (path === '/istota/api/chat/attachments' && method === 'POST') {
		return { path: `inbox/web-chat/mock/${Date.now()}.bin`, name: 'upload', size: 0 };
	}

	return undefined;
};

const mockAdminStats = {
	system: {
		version: '0.20.0',
		uptime_seconds: 345600,
		db_size_bytes: 119447552,
		python_version: '3.12.3',
		last_scheduler_run: new Date(Date.now() - 30_000).toISOString(),
		scheduler_healthy: true,
	},
	users: [
		{
			username: 'stefan',
			display_name: 'Stefan',
			is_admin: true,
			tasks_total: 11281,
			tasks_last_24h: 1734,
			tasks_avg_per_day: 373.7,
			tasks_by_source_24h: {
				talk: { count: 28, failed: 1, avg_duration_seconds: 18.4 },
				email: { count: 4, failed: 0, avg_duration_seconds: 22.1 },
				scheduled: { count: 1700, failed: 3, avg_duration_seconds: 1.2 },
				briefing: { count: 2, failed: 0, avg_duration_seconds: 35.0 },
			},
			tasks_interactive_24h: 32,
			tasks_automated_24h: 1702,
			tasks_failed_24h: 4,
			last_active: new Date(Date.now() - 60_000).toISOString(),
		},
		{
			username: 'kasia',
			display_name: 'Kasia',
			is_admin: false,
			tasks_total: 891,
			tasks_last_24h: 77,
			tasks_avg_per_day: 6.2,
			tasks_by_source_24h: {
				talk: { count: 4, failed: 0, avg_duration_seconds: 19.0 },
				scheduled: { count: 73, failed: 0, avg_duration_seconds: 1.1 },
			},
			tasks_interactive_24h: 4,
			tasks_automated_24h: 73,
			tasks_failed_24h: 0,
			last_active: new Date(Date.now() - 3600_000).toISOString(),
		},
	],
	scheduler: {
		jobs_total: 5,
		jobs_active: 4,
		jobs_paused: 1,
		jobs: [
			{
				id: 1,
				user_id: 'stefan',
				name: 'morning briefing',
				cron: '0 7 * * *',
				enabled: true,
				last_run_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
				last_success_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
				consecutive_failures: 0,
				last_error: null,
			},
			{
				id: 2,
				user_id: 'stefan',
				name: '_module.feeds.run_scheduled',
				cron: '*/5 * * * *',
				enabled: true,
				last_run_at: new Date(Date.now() - 3 * 60_000).toISOString(),
				last_success_at: new Date(Date.now() - 3 * 60_000).toISOString(),
				consecutive_failures: 0,
				last_error: null,
			},
			{
				id: 3,
				user_id: 'kasia',
				name: '_module.feeds.run_scheduled',
				cron: '*/5 * * * *',
				enabled: true,
				last_run_at: new Date(Date.now() - 3 * 60_000).toISOString(),
				last_success_at: new Date(Date.now() - 3 * 60_000).toISOString(),
				consecutive_failures: 0,
				last_error: null,
			},
			{
				id: 4,
				user_id: 'stefan',
				name: '_module.money.run_scheduled',
				cron: '*/30 * * * *',
				enabled: true,
				last_run_at: new Date(Date.now() - 12 * 60_000).toISOString(),
				last_success_at: new Date(Date.now() - 27 * 60_000).toISOString(),
				consecutive_failures: 1,
				last_error: 'timeout after 30s',
			},
			{
				id: 5,
				user_id: 'stefan',
				name: 'evening recap',
				cron: '0 21 * * *',
				enabled: false,
				last_run_at: new Date(Date.now() - 36 * 3600_000).toISOString(),
				last_success_at: null,
				consecutive_failures: 5,
				last_error: 'API Error: 429 rate_limited',
			},
		],
		last_errors: [
			{
				job_name: 'stefan/feeds.poll',
				error: 'timeout after 30s',
				timestamp: new Date(Date.now() - 12 * 60_000).toISOString(),
			},
		],
	},
	modules: {
		feeds: {
			backend: 'native',
			users_configured: 1,
			users_resolved: 1,
			feeds_total: 129,
			entries_total: 48201,
			entries_unread: 342,
			last_poll: new Date(Date.now() - 5 * 60_000).toISOString(),
			poll_errors_24h: 2,
		},
		money: { users_configured: 1 },
		location: {
			visits_total: 1204,
			places_total: 47,
			last_update: new Date(Date.now() - 90 * 60_000).toISOString(),
		},
	},
	tasks: {
		total: 12172,
		last_24h: 1811,
		avg_per_day_30d: 379.8,
		by_source: { talk: 32, email: 4, scheduled: 1773, briefing: 2 },
		failed_by_source_24h: { talk: 1, scheduled: 3 },
		avg_duration_seconds: 4.79,
		error_rate_24h: 0.0022,
		failed_24h: 4,
		interactive_24h: 36,
		automated_24h: 1775,
		interactive_avg_per_day_30d: 38.1,
		automated_avg_per_day_30d: 341.7,
	},
	storage: {
		db_size_bytes: 119447552,
		backups_count: 14,
		last_backup: new Date(Date.now() - 18 * 3600_000).toISOString(),
		nextcloud_mount_healthy: true,
	},
};

// Mock reader dataset — populated below so the dev UI has scrollable content.

interface MockFeedSource {
	id: number;
	title: string;
	site_url: string;
	category: { id: number; title: string };
}

interface MockEntry {
	id: number;
	title: string;
	url: string;
	content: string;
	images: string[];
	feed: MockFeedSource;
	status: 'read' | 'unread';
	starred: boolean;
	starred_at: string;
	published_at: string;
	created_at: string;
}

const mockReaderFeeds: MockFeedSource[] = [
	{ id: 1, title: 'Hacker News', site_url: 'https://news.ycombinator.com', category: { id: 1, title: 'Blogs' } },
	{ id: 2, title: 'The Verge', site_url: 'https://www.theverge.com', category: { id: 1, title: 'Blogs' } },
	{ id: 3, title: 'Daring Fireball', site_url: 'https://daringfireball.net', category: { id: 1, title: 'Blogs' } },
	{ id: 4, title: 'Nemfrog', site_url: 'https://nemfrog.tumblr.com', category: { id: 2, title: 'Tumblr' } },
	{ id: 5, title: 'Cats in a channel', site_url: 'https://are.na/cats', category: { id: 3, title: 'Are.na' } },
];

const sampleTitles = [
	'A small note on cache invalidation',
	'The unreasonable effectiveness of plain text',
	'Why list views still matter in 2026',
	'Notes from a week of dogfooding',
	'On the quiet joy of finishing things',
	'A case against premature abstraction',
	'Latency budgets, revisited',
	'How I learned to stop worrying and love SQLite',
	'Tiny tools beat platforms',
	'The room where it scrolls',
	'Mid-year reading list',
	'On naming things',
	'Drafts: an underrated feature',
	'Calm software in an anxious year',
	'The browser is the OS',
	'Three weeks with the new keyboard',
	'A short rant about modal dialogs',
	'Re-reading old code',
	'The case for progressive enhancement',
	'Sundays are for refactoring',
];

const sampleSnippets = [
	'A few thoughts I jotted down on the train this morning. Nothing groundbreaking, just a small observation that turned into something I keep thinking about.',
	'I have been reorganizing my notes and noticed a pattern I had not seen before. Sharing it here in case it is useful to someone else doing the same thing.',
	'There is a particular kind of mistake I keep making, and I want to write it down so I stop making it. Maybe writing helps. Maybe it does not.',
	'After a year of using this tool every day, here is what I would change. None of it is dramatic. Most of it is small. That is sort of the point.',
	'Quick demo of a thing I built last weekend. Probably not useful for anyone else, but it scratched an itch I had had for a while.',
];

function pad(n: number): string {
	return n < 10 ? `0${n}` : String(n);
}

function generateMockEntries(): MockEntry[] {
	const entries: MockEntry[] = [];
	const baseTime = Date.now();
	// 30 days back, two entries per hour-ish on average — enough to scroll.
	const total = 180;
	for (let i = 0; i < total; i++) {
		const feed = mockReaderFeeds[i % mockReaderFeeds.length];
		// Spread over ~30 days; published earlier than created by a small jitter
		// so the two sort orders produce visibly different results.
		const publishedAt = new Date(baseTime - i * 3.5 * 60 * 60 * 1000);
		const createdAt = new Date(publishedAt.getTime() + ((i * 17) % 41) * 60 * 1000);

		// Mix: every 3rd entry has 1 image, every 7th has a gallery, rest are text.
		const isGallery = i % 7 === 3;
		const isImage = !isGallery && i % 3 === 0;
		const images: string[] = [];
		if (isGallery) {
			// Vary gallery size: 2, 3, 4, 6 — covers all layout branches.
			const sizes = [2, 3, 4, 6];
			const galSize = sizes[Math.floor(i / 7) % sizes.length];
			for (let g = 0; g < galSize; g++) {
				images.push(`https://picsum.photos/seed/feed-${i}-${g}/600/600`);
			}
		} else if (isImage) {
			images.push(`https://picsum.photos/seed/feed-${i}/800/500`);
		}

		const title = `${sampleTitles[i % sampleTitles.length]} (#${total - i})`;
		const snippet = sampleSnippets[i % sampleSnippets.length];

		entries.push({
			id: i + 1,
			title,
			url: `${feed.site_url}/posts/${i + 1}`,
			content: `<p>${snippet}</p><p>This is mock content number ${i + 1}, served by the dev mock API.</p>`,
			images,
			feed,
			// First ~25% unread, rest read — gives the Unseen filter something to do.
			status: i < total * 0.25 ? 'unread' : 'read',
			starred: i % 11 === 0,
			starred_at: i % 11 === 0 ? createdAt.toISOString() : '',
			published_at: publishedAt.toISOString(),
			created_at: createdAt.toISOString(),
		});
	}
	return entries;
}

const mockReaderEntries: MockEntry[] = generateMockEntries();

function feedsListResponse(params: URLSearchParams): { feeds: MockFeedSource[]; entries: MockEntry[]; total: number } {
	const limit = Math.max(1, Math.min(500, Number(params.get('limit')) || 50));
	const offset = Math.max(0, Number(params.get('offset')) || 0);
	const before = params.get('before');
	const order = params.get('order') === 'created_at' ? 'created_at' : 'published_at';
	const feedId = params.get('feed_id') ? Number(params.get('feed_id')) : 0;
	const statusFilter = params.get('status'); // 'unread' | null
	const starredOnly = params.get('starred') === '1';

	let pool = mockReaderEntries;
	if (feedId) pool = pool.filter((e) => e.feed.id === feedId);
	if (statusFilter === 'unread') pool = pool.filter((e) => e.status !== 'read');
	if (starredOnly) pool = pool.filter((e) => e.starred);

	pool = [...pool].sort((a, b) => {
		const av = order === 'created_at' ? a.created_at : a.published_at;
		const bv = order === 'created_at' ? b.created_at : b.published_at;
		return bv.localeCompare(av); // desc
	});

	const total = pool.length;

	if (before) {
		const cutoffSec = Number(before);
		pool = pool.filter((e) => {
			const v = order === 'created_at' ? e.created_at : e.published_at;
			return Math.floor(new Date(v).getTime() / 1000) < cutoffSec;
		});
	}

	const slice = pool.slice(offset, offset + limit);
	return { feeds: mockReaderFeeds, entries: slice, total };
}

interface MockFeed {
	url: string;
	title?: string;
	category?: string;
	poll_interval_minutes?: number;
}
interface MockCategory {
	slug: string;
	title?: string;
}
const mockFeedsConfig: {
	settings: { default_poll_interval_minutes?: number };
	categories: MockCategory[];
	feeds: MockFeed[];
} = {
	settings: { default_poll_interval_minutes: 30 },
	categories: [
		{ slug: 'blogs', title: 'Blogs' },
		{ slug: 'tumblr', title: 'Tumblr' },
		{ slug: 'arena', title: 'Are.na' },
	],
	feeds: [
		{ url: 'https://example.com/feed.xml', title: 'Example Blog', category: 'blogs' },
		{ url: 'tumblr:nemfrog', title: 'Nemfrog', category: 'tumblr' },
		{ url: 'arena:cats-in-a-channel', category: 'arena', poll_interval_minutes: 60 },
	],
};

function feedsConfigResponse() {
	const now = new Date().toISOString();
	return {
		config: mockFeedsConfig,
		diagnostics: {
			total_feeds: mockFeedsConfig.feeds.length,
			total_entries: 42,
			unread_entries: 7,
			error_feeds: 0,
			last_poll_at: now,
		},
		feed_state: mockFeedsConfig.feeds.map((f) => ({
			url: f.url,
			last_fetched_at: now,
			last_error: null,
			error_count: 0,
		})),
	};
}

interface MockPlace {
	id: number;
	name: string;
	lat: number;
	lon: number;
	radius_meters: number;
	category: string;
	notes: string;
}

const mockPlaces: { places: MockPlace[] } = {
	places: [
		{ id: 1, name: 'Home', lat: 52.5200, lon: 13.4050, radius_meters: 80, category: 'home', notes: '' },
		{ id: 2, name: 'Office', lat: 52.5074, lon: 13.3904, radius_meters: 60, category: 'work', notes: '' },
		{ id: 3, name: 'Berghain Boiler Room (Side Entrance)', lat: 52.5111, lon: 13.4430, radius_meters: 50, category: 'social', notes: '' },
		{ id: 4, name: 'Climbing Gym', lat: 52.5300, lon: 13.4150, radius_meters: 40, category: 'gym', notes: '' },
		{ id: 5, name: 'Sunday Farmers Market on Maybachufer', lat: 52.4920, lon: 13.4280, radius_meters: 75, category: 'shopping', notes: '' },
		{ id: 6, name: 'Pizza Place', lat: 52.5180, lon: 13.4100, radius_meters: 30, category: 'food', notes: '' },
		{ id: 7, name: "Mom's", lat: 52.5400, lon: 13.4500, radius_meters: 100, category: 'family', notes: '' },
		{ id: 8, name: 'Co-working Spot', lat: 52.5050, lon: 13.3850, radius_meters: 45, category: 'work', notes: '' },
		{ id: 9, name: 'Dentist', lat: 52.5260, lon: 13.4020, radius_meters: 35, category: 'medical', notes: '' },
		{ id: 10, name: 'Café around the corner with the wifi password on the wall', lat: 52.5210, lon: 13.4080, radius_meters: 30, category: 'food', notes: '' },
		{ id: 11, name: 'Hotel Adlon', lat: 52.5163, lon: 13.3789, radius_meters: 50, category: 'hotel', notes: '' },
		{ id: 12, name: 'Friend Anna', lat: 52.5350, lon: 13.4200, radius_meters: 80, category: 'friend', notes: '' },
	],
};

interface MockDismissed {
	id: number;
	lat: number;
	lon: number;
	radius_meters: number;
	dismissed_at: string;
}
const mockDismissed: { dismissed: MockDismissed[] } = {
	dismissed: [
		{ id: 1, lat: 52.5000, lon: 13.4500, radius_meters: 120, dismissed_at: '2026-04-10T00:00:00Z' },
	],
};

interface MockCluster {
	lat: number;
	lon: number;
	radius_meters: number;
	total_pings: number;
	first_seen: string;
	last_seen: string;
}
const mockDiscover: { clusters: MockCluster[] } = {
	clusters: [
		{ lat: 52.5235, lon: 13.4115, radius_meters: 60, total_pings: 42, first_seen: '2026-04-15T08:00:00Z', last_seen: '2026-04-25T19:30:00Z' },
		{ lat: 52.4980, lon: 13.4380, radius_meters: 90, total_pings: 18, first_seen: '2026-04-20T12:00:00Z', last_seen: '2026-04-26T11:00:00Z' },
		{ lat: 52.5320, lon: 13.3950, radius_meters: 45, total_pings: 11, first_seen: '2026-04-22T17:00:00Z', last_seen: '2026-04-25T22:00:00Z' },
	],
};

const today = new Date().toISOString().slice(0, 10);
const mockPings = (() => {
	const pings: any[] = [];
	// Berlin morning, continuous tracking: 60 pings 1 min apart, 08:00-08:59.
	// Tight spacing keeps each edge under the dwell-gap threshold so this
	// stretch renders as the solid speed-coloured activity line.
	const berlinLat = 52.5200;
	const berlinLon = 13.4050;
	for (let i = 0; i < 60; i++) {
		const t = new Date();
		t.setHours(8, i, 0, 0);
		const stationary = i < 15;
		pings.push({
			timestamp: t.toISOString(),
			lat: berlinLat + Math.sin(i / 18) * 0.004 + i * 0.00012,
			lon: berlinLon + Math.cos(i / 18) * 0.004 + i * 0.00018,
			horizontal_accuracy: 15,
			activity_type: stationary ? 'stationary' : i < 35 ? 'walking' : 'in_vehicle',
			speed: stationary ? 0 : i < 35 ? 1.2 : 8.5,
			place: stationary ? 'Home' : null,
			place_id: stationary ? 1 : null,
		});
	}
	// ~14h transatlantic flight gap: next ping is in LA at 23:00 UTC.
	// Berlin → LAX is ~9,300 km; the implied speed easily exceeds the gap
	// threshold so this edge renders as the coral great-circle arc.
	// LA pings are 6 min apart, matching Overland's significant-location-change
	// mode, so each LA→LA edge crosses the dwell threshold and renders as the
	// muted sparse-sample dash.
	const laxLat = 33.9425;
	const laxLon = -118.4081;
	for (let i = 0; i < 30; i++) {
		const t = new Date();
		t.setHours(23 + Math.floor(i / 10), (i % 10) * 6, 0, 0);
		pings.push({
			timestamp: t.toISOString(),
			lat: laxLat + Math.sin(i / 6) * 0.008 + i * 0.0003,
			lon: laxLon + Math.cos(i / 6) * 0.008 + i * 0.0004,
			horizontal_accuracy: 18,
			activity_type: i < 5 ? 'stationary' : i < 20 ? 'in_vehicle' : 'walking',
			speed: i < 5 ? 0 : i < 20 ? 12.5 : 1.4,
			place: null,
			place_id: null,
		});
	}
	return { pings, count: pings.length };
})();
const mockDay = {
	date: today,
	timezone: 'Europe/Berlin',
	ping_count: 50,
	transit_pings: 20,
	stops: [
		{ lat: 52.5200, lon: 13.4050, name: 'Home', start_time: `${today}T07:00:00Z`, end_time: `${today}T08:30:00Z`, duration_min: 90, ping_count: 10 },
		{ lat: 52.5074, lon: 13.3904, name: 'Office', start_time: `${today}T09:00:00Z`, end_time: `${today}T17:00:00Z`, duration_min: 480, ping_count: 30 },
	],
};
const mockCurrent = {
	last_ping: { recorded_at: new Date().toISOString(), lat: 52.5200, lon: 13.4050, horizontal_accuracy: 12 },
	current_visit: { place: 'Home', place_id: 1, started_at: `${today}T07:00:00Z` },
};

const ledgers = { ledgers: ['main', 'business'] };
const checkResp = { error_count: 0, errors: [] };
const accountsResp = {
	accounts: [
		{ account: 'Assets:Checking', balance: '0.00 USD' },
		{ account: 'Assets:Savings', balance: '0.00 USD' },
		{ account: 'Expenses:Food', balance: '0.00 USD' },
		{ account: 'Income:Salary', balance: '0.00 USD' },
	],
};

let nextPlaceId = mockPlaces.places.length + 1;
let nextDismissedId = mockDismissed.dismissed.length + 1;

// Approximate distance between two coords in meters (sufficient for nearby clustering checks).
function distMeters(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
	const R = 6371000;
	const toRad = (d: number) => (d * Math.PI) / 180;
	const dLat = toRad(b.lat - a.lat);
	const dLon = toRad(b.lon - a.lon);
	const lat1 = toRad(a.lat);
	const lat2 = toRad(b.lat);
	const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
	return 2 * R * Math.asin(Math.sqrt(h));
}

function dropClusterNear(point: { lat: number; lon: number }, radius: number): void {
	mockDiscover.clusters = mockDiscover.clusters.filter(
		(c) => distMeters(c, point) > Math.max(radius, c.radius_meters),
	);
}

// Phase 5 — settings/secrets mock state. Plaintext values stay only in memory;
// the real backend never returns them.
const mockSecrets: Record<string, Record<string, string>> = {
	karakeep: {},
	google_workspace: {},
	monarch: {},
	feeds: {},
	overland: {},
};

interface ServiceSchema {
	service: string;
	label: string;
	fields: { key: string; label: string; type: string }[];
	used_by?: string[];
	oauth?: boolean;
}

const _CONNECTED_SCHEMAS: ServiceSchema[] = [
	{
		service: 'karakeep',
		label: 'Karakeep',
		used_by: ['bookmarks'],
		fields: [
			{ key: 'base_url', label: 'Base URL', type: 'url' },
			{ key: 'api_key', label: 'API key', type: 'password' },
		],
	},
	{
		service: 'google_workspace',
		label: 'Google Workspace',
		used_by: ['google_workspace'],
		oauth: true,
		fields: [],
	},
];

const _MODULE_SCHEMAS: Record<string, ServiceSchema[]> = {
	feeds: [
		{
			service: 'feeds',
			label: 'Feeds (Tumblr)',
			used_by: ['feeds'],
			fields: [
				{ key: 'tumblr_api_key', label: 'Tumblr API key (optional)', type: 'password' },
			],
		},
	],
	money: [
		{
			service: 'monarch',
			label: 'Monarch Money',
			used_by: ['money'],
			fields: [
				{ key: 'session_id', label: 'session_id cookie', type: 'password' },
				{ key: 'csrftoken', label: 'csrftoken cookie', type: 'password' },
			],
		},
	],
	location: [
		{
			service: 'overland',
			label: 'Overland GPS',
			used_by: ['location'],
			fields: [
				{ key: 'ingest_token', label: 'Ingest token', type: 'password' },
			],
		},
	],
};

const MODULE_NAMES = ['feeds', 'money', 'location'];

function buildServiceCard(s: ServiceSchema) {
	const stored = mockSecrets[s.service] || {};
	const configured_keys = Object.keys(stored).filter((k) => stored[k]);
	if (s.oauth) {
		const connected = configured_keys.length > 0;
		return {
			...s,
			status: connected ? 'configured' : 'missing',
			configured_keys,
			last_updated: null,
			connected,
			enabled: true,
		};
	}
	const required = s.fields
		.filter((f) => !f.label.toLowerCase().includes('optional'))
		.map((f) => f.key);
	let status: 'configured' | 'partial' | 'missing' = 'missing';
	if (required.length === 0) {
		status = configured_keys.length > 0 ? 'configured' : 'missing';
	} else if (required.every((k) => configured_keys.includes(k))) {
		status = 'configured';
	} else if (required.some((k) => configured_keys.includes(k))) {
		status = 'partial';
	}
	return { ...s, status, configured_keys, last_updated: null };
}

function mockSettingsServices(): { services: unknown[] } {
	return { services: _CONNECTED_SCHEMAS.map(buildServiceCard) };
}

// disabled_modules lives on the user profile and gates per-module status.
const mockDisabledModules = new Set<string>();

function mockModulesResponse() {
	return {
		modules: MODULE_NAMES,
		disabled: [...mockDisabledModules],
		enabled_for_user: Object.fromEntries(
			MODULE_NAMES.map((m) => [m, !mockDisabledModules.has(m)]),
		),
	};
}

function mockModuleServices(module: string) {
	const schemas = _MODULE_SCHEMAS[module];
	if (!schemas) return undefined;
	return {
		module,
		module_enabled: !mockDisabledModules.has(module),
		services: schemas.map(buildServiceCard),
	};
}

const handlers: MockHandler[] = [
	({ url }) => (url === '/istota/api/me' ? user : undefined),
	chatHandler,

	({ url }) => (url === '/istota/api/admin/stats' ? mockAdminStats : undefined),

	// Settings/secrets (Phase 5)
	(req) => {
		const { url, method, body } = req;
		if (url === '/istota/api/settings/services' && method === 'GET') {
			return mockSettingsServices();
		}
		if (url === '/istota/api/settings/modules' && method === 'GET') {
			return mockModulesResponse();
		}
		const moduleSvcMatch = url.match(/^\/istota\/api\/settings\/module-services\/([^/?]+)$/);
		if (moduleSvcMatch && method === 'GET') {
			const resp = mockModuleServices(moduleSvcMatch[1]);
			if (!resp) return { error: `Unknown module: ${moduleSvcMatch[1]}` };
			return resp;
		}
		const m = url.match(/^\/istota\/api\/settings\/secrets\/([^/]+)\/([^/?]+)$/);
		if (!m) return undefined;
		const [, service, key] = m;
		if (!mockSecrets[service]) return { error: 'unknown service' };
		if (method === 'PUT') {
			const value = (body?.value as string | undefined) ?? '';
			if (value) mockSecrets[service][key] = value;
			else delete mockSecrets[service][key];
			return { ok: true, service, key, configured: Boolean(value) };
		}
		if (method === 'DELETE') {
			const had = Boolean(mockSecrets[service][key]);
			delete mockSecrets[service][key];
			return { ok: true, deleted: had };
		}
		return undefined;
	},

	// Phase 6 — profile + resources
	(() => {
		const mockProfile: Record<string, unknown> = {
			user_id: user.username,
			display_name: user.display_name,
			timezone: 'UTC',
			email_addresses: ['user@example.com'],
			trusted_email_senders: [],
			log_channel: 'logs-room-token',
			alerts_channel: 'alerts-room-token',
			disabled_skills: [],
			disabled_modules: [],
			max_foreground_workers: 0,
			max_background_workers: 0,
			site_enabled: false,
			default_destination: 'talk',
			routing: {},
			purposes: ['reply', 'alert', 'log', 'briefing', 'notification'],
			delivery_surfaces: ['email', 'ntfy', 'talk'],
		};
		let nextResourceId = 100;
		const mockDbResources: {
			id: number; type: string; name: string; path: string; permissions: string; extras?: Record<string, unknown>;
		}[] = [];
		const resourceTypes = [
			{ type: 'calendar', label: 'Calendar (CalDAV)', needs_path: true, permissions: ['read', 'readwrite'] },
			{ type: 'folder', label: 'Nextcloud folder', needs_path: true, permissions: ['read', 'readwrite'] },
			{ type: 'todo_file', label: 'TODO file (markdown)', needs_path: true, permissions: ['read', 'readwrite'] },
			{ type: 'feeds', label: 'Feeds (RSS/Atom)', needs_path: false, permissions: ['read'] },
			{ type: 'money', label: 'Money (beancount)', needs_path: false, permissions: ['read'] },
			{ type: 'overland', label: 'Location (Overland GPS)', needs_path: false, permissions: ['read'] },
			{ type: 'karakeep', label: 'Karakeep bookmarks', needs_path: false, permissions: ['read'] },
		];
		return ({ url, method, body }: { url: string; method: string; body?: unknown }) => {
			if (url === '/istota/api/settings/profile' && method === 'GET') {
				return { profile: mockProfile };
			}
			if (url === '/istota/api/settings/profile' && method === 'PUT') {
				const patch = body as Record<string, unknown> | undefined;
				if (patch && typeof patch === 'object') {
					for (const [k, v] of Object.entries(patch)) {
						mockProfile[k] = v;
					}
					if (Array.isArray(patch.disabled_modules)) {
						mockDisabledModules.clear();
						for (const m of patch.disabled_modules as unknown[]) {
							if (typeof m === 'string' && MODULE_NAMES.includes(m)) {
								mockDisabledModules.add(m);
							}
						}
					}
				}
				return { ok: true, fields: Object.keys(patch ?? {}) };
			}
			if (url === '/istota/api/settings/resources' && method === 'GET') {
				return {
					types: resourceTypes,
					resources: [
						{ managed: 'config', type: 'feeds', name: 'Feeds', path: '', permissions: 'read' },
						...mockDbResources.map((r) => ({ managed: 'db', ...r })),
					],
				};
			}
			if (url === '/istota/api/settings/resources' && method === 'POST') {
				const p = body as Record<string, unknown> | undefined;
				if (!p || typeof p !== 'object') return { error: 'bad payload' };
				const id = nextResourceId++;
				const extras = p.extras as Record<string, unknown> | undefined;
				mockDbResources.push({
					id,
					type: String(p.type ?? ''),
					name: String(p.name ?? ''),
					path: String(p.path ?? p.type ?? ''),
					permissions: String(p.permissions ?? 'read'),
					...(extras && typeof extras === 'object' ? { extras } : {}),
				});
				return { ok: true, id };
			}
			const m = url.match(/^\/istota\/api\/settings\/resources\/(\d+)$/);
			if (m && method === 'DELETE') {
				const id = Number(m[1]);
				const idx = mockDbResources.findIndex((r) => r.id === id);
				if (idx >= 0) mockDbResources.splice(idx, 1);
				return { ok: true, deleted: idx >= 0 };
			}
			return undefined;
		};
	})(),

	// Phase 7b — briefings
	(() => {
		let nextBriefingId = 200;
		const mockDbBriefings: {
			id: number;
			name: string;
			cron: string;
			conversation_token: string;
			output: string;
			components: Record<string, unknown>;
			enabled: boolean;
		}[] = [];
		const tomlBriefings = [
			{
				name: 'morning',
				cron: '0 7 * * 1-5',
				conversation_token: 'abc123',
				output: 'talk' as const,
				components: { calendar: true, todos: true, email: true },
				enabled: true,
			},
		];
		return ({ url, method, body }: { url: string; method: string; body?: unknown }) => {
			if (url === '/istota/api/settings/briefings' && method === 'GET') {
				return {
					briefings: [
						...tomlBriefings.map((b) => ({ managed: 'config', ...b })),
						...mockDbBriefings.map((b) => ({ managed: 'db', ...b })),
					],
					rooms: [
						{ token: 'abc123', name: 'Log channel' },
						{ token: 'def456', name: 'Alerts channel' },
					],
					outputs: ['talk', 'email', 'ntfy'],
				};
			}
			if (url === '/istota/api/settings/briefings' && method === 'POST') {
				const p = body as Record<string, unknown> | undefined;
				if (!p || typeof p !== 'object') return { error: 'bad payload' };
				const name = String(p.name ?? '');
				const existing = mockDbBriefings.findIndex((b) => b.name === name);
				const row = {
					id: existing >= 0 ? mockDbBriefings[existing].id : nextBriefingId++,
					name,
					cron: String(p.cron ?? ''),
					conversation_token: String(p.conversation_token ?? ''),
					output: (p.output as string) ?? 'talk',
					components:
						(p.components as Record<string, unknown> | undefined) ?? {},
					enabled: p.enabled !== false,
				};
				if (existing >= 0) mockDbBriefings[existing] = row;
				else mockDbBriefings.push(row);
				return {
					ok: true,
					id: row.id,
					state: existing >= 0 ? 'updated' : 'created',
				};
			}
			const m = url.match(/^\/istota\/api\/settings\/briefings\/(\d+)$/);
			if (m && method === 'DELETE') {
				const id = Number(m[1]);
				const idx = mockDbBriefings.findIndex((b) => b.id === id);
				if (idx >= 0) mockDbBriefings.splice(idx, 1);
				return { ok: true, deleted: idx >= 0 };
			}
			return undefined;
		};
	})(),

	// Feeds settings: config GET/PUT
	({ url, method, body }) => {
		if (url !== '/istota/api/feeds/config') return undefined;
		if (method === 'GET') return feedsConfigResponse();
		if (method === 'PUT') {
			const cfg = body?.config;
			if (cfg && typeof cfg === 'object') {
				mockFeedsConfig.settings = cfg.settings ?? {};
				mockFeedsConfig.categories = cfg.categories ?? [];
				mockFeedsConfig.feeds = cfg.feeds ?? [];
			}
			return {
				status: 'ok',
				sync: {
					categories_added: 0,
					feeds_added: 0,
					feeds_updated: mockFeedsConfig.feeds.length,
				},
			};
		}
		return undefined;
	},

	({ url, method }) => {
		if (url !== '/istota/api/feeds/import-opml' || method !== 'POST') return undefined;
		return {
			status: 'ok',
			feeds_added: 1,
			feeds_updated: 0,
			categories_added: 1,
			rewritten_bridger_urls: 0,
		};
	},

	// Reader: GET /feeds with pagination, sorting, filtering
	({ url, method }) => {
		if (method !== 'GET') return undefined;
		const [path, query] = url.split('?');
		if (path !== '/istota/api/feeds') return undefined;
		return feedsListResponse(new URLSearchParams(query ?? ''));
	},

	// Reader mutations — accept and acknowledge.
	({ url, method, body }) => {
		const m = url.match(/^\/istota\/api\/feeds\/entries\/(\d+)$/);
		if (!m || method !== 'PUT') return undefined;
		const id = Number(m[1]);
		const entry = mockReaderEntries.find((e) => e.id === id);
		if (entry && body && typeof body === 'object') {
			if (typeof body.starred === 'boolean') {
				entry.starred = body.starred;
				entry.starred_at = body.starred ? new Date().toISOString() : '';
			}
			if (typeof body.status === 'string') {
				entry.status = body.status === 'read' ? 'read' : 'unread';
			}
		}
		return { status: 'ok' };
	},
	({ url, method, body }) => {
		if (url !== '/istota/api/feeds/entries/batch' || method !== 'PUT') return undefined;
		const ids: number[] = Array.isArray(body?.entry_ids) ? body.entry_ids : [];
		const status = body?.status === 'read' ? 'read' : 'unread';
		for (const id of ids) {
			const e = mockReaderEntries.find((x) => x.id === id);
			if (e) e.status = status;
		}
		return { status: 'ok', updated: ids.length };
	},
	({ url, method, body }) => {
		if (url !== '/istota/api/feeds/mark-as-read' || method !== 'POST') return undefined;
		const scope = body?.scope;
		const beforeId: number | undefined = body?.before_id;
		const targetId: number | undefined = body?.id;
		let updated = 0;
		for (const e of mockReaderEntries) {
			if (e.status === 'read') continue;
			if (beforeId != null && e.id > beforeId) continue;
			if (scope === 'feed' && targetId != null && e.feed.id !== targetId) continue;
			e.status = 'read';
			updated++;
		}
		return { status: 'ok', updated };
	},
	({ url, method }) => {
		if (url !== '/istota/api/feeds/refresh' || method !== 'POST') return undefined;
		return { status: 'ok' };
	},
	({ url }) => {
		if (url !== '/istota/api/location/settings-info') return undefined;
		return {
			webhook_url: 'https://example.invalid/webhooks/location?token=<token>',
			module_enabled: !mockDisabledModules.has('location'),
			place_detection: {
				accuracy_threshold_m: 100,
				visit_exit_minutes: 5,
			},
		};
	},

	({ url }) => (url.startsWith('/istota/api/location/current') ? mockCurrent : undefined),

	// Place stats
	({ url }) => {
		const m = url.match(/\/istota\/api\/location\/places\/(\d+)\/stats/);
		if (!m) return undefined;
		return {
			place_id: Number(m[1]),
			total_visits: 0,
			first_visit: null,
			last_visit: null,
			avg_duration_min: null,
			total_duration_min: null,
			longest_visit_min: null,
		};
	},

	// Place CRUD
	({ url, method, body }) => {
		if (!url.startsWith('/istota/api/location/places')) return undefined;

		const idMatch = url.match(/\/istota\/api\/location\/places\/(\d+)$/);
		if (idMatch && method === 'PUT') {
			const id = Number(idMatch[1]);
			const idx = mockPlaces.places.findIndex((p) => p.id === id);
			if (idx >= 0) {
				mockPlaces.places[idx] = { ...mockPlaces.places[idx], ...body };
			}
			return mockPlaces.places[idx] ?? {};
		}
		if (idMatch && method === 'DELETE') {
			const id = Number(idMatch[1]);
			mockPlaces.places = mockPlaces.places.filter((p) => p.id !== id);
			return {};
		}
		if (method === 'POST') {
			const created: MockPlace = {
				id: nextPlaceId++,
				name: body?.name ?? 'Untitled',
				lat: body?.lat ?? 0,
				lon: body?.lon ?? 0,
				radius_meters: body?.radius_meters ?? 100,
				category: body?.category ?? 'other',
				notes: body?.notes ?? '',
			};
			mockPlaces.places.push(created);
			dropClusterNear(created, created.radius_meters);
			return created;
		}
		return mockPlaces;
	},

	// Dismissed clusters
	({ url, method, body }) => {
		if (!url.startsWith('/istota/api/location/dismissed-clusters')) return undefined;

		const idMatch = url.match(/\/istota\/api\/location\/dismissed-clusters\/(\d+)$/);
		if (idMatch && method === 'DELETE') {
			const id = Number(idMatch[1]);
			mockDismissed.dismissed = mockDismissed.dismissed.filter((d) => d.id !== id);
			return {};
		}
		if (method === 'POST') {
			const created: MockDismissed = {
				id: nextDismissedId++,
				lat: body?.lat ?? 0,
				lon: body?.lon ?? 0,
				radius_meters: body?.radius_meters ?? 100,
				dismissed_at: new Date().toISOString(),
			};
			mockDismissed.dismissed.push(created);
			dropClusterNear(created, created.radius_meters);
			return created;
		}
		return mockDismissed;
	},

	({ url }) => (url.startsWith('/istota/api/location/discover-places') ? mockDiscover : undefined),
	({ url }) => (url.startsWith('/istota/api/location/pings') ? mockPings : undefined),
	({ url }) => (url.startsWith('/istota/api/location/day-summary') ? mockDay : undefined),
	({ url }) => (url.startsWith('/istota/money/api/ledgers') ? ledgers : undefined),
	({ url }) => (url.startsWith('/istota/money/api/check') ? checkResp : undefined),
	({ url }) => (url.startsWith('/istota/money/api/accounts') ? accountsResp : undefined),
	({ url }) => {
		if (!url.startsWith('/istota/money/api/business-settings')) return undefined;
		return {
			status: 'ok',
			entities: [
				{
					key: 'main',
					name: 'Acme Studio LLC',
					address: '123 Example St, Berlin',
					email: 'billing@example.com',
					payment_instructions: 'Wire to IBAN DE00 …',
					logo: '',
					ar_account: 'Assets:AR:Acme',
					bank_account: 'Assets:Checking',
					currency: 'EUR',
				},
			],
			services: [
				{ key: 'consulting', display_name: 'Consulting', rate: 150, type: 'hours', income_account: 'Income:Consulting' },
				{ key: 'design', display_name: 'Design', rate: 1200, type: 'days', income_account: 'Income:Design' },
			],
			defaults: {
				currency: 'EUR',
				default_entity: 'main',
				default_ar_account: 'Assets:AR:Acme',
				default_bank_account: 'Assets:Checking',
				invoice_output: '/tmp/invoices',
				next_invoice_number: 42,
				notifications: 'email',
				days_until_overdue: 14,
			},
		};
	},

	// Money module mock — transactions + invoices with stateful action
	// handlers so the row-expand UX and the kebab actions (edit txn, mark
	// paid/pending, download PDF) are exercisable end-to-end without a backend.
	(() => {
		const PREFIX = '/istota/money/api';
		const today = () => new Date().toISOString().slice(0, 10);

		interface Txn {
			date: string; flag: string; payee: string; narration: string;
			account: string; position: string; id: string;
		}
		const transactions: Txn[] = [
			{ id: 'mock-1', date: '2026-05-28', flag: '*', payee: 'Whole Foods', narration: 'Groceries', account: 'Expenses:Food', position: '-82.14 USD' },
			{ id: 'mock-2', date: '2026-05-28', flag: '*', payee: 'Acme Corp', narration: 'May salary', account: 'Income:Salary', position: '5200.00 USD' },
			{ id: 'mock-3', date: '2026-05-26', flag: '*', payee: 'Shell', narration: 'Fuel', account: 'Expenses:Auto', position: '-54.30 USD' },
			{ id: 'mock-4', date: '2026-05-24', flag: '*', payee: 'Netflix', narration: 'Subscription', account: 'Expenses:Subscriptions', position: '-15.99 USD' },
			{ id: 'mock-5', date: '2026-05-22', flag: '*', payee: 'Transfer', narration: 'To savings', account: 'Assets:Savings', position: '500.00 USD' },
			{ id: 'mock-6', date: '2026-05-20', flag: '*', payee: 'Cafe Luna', narration: 'Coffee', account: 'Expenses:Food', position: '-6.75 USD' },
		];

		interface Invoice {
			invoice_number: string; client: string; client_key: string;
			date: string; total: number; status: string; paid_date?: string;
		}
		const invoices: Invoice[] = [
			{ invoice_number: 'INV-000042', client: 'Globex', client_key: 'globex', date: '2026-05-15', total: 4500, status: 'outstanding' },
			{ invoice_number: 'INV-000041', client: 'Initech', client_key: 'initech', date: '2026-04-30', total: 1800, status: 'paid', paid_date: '2026-05-10' },
			{ invoice_number: 'INV-000040', client: 'Globex', client_key: 'globex', date: '2026-04-15', total: 3200, status: 'outstanding' },
			{ invoice_number: 'INV-000039', client: 'Hooli', client_key: 'hooli', date: '2026-03-31', total: 950, status: 'draft' },
		];

		const invoiceItems: Record<string, Array<{ description: string; detail: string; quantity: number; rate: number; discount: number; amount: number }>> = {
			'INV-000042': [
				{ description: 'Consulting', detail: 'May engagement', quantity: 30, rate: 150, discount: 0, amount: 4500 },
			],
			'INV-000041': [
				{ description: 'Design', detail: 'Brand refresh', quantity: 1.5, rate: 1200, discount: 0, amount: 1800 },
			],
			'INV-000040': [
				{ description: 'Consulting', detail: 'April engagement', quantity: 20, rate: 150, discount: 0, amount: 3000 },
				{ description: 'Support', detail: 'Retainer', quantity: 1, rate: 200, discount: 0, amount: 200 },
			],
			'INV-000039': [
				{ description: 'Consulting', detail: 'Scoping', quantity: 6, rate: 150, discount: 0, amount: 900 },
				{ description: 'Travel', detail: 'Reimbursement', quantity: 1, rate: 50, discount: 0, amount: 50 },
			],
		};

		return ({ url, method, body }: { url: string; method: string; body?: any }) => {
			if (!url.startsWith(PREFIX)) return undefined;
			const parsed = new URL(url, 'http://mock');
			const path = parsed.pathname.slice(PREFIX.length); // e.g. /transactions
			const q = parsed.searchParams;

			// --- Invoice action routes (must precede the broad /invoices match) ---
			const action = path.match(/^\/invoices\/([^/]+)\/(mark-paid|mark-pending|pdf)$/);
			if (action) {
				const number = decodeURIComponent(action[1]);
				const verb = action[2];
				const inv = invoices.find((i) => i.invoice_number === number);
				if (verb === 'pdf') {
					return { status: 'ok', note: 'PDF download only works against the real backend' };
				}
				if (!inv) return { status: 'error', error: 'invoice not found' };
				if (verb === 'mark-paid') {
					inv.status = 'paid';
					inv.paid_date = (body && body.paid_date) || today();
					return { status: 'ok', invoice_number: number, paid_date: inv.paid_date, count: 1 };
				}
				inv.status = 'outstanding';
				delete inv.paid_date;
				return { status: 'ok', invoice_number: number, count: 1 };
			}

			if (path === '/transactions/update' && method === 'POST') {
				const t = transactions.find((x) => x.id === body?.id);
				if (!t) return { status: 'error', error: `Transaction not found: ${body?.id}` };
				if (body.new_payee !== undefined) t.payee = body.new_payee;
				if (body.new_narration !== undefined) t.narration = body.new_narration;
				if (body.new_date !== undefined && body.new_date) t.date = body.new_date;
				if (body.new_account !== undefined) t.account = body.new_account;
				if (body.new_position !== undefined) t.position = body.new_position;
				return { status: 'ok', id: t.id };
			}

			if (path === '/transactions' && method === 'GET') {
				let rows = transactions;
				const account = q.get('account');
				if (account) rows = rows.filter((t) => t.account === account);
				const filter = q.get('filter');
				if (filter) {
					const f = filter.toLowerCase();
					rows = rows.filter(
						(t) => t.payee.toLowerCase().includes(f) || t.narration.toLowerCase().includes(f),
					);
				}
				const perPage = Number(q.get('per_page') || 100);
				const page = Number(q.get('page') || 1);
				const start = (page - 1) * perPage;
				return {
					status: 'ok',
					transactions: rows.slice(start, start + perPage),
					total: rows.length,
					page,
					per_page: perPage,
				};
			}

			if (path === '/postings' && method === 'GET') {
				const account = q.get('account') || '';
				const position = q.get('position') || '';
				return {
					status: 'ok',
					postings: [
						{ account, position },
						{ account: 'Assets:Checking', position: '' },
					],
				};
			}

			if (path === '/invoices' && method === 'GET') {
				const showAll = q.get('show_all') === 'true';
				const list = showAll ? invoices : invoices.filter((i) => i.status !== 'paid');
				const outstanding = invoices.filter((i) => i.status === 'outstanding');
				return {
					status: 'ok',
					invoice_count: invoices.length,
					outstanding_count: outstanding.length,
					invoices: list,
				};
			}

			if (path === '/invoice-details' && method === 'GET') {
				const number = q.get('invoice_number') || '';
				return { status: 'ok', invoice_number: number, items: invoiceItems[number] || [] };
			}

			return undefined;
		};
	})(),

	// Health module mock — populated with a realistic shape so the UI is
	// browsable end-to-end without a backend.
	(() => {
		const iso = (daysAgo: number, hour = 9) => {
			const d = new Date();
			d.setUTCDate(d.getUTCDate() - daysAgo);
			d.setUTCHours(hour, 0, 0, 0);
			return d.toISOString();
		};

		interface Stat {
			id: number; metric: string; value: number; unit: string;
			measured_at: string; source: string; source_ref: number | null; notes: string | null;
		}
		interface Bio {
			id: number; panel_id: number; name: string; display_name: string | null;
			value: number; unit: string; ref_range_low: number | null;
			ref_range_high: number | null; flag: string | null;
		}
		interface Panel {
			id: number; drawn_at: string; lab_name: string | null;
			panel_type: string | null; source_file: string | null;
			source_mime: string | null; ocr_text: string | null;
			draft: boolean; notes: string | null;
			encounter_id: number | null;
		}

		const settings = {
			dob: '1985-03-12',
			height_cm: 178,
			sex: 'M' as 'M' | 'F' | null,
			display_units: { weight: 'kg' as 'kg' | 'lb', height: 'cm' as 'cm' | 'ft_in', temp: 'C' as 'C' | 'F' },
		};

		// Garmin connection state — toggle via the settings card.
		// Test branches: email "*+mfa*" → MFA flow (code 123456); "*+bad*" → bad creds.
		const garmin: {
			connected: boolean;
			email: string | null;
			last_sync: string | null;
			error: string | null;
			pendingEmail: string | null;
		} = {
			connected: false,
			email: null,
			last_sync: null,
			error: null,
			pendingEmail: null,
		};

		// Encounters / diagnoses — kept in-closure so they survive across
		// requests in dev mode.
		interface Encounter {
			id: number;
			encounter_date: string;
			encounter_type: string;
			provider: string | null;
			facility: string | null;
			specialty: string | null;
			reason: string | null;
			notes: string | null;
			created_at: string;
		}
		interface Diagnosis {
			id: number;
			name: string;
			icd10: string | null;
			status: 'active' | 'resolved' | 'chronic';
			date_diagnosed: string | null;
			date_resolved: string | null;
			encounter_id: number | null;
			severity: 'mild' | 'moderate' | 'severe' | null;
			notes: string | null;
			created_at: string;
		}
		let nextEncounterId = 1;
		let nextDiagnosisId = 1;
		const encounters: Encounter[] = [
			{
				id: nextEncounterId++,
				encounter_date: '2025-09-15',
				encounter_type: 'visit',
				provider: 'Dr. Patel',
				facility: 'Kaiser Sunset',
				specialty: 'primary_care',
				reason: 'Annual physical',
				notes: 'All clear. Recommended continuing exercise routine.',
				created_at: new Date().toISOString(),
			},
			{
				id: nextEncounterId++,
				encounter_date: '2026-03-04',
				encounter_type: 'procedure',
				provider: 'Dr. Cohen',
				facility: 'Kaiser Sunset',
				specialty: 'gastroenterology',
				reason: 'Screening colonoscopy',
				notes: 'Grade I-II internal hemorrhoids found. No polyps. Follow-up in 5 years.',
				created_at: new Date().toISOString(),
			},
		];
		const diagnoses: Diagnosis[] = [
			{
				id: nextDiagnosisId++,
				name: 'Internal hemorrhoids',
				icd10: 'K64.0',
				status: 'active',
				date_diagnosed: '2026-03-04',
				date_resolved: null,
				encounter_id: 2,
				severity: 'mild',
				notes: 'Found on screening colonoscopy. No active bleeding.',
				created_at: new Date().toISOString(),
			},
			{
				id: nextDiagnosisId++,
				name: 'Seasonal allergies',
				icd10: 'J30.2',
				status: 'chronic',
				date_diagnosed: null,
				date_resolved: null,
				encounter_id: null,
				severity: null,
				notes: null,
				created_at: new Date().toISOString(),
			},
		];

		interface Immunization {
			id: number;
			name: string;
			product_name: string | null;
			date_given: string;
			manufacturer: string | null;
			dose_label: string | null;
			lot_number: string | null;
			route: string | null;
			site: string | null;
			administered_by: string | null;
			facility: string | null;
			encounter_id: number | null;
			cvx_code: string | null;
			notes: string | null;
			source: string;
			created_at: string;
		}
		let nextImmunizationId = 1;
		const immunizations: Immunization[] = [
			{ id: nextImmunizationId++, name: 'Influenza', product_name: 'Fluzone Trivalent', date_given: '2025-11-28', manufacturer: 'Sanofi', dose_label: 'Annual 2025-26', lot_number: null, route: 'IM', site: 'left deltoid', administered_by: null, facility: 'CVS Pharmacy', encounter_id: null, cvx_code: null, notes: null, source: 'manual', created_at: new Date().toISOString() },
			{ id: nextImmunizationId++, name: 'Influenza', product_name: 'Fluzone Quadrivalent', date_given: '2023-10-23', manufacturer: 'Sanofi', dose_label: null, lot_number: null, route: 'IM', site: null, administered_by: null, facility: 'CVS Pharmacy', encounter_id: null, cvx_code: null, notes: null, source: 'manual', created_at: new Date().toISOString() },
			{ id: nextImmunizationId++, name: 'Tdap', product_name: 'Boostrix', date_given: '2016-12-01', manufacturer: 'GSK', dose_label: null, lot_number: null, route: 'IM', site: null, administered_by: null, facility: null, encounter_id: null, cvx_code: null, notes: null, source: 'manual', created_at: new Date().toISOString() },
			{ id: nextImmunizationId++, name: 'COVID-19', product_name: 'Janssen/J&J', date_given: '2021-03-17', manufacturer: 'Janssen', dose_label: null, lot_number: null, route: 'IM', site: null, administered_by: null, facility: null, encounter_id: null, cvx_code: null, notes: 'External Administration', source: 'manual', created_at: new Date().toISOString() },
			{ id: nextImmunizationId++, name: 'Typhoid', product_name: 'Typhim Vi', date_given: '2023-10-23', manufacturer: 'Sanofi', dose_label: null, lot_number: null, route: 'IM', site: null, administered_by: null, facility: null, encounter_id: null, cvx_code: null, notes: null, source: 'manual', created_at: new Date().toISOString() },
		];

		let nextStatId = 1;
		const stats: Stat[] = [];
		// Weight — daily-ish, 2 years of history with a slow downward drift
		// followed by a stabilization around 82 kg.
		for (let i = 730; i >= 0; i -= 2) {
			const trend = 86 - (86 - 82) * Math.min(1, (730 - i) / 500);
			const noise = (Math.random() - 0.5) * 0.6;
			stats.push({
				id: nextStatId++, metric: 'weight',
				value: Math.round((trend + noise) * 10) / 10,
				unit: 'kg', measured_at: iso(i), source: 'manual', source_ref: null, notes: null,
			});
		}
		// Resting HR — every 3-4 days, ~60 bpm with seasonal swings.
		for (let i = 365; i >= 0; i -= 4) {
			const trend = 60 + Math.sin(i / 30) * 3;
			const noise = (Math.random() - 0.5) * 4;
			stats.push({
				id: nextStatId++, metric: 'resting_hr',
				value: Math.max(50, Math.round(trend + noise)),
				unit: 'bpm', measured_at: iso(i), source: 'manual', source_ref: null, notes: null,
			});
		}
		// Body fat % — bi-weekly
		for (let i = 365; i >= 0; i -= 14) {
			const trend = 19 - (19 - 17) * Math.min(1, (365 - i) / 365);
			stats.push({
				id: nextStatId++, metric: 'body_fat_pct',
				value: Math.round((trend + (Math.random() - 0.5) * 0.6) * 10) / 10,
				unit: '%', measured_at: iso(i), source: 'manual', source_ref: null, notes: null,
			});
		}
		// Body temp — sparse, every few weeks
		for (let i = 180; i >= 0; i -= 20) {
			stats.push({
				id: nextStatId++, metric: 'body_temp',
				value: Math.round((36.6 + (Math.random() - 0.5) * 0.5) * 10) / 10,
				unit: '°C', measured_at: iso(i), source: 'manual', source_ref: null, notes: null,
			});
		}
		// SpO2 — weekly
		for (let i = 120; i >= 0; i -= 7) {
			stats.push({
				id: nextStatId++, metric: 'blood_oxygen',
				value: 97 + Math.floor(Math.random() * 2),
				unit: '%', measured_at: iso(i), source: 'manual', source_ref: null, notes: null,
			});
		}
		// Blood pressure — every few days
		for (let i = 180; i >= 0; i -= 3) {
			const sys = 118 + Math.round(Math.random() * 12);
			const dia = 74 + Math.round(Math.random() * 10);
			stats.push({
				id: nextStatId++, metric: 'blood_pressure_systolic',
				value: sys, unit: 'mmHg', measured_at: iso(i),
				source: 'manual', source_ref: null, notes: null,
			});
			stats.push({
				id: nextStatId++, metric: 'blood_pressure_diastolic',
				value: dia, unit: 'mmHg', measured_at: iso(i),
				source: 'manual', source_ref: null, notes: null,
			});
		}

		// Panels: a multi-year longitudinal record + one draft awaiting review.
		const panels: Panel[] = [
			{ id: 1, drawn_at: '2018-01-10', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + CMP + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 2, drawn_at: '2019-04-04', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 3, drawn_at: '2021-06-23', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 4, drawn_at: '2022-05-03', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 5, drawn_at: '2023-09-01', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + CMP + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 6, drawn_at: '2024-07-27', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + CMP + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: null },
			{ id: 7, drawn_at: '2025-11-28', lab_name: 'Kaiser, Los Angeles CA', panel_type: 'CBC + CMP + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: null, encounter_id: 1 },
			{ id: 8, drawn_at: '2026-04-22', lab_name: 'Quest Diagnostics', panel_type: 'Lipid + Thyroid + Iron + Vitamins', source_file: null, source_mime: null, ocr_text: null, draft: false, notes: 'Pre-surgical workup', encounter_id: 2 },
			{ id: 9, drawn_at: '2026-05-09', lab_name: 'Quest Diagnostics', panel_type: 'CBC + CMP + Lipid', source_file: null, source_mime: null, ocr_text: null, draft: true, notes: 'Pending review', encounter_id: null },
		];

		const biomarkers: Bio[] = [];
		let nextBioId = 1;
		const seed = (
			panelId: number,
			items: [string, number, string, number | null, number | null, string | null][],
		) => {
			for (const [name, value, unit, low, high, flag] of items) {
				biomarkers.push({
					id: nextBioId++, panel_id: panelId, name,
					display_name: null, value, unit,
					ref_range_low: low, ref_range_high: high, flag,
				});
			}
		};

		// 2018-01-10 — Kaiser, LA
		seed(1, [
			['WBC', 4.9, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.61, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.8, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 42.5, '%', 41, 53, null],
			['MCV', 92.2, 'fL', 80, 100, null],
			['MCH', 32.1, 'pg', 27, 33, null],
			['MCHC', 34.8, 'g/dL', 32, 36, null],
			['RDW', 13.3, '%', 11.5, 14.5, null],
			['Platelets', 242, '10^3/uL', 150, 400, null],
			['Creatinine', 0.93, 'mg/dL', 0.74, 1.35, null],
			['eGFR', 107, 'mL/min/1.73m^2', 60, null, null],
			['Glucose', 96, 'mg/dL', 70, 99, null],
			['Bilirubin_Total', 0.7, 'mg/dL', 0.1, 1.2, null],
			['ALT', 22, 'U/L', 7, 56, null],
			['Cholesterol_Total', 193, 'mg/dL', null, 200, null],
			['Triglycerides', 97, 'mg/dL', null, 150, null],
			['HDL', 67, 'mg/dL', 40, null, null],
			['LDL', 107, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 2.9, 'ratio', null, 5.0, null],
		]);
		// 2019-04-04
		seed(2, [
			['WBC', 5.6, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.73, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 15.0, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 43.8, '%', 41, 53, null],
			['Platelets', 274, '10^3/uL', 150, 400, null],
			['Creatinine', 1.02, 'mg/dL', 0.74, 1.35, null],
			['Glucose', 89, 'mg/dL', 70, 99, null],
			['ALT', 19, 'U/L', 7, 56, null],
			['Vitamin_D', 12, 'ng/mL', 30, 100, 'L'],
			['HbA1c', 5.4, '%', null, 5.6, null],
			['Cholesterol_Total', 201, 'mg/dL', null, 200, 'H'],
			['Triglycerides', 98, 'mg/dL', null, 150, null],
			['HDL', 57, 'mg/dL', 40, null, null],
			['LDL', 124, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 3.5, 'ratio', null, 5.0, null],
		]);
		// 2021-06-23
		seed(3, [
			['WBC', 5.1, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.7, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.8, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 43.3, '%', 41, 53, null],
			['Platelets', 268, '10^3/uL', 150, 400, null],
			['Creatinine', 0.94, 'mg/dL', 0.74, 1.35, null],
			['Glucose', 90, 'mg/dL', 70, 99, null],
			['ALT', 16, 'U/L', 7, 56, null],
			['Vitamin_D', 14, 'ng/mL', 30, 100, 'L'],
			['HbA1c', 5.3, '%', null, 5.6, null],
			['Cholesterol_Total', 187, 'mg/dL', null, 200, null],
			['Triglycerides', 95, 'mg/dL', null, 150, null],
			['HDL', 53, 'mg/dL', 40, null, null],
			['LDL', 115, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 3.5, 'ratio', null, 5.0, null],
		]);
		// 2022-05-03
		seed(4, [
			['WBC', 5.4, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.59, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.5, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 41.4, '%', 41, 53, null],
			['Platelets', 268, '10^3/uL', 150, 400, null],
			['Creatinine', 1.00, 'mg/dL', 0.74, 1.35, null],
			['Glucose', 89, 'mg/dL', 70, 99, null],
			['ALT', 28, 'U/L', 7, 56, null],
			['Vitamin_D', 14, 'ng/mL', 30, 100, 'L'],
			['HbA1c', 5.3, '%', null, 5.6, null],
			['Cholesterol_Total', 224, 'mg/dL', null, 200, 'H'],
			['Triglycerides', 121, 'mg/dL', null, 150, null],
			['HDL', 55, 'mg/dL', 40, null, null],
			['LDL', 147, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 4.1, 'ratio', null, 5.0, null],
		]);
		// 2023-09-01
		seed(5, [
			['WBC', 4.4, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.54, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.2, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 42.6, '%', 41, 53, null],
			['Platelets', 250, '10^3/uL', 150, 400, null],
			['Sodium', 140, 'mmol/L', 135, 145, null],
			['Potassium', 4.1, 'mmol/L', 3.5, 5.0, null],
			['Chloride', 104, 'mmol/L', 96, 106, null],
			['CO2', 30, 'mmol/L', 22, 29, 'H'],
			['Creatinine', 1.12, 'mg/dL', 0.74, 1.35, null],
			['ALT', 18, 'U/L', 7, 56, null],
			['Vitamin_D', 11, 'ng/mL', 30, 100, 'L'],
			['HbA1c', 5.3, '%', null, 5.6, null],
			['Cholesterol_Total', 192, 'mg/dL', null, 200, null],
			['Triglycerides', 129, 'mg/dL', null, 150, null],
			['HDL', 56, 'mg/dL', 40, null, null],
			['LDL', 113, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 3.4, 'ratio', null, 5.0, null],
		]);
		// 2024-07-27
		seed(6, [
			['WBC', 6.4, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.73, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.5, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 43.5, '%', 41, 53, null],
			['Platelets', 278, '10^3/uL', 150, 400, null],
			['Sodium', 139, 'mmol/L', 135, 145, null],
			['Potassium', 4.4, 'mmol/L', 3.5, 5.0, null],
			['Chloride', 102, 'mmol/L', 96, 106, null],
			['CO2', 28, 'mmol/L', 22, 29, null],
			['Creatinine', 1.04, 'mg/dL', 0.74, 1.35, null],
			['ALT', 20, 'U/L', 7, 56, null],
			['HbA1c', 5.5, '%', null, 5.6, null],
			['Cholesterol_Total', 229, 'mg/dL', null, 200, 'H'],
			['Triglycerides', 165, 'mg/dL', null, 150, 'H'],
			['HDL', 51, 'mg/dL', 40, null, null],
			['LDL', 148, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 4.5, 'ratio', null, 5.0, null],
		]);
		// 2025-11-28
		seed(7, [
			['WBC', 6.1, '10^3/uL', 4.0, 11.0, null],
			['RBC', 4.66, '10^6/uL', 4.5, 5.9, null],
			['Hemoglobin', 14.6, 'g/dL', 13.5, 17.5, null],
			['Hematocrit', 42.9, '%', 41, 53, null],
			['Platelets', 272, '10^3/uL', 150, 400, null],
			['Sodium', 135, 'mmol/L', 135, 145, null],
			['Potassium', 4.3, 'mmol/L', 3.5, 5.0, null],
			['Chloride', 100, 'mmol/L', 96, 106, null],
			['CO2', 31, 'mmol/L', 22, 29, 'H'],
			['Creatinine', 0.99, 'mg/dL', 0.74, 1.35, null],
			['ALT', 24, 'U/L', 7, 56, null],
			['Vitamin_B12', 412, 'pg/mL', 200, 900, null],
			['Homocysteine', 11, 'umol/L', null, 10.4, 'H'],
			['HbA1c', 5.4, '%', null, 5.6, null],
			['Cholesterol_Total', 219, 'mg/dL', null, 200, 'H'],
			['Triglycerides', 90, 'mg/dL', null, 150, null],
			['HDL', 55, 'mg/dL', 40, null, null],
			['LDL', 148, 'mg/dL', null, 100, 'H'],
			['Cholesterol_HDL_Ratio', 4.0, 'ratio', null, 5.0, null],
		]);
		// 2026-04-22 — Quest, expanded panel
		seed(8, [
			['Cholesterol_Total', 188, 'mg/dL', null, 200, null],
			['LDL', 108, 'mg/dL', null, 100, 'H'],
			['HDL', 56, 'mg/dL', 40, null, null],
			['Triglycerides', 118, 'mg/dL', null, 150, null],
			['Cholesterol_HDL_Ratio', 3.4, 'ratio', null, 5.0, null],
			['TSH', 1.8, 'mIU/L', 0.4, 4.0, null],
			['Free_T3', 3.1, 'pg/mL', 2.3, 4.2, null],
			['Free_T4', 1.2, 'ng/dL', 0.8, 1.8, null],
			['Iron', 95, 'ug/dL', 65, 175, null],
			['Ferritin', 145, 'ng/mL', 30, 400, null],
			['Iron_Saturation', 32, '%', 20, 50, null],
			['Vitamin_D', 38, 'ng/mL', 30, 100, null],
			['Vitamin_B12', 528, 'pg/mL', 200, 900, null],
			['Homocysteine', 9.2, 'umol/L', null, 10.4, null],
			['HbA1c', 5.4, '%', null, 5.6, null],
		]);

		const panelDict = (p: Panel) => {
			const own = biomarkers.filter((b) => b.panel_id === p.id);
			const flagged = own.filter((b) => b.flag).length;
			return {
				id: p.id, drawn_at: p.drawn_at, lab_name: p.lab_name,
				panel_type: p.panel_type, biomarker_count: own.length,
				flagged_count: flagged, draft: p.draft, notes: p.notes,
				has_source: false, encounter_id: p.encounter_id,
			};
		};

		const latestByMetric = (): Record<string, Stat> => {
			const out: Record<string, Stat> = {};
			for (const s of stats) {
				const prev = out[s.metric];
				if (!prev || s.measured_at > prev.measured_at) out[s.metric] = s;
			}
			return out;
		};

		return ({ url, method, body }: { url: string; method: string; body?: any }) => {
			// /stats endpoints
			if (url.startsWith('/istota/api/health/stats/latest') && method === 'GET') {
				return { stats: latestByMetric() };
			}
			if (url.startsWith('/istota/api/health/stats/series') && method === 'GET') {
				const u = new URL(url, 'http://x');
				const metric = u.searchParams.get('metric') || '';
				const since = u.searchParams.get('since');
				const points = stats
					.filter((s) => s.metric === metric && (!since || s.measured_at >= since))
					.sort((a, b) => a.measured_at.localeCompare(b.measured_at))
					.map((s) => ({ measured_at: s.measured_at, value: s.value, unit: s.unit }));
				return { metric, points };
			}
			if (url.startsWith('/istota/api/health/stats') && method === 'GET') {
				const u = new URL(url, 'http://x');
				const metric = u.searchParams.get('metric');
				const since = u.searchParams.get('since');
				const limit = Number(u.searchParams.get('limit') || 200);
				let rows = [...stats];
				if (metric) rows = rows.filter((s) => s.metric === metric);
				if (since) rows = rows.filter((s) => s.measured_at >= since);
				rows.sort((a, b) => b.measured_at.localeCompare(a.measured_at));
				return { stats: rows.slice(0, limit) };
			}
			if (url === '/istota/api/health/stats' && method === 'POST') {
				const s: Stat = {
					id: nextStatId++,
					metric: body.metric,
					value: Number(body.value),
					unit: body.unit,
					measured_at: body.measured_at || new Date().toISOString(),
					source: body.source || 'manual',
					source_ref: null,
					notes: body.notes ?? null,
				};
				stats.push(s);
				return { status: 'ok', id: s.id };
			}
			const delStatMatch = url.match(/^\/istota\/api\/health\/stats\/(\d+)$/);
			if (delStatMatch && method === 'DELETE') {
				const id = Number(delStatMatch[1]);
				const idx = stats.findIndex((s) => s.id === id);
				if (idx >= 0) stats.splice(idx, 1);
				return { status: 'ok' };
			}

			// /panels endpoints
			if (url === '/istota/api/health/panels' && method === 'GET') {
				return { panels: panels.slice().sort((a, b) => b.drawn_at.localeCompare(a.drawn_at)).map(panelDict) };
			}
			if (url.startsWith('/istota/api/health/panels?') && method === 'GET') {
				return { panels: panels.slice().sort((a, b) => b.drawn_at.localeCompare(a.drawn_at)).map(panelDict) };
			}
			if (url === '/istota/api/health/panels' && method === 'POST') {
				if (body.encounter_id != null && !encounters.find((e) => e.id === body.encounter_id)) {
					return { error: 'encounter not found' };
				}
				const p: Panel = {
					id: panels.length + 1,
					drawn_at: body.drawn_at,
					lab_name: body.lab_name || null,
					panel_type: body.panel_type || null,
					source_file: null, source_mime: null, ocr_text: null,
					draft: false, notes: body.notes ?? null,
					encounter_id: body.encounter_id ?? null,
				};
				panels.push(p);
				return { status: 'ok', id: p.id };
			}
			const panelDetailMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)$/);
			if (panelDetailMatch) {
				const id = Number(panelDetailMatch[1]);
				const p = panels.find((x) => x.id === id);
				if (!p) return { error: 'not found' };
				if (method === 'GET') {
					return {
						panel: panelDict(p),
						biomarkers: biomarkers.filter((b) => b.panel_id === id),
						source: { available: false, mime: null },
					};
				}
				if (method === 'PUT') {
					if (typeof body.draft === 'boolean') p.draft = body.draft;
					if (body.lab_name !== undefined) p.lab_name = body.lab_name;
					if (body.panel_type !== undefined) p.panel_type = body.panel_type;
					if (body.notes !== undefined) p.notes = body.notes;
					if (body.encounter_id !== undefined) {
						if (body.encounter_id !== null && !encounters.find((e) => e.id === body.encounter_id)) {
							return { error: 'encounter not found' };
						}
						p.encounter_id = body.encounter_id;
					}
					return { status: 'ok' };
				}
				if (method === 'DELETE') {
					const idx = panels.findIndex((x) => x.id === id);
					if (idx >= 0) panels.splice(idx, 1);
					for (let i = biomarkers.length - 1; i >= 0; i--) {
						if (biomarkers[i].panel_id === id) biomarkers.splice(i, 1);
					}
					return { status: 'ok' };
				}
			}
			const bioMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)\/biomarkers$/);
			if (bioMatch) {
				const id = Number(bioMatch[1]);
				if (method === 'POST') {
					for (let i = biomarkers.length - 1; i >= 0; i--) {
						if (biomarkers[i].panel_id === id) biomarkers.splice(i, 1);
					}
					const incoming: any[] = body?.biomarkers || [];
					for (const b of incoming) {
						biomarkers.push({
							id: nextBioId++, panel_id: id, name: b.name,
							display_name: b.display_name ?? null,
							value: Number(b.value), unit: b.unit,
							ref_range_low: b.ref_range_low ?? null,
							ref_range_high: b.ref_range_high ?? null,
							flag: b.flag ?? null,
						});
					}
					if (body?.confirm) {
						const p = panels.find((x) => x.id === id);
						if (p) p.draft = false;
					}
					return { status: 'ok', count: incoming.length };
				}
				if (method === 'GET') {
					return { biomarkers: biomarkers.filter((b) => b.panel_id === id) };
				}
			}
			const extractMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)\/extract$/);
			if (extractMatch && method === 'POST') {
				return {
					biomarkers: [
						{ name: 'WBC', value: 7.4, unit: '10^3/uL', ref_range_low: 4.0, ref_range_high: 11.0, flag: null },
						{ name: 'Hemoglobin', value: 15.0, unit: 'g/dL', ref_range_low: 13.5, ref_range_high: 17.5, flag: null },
						{ name: 'LDL', value: 112, unit: 'mg/dL', ref_range_low: null, ref_range_high: 100, flag: 'H' },
						{ name: 'HDL', value: 54, unit: 'mg/dL', ref_range_low: 40, ref_range_high: null, flag: null },
					],
					drawn_at: '2025-11-28',
					lab_name: 'Kaiser',
					panel_type: 'CBC + Lipid Panel',
					warnings: [],
					raw_text: 'Mock OCR text — replace with real extraction output.',
				};
			}

			if (url === '/istota/api/health/csv/import' && method === 'POST') {
					return {
						status: 'ok',
						panels_created: 2,
						panels_replaced: 0,
						panels_skipped: 0,
						biomarkers_created: 8,
						rows_processed: 2,
						warnings: [],
					};
				}

				// /biomarkers endpoints
				if (url.startsWith('/istota/api/health/biomarkers/trend') && method === 'GET') {
				const u = new URL(url, 'http://x');
				const name = u.searchParams.get('name') || '';
				// Match by canonical name OR by alias.
				const ref = BIOMARKER_REFS.find((r) => r.name === name)
					|| BIOMARKER_REFS.find((r) => (r.aliases || []).some((a) => a.toLowerCase() === name.toLowerCase()));
				const canonical = ref?.name || name;
				const matches = biomarkers
					.filter((b) => b.name === canonical)
					.map((b) => {
						const p = panels.find((x) => x.id === b.panel_id);
						return { drawn_at: p?.drawn_at || '', value: b.value, unit: b.unit, flag: b.flag };
					})
					.filter((x) => Boolean(x.drawn_at) && panels.find((p) => p.drawn_at === x.drawn_at && !p.draft))
					.sort((a, b) => a.drawn_at.localeCompare(b.drawn_at));
				// Use sex-specific male range if present, else unisex, else widest.
				const lowM = ref?.ref_range_low_m ?? null;
				const highM = ref?.ref_range_high_m ?? null;
				const low = lowM ?? ref?.ref_range_low ?? null;
				const high = highM ?? ref?.ref_range_high ?? null;
				return {
					name: canonical,
					display_name: ref?.display_name || canonical,
					points: matches,
					unit_mismatch: false,
					ref_range_low: low,
					ref_range_high: high,
					unit: ref?.default_unit ?? null,
				};
			}
			if (url === '/istota/api/health/biomarkers/summary' && method === 'GET') {
				const byName = new Map<string, Bio[]>();
				for (const b of biomarkers) {
					const arr = byName.get(b.name) || [];
					arr.push(b);
					byName.set(b.name, arr);
				}
				const summary: any[] = [];
				for (const [name, items] of byName.entries()) {
					items.sort((a, b) => {
						const pa = panels.find((p) => p.id === a.panel_id);
						const pb = panels.find((p) => p.id === b.panel_id);
						return (pa?.drawn_at || '').localeCompare(pb?.drawn_at || '');
					});
					const latestBio = items[items.length - 1];
					const previousBio = items.length > 1 ? items[items.length - 2] : null;
					const drawn = (b: Bio) => panels.find((p) => p.id === b.panel_id)?.drawn_at || '';
					const dir =
						previousBio && latestBio.value > previousBio.value * 1.01
							? 'up'
							: previousBio && latestBio.value < previousBio.value * 0.99
								? 'down'
								: 'flat';
					summary.push({
						name,
						latest: { drawn_at: drawn(latestBio), value: latestBio.value, unit: latestBio.unit, flag: latestBio.flag },
						previous: previousBio
							? { drawn_at: drawn(previousBio), value: previousBio.value, unit: previousBio.unit, flag: previousBio.flag }
							: null,
						direction: dir,
						sample_count: items.length,
					});
				}
				summary.sort((a, b) => a.name.localeCompare(b.name));
				return { summary };
			}
			if (url === '/istota/api/health/biomarkers/refs' && method === 'GET') {
				return { refs: BIOMARKER_REFS };
			}

			// Biomarker out-of-range explainer.
			const explainerMatch = url.match(
				/^\/istota\/api\/health\/biomarkers\/([^/]+)\/explainer(?:\?(.*))?$/,
			);
			if (explainerMatch && method === 'GET') {
				const requestedName = decodeURIComponent(explainerMatch[1]);
				const params = new URLSearchParams(explainerMatch[2] || '');
				const direction = params.get('direction');
				if (direction !== 'high' && direction !== 'low') {
					return { error: "direction must be 'high' or 'low'" };
				}
				// Canonicalise via the loaded refs (handles alias lookups).
				const ref = BIOMARKER_REFS.find((r) => r.name === requestedName)
					|| BIOMARKER_REFS.find((r) => (r.aliases || []).some(
						(a) => a.toLowerCase() === requestedName.toLowerCase(),
					));
				const canonical = ref?.name || requestedName;
				const displayName = ref?.display_name || canonical;

				const STUBS: Record<string, { summary: string; causes: string[]; mitigations: string[] }> = {
					'CO2:high': {
						summary:
							'Elevated CO2 (bicarbonate) can reflect a shift in acid-base balance. A single high reading is rarely meaningful on its own — context, hydration, and trends matter.',
						causes: [
							'Mild dehydration or volume contraction may raise bicarbonate transiently.',
							'Chronic diuretic use is commonly associated with elevated CO2.',
							'Persistent vomiting or low potassium can drive metabolic alkalosis.',
							'Compensatory response to chronic respiratory conditions may show up here.',
							'Antacid-heavy regimens (calcium carbonate, baking soda) can nudge values up.',
						],
						mitigations: [
							'Consider a repeat test in 2–4 weeks to confirm the trend.',
							'Review hydration status — measure intake and salt loss over recent weeks.',
							'Discuss any diuretics, PPIs, or alkali supplements with your prescriber.',
							'Bring electrolyte panel context (Na, K, Cl) to your clinician for interpretation.',
						],
					},
					'Vitamin_D:low': {
						summary:
							'Low 25-OH Vitamin D is common, especially in northern latitudes and during winter. It plays roles in bone, immune, and muscle health.',
						causes: [
							'Limited sun exposure or sunscreen use, especially in winter months.',
							'Darker skin can be associated with lower endogenous synthesis at the same exposure.',
							'Malabsorption (celiac, IBD, gastric bypass) reduces dietary uptake.',
							'Obesity may sequester vitamin D in adipose tissue and lower serum levels.',
							'Some medications (corticosteroids, anticonvulsants) accelerate breakdown.',
						],
						mitigations: [
							'Discuss whether a vitamin D supplement is appropriate with your clinician.',
							'Increase dietary sources (fatty fish, fortified dairy, egg yolks) gradually.',
							'Aim for safe, regular sun exposure — minutes vary by skin tone and season.',
							'Retest in 8–12 weeks after any intervention to gauge response.',
						],
					},
					'LDL:high': {
						summary:
							'Elevated LDL cholesterol is the strongest routine lipid contributor to atherosclerotic cardiovascular risk. Targets depend on overall risk profile, not a single value.',
						causes: [
							'Diet high in saturated fat, refined carbohydrates, or trans fats.',
							'Genetic factors (familial hypercholesterolemia is more common than appreciated).',
							'Hypothyroidism can raise LDL noticeably.',
							'Sedentary lifestyle and excess body weight are associated with higher LDL.',
							'Some medications (corticosteroids, beta blockers, retinoids) can elevate it.',
						],
						mitigations: [
							'Discuss overall cardiovascular risk with your clinician — not just the LDL number.',
							'Consider dietary changes emphasising fiber, fish, nuts, and unsaturated fats.',
							'Review activity levels and sleep — both move LDL over time.',
							'If recent results have trended up, ask about thyroid and family-history workup.',
						],
					},
					'Cholesterol_Total:high': {
						summary:
							'Total cholesterol above ~200 mg/dL is a coarse signal — the breakdown into LDL, HDL, and triglycerides gives a far better picture of cardiovascular risk.',
						causes: [
							'Genetics often dominate, especially when LDL is the bulk of the elevation.',
							'Diet quality (saturated and trans fats) shifts total cholesterol over weeks.',
							'High HDL can elevate total cholesterol without raising risk.',
							'Hypothyroidism and kidney disease are commonly associated.',
							'Pregnancy and the post-partum period can transiently raise total cholesterol.',
						],
						mitigations: [
							'Look at LDL, HDL, and triglycerides separately rather than total alone.',
							'Discuss whether a calculated risk score is appropriate for the next visit.',
							'Repeat fasted if the prior test was non-fasting.',
							'Review lifestyle changes incrementally rather than chasing a single number.',
						],
					},
					'Triglycerides:high': {
						summary:
							'Elevated triglycerides are sensitive to recent meals, alcohol, and refined carbohydrates — a single non-fasted value is often not informative on its own.',
						causes: [
							'Non-fasting samples can read 30–50% higher than fasted.',
							'Recent heavy alcohol intake elevates triglycerides for days.',
							'Diets high in refined carbohydrates and fructose drive triglyceride production.',
							'Uncontrolled diabetes and insulin resistance commonly raise triglycerides.',
							'Some medications (estrogens, retinoids, beta blockers, thiazides) are associated.',
						],
						mitigations: [
							'Re-test fasted (9+ hours, water only) before drawing conclusions.',
							'Discuss alcohol patterns over the past week with your clinician.',
							'Consider reducing refined-carb intake and increasing fiber.',
							'Bring HbA1c context if insulin resistance is a known concern.',
						],
					},
					'Homocysteine:high': {
						summary:
							'Elevated homocysteine is associated with cardiovascular and cognitive risk over time. It often responds well to specific B-vitamin support.',
						causes: [
							'B12, folate, or B6 deficiency is the most common driver.',
							'MTHFR polymorphisms can reduce homocysteine clearance.',
							'Kidney disease impairs homocysteine excretion.',
							'Hypothyroidism is commonly associated.',
							'Some medications (methotrexate, anticonvulsants, metformin) can raise it.',
						],
						mitigations: [
							'Discuss B12, folate, and B6 levels with your clinician before supplementing blindly.',
							'Consider methylated B-vitamin forms if MTHFR variants are suspected.',
							'Review thyroid and kidney panels for context.',
							'Re-test in 8–12 weeks after any intervention to gauge response.',
						],
					},
					'Sodium:low': {
						summary:
							'Mildly low sodium (hyponatremia) typically reflects water balance rather than salt intake — the body is holding more water than usual relative to electrolytes.',
						causes: [
							'Over-hydration (especially in endurance athletes) can dilute serum sodium.',
							'Thiazide diuretics commonly cause mild hyponatremia.',
							'SIADH (a hormone signaling issue) keeps water in the body.',
							'Heart, kidney, or liver dysfunction can shift fluid balance.',
							'Severe vomiting or diarrhea, paired with plain-water replacement, can lower sodium.',
						],
						mitigations: [
							'Avoid drinking large volumes of plain water on the day of testing.',
							'Discuss any diuretic, SSRI, or chemotherapy use with your prescriber.',
							'Bring osmolality and a urine sodium to the next visit if hyponatremia persists.',
							'Repeat the test — single mildly-low values are common with no clinical relevance.',
						],
					},
				};

				const key = `${canonical}:${direction}`;
				const stub = STUBS[key];
				if (stub) {
					return {
						name: canonical,
						display_name: displayName,
						direction,
						summary: stub.summary,
						causes: stub.causes,
						mitigations: stub.mitigations,
						disclaimer:
							'Educational information only — not medical advice or diagnosis. Discuss your results with a healthcare professional before acting on them.',
						source: 'cache',
						generated_at: new Date(Date.now() - 86400_000).toISOString(),
					};
				}
				// Generic fallback for any other biomarker.
				return {
					name: canonical,
					display_name: displayName,
					direction,
					summary: `A ${direction} ${displayName} reading sits outside the typical reference range. A single value isn't enough to draw conclusions — trends, recent context, and clinical correlation matter.`,
					causes: [
						'Recent illness, dehydration, or stress can shift values temporarily.',
						'Medications, supplements, and recent meals can move many markers.',
						'Inter-lab and inter-assay variability is real; repeat testing helps confirm.',
					],
					mitigations: [
						'Discuss the result with your healthcare provider before acting on it.',
						'Consider a repeat test in a few weeks to confirm the trend.',
						'Review recent changes in medication, diet, and lifestyle for context.',
					],
					disclaimer:
						'Educational information only — not medical advice or diagnosis. Discuss your results with a healthcare professional before acting on them.',
					source: 'fallback',
					generated_at: null,
				};
			}

			// Spreadsheet matrix: confirmed panels × every biomarker, grouped by category.
			if (url === '/istota/api/health/bloodwork/matrix' && method === 'GET') {
				const confirmed = panels
					.filter((p) => !p.draft)
					.sort((a, b) => a.drawn_at.localeCompare(b.drawn_at));
				const seenMarker: Record<string, { unit: string }> = {};
				const values: Record<string, Record<string, { value: number; unit: string; flag: string | null }>> = {};
				for (const p of confirmed) {
					values[String(p.id)] = {};
					for (const b of biomarkers.filter((b) => b.panel_id === p.id)) {
						if (!seenMarker[b.name]) seenMarker[b.name] = { unit: b.unit };
						values[String(p.id)][b.name] = { value: b.value, unit: b.unit, flag: b.flag };
					}
				}
				const refByName = new Map(BIOMARKER_REFS.map((r) => [r.name, r] as const));
				const widestRange = (r: typeof BIOMARKER_REFS[number]) => {
					const lows: number[] = [];
					const highs: number[] = [];
					if (r.ref_range_low != null) lows.push(r.ref_range_low);
					if (r.ref_range_low_m != null) lows.push(r.ref_range_low_m);
					if (r.ref_range_low_f != null) lows.push(r.ref_range_low_f);
					if (r.ref_range_high != null) highs.push(r.ref_range_high);
					if (r.ref_range_high_m != null) highs.push(r.ref_range_high_m);
					if (r.ref_range_high_f != null) highs.push(r.ref_range_high_f);
					return [
						lows.length ? Math.min(...lows) : null,
						highs.length ? Math.max(...highs) : null,
					] as const;
				};
				const catOrder: string[] = [];
				const catMarkers: Record<string, any[]> = {};
				for (const r of BIOMARKER_REFS) {
					if (!catMarkers[r.category]) {
						catOrder.push(r.category);
						catMarkers[r.category] = [];
					}
				}
				for (const name of Object.keys(seenMarker)) {
					const r = refByName.get(name);
					const cat = r?.category || 'Other';
					if (!catMarkers[cat]) {
						catOrder.push(cat);
						catMarkers[cat] = [];
					}
					let low: number | null = null, high: number | null = null;
					if (r) [low, high] = widestRange(r);
					catMarkers[cat].push({
						name,
						display_name: r?.display_name || name,
						unit: r?.default_unit || seenMarker[name].unit,
						ref_range_low: low,
						ref_range_high: high,
						category: cat,
					});
				}
				const orderedCats = catOrder
					.filter((c) => catMarkers[c]?.length)
					.map((c) => ({
						name: c,
						markers: [...catMarkers[c]].sort((a, b) => a.display_name.localeCompare(b.display_name)),
					}));
				return {
					categories: orderedCats,
					panels: confirmed.map((p) => ({
						id: p.id,
						drawn_at: p.drawn_at,
						lab_name: p.lab_name,
						panel_type: p.panel_type,
					})),
					values,
				};
			}

			// /settings endpoints
			if (url === '/istota/api/health/settings' && method === 'GET') {
				return { settings };
			}
			if (url === '/istota/api/health/settings' && method === 'PUT') {
				if (body && typeof body === 'object') {
					if ('dob' in body) settings.dob = body.dob;
					if ('height_cm' in body) settings.height_cm = body.height_cm;
					if ('sex' in body) settings.sex = body.sex;
					if (body.display_units) {
						settings.display_units = { ...settings.display_units, ...body.display_units };
					}
				}
				return { status: 'ok', settings };
			}

			// /garmin/* — keep state in this closure so connect/MFA/disconnect/sync
			// all see consistent connection status across calls.
			if (url === '/istota/api/health/garmin/status' && method === 'GET') {
				return {
					connected: garmin.connected,
					email: garmin.connected ? garmin.email : null,
					last_sync: garmin.connected ? garmin.last_sync : null,
					error: garmin.error,
				};
			}
			if (url === '/istota/api/health/garmin/connect' && method === 'POST') {
				if (!body || typeof body !== 'object' || !body.email || !body.password) {
					return { status: 'error', error: 'email and password are required' };
				}
				// MFA branch: any email containing "+mfa" triggers the MFA flow.
				if (typeof body.email === 'string' && body.email.includes('+mfa')) {
					garmin.pendingEmail = body.email;
					return { status: 'mfa_required', prompt: 'Enter Garmin MFA code (mock: 123456)' };
				}
				// Bad-credentials branch: emails containing "+bad" fail.
				if (typeof body.email === 'string' && body.email.includes('+bad')) {
					return { status: 'error', error: 'invalid credentials' };
				}
				garmin.connected = true;
				garmin.email = body.email;
				garmin.last_sync = null;
				garmin.error = null;
				return { status: 'ok' };
			}
			if (url === '/istota/api/health/garmin/mfa' && method === 'POST') {
				if (!body || typeof body !== 'object' || typeof body.code !== 'string') {
					return { status: 'error', error: 'code is required' };
				}
				if (!garmin.pendingEmail) {
					return { status: 'error', error: 'no pending Garmin auth — restart from /garmin/connect' };
				}
				if (body.code !== '123456') {
					return { status: 'error', error: 'invalid MFA code' };
				}
				garmin.connected = true;
				garmin.email = garmin.pendingEmail;
				garmin.last_sync = null;
				garmin.error = null;
				garmin.pendingEmail = null;
				return { status: 'ok' };
			}
			if (url === '/istota/api/health/garmin/disconnect' && method === 'POST') {
				garmin.connected = false;
				garmin.email = null;
				garmin.last_sync = null;
				garmin.error = null;
				garmin.pendingEmail = null;
				return { status: 'ok' };
			}
			if (url === '/istota/api/health/garmin/sync' && method === 'POST') {
				if (!garmin.connected) {
					return {
						inserted: 0, skipped: 0, errored: 0,
						days_processed: 0,
						errors: ['no Garmin tokens — connect via /garmin/connect'],
						auth_error: true,
					};
				}
				const daysBack = Math.max(
					1, Math.min(90, Number(body?.days_back) || 7),
				);
				garmin.last_sync = new Date().toISOString();
				const inserted = Math.max(0, 5 * daysBack - 2);
				return {
					inserted,
					skipped: 2,
					errored: 0,
					days_processed: daysBack,
					errors: [],
					auth_error: false,
				};
			}

			// /encounters
			if (url === '/istota/api/health/encounters/extract' && method === 'POST') {
				// Dev fixture: pretend the LLM extracted a single visit from the
				// uploaded paperwork. Real backend route runs OCR + brain call.
				return {
					mode: 'vision',
					rows: [
						{
							encounter_date: '2026-04-14',
							encounter_type: 'visit',
							provider: 'Dr. Jane Smith, MD',
							facility: 'Kaiser Permanente — Sunset',
							specialty: 'primary care',
							reason: 'Annual physical',
							notes:
								'BP and labs normal. Recommended continuing current exercise routine; follow up in 12 months unless symptomatic.',
							diagnoses: [
								{
									name: 'Essential hypertension, well controlled',
									icd10: 'I10',
									status: 'chronic',
									severity: 'mild',
								},
							],
							confidence: 'high',
						},
					],
					warnings: [
						'Mock extraction (dev mode) — the real LLM runs against the uploaded file.',
					],
				};
			}
			if (url === '/istota/api/health/encounters/bulk' && method === 'POST') {
				if (!body || !Array.isArray(body.rows)) return { error: 'rows must be a list' };
				const encIds: number[] = [];
				const diagIds: number[] = [];
				for (let i = 0; i < body.rows.length; i++) {
					const r = body.rows[i];
					if (!r.encounter_date || !r.encounter_type) {
						return { error: `row ${i} missing fields` };
					}
					const enc: Encounter = {
						id: nextEncounterId++,
						encounter_date: String(r.encounter_date),
						encounter_type: String(r.encounter_type),
						provider: r.provider || null,
						facility: r.facility || null,
						specialty: r.specialty || null,
						reason: r.reason || null,
						notes: r.notes || null,
						created_at: new Date().toISOString(),
					};
					encounters.push(enc);
					encIds.push(enc.id);
					for (const d of r.diagnoses || []) {
						if (!d || !d.name) continue;
						const dx: Diagnosis = {
							id: nextDiagnosisId++,
							name: String(d.name),
							icd10: d.icd10 || null,
							status: (d.status as Diagnosis['status']) || 'active',
							date_diagnosed: enc.encounter_date,
							date_resolved: null,
							encounter_id: enc.id,
							severity: (d.severity as Diagnosis['severity']) || null,
							notes: null,
							created_at: new Date().toISOString(),
						};
						diagnoses.push(dx);
						diagIds.push(dx.id);
					}
				}
				return { status: 'ok', ids: encIds, count: encIds.length, diagnosis_ids: diagIds };
			}
			if (url.startsWith('/istota/api/health/encounters') && method === 'GET') {
				const encMatch = url.match(/^\/istota\/api\/health\/encounters\/(\d+)$/);
				if (encMatch) {
					const id = Number(encMatch[1]);
					const enc = encounters.find((e) => e.id === id);
					if (!enc) return { error: 'encounter not found' };
					const linkedDiag = diagnoses.filter((d) => d.encounter_id === id);
					const linkedPanels = panels
						.filter((p) => p.encounter_id === id)
						.slice()
						.sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
						.map(panelDict);
					return { encounter: enc, diagnoses: linkedDiag, panels: linkedPanels };
				}
				const u = new URL(url, 'http://x');
				const since = u.searchParams.get('since');
				const until = u.searchParams.get('until');
				const t = u.searchParams.get('type');
				let rows = [...encounters];
				if (since) rows = rows.filter((e) => e.encounter_date >= since);
				if (until) rows = rows.filter((e) => e.encounter_date <= until);
				if (t) rows = rows.filter((e) => e.encounter_type === t);
				rows.sort(
					(a, b) =>
						b.encounter_date.localeCompare(a.encounter_date) || b.id - a.id,
				);
				return { encounters: rows };
			}
			if (url === '/istota/api/health/encounters' && method === 'POST') {
				if (!body || typeof body !== 'object') return { error: 'bad body' };
				if (!body.encounter_date || !body.encounter_type) {
					return { error: 'encounter_date and encounter_type are required' };
				}
				const enc: Encounter = {
					id: nextEncounterId++,
					encounter_date: body.encounter_date,
					encounter_type: body.encounter_type,
					provider: body.provider || null,
					facility: body.facility || null,
					specialty: body.specialty || null,
					reason: body.reason || null,
					notes: body.notes || null,
					created_at: new Date().toISOString(),
				};
				encounters.push(enc);
				return { status: 'ok', id: enc.id };
			}
			const encUpdMatch = url.match(/^\/istota\/api\/health\/encounters\/(\d+)$/);
			if (encUpdMatch && method === 'PUT') {
				const id = Number(encUpdMatch[1]);
				const enc = encounters.find((e) => e.id === id);
				if (!enc) return { error: 'encounter not found' };
				const allowed = ['encounter_date', 'encounter_type', 'provider', 'facility', 'specialty', 'reason', 'notes'];
				for (const k of allowed) {
					if (body && k in body && body[k] !== undefined) (enc as any)[k] = body[k];
				}
				return { status: 'ok' };
			}
			if (encUpdMatch && method === 'DELETE') {
				const id = Number(encUpdMatch[1]);
				const idx = encounters.findIndex((e) => e.id === id);
				if (idx < 0) return { error: 'encounter not found' };
				encounters.splice(idx, 1);
				// Mirror ON DELETE SET NULL on diagnoses.encounter_id + panels.encounter_id.
				for (const d of diagnoses) {
					if (d.encounter_id === id) d.encounter_id = null;
				}
				for (const p of panels) {
					if (p.encounter_id === id) p.encounter_id = null;
				}
				return { status: 'ok' };
			}

			// /diagnoses
			if (url.startsWith('/istota/api/health/diagnoses') && method === 'GET') {
				const diagMatch = url.match(/^\/istota\/api\/health\/diagnoses\/(\d+)$/);
				if (diagMatch) {
					const id = Number(diagMatch[1]);
					const d = diagnoses.find((x) => x.id === id);
					if (!d) return { error: 'diagnosis not found' };
					const enc = d.encounter_id
						? encounters.find((e) => e.id === d.encounter_id) || null
						: null;
					return { diagnosis: d, encounter: enc };
				}
				const u = new URL(url, 'http://x');
				const status = u.searchParams.get('status');
				let rows = [...diagnoses];
				if (status && status !== 'all') rows = rows.filter((d) => d.status === status);
				const statusOrder = { active: 0, chronic: 1, resolved: 2 } as const;
				rows.sort((a, b) => {
					const sa = statusOrder[a.status] ?? 3;
					const sb = statusOrder[b.status] ?? 3;
					if (sa !== sb) return sa - sb;
					return (b.date_diagnosed || '').localeCompare(a.date_diagnosed || '');
				});
				return { diagnoses: rows };
			}
			if (url === '/istota/api/health/diagnoses' && method === 'POST') {
				if (!body || typeof body !== 'object' || !body.name) {
					return { error: 'name is required' };
				}
				const status = body.status || 'active';
				if (!['active', 'resolved', 'chronic'].includes(status)) {
					return { error: 'unknown status' };
				}
				if (body.encounter_id != null && !encounters.find((e) => e.id === body.encounter_id)) {
					return { error: 'encounter not found' };
				}
				const d: Diagnosis = {
					id: nextDiagnosisId++,
					name: String(body.name),
					icd10: body.icd10 || null,
					status,
					date_diagnosed: body.date_diagnosed || null,
					date_resolved: body.date_resolved || null,
					encounter_id: body.encounter_id ?? null,
					severity: body.severity || null,
					notes: body.notes || null,
					created_at: new Date().toISOString(),
				};
				diagnoses.push(d);
				return { status: 'ok', id: d.id };
			}
			const diagUpdMatch = url.match(/^\/istota\/api\/health\/diagnoses\/(\d+)$/);
			if (diagUpdMatch && method === 'PUT') {
				const id = Number(diagUpdMatch[1]);
				const d = diagnoses.find((x) => x.id === id);
				if (!d) return { error: 'diagnosis not found' };
				const allowed = ['name', 'icd10', 'status', 'date_diagnosed', 'date_resolved', 'encounter_id', 'severity', 'notes'];
				for (const k of allowed) {
					if (body && k in body) (d as any)[k] = body[k];
				}
				return { status: 'ok' };
			}
			if (diagUpdMatch && method === 'DELETE') {
				const id = Number(diagUpdMatch[1]);
				const idx = diagnoses.findIndex((x) => x.id === id);
				if (idx < 0) return { error: 'diagnosis not found' };
				diagnoses.splice(idx, 1);
				return { status: 'ok' };
			}

			// /history/summary
			if (url === '/istota/api/health/history/summary' && method === 'GET') {
				const oneYearAgo = new Date(Date.now() - 365 * 86400 * 1000)
					.toISOString()
					.slice(0, 10);
				const fiveYearsAgo = new Date(Date.now() - 5 * 365 * 86400 * 1000)
					.toISOString()
					.slice(0, 10);
				const active = diagnoses.filter((d) => d.status === 'active');
				const chronic = diagnoses.filter((d) => d.status === 'chronic');
				const recent = encounters
					.filter((e) => e.encounter_date >= oneYearAgo)
					.sort((a, b) => b.encounter_date.localeCompare(a.encounter_date));
				const procs = encounters
					.filter((e) => e.encounter_type === 'procedure' && e.encounter_date >= fiveYearsAgo)
					.sort((a, b) => b.encounter_date.localeCompare(a.encounter_date))
					.slice(0, 5);
				return {
					active_diagnoses: active,
					chronic_diagnoses: chronic,
					recent_encounters: recent,
					recent_procedures: procs,
				};
			}

			// /dashboard
			if (url === '/istota/api/health/dashboard' && method === 'GET') {
				const latest = latestByMetric();
				const recent = panels
					.filter((p) => !p.draft)
					.sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
					.slice(0, 3)
					.map(panelDict);
				const flagged: any[] = [];
				const seen = new Set<string>();
				const sortedPanels = [...panels].sort((a, b) => b.drawn_at.localeCompare(a.drawn_at));
				for (const p of sortedPanels) {
					if (p.draft) continue;
					for (const b of biomarkers.filter((b) => b.panel_id === p.id && b.flag)) {
						if (seen.has(b.name)) continue;
						seen.add(b.name);
						flagged.push({ ...b, panel_id: p.id, drawn_at: p.drawn_at, lab_name: p.lab_name });
					}
				}
				const weight = latest['weight'];
				const bmi =
					weight && settings.height_cm
						? Math.round((weight.value / Math.pow(settings.height_cm / 100, 2)) * 100) / 100
						: null;
				const activeDiagCount =
					diagnoses.filter((d) => d.status === 'active').length +
					diagnoses.filter((d) => d.status === 'chronic').length;
				const recentEncounters = [...encounters]
					.sort(
						(a, b) =>
							b.encounter_date.localeCompare(a.encounter_date) || b.id - a.id,
					)
					.slice(0, 3);
				return {
					latest_stats: latest,
					bmi,
					recent_panels: recent,
					alerts: flagged.slice(0, 20),
					settings,
					active_diagnoses_count: activeDiagCount,
					recent_encounters: recentEncounters,
				};
			}


				// ---- Immunizations ---------------------------------------------
				function _parseDateUS(raw: string): string | null {
					const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
					if (iso) return raw;
					const m = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
					if (!m) return null;
					let [, mo, dy, yr] = m;
					if (yr.length === 2) yr = parseInt(yr, 10) < 70 ? `20${yr}` : `19${yr}`;
					const d = new Date(`${yr}-${mo.padStart(2,'0')}-${dy.padStart(2,'0')}T00:00:00Z`);
					if (isNaN(d.getTime())) return null;
					return d.toISOString().slice(0, 10);
				}

				function _resolveFamily(product: string): { name: string; ref?: typeof IMMUNIZATION_REFS[number] } {
					const p = product.toLowerCase();
					const candidates: Array<{ alias: string; ref: typeof IMMUNIZATION_REFS[number] }> = [];
					for (const r of IMMUNIZATION_REFS) {
						candidates.push({ alias: r.name.toLowerCase(), ref: r });
						for (const a of r.aliases || []) candidates.push({ alias: a.toLowerCase(), ref: r });
					}
					candidates.sort((a, b) => b.alias.length - a.alias.length);
					for (const c of candidates) {
						const idx = p.indexOf(c.alias);
						if (idx === -1) continue;
						const before = idx > 0 ? p[idx - 1] : ' ';
						const after = idx + c.alias.length < p.length ? p[idx + c.alias.length] : ' ';
						if (!/[a-z0-9]/.test(before) && !/[a-z0-9]/.test(after)) {
							return { name: c.ref.name, ref: c.ref };
						}
					}
					return { name: 'Unknown' };
				}

				function _parsePaste(text: string) {
					const out: any[] = [];
					for (const rawLine of text.split('\n')) {
						const line = rawLine.trim();
						if (!line) continue;
						let product: string | null = null;
						let date: string | null = null;
						let confidence: string = 'low';
						let m = line.match(/^(.+?)\s*\(\s*Given\s+(\d{1,2}\/\d{1,2}\/\d{2,4})\s*\)\s*$/i);
						if (m) {
							product = m[1].trim();
							date = _parseDateUS(m[2]);
							confidence = date ? 'high' : 'medium';
						} else {
							m = line.match(/^(.+?)\s+(\d{4}-\d{2}-\d{2})\s*$/);
							if (m) {
								product = m[1].trim();
								date = _parseDateUS(m[2]);
								confidence = date ? 'high' : 'medium';
							} else {
								product = line;
								confidence = 'manual';
							}
						}
						const resolved = _resolveFamily(product || '');
						out.push({
							name: resolved.name,
							product_name: product || null,
							date_given: date,
							source_line: line,
							confidence,
							notes: resolved.name === 'Unknown' ? line : null,
						});
					}
					return out;
				}

				function _computeCoverage() {
					const today = new Date();
					const todayMs = today.getTime();
					const out = IMMUNIZATION_REFS.map((ref) => {
						const matching = immunizations.filter((r) => r.name === ref.name);
						const dates = matching.map((r) => r.date_given).filter(Boolean).sort();
						const lastGiven = dates.length ? dates[dates.length - 1] : null;
						const doseCount = matching.length;
						let status = 'never_recorded';
						let nextDue: string | null = null;
						let isOverdue = false;
						let daysUntilDue: number | null = null;
						const lastDate = lastGiven ? new Date(lastGiven + 'T00:00:00Z') : null;
						if (ref.schedule === 'risk_based') {
							status = doseCount === 0 ? 'risk_based' : 'up_to_date';
						} else if (ref.schedule === 'annual' || ref.schedule === 'every_10y') {
							if (!lastDate) {
								status = 'never_recorded';
							} else {
								const interval = ref.interval_days ?? (ref.schedule === 'annual' ? 365 : 3650);
								const due = new Date(lastDate.getTime() + interval * 86400 * 1000);
								nextDue = due.toISOString().slice(0, 10);
								const delta = Math.floor((due.getTime() - todayMs) / 86400 / 1000);
								daysUntilDue = delta;
								if (delta < 0) { status = 'overdue'; isOverdue = true; }
								else if (delta <= 30) status = 'due_soon';
								else status = 'up_to_date';
							}
						} else if (ref.schedule === 'lifetime_after_series' || ref.schedule === 'series_then_booster') {
							const required = ref.primary_series_doses ?? 1;
							if (doseCount === 0) status = 'series_incomplete';
							else if (doseCount >= required) status = 'up_to_date';
							else status = 'series_incomplete';
						} else if (ref.schedule === 'travel_pre_trip') {
							if (!lastDate) status = 'never_recorded';
							else {
								const interval = ref.interval_days ?? 365;
								const due = new Date(lastDate.getTime() + interval * 86400 * 1000);
								nextDue = due.toISOString().slice(0, 10);
								const delta = Math.floor((due.getTime() - todayMs) / 86400 / 1000);
								daysUntilDue = delta;
								if (delta < 0) { status = 'expired'; isOverdue = true; }
								else status = 'up_to_date';
							}
						} else {
							status = doseCount > 0 ? 'up_to_date' : 'never_recorded';
						}
						return {
							name: ref.name,
							display_name: ref.display_name,
							category: ref.category,
							status,
							last_given: lastGiven,
							dose_count: doseCount,
							next_due: nextDue,
							is_overdue: isOverdue,
							days_until_due: daysUntilDue,
						};
					});
					const canonical = new Set(IMMUNIZATION_REFS.map((r) => r.name));
					const otherMap = new Map<string, any[]>();
					for (const r of immunizations) {
						if (canonical.has(r.name)) continue;
						if (!otherMap.has(r.name)) otherMap.set(r.name, []);
						otherMap.get(r.name)!.push(r);
					}
					const other = [];
					for (const [name, group] of otherMap) {
						other.push({
							name, display_name: name, category: 'other',
							status: 'recorded',
							last_given: group.map((r) => r.date_given).sort().pop() || null,
							dose_count: group.length,
							next_due: null, is_overdue: false, days_until_due: null,
						});
					}
					return { coverage: out, other };
				}

				if (url === '/istota/api/health/immunizations/refs' && method === 'GET') {
					return { refs: IMMUNIZATION_REFS };
				}
				if (url === '/istota/api/health/immunizations/coverage' && method === 'GET') {
					return _computeCoverage();
				}
				if (url === '/istota/api/health/immunizations/parse' && method === 'POST') {
					if (!body || typeof body.text !== 'string') return { error: 'text is required' };
					return { rows: _parsePaste(body.text) };
				}
				if (url === '/istota/api/health/immunizations/extract' && method === 'POST') {
					// Dev fixture: mock the LLM extraction so the review UI is
					// reachable in offline development. The real backend route
					// runs OCR / vision against the uploaded file.
					return {
						mode: 'vision',
						rows: [
							{
								name: 'Influenza',
								product_name: 'Fluzone Quadrivalent',
								date_given: '2025-11-12',
								source_line: '',
								confidence: 'high',
								notes: null,
							},
							{
								name: 'COVID-19',
								product_name: 'Comirnaty',
								date_given: '2024-10-04',
								source_line: '',
								confidence: 'high',
								notes: null,
							},
							{
								name: 'Unknown',
								product_name: 'Adacel — pertussis booster',
								date_given: null,
								source_line: '',
								confidence: 'manual',
								notes: 'Date not visible in source — please add manually',
							},
						],
						warnings: [
							'Mock extraction (dev mode) — the real LLM runs against the uploaded file.',
						],
					};
				}
				if (url === '/istota/api/health/immunizations/bulk' && method === 'POST') {
					if (!body || !Array.isArray(body.rows)) return { error: 'rows must be a list' };
					const ids: number[] = [];
					for (let i = 0; i < body.rows.length; i++) {
						const r = body.rows[i];
						if (!r.name || !r.date_given) return { error: `row ${i} missing fields` };
						const imm: Immunization = {
							id: nextImmunizationId++,
							name: String(r.name),
							product_name: r.product_name || null,
							date_given: String(r.date_given),
							manufacturer: r.manufacturer || null,
							dose_label: r.dose_label || null,
							lot_number: r.lot_number || null,
							route: r.route || null,
							site: r.site || null,
							administered_by: r.administered_by || null,
							facility: r.facility || null,
							encounter_id: r.encounter_id ?? null,
							cvx_code: r.cvx_code || null,
							notes: r.notes || null,
							source: r.source || 'import',
							created_at: new Date().toISOString(),
						};
						immunizations.push(imm);
						ids.push(imm.id);
					}
					return { status: 'ok', ids, count: ids.length };
				}
				const immExplainerMatch = url.match(/^\/istota\/api\/health\/immunizations\/([^/?]+)\/explainer$/);
				if (immExplainerMatch && method === 'GET') {
					const target = decodeURIComponent(immExplainerMatch[1]);
					const ref = IMMUNIZATION_REFS.find((r) => r.name === target)
						|| IMMUNIZATION_REFS.find((r) => (r.aliases || []).some((a) => a.toLowerCase() === target.toLowerCase()));
					if (!ref) return { error: 'vaccine not found' };
					const cov = _computeCoverage().coverage.find((c) => c.name === ref.name);
					const status = cov?.status || 'never_recorded';
					const disclaimer = 'Educational information only — not medical advice or diagnosis. Discuss vaccination decisions with your clinician.';
					const data = IMMUNIZATION_EXPLAINERS[ref.name];
					if (!data) {
						return {
							name: ref.name, display_name: ref.display_name, status,
							summary: `${ref.display_name} is recommended for many adults; the current coverage indicator shows that records or doses may be incomplete. Confirm your history and the current recommended schedule with a clinician.`,
							why_it_matters: [],
							disclaimer, source: 'fallback', generated_at: null,
						};
					}
					return {
						name: ref.name,
						display_name: ref.display_name,
						status,
						summary: data.summary,
						why_it_matters: data.why_it_matters,
						disclaimer,
						source: 'static',
						generated_at: null,
					};
				}
				if (url.startsWith('/istota/api/health/immunizations') && method === 'GET') {
					const idMatch = url.match(/^\/istota\/api\/health\/immunizations\/(\d+)$/);
					if (idMatch) {
						const id = Number(idMatch[1]);
						const row = immunizations.find((x) => x.id === id);
						if (!row) return { error: 'immunization not found' };
						const enc = row.encounter_id ? encounters.find((e) => e.id === row.encounter_id) || null : null;
						return { immunization: row, encounter: enc };
					}
					const u = new URL(url, 'http://x');
					const filterName = u.searchParams.get('name');
					const since = u.searchParams.get('since');
					const until = u.searchParams.get('until');
					let rows = [...immunizations];
					if (filterName) rows = rows.filter((r) => r.name === filterName);
					if (since) rows = rows.filter((r) => r.date_given >= since);
					if (until) rows = rows.filter((r) => r.date_given <= until);
					rows.sort((a, b) => b.date_given.localeCompare(a.date_given) || b.id - a.id);
					return { immunizations: rows };
				}
				if (url === '/istota/api/health/immunizations' && method === 'POST') {
					if (!body || !body.name || !body.date_given) {
						return { error: 'name and date_given required' };
					}
					const imm: Immunization = {
						id: nextImmunizationId++,
						name: String(body.name),
						product_name: body.product_name || null,
						date_given: String(body.date_given),
						manufacturer: body.manufacturer || null,
						dose_label: body.dose_label || null,
						lot_number: body.lot_number || null,
						route: body.route || null,
						site: body.site || null,
						administered_by: body.administered_by || null,
						facility: body.facility || null,
						encounter_id: body.encounter_id ?? null,
						cvx_code: body.cvx_code || null,
						notes: body.notes || null,
						source: body.source || 'manual',
						created_at: new Date().toISOString(),
					};
					immunizations.push(imm);
					return { status: 'ok', id: imm.id };
				}
				const immUpdMatch = url.match(/^\/istota\/api\/health\/immunizations\/(\d+)$/);
				if (immUpdMatch && method === 'PUT') {
					const id = Number(immUpdMatch[1]);
					const imm = immunizations.find((x) => x.id === id);
					if (!imm) return { error: 'immunization not found' };
					const allowed = [
						'name', 'product_name', 'date_given', 'manufacturer', 'dose_label',
						'lot_number', 'route', 'site', 'administered_by', 'facility',
						'encounter_id', 'cvx_code', 'notes',
					];
					for (const k of allowed) {
						if (body && k in body) (imm as any)[k] = body[k];
					}
					return { status: 'ok' };
				}
				if (immUpdMatch && method === 'DELETE') {
					const id = Number(immUpdMatch[1]);
					const idx = immunizations.findIndex((x) => x.id === id);
					if (idx < 0) return { error: 'immunization not found' };
					immunizations.splice(idx, 1);
					return { status: 'ok' };
				}

			return undefined;
		};
	})(),
];

function readBody(req: any): Promise<any> {
	return new Promise((resolve) => {
		const chunks: Buffer[] = [];
		req.on('data', (c: Buffer) => chunks.push(c));
		req.on('end', () => {
			if (chunks.length === 0) return resolve(undefined);
			const raw = Buffer.concat(chunks).toString('utf8');
			try {
				resolve(JSON.parse(raw));
			} catch {
				resolve(raw);
			}
		});
		req.on('error', () => resolve(undefined));
	});
}

export function mockApi(): Plugin {
	return {
		name: 'istota-mock-api',
		configureServer(server) {
			server.middlewares.use((req, res, next) => {
				if (!req.url?.startsWith('/istota/api/') && !req.url?.startsWith('/istota/money/api/')) return next();

				const method = req.method ?? 'GET';
				const respond = (body: unknown) => {
					res.setHeader('Content-Type', 'application/json');
					res.statusCode = 200;
					res.end(JSON.stringify(body));
				};

				const dispatch = (parsedBody: any) => {
					const ctx: MockReq = { url: req.url!, method, body: parsedBody };
					for (const h of handlers) {
						const body = h(ctx);
						if (body !== undefined) {
							respond(body);
							return;
						}
					}
					if (method !== 'GET') {
						respond({});
						return;
					}
					res.statusCode = 404;
					res.end('mock not implemented');
				};

				if (method === 'GET' || method === 'HEAD') {
					dispatch(undefined);
				} else {
					readBody(req).then(dispatch);
				}
			});
		},
	};
}
