import { render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { SearchResultRow } from '$lib/components/ui';
import type { SearchHit } from '$lib/api';

afterEach(() => vi.useRealTimers());

it('renders calendar dates without a UTC day shift', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-06-01T12:00:00Z'));
  const hit: SearchHit = {
    id: 'health:panels:1',
    kind: 'health_panel',
    title: 'Lab panel',
    subtitle: null,
    snippet: 'Falcon',
    highlights: [],
    date: '2026-01-04',
    link: null,
    badges: [],
  };
  render(SearchResultRow, {
    hit,
    id: 'result',
    active: false,
    actionable: false,
    onclick: () => {},
  });
  expect(screen.getByText('Jan 4, 2026')).toBeInTheDocument();
  expect(screen.queryByText('Jan 3, 2026')).not.toBeInTheDocument();
});
