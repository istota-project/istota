import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import EmptyState from './EmptyState.svelte';

afterEach(cleanup);

const message = (text: string) =>
  createRawSnippet(() => ({ render: () => `<span>${text}</span>` }));

describe('EmptyState', () => {
  it('renders the message with no action', () => {
    render(EmptyState, { children: message('No accounts found.') });
    expect(screen.getByText('No accounts found.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('renders a navigating action as a link', () => {
    render(EmptyState, {
      children: message('No snapshots yet.'),
      actionLabel: 'Import a snapshot',
      actionHref: '/istota/money/portfolio/import',
    });
    expect(screen.getByRole('link', { name: 'Import a snapshot' })).toHaveAttribute(
      'href',
      '/istota/money/portfolio/import',
    );
  });

  it('renders an in-page action as a button', async () => {
    const onaction = vi.fn();
    render(EmptyState, {
      children: message('No measurements yet.'),
      actionLabel: 'Log a measurement',
      onaction,
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Log a measurement' }));
    expect(onaction).toHaveBeenCalledOnce();
  });

  it('draws no action from a label alone', () => {
    // A button that goes nowhere is worse than none.
    render(EmptyState, { children: message('Nothing yet.'), actionLabel: 'Do it' });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders a title above the message', () => {
    render(EmptyState, {
      children: message('Once one runs it appears here.'),
      title: 'No briefings yet',
    });
    expect(screen.getByRole('heading', { name: 'No briefings yet' })).toBeInTheDocument();
  });
});
