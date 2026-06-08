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
	it('collapsed shows the latest reasoning AND active action together', () => {
		const steps = [
			thinkStep('Listing the files.', 'k1'),
			toolStep('list files', 't1', { success: true }),
			thinkStep('Now collating the rounds.', 'k2'),
			toolStep('collate with python', 't2', { running: true }),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		// Latest reasoning + the active (running) tool, combined.
		expect(text).toContain('Now collating the rounds.');
		expect(text).toContain('collate with python');
		// Earlier steps are NOT shown while collapsed.
		expect(text).not.toContain('Listing the files.');
		expect(text).not.toContain('list files');
		// Tool count badge.
		expect(text).toContain('2');
	});

	it('plain narration text is never shown (chip = reasoning + tools only)', () => {
		const steps = [
			textStep('Let me look that up for you.', 's1'),
			toolStep('search the web', 't1', { running: true }),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		// The narration text is dropped; only the tool action shows.
		expect(text).not.toContain('Let me look that up for you.');
		expect(text).toContain('search the web');
	});

	it('expanding reveals the reasoning + tool chain in order (no narration)', async () => {
		const steps = [
			thinkStep('Planning the listing.', 'k1'),
			toolStep('list files', 't1', { success: true }),
			textStep('Some narration that should not show.', 's1'),
			thinkStep('Now collating the rounds.', 'k2'),
			toolStep('collate with python', 't2', { success: true }),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		const text = container.textContent ?? '';
		expect(text).toContain('Planning the listing.');
		expect(text).toContain('list files');
		expect(text).toContain('Now collating the rounds.');
		expect(text).toContain('collate with python');
		// Narration is dropped even in the expanded chain.
		expect(text).not.toContain('Some narration that should not show.');
	});

	it('renders just the action when there is no reasoning', () => {
		const steps = [toolStep('run rng.py', 't1', { running: true })];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		expect(container.textContent).toContain('run rng.py');
	});

	it('collapsed shows the latest thinking step (no emoji marker)', () => {
		const steps = [
			thinkStep('Considering the request.', 'k1'),
			toolStep('search', 't1', { success: true }),
			thinkStep('Summarizing the findings.', 'k2'),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		// The latest step is a thinking step → shown collapsed, styled (not glyphed).
		expect(text).not.toContain('💭');
		expect(text).toContain('Summarizing the findings.');
		expect(container.querySelector('.msg.thinking')).not.toBeNull();
		// Earlier thinking is hidden while collapsed.
		expect(text).not.toContain('Considering the request.');
	});

	it('expanded renders thinking rows distinctly (own class, no emoji)', async () => {
		const steps = [
			thinkStep('Reasoning step one.', 'k1'),
			toolStep('list files', 't1', { success: true }),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		const text = container.textContent ?? '';
		expect(text).toContain('Reasoning step one.');
		expect(text).not.toContain('💭');
		expect(text).toContain('list files');
		// The thinking row uses its own class, distinct from a tool action row.
		expect(container.querySelector('.step-thinking')).not.toBeNull();
	});

	it('expanded header shows a static label, not the live latest step (no duplicate)', async () => {
		const steps = [
			thinkStep('Considering the request.', 'k1'),
			toolStep('search the web', 't1', { success: true }),
			thinkStep('Summarizing the findings.', 'k2'),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		// The latest step appears exactly once — in the chain, not also in the head.
		const occurrences = (container.textContent ?? '').split('Summarizing the findings.').length - 1;
		expect(occurrences).toBe(1);
		// The expanded head carries a static label instead of the live step.
		expect(container.textContent).toContain('Activity');
	});
});
