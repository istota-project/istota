import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import SearchResults from './SearchResults.svelte';
import type { SearchResultsData, SearchResultItem } from '$lib/stores/segments';

afterEach(cleanup);

function conv(overrides: Partial<SearchResultItem> = {}): SearchResultItem {
	return {
		source_type: 'conversation',
		summary: 'the falcon migration timeline',
		date: '2026-07-15',
		room_token: 'room1',
		room_name: 'Falcon planning',
		task_id: 42,
		talk_message_id: null,
		talk_link: null,
		...overrides,
	};
}

function memory(overrides: Partial<SearchResultItem> = {}): SearchResultItem {
	return {
		source_type: 'memory_file',
		summary: 'falcon is my project codename',
		date: '',
		room_token: null,
		room_name: null,
		task_id: null,
		talk_message_id: null,
		talk_link: null,
		...overrides,
	};
}

function data(results: SearchResultItem[]): SearchResultsData {
	return { kind: 'search_results', query: 'falcon', results, text: 'fallback' };
}

describe('SearchResults', () => {
	it('renders conversation and memory cards with summaries', () => {
		const { container } = render(SearchResults, {
			data: data([conv(), memory()]),
			onJump: vi.fn(),
		});
		expect(container.textContent).toContain('the falcon migration timeline');
		expect(container.textContent).toContain('falcon is my project codename');
		// One card per result.
		expect(container.querySelectorAll('.card').length).toBe(2);
	});

	it('shows a jump button only for conversation results with a task_id', () => {
		const { container } = render(SearchResults, {
			data: data([conv(), memory()]),
			onJump: vi.fn(),
		});
		// Exactly one jump button (the conversation card), none for the memory card.
		const jumpBtns = container.querySelectorAll('.jump-btn');
		expect(jumpBtns.length).toBe(1);
	});

	it('calls onJump with room token and task id', async () => {
		const onJump = vi.fn();
		const { container } = render(SearchResults, { data: data([conv()]), onJump });
		await fireEvent.click(container.querySelector('.jump-btn')!);
		expect(onJump).toHaveBeenCalledWith('room1', 42);
	});

	it('renders no jump button when onJump is absent', () => {
		const { container } = render(SearchResults, { data: data([conv()]) });
		expect(container.querySelector('.jump-btn')).toBeNull();
	});

	it('falls back to a Talk link for a conversation with a talk_message_id and no jump', () => {
		const { container } = render(SearchResults, {
			data: data([
				conv({ task_id: null, talk_message_id: 5, talk_link: 'https://nc/call/room1#message_5' }),
			]),
			onJump: vi.fn(),
		});
		const link = container.querySelector('a.talk-link') as HTMLAnchorElement | null;
		expect(link).not.toBeNull();
		expect(link!.getAttribute('href')).toContain('#message_5');
	});

	it('renders an empty state for no results', () => {
		const { container } = render(SearchResults, { data: data([]), onJump: vi.fn() });
		expect(container.querySelector('.empty')).not.toBeNull();
		expect(container.querySelectorAll('.card').length).toBe(0);
	});
});
