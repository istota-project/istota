import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import FeedSubscriptionModal from './FeedSubscriptionModal.svelte';
import { pressEnter } from '$lib/test/implicitSubmit';

afterEach(cleanup);

const draft = {
  url: 'https://example.com/feed.xml',
  title: 'Example',
  category: '',
  poll_interval_minutes: '',
};

function mount(mode: 'add' | 'edit', onSubmit = vi.fn()) {
  render(FeedSubscriptionModal, {
    mode,
    initial:
      mode === 'add' ? { url: '', title: '', category: '', poll_interval_minutes: '' } : draft,
    categoryOptions: [{ value: '', label: '(none)' }],
    defaultInterval: 30,
    onSubmit,
    onCancel: vi.fn(),
  });
  return onSubmit;
}

describe('FeedSubscriptionModal', () => {
  it('words the add mode as adding, with the optional fields marked', () => {
    mount('add');
    expect(screen.getByRole('dialog', { name: 'Add subscription' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Title (optional)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
  });

  it('opens the edit mode on the draft it was given and submits the edited copy on Enter', async () => {
    const onSubmit = mount('edit');
    expect(screen.getByRole('dialog', { name: 'Edit subscription' })).toBeInTheDocument();
    const title = screen.getByRole('textbox', { name: 'Title' });
    expect(title).toHaveValue('Example');
    await fireEvent.input(title, { target: { value: 'Renamed' } });
    await pressEnter(title);
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: 'Renamed' }));
    // The caller's draft is a seed, not the thing being edited.
    expect(draft.title).toBe('Example');
  });

  it('keeps the dialog open with the reason when the page refuses the draft', async () => {
    mount(
      'edit',
      vi.fn(() => {
        throw new Error('A different subscription already uses that URL.');
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A different subscription already uses that URL.',
    );
    expect(screen.getByRole('dialog', { name: 'Edit subscription' })).toBeInTheDocument();
  });
});
