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

describe('ActivityTrace', () => {
	it('collapsed shows the current progress message AND action together', () => {
		const steps = [
			textStep('Listing the files.', 's1'),
			toolStep('list files', 't1', { success: true }),
			textStep('Now collating the rounds.', 's2'),
			toolStep('collate with python', 't2', { running: true }),
		];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		const text = container.textContent ?? '';
		// Latest progress message + the active (running) tool, combined.
		expect(text).toContain('Now collating the rounds.');
		expect(text).toContain('collate with python');
		// Earlier steps are NOT shown while collapsed.
		expect(text).not.toContain('Listing the files.');
		expect(text).not.toContain('list files');
		// Tool count badge.
		expect(text).toContain('2');
	});

	it('expanding reveals the whole interleaved chain in order', async () => {
		const steps = [
			textStep('Listing the files.', 's1'),
			toolStep('list files', 't1', { success: true }),
			textStep('Now collating the rounds.', 's2'),
			toolStep('collate with python', 't2', { success: true }),
		];
		const { container, getByRole } = render(ActivityTrace, { steps, streaming: false });
		await fireEvent.click(getByRole('button'));
		await tick();
		const text = container.textContent ?? '';
		expect(text).toContain('Listing the files.');
		expect(text).toContain('list files');
		expect(text).toContain('Now collating the rounds.');
		expect(text).toContain('collate with python');
	});

	it('renders just the action when there is no narration', () => {
		const steps = [toolStep('run rng.py', 't1', { running: true })];
		const { container } = render(ActivityTrace, { steps, streaming: true });
		expect(container.textContent).toContain('run rng.py');
	});
});
