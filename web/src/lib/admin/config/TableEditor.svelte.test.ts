import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { AdminConfigField, ConfigOp } from '$lib/api';
import { configField } from '$lib/test/adminConfig';
import TableEditor from './TableEditor.svelte';

afterEach(cleanup);

function mount(field: AdminConfigField, op?: ConfigOp) {
  const onOp = vi.fn();
  render(TableEditor, { props: { field, op, onOp } });
  return { onOp, textarea: screen.getByRole('textbox') as HTMLTextAreaElement };
}

const aliases = configField({
  path: ['models', 'aliases'],
  editor: 'table',
  in_file: true,
  toml: '[aliases]\nfast = "haiku"\n',
});

describe('the table editor', () => {
  it('starts from the table as config.toml holds it', () => {
    expect(mount(aliases).textarea.value).toBe('[aliases]\nfast = "haiku"\n');
  });

  it('starts empty for a table the file does not hold', () => {
    expect(mount(configField({ ...aliases, in_file: false, toml: '' })).textarea.value).toBe('');
  });

  it('shows a pending edit over the file value', () => {
    const { textarea } = mount(aliases, {
      op: 'set_toml',
      path: ['models', 'aliases'],
      toml: '[aliases]\nsmart = "opus"\n',
    });
    expect(textarea.value).toBe('[aliases]\nsmart = "opus"\n');
  });

  it('emits set_toml with the snippet, and nothing when it is the file value again', async () => {
    const { onOp, textarea } = mount(aliases);
    const edited = '[aliases]\nfast = "haiku"\nsmart = "opus"\n';
    await fireEvent.input(textarea, { target: { value: edited } });
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set_toml', path: ['models', 'aliases'], toml: edited },
      ['models', 'aliases'],
    );
    await fireEvent.input(textarea, { target: { value: '[aliases]\nfast = "haiku"\n' } });
    expect(onOp).toHaveBeenLastCalledWith(null, ['models', 'aliases']);
  });

  it('refuses an empty snippet and points at removing the key instead', async () => {
    const { onOp, textarea } = mount(aliases);
    await fireEvent.input(textarea, { target: { value: '  \n' } });
    expect(onOp).toHaveBeenLastCalledWith(null, ['models', 'aliases']);
    expect(screen.getByText(/Use Reset to default to remove the key/)).toBeInTheDocument();
  });

  it('keeps an empty list distinct from an absent key', async () => {
    const blocks = configField({
      path: ['briefing_shared_blocks'],
      editor: 'table',
      in_file: false,
      toml: '',
    });
    const { onOp, textarea } = mount(blocks);
    expect(screen.getByText(/shipped default blocks apply/)).toBeInTheDocument();
    await fireEvent.input(textarea, { target: { value: 'briefing_shared_blocks = []\n' } });
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set_toml', path: ['briefing_shared_blocks'], toml: 'briefing_shared_blocks = []\n' },
      ['briefing_shared_blocks'],
    );
  });

  it('explains the credential sentinel when the table holds one', () => {
    mount(
      configField({
        path: ['brain', 'native', 'extra_headers'],
        editor: 'table',
        in_file: true,
        toml: '[extra_headers]\nx-tenant = "<unchanged>"\n',
      }),
    );
    expect(screen.getByText(/stands for a stored credential/)).toBeInTheDocument();
  });
});
