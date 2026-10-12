import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import RawEditor from './RawEditor.svelte';

afterEach(cleanup);

const FILE = 'bot_name = "Filebot"\n\n[email]\nimap_password = "<unchanged>"\n';

function mount(props: { initial?: string | null; pending?: string | null; locked?: string }) {
  const onChange = vi.fn();
  render(RawEditor, {
    props: {
      initial: props.initial === undefined ? FILE : props.initial,
      pending: props.pending ?? null,
      lockedReason: props.locked ?? null,
      onChange,
    },
  });
  return { onChange };
}

describe('the raw editor', () => {
  it('opens on the file with every credential a sentinel', async () => {
    mount({});
    expect(screen.queryByRole('textbox')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Edit as text' }));
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(FILE);
    expect(screen.getByText(/stands for a stored credential and keeps it/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Users' })).toHaveAttribute(
      'href',
      expect.stringMatching(/\/admin\/users$/),
    );
  });

  it('emits the new text, and nothing once it is the file again', async () => {
    const { onChange } = mount({});
    await fireEvent.click(screen.getByRole('button', { name: 'Edit as text' }));
    const textarea = screen.getByRole('textbox');
    await fireEvent.input(textarea, { target: { value: 'bot_name = "Other"\n' } });
    expect(onChange).toHaveBeenLastCalledWith('bot_name = "Other"\n');
    await fireEvent.input(textarea, { target: { value: FILE } });
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('shows pending text open, with an undo', async () => {
    const { onChange } = mount({ pending: 'a = 1\n' });
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('a = 1\n');
    await fireEvent.click(screen.getByRole('button', { name: 'Undo text changes' }));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('cannot be opened while the form holds changes', () => {
    mount({ locked: 'Review or discard the 2 changes made in the form first.' });
    expect(screen.getByRole('button', { name: 'Edit as text' })).toBeDisabled();
    expect(screen.getByText(/made in the form first/)).toBeInTheDocument();
  });

  it('says why an unparsable file is not shown', async () => {
    mount({ initial: null });
    await fireEvent.click(screen.getByRole('button', { name: 'Edit as text' }));
    expect(screen.getByText(/does not parse/)).toBeInTheDocument();
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
  });
});
