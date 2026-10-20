import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import Harness from './PageHeader.harness.svelte';
import BackLink from './BackLink.svelte';

afterEach(cleanup);

describe('PageHeader', () => {
  it('renders the title as the page heading', () => {
    render(Harness, { title: 'Labs' });
    const h1 = screen.getByRole('heading', { level: 1, name: 'Labs' });
    expect(h1).toHaveClass('title-page');
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('renders the back link, description, badge and actions', () => {
    const { container } = render(Harness, {
      title: 'Conditions',
      back: { href: '/istota/health/history', label: 'Medical history' },
      description: 'Every diagnosis on file.',
      withBadge: true,
      withActions: true,
    });
    expect(screen.getByRole('link', { name: 'Medical history' })).toHaveAttribute(
      'href',
      '/istota/health/history',
    );
    expect(screen.getByText('Every diagnosis on file.')).toHaveClass('description');
    expect(screen.getByTestId('badge')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
    expect(container.querySelector('header.ui-page-header')).not.toBeNull();
  });

  it('aligns the row to the top when asked', () => {
    const { container } = render(Harness, {
      title: 'Encounter',
      align: 'start',
      withActions: true,
    });
    expect(container.querySelector('.ui-page-header')).toHaveClass('align-start');
  });

  it('draws no heading without a title', () => {
    render(Harness, { description: 'Only a line of explanation.' });
    expect(screen.queryByRole('heading')).toBeNull();
  });
});

describe('BackLink', () => {
  it('is a real link with a hidden arrow glyph', () => {
    render(BackLink, { href: '/istota/health/labs', label: 'Labs' });
    const link = screen.getByRole('link', { name: 'Labs' });
    expect(link).toHaveAttribute('href', '/istota/health/labs');
    expect(link).toHaveClass('link-muted');
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });
});
