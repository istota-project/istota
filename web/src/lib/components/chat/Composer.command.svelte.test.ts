/**
 * Typing a `!command` while a turn is in flight (ISSUE-300, re-cut by
 * ISSUE-238).
 *
 * The composer used to hold the rule itself. Its send control was one button
 * in two modes, and the mode gate — `busy && !!onCancel && !isInlineCommand` —
 * refused ordinary text while a turn ran and carved a command out of that
 * refusal, so that `!steer`, the one way to reach a task already running, was
 * typeable on the surface watching the stream.
 *
 * There is nothing left to carve out of. A message typed against a running
 * turn is queued and sent as the next one, so everything is sendable and the
 * composer holds no opinion about what it is sending. The rule still exists,
 * at the seam that owns the invariant it protects: the store's `send()` routes
 * a known command to `sendInlineCommand`, which claims none of the three
 * things the non-re-entrant `runTurn` owns, and queues everything else.
 * `chat.command.test.ts` and `chat.queue.test.ts` are where that is asserted.
 *
 * What is left here is the composer's half: it hands every submission to
 * `onSend` whatever the turn is doing, and it no longer waits on the command
 * catalogue to decide whether it may.
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

/** Let the catalogue fetch resolve and anything watching it render. */
async function settle() {
  await Promise.resolve();
  await tick();
  await tick();
}

function mount(props: Record<string, unknown> = {}) {
  const onSend = vi.fn();
  const onCancel = vi.fn();
  const utils = render(Composer, { onSend, onCancel, busy: true, queueing: true, ...props });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea, onSend, onCancel };
}

async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
}

const sendBtn = (c: HTMLElement) =>
  c.querySelector('.icon-btn.send:not([aria-label="Finish recording"])') as HTMLButtonElement;
const stopBtn = (c: HTMLElement) => c.querySelector('.icon-btn.stop') as HTMLButtonElement | null;

describe('Composer — a !command while a turn is in flight', () => {
  it('offers Send for a command, with Stop beside it rather than in its place', async () => {
    const { container, textarea, onSend, onCancel } = mount();
    await settle();
    await type(textarea, '!steer check the other branch too');

    expect(sendBtn(container).getAttribute('aria-label')).toBe('Send');
    expect(sendBtn(container).disabled).toBe(false);
    expect(stopBtn(container)).toBeTruthy();

    await fireEvent.click(sendBtn(container));
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

  it('hands ordinary text over too, for the store to queue', async () => {
    // The refusal ISSUE-238 removed. What the composer used to eat, it now
    // passes on; `send()` sees a busy room and a body that is not a command,
    // and enqueues it as the next turn.
    const { container, textarea, onSend, onCancel } = mount();
    await settle();
    await type(textarea, 'let me in');

    await fireEvent.click(sendBtn(container));
    expect(onSend).toHaveBeenCalledWith('let me in', [], null);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('hands over a name the server does not register, rather than judging it', async () => {
    // The composer no longer holds a copy of the command rule, so it has no
    // opinion here. `send()` asks the same catalogue and, finding nothing
    // registered, queues this as an ordinary message.
    const { textarea, onSend } = mount();
    await settle();
    await type(textarea, '!nope do the thing');

    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('!nope do the thing', [], null);
  });

  it('hands over the literal !model prefix, which makes a task rather than an answer', async () => {
    // The one `!`-prefixed body that really does become a task. It queues like
    // any other message now, which is what the queue is for.
    const { textarea, onSend } = mount();
    await settle();
    await type(textarea, '!model opus write me a poem');

    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('!model opus write me a poem', [], null);
  });

  it('leaves the draft of a send still awaiting its ack', async () => {
    // Two sends can be open in one room, and the draft slot holds one. The
    // command is not a draft, and neither is a queued message — it has its own
    // persisted copy — while the message still in flight *is*: its stored copy
    // is the only one that survives a reload if its send fails.
    localStorage.clear();
    const { container, textarea, rerender, onSend } = mount({
      busy: false,
      queueing: false,
      draftKey: 'room:3',
    });
    await settle();
    await type(textarea, 'the message that matters');
    await fireEvent.click(sendBtn(container));
    expect(readDraft('room:3')).toBe('the message that matters');

    // Its POST is still open — which is exactly when the user asks what is
    // going on — so the composer goes busy without the send having settled.
    await rerender({ busy: true, queueing: true });
    await tick();
    await type(textarea, '!status');
    await fireEvent.click(sendBtn(container));

    expect(onSend).toHaveBeenCalledTimes(2);
    expect(readDraft('room:3')).toBe('the message that matters');
  });

  it('sends a command carrying an attachment, and lets the store decide', async () => {
    // An attachment used to disqualify a command from the composer's carve-out,
    // because a file belongs to a task. The store still applies exactly that
    // rule at the seam that routes the send; here it is one more submission.
    (uploadChatAttachment as ReturnType<typeof vi.fn>).mockResolvedValue({
      path: 'inbox/a.png',
      name: 'a.png',
      size: 10,
    });
    const { container, textarea, onSend } = mount();
    await settle();
    await type(textarea, '!steer look at this');
    const input = container.querySelector('input[data-picker="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'a.png')] });
    await fireEvent.change(input);
    await tick();
    await tick();
    expect(container.querySelector('.attach-chip')).toBeTruthy();

    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('!steer look at this', [expect.anything()], null);
  });

  it('sends when no turn is running, exactly as it does when one is', async () => {
    const { container, textarea, onSend } = mount({ busy: false, queueing: false });
    await settle();
    await type(textarea, '!steer hello');

    expect(sendBtn(container).getAttribute('aria-label')).toBe('Send');
    expect(stopBtn(container)).toBeNull();
    await fireEvent.click(sendBtn(container));
    expect(onSend).toHaveBeenCalledWith('!steer hello', [], null);
  });

  it('does not wait on the command catalogue to decide it may send', async () => {
    // The catalogue used to gate the button: until the fetch resolved, a
    // command was refused, because a name we could not confirm was not one to
    // let through. Nothing in the composer reads it now, so a catalogue that
    // never lands costs autocompletion and nothing else.
    (fetchChatCommands as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    const { textarea, onSend } = mount();
    await settle();
    await type(textarea, '!steer hello');

    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('!steer hello', [], null);
  });
});
