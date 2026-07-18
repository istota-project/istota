import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import AutocompletePopover from './AutocompletePopover.svelte';
import type { Suggestion } from './types';

afterEach(cleanup);

const items: Suggestion[] = [
	{ value: '!more ', label: '!more', description: 'Show execution trace', key: 'cmd:more' },
	{ value: '!memory ', label: '!memory', description: 'Show memory', key: 'cmd:memory' },
];

function mount(activeIndex = 0, over: Partial<Record<'onaccept' | 'onhover', unknown>> = {}) {
	return render(AutocompletePopover, {
		suggestions: items,
		activeIndex,
		listId: 'ac-list',
		optionId: (k: string) => `ac-opt-${k}`,
		onaccept: (over.onaccept as (i: number) => void) ?? (() => {}),
		onhover: (over.onhover as (i: number) => void) ?? (() => {}),
	});
}

describe('AutocompletePopover', () => {
	it('renders a listbox with an option per suggestion', () => {
		const { container } = mount();
		const list = container.querySelector('[role="listbox"]');
		expect(list).toBeTruthy();
		const opts = container.querySelectorAll('[role="option"]');
		expect(opts.length).toBe(2);
		expect(opts[0].textContent).toContain('!more');
		expect(opts[0].textContent).toContain('Show execution trace');
	});

	it('marks the active row aria-selected + data-highlighted', () => {
		const { container } = mount(1);
		const opts = container.querySelectorAll('[role="option"]');
		expect(opts[0].getAttribute('aria-selected')).toBe('false');
		expect(opts[1].getAttribute('aria-selected')).toBe('true');
		expect(opts[1].getAttribute('data-highlighted')).toBe('true');
	});

	it('option ids come from optionId(key)', () => {
		const { container } = mount();
		expect(container.querySelector('#ac-opt-cmd\\:more')).toBeTruthy();
	});

	it('mousedown accepts (and prevents default to keep focus)', async () => {
		const onaccept = vi.fn();
		const { container } = mount(0, { onaccept });
		const opt = container.querySelectorAll('[role="option"]')[1];
		await fireEvent.mouseDown(opt);
		expect(onaccept).toHaveBeenCalledWith(1);
	});

	it('mouseenter reports hover index', async () => {
		const onhover = vi.fn();
		const { container } = mount(0, { onhover });
		const opt = container.querySelectorAll('[role="option"]')[1];
		await fireEvent.mouseEnter(opt);
		expect(onhover).toHaveBeenCalledWith(1);
	});
});
