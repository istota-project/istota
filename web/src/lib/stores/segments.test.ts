import { describe, it, expect } from 'vitest';
import {
	applyEvent,
	answerText,
	isRenderable,
	type ChatMessage,
	type Segment,
} from './segments';

function freshAssistant(): ChatMessage {
	return {
		cid: 1,
		role: 'assistant',
		text: '',
		segments: [],
		streaming: true,
	};
}

function feed(m: ChatMessage, events: [string, Record<string, unknown>][]): void {
	for (const [kind, payload] of events) applyEvent(m, kind, payload);
}

function texts(m: ChatMessage): Extract<Segment, { kind: 'text' }>[] {
	return m.segments.filter((s): s is Extract<Segment, { kind: 'text' }> => s.kind === 'text');
}
function tools(m: ChatMessage): Extract<Segment, { kind: 'tool' }>[] {
	return m.segments.filter((s): s is Extract<Segment, { kind: 'tool' }> => s.kind === 'tool');
}

describe('applyEvent reducer', () => {
	it('no-tool Q&A: deltas then result → single answer segment', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'The ' }],
			['text_delta', { text: 'answer ' }],
			['text_delta', { text: 'is 42.' }],
			['result', { text: 'The answer is 42.' }],
			['done', { duration_seconds: 1.2 }],
		]);
		expect(m.segments).toHaveLength(1);
		const seg = m.segments[0];
		expect(seg.kind).toBe('text');
		expect((seg as any).settled).toBe(false);
		expect((seg as any).text).toBe('The answer is 42.');
		expect(m.text).toBe('The answer is 42.');
		expect(m.streaming).toBe(false);
		expect(m.durationSeconds).toBe(1.2);
	});

	it('single tool turn: narration settles, answer is last text', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'Let me check the calendar.' }],
			['tool_start', { tool_name: 'Bash', description: 'calendar list', tool_call_id: 'c1' }],
			['tool_end', { tool_call_id: 'c1', success: true }],
			['text_delta', { text: 'You have 2 events.' }],
			['result', { text: 'You have 2 events today.' }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['text', 'tool', 'text']);
		const [narration, , answer] = m.segments;
		expect((narration as any).settled).toBe(true);
		expect((narration as any).text).toBe('Let me check the calendar.');
		expect((answer as any).settled).toBe(false);
		expect((answer as any).text).toBe('You have 2 events today.');
		expect(m.text).toBe('You have 2 events today.');
	});

	it('tool-first turn: no empty leading text segment', () => {
		const m = freshAssistant();
		feed(m, [
			['tool_start', { tool_name: 'Bash', description: 'ls', tool_call_id: 'c1' }],
			['tool_end', { tool_call_id: 'c1', success: true }],
			['text_delta', { text: 'Done.' }],
			['result', { text: 'Done.' }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['tool', 'text']);
		expect(texts(m)).toHaveLength(1);
	});

	it('parallel tools in one turn: narration not duplicated', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'Running both.' }],
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: 'c1' }],
			['tool_start', { tool_name: 'Bash', description: 'b', tool_call_id: 'c2' }],
			['tool_end', { tool_call_id: 'c1', success: true }],
			['tool_end', { tool_call_id: 'c2', success: true }],
			['result', { text: 'Both done.' }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['text', 'tool', 'tool', 'text']);
		expect(texts(m)[0].settled).toBe(true);
		expect(texts(m)[0].text).toBe('Running both.');
	});

	it('CM rewrite / terse: result overwrites the streamed last text', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'draft' }],
			['result', { text: 'corrected' }],
		]);
		expect(answerText(m)).toBe('corrected');
		expect(texts(m)).toHaveLength(1);
	});

	it('result with last segment a tool: appends a trailing text segment', () => {
		const m = freshAssistant();
		feed(m, [
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: 'c1' }],
			['tool_end', { tool_call_id: 'c1', success: true }],
			['result', { text: 'ans' }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['tool', 'text']);
		expect(answerText(m)).toBe('ans');
	});

	it('result with empty text keeps the streamed answer', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'streamed answer' }],
			['result', { text: '' }],
		]);
		expect(answerText(m)).toBe('streamed answer');
	});

	it('tool_progress sets live output; tool_end clears it and records success', () => {
		const m = freshAssistant();
		feed(m, [
			['tool_start', { tool_name: 'Bash', description: 'run', tool_call_id: 'c1' }],
			['tool_progress', { tool_call_id: 'c1', text: 'line 1\nline 2' }],
		]);
		expect(tools(m)[0].tool.progress).toBe('line 1\nline 2');
		expect(tools(m)[0].tool.running).toBe(true);

		applyEvent(m, 'tool_end', { tool_call_id: 'c1', success: false });
		expect(tools(m)[0].tool.progress).toBeUndefined();
		expect(tools(m)[0].tool.running).toBe(false);
		expect(tools(m)[0].tool.success).toBe(false);
	});

	it('cancelled with no answer appends a cancelled notice', () => {
		const m = freshAssistant();
		feed(m, [['cancelled', {}]]);
		expect(answerText(m)).toBe('_(cancelled)_');
		expect(m.streaming).toBe(false);
	});

	it('cancelled with a streamed answer keeps the answer', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'partial answer' }],
			['cancelled', {}],
		]);
		expect(answerText(m)).toBe('partial answer');
	});

	it('late text_delta after a terminal event is ignored', () => {
		const m = freshAssistant();
		feed(m, [
			['result', { text: 'final' }],
			['text_delta', { text: ' stray' }],
		]);
		expect(answerText(m)).toBe('final');
	});

	it('error replaces the trailing text and marks the message', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'working' }],
			['tool_start', { tool_name: 'Bash', description: 'boom', tool_call_id: 'c1' }],
			['error', { message: 'It blew up.' }],
		]);
		expect(m.error).toBe(true);
		expect(m.streaming).toBe(false);
		expect(answerText(m)).toBe('It blew up.');
		// The narration + tool chip stay visible for debugging.
		expect(m.segments.map((s) => s.kind)).toEqual(['text', 'tool', 'text']);
		expect((m.segments[0] as any).settled).toBe(true);
	});

	it('confirmation sets the prompt and pending status', () => {
		const m = freshAssistant();
		feed(m, [['confirmation', { prompt: 'Send this email?' }]]);
		expect(m.confirmation).toBe(true);
		expect(m.status).toBe('pending_confirmation');
		expect(answerText(m)).toBe('Send this email?');
		expect(m.streaming).toBe(false);
	});

	it('done finalizes running tools without a tool_end', () => {
		const m = freshAssistant();
		feed(m, [
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: 'c1' }],
			['result', { text: 'ok' }],
			['done', { duration_seconds: 3 }],
		]);
		expect(tools(m)[0].tool.running).toBe(false);
		expect(m.durationSeconds).toBe(3);
	});

	it('done carries the model name onto the message', () => {
		const m = freshAssistant();
		feed(m, [
			['result', { text: 'ok' }],
			['done', { duration_seconds: 2, model: 'claude-opus-4-8' }],
		]);
		expect(m.model).toBe('claude-opus-4-8');
	});

	it('done without a model leaves the field unset', () => {
		const m = freshAssistant();
		feed(m, [
			['result', { text: 'ok' }],
			['done', { duration_seconds: 2 }],
		]);
		expect(m.model).toBeUndefined();
	});

	it('empty whitespace narration is settled but not renderable', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: '   ' }],
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: 'c1' }],
		]);
		const narration = m.segments[0];
		expect((narration as any).settled).toBe(true);
		expect(isRenderable(narration)).toBe(false);
		expect(isRenderable(m.segments[1])).toBe(true);
	});

	it('empty tool_call_id (ClaudeCodeBrain) → distinct synthesized ids', () => {
		// The default brain emits tool_call_id:'' for every tool. Each tool
		// segment must still get a unique id, or the keyed {#each} collides.
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'Running.' }],
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: '' }],
			['tool_start', { tool_name: 'Bash', description: 'b', tool_call_id: '' }],
			['tool_start', { tool_name: 'Bash', description: 'c', tool_call_id: '' }],
			['result', { text: 'done' }],
		]);
		const ids = m.segments.map((s) => s.id);
		expect(new Set(ids).size).toBe(ids.length); // all unique
		expect(tools(m).map((t) => t.id)).toEqual(['t0', 't1', 't2']);
	});

	it('unknown event kinds are ignored', () => {
		const m = freshAssistant();
		feed(m, [['mystery', { foo: 1 }]]);
		expect(m.segments).toHaveLength(0);
	});
});

function thinks(m: ChatMessage): Extract<Segment, { kind: 'thinking' }>[] {
	return m.segments.filter((s): s is Extract<Segment, { kind: 'thinking' }> => s.kind === 'thinking');
}

describe('applyEvent — thinking segments', () => {
	it('consecutive thinking deltas accumulate into one thinking segment', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: 'The user ' }],
			['thinking', { text: 'wants X. ' }],
			['thinking', { text: 'I should search.' }],
		]);
		expect(m.segments).toHaveLength(1);
		const seg = m.segments[0];
		expect(seg.kind).toBe('thinking');
		expect((seg as any).text).toBe('The user wants X. I should search.');
		expect((seg as any).settled).toBe(false);
	});

	it('thinking is never returned by answerText', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: 'reasoning only' }],
			['done', {}],
		]);
		expect(answerText(m)).toBe('');
	});

	it('tool_start settles an open thinking segment (folds into the chip)', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: 'Let me look it up.' }],
			['tool_start', { tool_name: 'Bash', description: 'search', tool_call_id: 'c1' }],
		]);
		expect(thinks(m)[0].settled).toBe(true);
		expect(m.segments.map((s) => s.kind)).toEqual(['thinking', 'tool']);
	});

	it('text_delta after thinking settles thinking and opens a fresh answer block', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: 'Reasoning lead-in.' }],
			['text_delta', { text: 'The answer ' }],
			['text_delta', { text: 'is 42.' }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['thinking', 'text']);
		expect(thinks(m)[0].settled).toBe(true);
		expect(answerText(m)).toBe('The answer is 42.');
	});

	it('full turn: thinking → tool → thinking → answer → result', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: 'First I plan.' }],
			['tool_start', { tool_name: 'WebSearch', description: 'search', tool_call_id: 'c1' }],
			['tool_end', { tool_call_id: 'c1', success: true }],
			['thinking', { text: 'Now I summarize.' }],
			['text_delta', { text: 'Here it is.' }],
			['result', { text: 'Here it is.' }],
			['done', { duration_seconds: 2 }],
		]);
		expect(m.segments.map((s) => s.kind)).toEqual(['thinking', 'tool', 'thinking', 'text']);
		// Both thinking segments settled; the answer is the trailing text.
		expect(thinks(m).every((t) => t.settled)).toBe(true);
		expect(answerText(m)).toBe('Here it is.');
		// The two thinking segments carry distinct ids (no keyed {#each} collision).
		const ids = thinks(m).map((t) => t.id);
		expect(new Set(ids).size).toBe(ids.length);
	});

	it('empty whitespace thinking is non-renderable once settled', () => {
		const m = freshAssistant();
		feed(m, [
			['thinking', { text: '   ' }],
			['tool_start', { tool_name: 'Bash', description: 'a', tool_call_id: 'c1' }],
		]);
		const t = m.segments[0];
		expect(t.kind).toBe('thinking');
		expect((t as any).settled).toBe(true);
		expect(isRenderable(t)).toBe(false);
	});

	it('a late thinking delta after the message terminated is ignored', () => {
		const m = freshAssistant();
		feed(m, [
			['text_delta', { text: 'answer' }],
			['result', { text: 'answer' }],
			['thinking', { text: 'stray' }],
		]);
		expect(thinks(m)).toHaveLength(0);
		expect(answerText(m)).toBe('answer');
	});
});
