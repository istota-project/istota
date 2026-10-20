import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
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

describe('file path check', () => {
  it('checks once after typing stops and drops an answer for an older path', async () => {
    vi.mocked(api.getBriefingPathSuggestions).mockResolvedValue({ paths: [] });
    let answerOld!: (value: { ok: boolean; resolved?: string }) => void;
    vi.mocked(api.checkBriefingPath)
      .mockReturnValueOnce(new Promise((resolve) => (answerOld = resolve)))
      .mockResolvedValueOnce({ ok: true, resolved: '/files/new.md' });
    const props = { kind: 'todos', config: { path: 'old.md' }, onChange: vi.fn() };
    const { rerender } = render(SourceConfigFields, props);
    await waitFor(() => expect(api.checkBriefingPath).toHaveBeenCalledWith('old.md'));

    await rerender({ ...props, config: { path: 'ne' } });
    await rerender({ ...props, config: { path: 'new.md' } });
    await waitFor(() => expect(screen.getByText('Resolves to /files/new.md')).toBeTruthy());
    expect(api.checkBriefingPath).toHaveBeenCalledTimes(2);

    answerOld({ ok: false });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText('Resolves to /files/new.md')).toBeTruthy();
  });
});
