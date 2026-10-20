import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import Spinner from './Spinner.svelte';

afterEach(cleanup);

describe('Spinner', () => {
  it('draws the shared .spinner, hidden from a screen reader', () => {
    const { container } = render(Spinner);
    const spinner = container.querySelector('.spinner')!;
    expect(spinner).not.toBeNull();
    expect(spinner).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('.visually-hidden')).toBeNull();
  });

  it('says its label to a screen reader only', () => {
    const { container } = render(Spinner, { label: 'Applying' });
    expect(screen.getByText('Applying')).toHaveClass('visually-hidden');
    expect(container.querySelector('.spinner')).toHaveAttribute('aria-hidden', 'true');
  });

  it('is the md size unless asked to be small', () => {
    const { container } = render(Spinner);
    expect(container.querySelector('.spinner')).not.toHaveClass('ui-spinner-sm');
    cleanup();
    const small = render(Spinner, { size: 'sm' });
    expect(small.container.querySelector('.spinner')).toHaveClass('ui-spinner-sm');
  });
});
