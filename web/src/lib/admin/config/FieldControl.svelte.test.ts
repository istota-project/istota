import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { AdminConfigField, ConfigOp } from '$lib/api';
import { configField } from '$lib/test/adminConfig';
import FieldControl from './FieldControl.svelte';

afterEach(cleanup);

function mount(field: AdminConfigField, extra: { op?: ConfigOp; secretPending?: string } = {}) {
  const onOp = vi.fn();
  const onSecret = vi.fn();
  const view = render(FieldControl, {
    props: {
      field,
      op: extra.op,
      secretPending: extra.secretPending,
      editable: true,
      onOp,
      onSecret,
    },
  });
  return { ...view, onOp, onSecret };
}

describe('the field doc', () => {
  it.each([
    ['a string field', configField({ doc: 'Where mail is read from.' })],
    ['a bool field', configField({ doc: 'Where mail is read from.', editor: 'bool', value: true })],
    [
      'a credential field',
      configField({
        doc: 'Where mail is read from.',
        secret: true,
        set: true,
        value: null,
        editor: 'secret',
        credential: { kind: 'file', name: 'istota_email_imap_password' },
      }),
    ],
  ])('is behind the label hint on %s, not a disclosure', async (_kind, field) => {
    const { container } = mount(field);
    expect(container.querySelector('details')).toBeNull();
    expect(screen.queryByText('Where mail is read from.')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByLabelText(`About ${field.name}`));
    expect(await screen.findByText('Where mail is read from.')).toBeInTheDocument();
  });

  it('puts Reset on the control row', () => {
    const { container } = mount(configField({ in_file: true }));
    const reset = screen.getByRole('button', { name: 'Reset' });
    expect(reset.closest('.field-row.inline-meta')).toBe(container.querySelector('.field-row'));
    expect(container.querySelector('.field-row .field.inline')).not.toBeNull();
  });

  it('puts the default mark inside the control, not in the row beside it', () => {
    mount(configField({ in_file: false, default: 'd' }));
    const mark = screen.getByRole('img', { name: 'Not in config.toml; the default applies' });
    expect(mark.closest('.adorned')?.querySelector('input')).not.toBeNull();
  });
});

describe('a control per editor kind', () => {
  it('a string field emits a set at its path', async () => {
    const { onOp } = mount(configField({ path: ['email', 'imap_host'], value: 'old' }));
    await fireEvent.input(screen.getByLabelText('imap_host'), { target: { value: 'new' } });
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set', path: ['email', 'imap_host'], value: 'new' },
      ['email', 'imap_host'],
    );
  });

  it('typing the loaded value back drops the change', async () => {
    const { onOp } = mount(configField({ value: 'old' }), {
      op: { op: 'set', path: ['email', 'imap_host'], value: 'x' },
    });
    await fireEvent.input(screen.getByLabelText('imap_host'), { target: { value: 'old' } });
    expect(onOp).toHaveBeenLastCalledWith(null, ['email', 'imap_host']);
  });

  it('a bool field is a checkbox', async () => {
    const field = configField({ path: ['logging', 'rotate'], value: true, editor: 'bool' });
    const { onOp } = mount(field);
    const box = screen.getByLabelText('rotate') as HTMLInputElement;
    expect(box.checked).toBe(true);
    await fireEvent.click(box);
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set', path: ['logging', 'rotate'], value: false },
      ['logging', 'rotate'],
    );
  });

  it('an int field refuses a fraction and emits a whole number', async () => {
    const field = configField({ path: ['sms', 'max_segments'], value: 6, editor: 'int' });
    const { onOp } = mount(field);
    const input = screen.getByLabelText('max_segments');
    await fireEvent.input(input, { target: { value: '2.5' } });
    expect(onOp).toHaveBeenLastCalledWith(null, ['sms', 'max_segments']);
    expect(screen.getByText('Enter a whole number.')).toBeInTheDocument();
    await fireEvent.input(input, { target: { value: '8' } });
    expect(onOp).toHaveBeenLastCalledWith({ op: 'set', path: ['sms', 'max_segments'], value: 8 }, [
      'sms',
      'max_segments',
    ]);
  });

  it('a list field takes one item per line', async () => {
    const field = configField({ path: ['sms', 'service_numbers'], value: ['+1'], editor: 'list' });
    const { onOp } = mount(field);
    await fireEvent.input(screen.getByLabelText('service_numbers'), {
      target: { value: '+1\n+2\n' },
    });
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set', path: ['sms', 'service_numbers'], value: ['+1', '+2'] },
      ['sms', 'service_numbers'],
    );
  });

  it('a multi-choice field toggles chips in vocabulary order', async () => {
    const field = configField({
      path: ['web', 'auth'],
      value: ['nextcloud'],
      editor: 'multi_choice',
      multi_choices: ['email', 'nextcloud'],
    });
    const { onOp } = mount(field);
    await fireEvent.click(screen.getByRole('button', { name: 'email' }));
    expect(onOp).toHaveBeenLastCalledWith(
      { op: 'set', path: ['web', 'auth'], value: ['email', 'nextcloud'] },
      ['web', 'auth'],
    );
  });

  it('a choice field renders a select on its current value', () => {
    mount(
      configField({
        path: ['logging', 'output'],
        value: 'both',
        editor: 'choice',
        choices: ['console', 'file', 'both'],
      }),
    );
    expect(screen.getByRole('button', { name: 'output' })).toHaveTextContent('both');
  });

  it('a table field is edited as TOML and can be removed from the file', async () => {
    const field = configField({
      path: ['models', 'aliases'],
      editor: 'table',
      value: { fast: 'haiku' },
      in_file: true,
      toml: '[aliases]\nfast = "haiku"\n',
    });
    const { onOp } = mount(field);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(
      '[aliases]\nfast = "haiku"\n',
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    expect(onOp).toHaveBeenLastCalledWith({ op: 'unset', path: ['models', 'aliases'] }, [
      'models',
      'aliases',
    ]);
  });

  it('a table pending removal shows no editor', () => {
    mount(configField({ path: ['models', 'aliases'], editor: 'table', in_file: true, toml: 'x' }), {
      op: { op: 'unset', path: ['models', 'aliases'] },
    });
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Removed from config.toml.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();
  });
});

describe('defaults and reset', () => {
  it('reset to default emits unset', async () => {
    const { onOp } = mount(configField({ value: 'x', default: '', in_file: true }));
    await fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    expect(onOp).toHaveBeenLastCalledWith({ op: 'unset', path: ['email', 'imap_host'] }, [
      'email',
      'imap_host',
    ]);
  });

  it('a key not in the file is empty with the default as placeholder, and marked', () => {
    mount(configField({ value: 'd', default: 'd', in_file: false }));
    const input = screen.getByLabelText('imap_host') as HTMLInputElement;
    expect(input.value).toBe('');
    expect(input.placeholder).toBe('d');
    expect(
      screen.getByRole('img', { name: 'Not in config.toml; the default applies' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reset' })).toBeNull();
  });

  it('keeps the mark while a key not in the file is changed', () => {
    const field = configField({ value: 'd', default: 'd', in_file: false });
    mount(field, { op: { op: 'set', path: field.path, value: 'x' } });
    expect((screen.getByLabelText('imap_host') as HTMLInputElement).value).toBe('x');
    expect(
      screen.getByRole('img', { name: 'Not in config.toml yet; this change adds it' }),
    ).toBeInTheDocument();
  });

  it('a key in the file has no mark', () => {
    mount(configField({ value: 'x', default: 'd', in_file: true }));
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('emptying a changed default field drops the change', async () => {
    const field = configField({ value: 'd', default: 'd', in_file: false });
    const { onOp } = mount(field, { op: { op: 'set', path: field.path, value: 'x' } });
    await fireEvent.input(screen.getByLabelText('imap_host'), { target: { value: '' } });
    expect(onOp).toHaveBeenLastCalledWith(null, field.path);
  });

  it('emptying a field in the file resets it', async () => {
    const field = configField({ value: 'x', default: 'd', in_file: true });
    const { onOp } = mount(field);
    await fireEvent.input(screen.getByLabelText('imap_host'), { target: { value: '' } });
    expect(onOp).toHaveBeenLastCalledWith({ op: 'unset', path: field.path }, field.path);
  });

  it('a pending reset shows the default as placeholder and can be undone', async () => {
    const field = configField({ value: 'x', default: 'd', in_file: true });
    const { onOp } = mount(field, { op: { op: 'unset', path: field.path } });
    const input = screen.getByLabelText('imap_host') as HTMLInputElement;
    expect(input.value).toBe('');
    expect(input.placeholder).toBe('d');
    await fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(onOp).toHaveBeenLastCalledWith(null, field.path);
  });
});

describe('read-only fields', () => {
  it('show their value and the reason, with no input', () => {
    mount(
      configField({
        path: ['site', 'hostname'],
        value: 'istota.example.org',
        editor: 'readonly',
        read_only_reason: 'Checked against host.env at start. Change it on the host.',
      }),
    );
    expect(screen.getByText('istota.example.org')).toBeInTheDocument();
    expect(
      screen.getByText('Checked against host.env at start. Change it on the host.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('name the environment variable that overrides them', () => {
    mount(
      configField({
        path: ['web', 'token_storage'],
        editor: 'readonly',
        env_override: 'ISTOTA_WEB_TOKEN_STORAGE',
        read_only_reason: 'Set by the environment variable ISTOTA_WEB_TOKEN_STORAGE.',
      }),
    );
    expect(screen.getByText(/ISTOTA_WEB_TOKEN_STORAGE/)).toBeInTheDocument();
  });

  it('render only the value when the page cannot edit', () => {
    render(FieldControl, {
      props: {
        field: configField({ value: 'imap.example.org', in_file: true }),
        op: undefined,
        editable: false,
        onOp: vi.fn(),
        onSecret: vi.fn(),
      },
    });
    expect(screen.getByText('imap.example.org')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reset' })).toBeNull();
  });
});

describe('credentials', () => {
  const secret = configField({
    path: ['email', 'imap_password'],
    secret: true,
    set: true,
    value: null,
    editor: 'secret',
    credential: { kind: 'file', name: 'istota_email_imap_password' },
  });

  it('never render a stored value, only that one is set', () => {
    const { container } = mount(secret);
    const input = screen.getByLabelText('imap_password') as HTMLInputElement;
    expect(input.value).toBe('');
    expect(input.type).toBe('password');
    expect(input.placeholder).toMatch(/stored/);
    expect(container.innerHTML).not.toMatch(/value="[^"]+"/);
  });

  it('route a new value and a clear by the credential name', async () => {
    const { onSecret } = mount(secret);
    await fireEvent.input(screen.getByLabelText('imap_password'), { target: { value: 's3cret' } });
    expect(onSecret).toHaveBeenLastCalledWith('istota_email_imap_password', 's3cret');
    await fireEvent.click(screen.getByRole('button', { name: 'Clear stored imap_password' }));
    expect(onSecret).toHaveBeenLastCalledWith('istota_email_imap_password', '');
  });

  it('show a pending clear as a state with undo', async () => {
    const { onSecret } = mount(secret, { secretPending: '' });
    expect(screen.getByText('Will be cleared.')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(onSecret).toHaveBeenLastCalledWith('istota_email_imap_password', null);
  });
});
