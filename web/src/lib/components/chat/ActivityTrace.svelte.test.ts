import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Segment } from '$lib/stores/segments';
import ActivityTrace from './ActivityTrace.svelte';

afterEach(cleanup);

function textStep(text: string, id: string): Segment {
	return { kind: 'text', id, text, settled: true };
}
function toolStep(desc: string, id: string, opts: { running?: boolean; success?: boolean } = {}): Segment {
	return {
		kind: 'tool',
		id,
		tool: { id, name: 'Bash', description: desc, running: opts.running ?? false, success: opts.success },
	};
}
function thinkStep(text: string, id: string): Segment {
	return { kind: 'thinking', id, text, settled: true };
}

describe('ActivityTrace', () => {
	it('collapsed shows the active action only', () => {
		const steps = [
			toolStep('list files', 't1', { success: true }),
			toolStep('collate with python', 't2', { running: true }),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		// The active (running) tool is shown.
		expect(text).toContain('collate with python');
		// Earlier tools are NOT shown while collapsed.
		expect(text).not.toContain('list files');
		// Tool count badge.
		expect(text).toContain('2');
	});

	it('reasoning and narration are never shown (chip = tool actions only)', () => {
		const steps = [
			thinkStep('I should look that up.', 'k1'),
			textStep('Let me look that up for you.', 's1'),
			toolStep('search the web', 't1', { running: true }),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		expect(text).not.toContain('I should look that up.');
		expect(text).not.toContain('Let me look that up for you.');
		// Only the tool action shows.
		expect(text).toContain('search the web');
	});

	it('expanding lists every tool call in order (no reasoning/narration)', async () => {
		const steps = [
			thinkStep('Planning the listing.', 'k1'),
			toolStep('list files', 't1', { success: true }),
			textStep('Some narration that should not show.', 's1'),
			thinkStep('Now collating.', 'k2'),
			toolStep('collate with python', 't2', { success: true }),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		const text = container.textContent ?? '';
		expect(text).toContain('list files');
		expect(text).toContain('collate with python');
		// Neither reasoning nor narration appear in the expanded chain.
		expect(text).not.toContain('Planning the listing.');
		expect(text).not.toContain('Now collating.');
		expect(text).not.toContain('Some narration that should not show.');
		// No leftover thinking-row class.
		expect(container.querySelector('.step-thinking')).toBeNull();
	});

	it('renders the action for a lone tool', () => {
		const steps = [toolStep('run rng.py', 't1', { running: true })];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		expect(container.textContent).toContain('run rng.py');
	});

	it('no 💭 marker anywhere', async () => {
		const steps = [thinkStep('Reasoning one.', 'k1'), toolStep('list files', 't1', { success: true })];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		expect(container.textContent ?? '').not.toContain('💭');
	});

	it('expanded header shows a static label, not the latest action (no duplicate)', async () => {
		const steps = [
			toolStep('search the web', 't1', { success: true }),
			toolStep('fetch the page', 't2', { success: true }),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		// The latest tool appears exactly once — in the chain, not also in the head.
		const occurrences = (container.textContent ?? '').split('fetch the page').length - 1;
		expect(occurrences).toBe(1);
		// The expanded head carries a static label instead of the live action.
		expect(container.textContent).toContain('Activity');
	});
});
