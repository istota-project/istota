import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { pageLoading } from '$lib/stores/pageLoading';
import { clearNotices, notices } from '$lib/stores/notices';
import Harness from './LoadState.harness.svelte';

afterEach(cleanup);
beforeEach(() => clearNotices());

const data = () => screen.queryByTestId('data');

describe('LoadState, pane scope', () => {
  it('holds the pane with PageLoading while loading, raising the fixed loader', () => {
    const { container } = render(Harness, { loading: true, loadingLabel: 'Loading jobs…' });
    expect(container.querySelector('.center-msg.page-loading')).not.toBeNull();
    expect(screen.getByText('Loading jobs…')).toHaveClass('visually-hidden');
    expect(get(pageLoading)).toBe(true);
    expect(data()).toBeNull();
  });

  it('renders a failure as .center-msg error, with Retry when asked', async () => {
    const onRetry = vi.fn();
    const { container } = render(Harness, { loading: false, error: 'It broke', onRetry });
    const box = container.querySelector('.center-msg.error');
    expect(box).toHaveTextContent('It broke');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledOnce();
    expect(data()).toBeNull();
  });

  it('renders no Retry without onRetry', () => {
    render(Harness, { loading: false, error: 'It broke' });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders the empty snippet instead of the data when empty', () => {
    render(Harness, { loading: false, empty: true });
    expect(screen.getByTestId('empty')).toBeInTheDocument();
    expect(data()).toBeNull();
  });

  it('renders the data once loaded', () => {
    render(Harness, { loading: false });
    expect(data()).not.toBeNull();
    expect(screen.queryByTestId('empty')).toBeNull();
  });

  it('keeps the data on screen while a refresh loads', () => {
    const { container } = render(Harness, { loading: true, hasData: true });
    expect(data()).not.toBeNull();
    expect(container.querySelector('.page-loading')).toBeNull();
  });

  it('reports a failed refresh as a notice and keeps the data', () => {
    const { container } = render(Harness, {
      loading: false,
      hasData: true,
      error: 'Refresh failed',
    });
    expect(data()).not.toBeNull();
    expect(container.querySelector('.center-msg')).toBeNull();
    const shown = get(notices);
    expect(shown.some((n) => n.message === 'Refresh failed' && n.severity === 'error')).toBe(true);
  });

  it('shows the header with the data and the empty state, never with loading or failure', async () => {
    const { rerender } = render(Harness, { loading: true, withHeader: true });
    expect(screen.queryByTestId('header')).toBeNull();
    await rerender({ loading: false, error: 'It broke', withHeader: true });
    expect(screen.queryByTestId('header')).toBeNull();
    await rerender({ loading: false, error: null, empty: true, withHeader: true });
    expect(screen.getByTestId('header')).toBeInTheDocument();
    expect(screen.getByTestId('empty')).toBeInTheDocument();
    await rerender({ loading: false, error: null, empty: false, withHeader: true });
    expect(screen.getByTestId('header')).toBeInTheDocument();
  });
});

describe('LoadState, card scope', () => {
  it('renders the in-card loading line as a caption', () => {
    const { container } = render(Harness, { loading: true, scope: 'card' });
    const line = container.querySelector('p.caption');
    expect(line).toHaveTextContent('Loading…');
    expect(container.querySelector('.page-loading')).toBeNull();
    expect(get(pageLoading)).toBe(false);
  });

  it('renders a failure as an inline danger Alert', () => {
    const { container } = render(Harness, { loading: false, error: 'No rules', scope: 'card' });
    const banner = container.querySelector('.ui-alert-inline.banner.danger');
    expect(banner).toHaveTextContent('No rules');
    expect(banner).toHaveAttribute('role', 'alert');
    expect(container.querySelector('.center-msg')).toBeNull();
  });
});
