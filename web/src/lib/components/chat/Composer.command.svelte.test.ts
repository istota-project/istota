/**
 * Typing a `!command` while a turn is in flight (ISSUE-300).
 *
 * The mode gate is one derivation shared by the button and the send chord, and
 * it used to be `busy && !!onCancel` — which refused a command exactly as it
 * refused ordinary text. That put `!steer`, the one way to reach a task that is
 * already running, out of reach on the surface where the stream is being
 * watched.
 *
 * What the gate must keep refusing is anything that would start a second turn:
 * ordinary text, a name the server does not register as a command, and the
 * `!model` prefix, which is a `!`-word that produces a task rather than an
 * inline answer.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
  chatConfigOnce: vi.fn(() => new Promise(() => {})),
}));
vi.mock('$lib/platform/nativePicker', () => ({
  nativePickersAvailable: vi.fn(() => false),
  takePhoto: vi.fn(),
  pickPhotos: vi.fn(),
  pickDocuments: vi.fn(),
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
}));

import { fetchChatCommands, uploadChatAttachment } from '$lib/api';
import { resetCommandCatalogue } from './autocomplete/providers';
import { readDraft } from '$lib/stores/drafts';
import Composer from './Composer.svelte';

const CATALOGUE = {
  commands: [
    { name: 'steer', help: 'Send a note into the running task' },
    { name: 'status', help: 'What is running' },
  ],
  model_aliases: [{ alias: 'opus', target: 'claude-opus-4-8', effort: null }],
};

afterEach(cleanup);
beforeEach(() => {
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue(CATALOGUE);
});

/** Let the catalogue fetch resolve and its effect on the gate render. */
async function settle() {
  await Promise.resolve();
  await tick();
  await tick();
}

function mount(props: Record<string, unknown> = {}) {
  const onSend = vi.fn();
  const onCancel = vi.fn();
  const utils = render(Composer, { onSend, onCancel, busy: true, ...props });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea, onSend, onCancel };
}

async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
}

const primary = (c: HTMLElement) =>
  c.querySelector('.icon-btn.send:not([aria-label="Finish recording"])') as HTMLButtonElement;

describe('Composer — a !command while a turn is in flight', () => {
  it('offers Send for a command the server registers', async () => {
    const { container, textarea, onSend, onCancel } = mount();
    await settle();
    await type(textarea, '!steer check the other branch too');

    expect(primary(container).getAttribute('aria-label')).toBe('Send');
    expect(primary(container).classList.contains('stop')).toBe(false);
    expect(primary(container).disabled).toBe(false);

    await fireEvent.click(primary(container));
    expect(onSend).toHaveBeenCalledWith('!steer check the other branch too', [], null);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('reaches the send chord too, not only the button', async () => {
    // The chord is a separate path with its own guard, and a command that can
    // only be sent by mouse is nearly as hidden as one that cannot be sent.
    const { textarea, onSend } = mount();
    await settle();
    await type(textarea, '!status');

    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('!status', [], null);
  });

  it('keeps refusing ordinary text', async () => {
    const { container, textarea, onSend, onCancel } = mount();
    await settle();
    await type(textarea, 'let me in');

    expect(primary(container).getAttribute('aria-label')).toBe('Stop');
    await fireEvent.click(primary(container));
    expect(onSend).not.toHaveBeenCalled();
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('keeps refusing a name the server does not register', async () => {
    // The catalogue is the only evidence the client has. The server would
    // answer this one inline too, so the refusal is conservatism rather than a
    // claim that it would become a task.
    const { container, textarea, onSend } = mount();
    await settle();
    await type(textarea, '!nope do the thing');

    expect(primary(container).getAttribute('aria-label')).toBe('Stop');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('keeps refusing the literal !model prefix, which makes a task rather than an answer', async () => {
    // The one `!`-prefixed body that really does become a task. Refused
    // because nothing is registered under the name `model`.
    const { container, textarea, onSend } = mount();
    await settle();
    await type(textarea, '!model opus write me a poem');

    expect(primary(container).getAttribute('aria-label')).toBe('Stop');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('leaves the draft of a send still awaiting its ack', async () => {
    // Two sends can be open in one room now, and the draft slot is per room.
    // The command is not a draft; the message still in flight is, and its
    // stored copy is the only one that survives a reload if its send fails.
    localStorage.clear();
    const { container, textarea, rerender, onSend } = mount({ busy: false, draftKey: 'room:3' });
    await settle();
    await type(textarea, 'the message that matters');
    await fireEvent.click(primary(container));
    expect(readDraft('room:3')).toBe('the message that matters');

    // Its POST is still open — which is exactly when the user asks what is
    // going on — so the composer goes busy without the send having settled.
    await rerender({ busy: true });
    await tick();
    await type(textarea, '!status');
    await fireEvent.click(primary(container));

    expect(onSend).toHaveBeenCalledTimes(2);
    expect(readDraft('room:3')).toBe('the message that matters');
  });

  it('refuses a command carrying an attachment', async () => {
    // An attachment is what makes `!model <alias>` meaningful with no prompt,
    // and a command does nothing with one. Either way it belongs to a task.
    (uploadChatAttachment as ReturnType<typeof vi.fn>).mockResolvedValue({
      path: 'inbox/a.png',
      name: 'a.png',
      size: 10,
    });
    const { container, textarea, onSend } = mount();
    await settle();
    await type(textarea, '!steer look at this');
    // Staged after the text, so the gate has to re-derive against it.
    const input = container.querySelector('input[data-picker="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'a.png')] });
    await fireEvent.change(input);
    await tick();
    await tick();
    expect(container.querySelector('.attach-chip')).toBeTruthy();

    expect(primary(container).getAttribute('aria-label')).toBe('Stop');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('leaves the gate open when no turn is running', async () => {
    const { container, textarea, onSend } = mount({ busy: false });
    await settle();
    await type(textarea, '!steer hello');

    expect(primary(container).getAttribute('aria-label')).toBe('Send');
    await fireEvent.click(primary(container));
    expect(onSend).toHaveBeenCalledWith('!steer hello', [], null);
  });

  it('stays shut while the catalogue has not landed', async () => {
    // The check is a snapshot of a fetch, and the safe answer before it
    // resolves is "not a command" — the gate as it was.
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    const { container, textarea, onSend } = mount();
    await settle();
    await type(textarea, '!steer hello');

    expect(primary(container).getAttribute('aria-label')).toBe('Stop');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });
});
