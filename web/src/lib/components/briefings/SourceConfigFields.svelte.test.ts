import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import SourceConfigFields from './SourceConfigFields.svelte';

afterEach(cleanup);

function mountSenders(onChange = vi.fn()) {
  render(SourceConfigFields, {
    kind: 'email',
    config: { mode: 'senders', senders: ['news@example.com', '*@example.org'] },
    onChange,
  });
  return onChange;
}

describe('email sender filters', () => {
  // The caption used to be a <label> around the chip list, so a click on it
  // activated the first labelable descendant: the first chip's remove button.
  it('does not remove a sender when the caption is clicked', async () => {
    const onChange = mountSenders();
    await fireEvent.click(screen.getByText('Sender filters'));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('still removes a sender from its own button', async () => {
    const onChange = mountSenders();
    await fireEvent.click(screen.getByRole('button', { name: 'Remove news@example.com' }));
    expect(onChange).toHaveBeenCalledWith({ senders: ['*@example.org'] });
  });

  it('names the add field without the caption', () => {
    mountSenders();
    expect(screen.getByRole('textbox', { name: 'Add a sender pattern' })).toBeTruthy();
  });
});
