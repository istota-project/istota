/**
 * The staged reply in the composer: the chip, the ways it clears, and its
 * round-trip through the draft.
 *
 * The Escape ordering is the one that breaks silently — the autocomplete
 * popover owns Escape first, so a chip that grabbed it would dismiss the
 * citation instead of the menu the user was looking at.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
  chatConfigOnce: vi.fn(async () => ({
    max_prompt_chars: 32000,
    max_attachment_mb: 25,
    attachment_extensions: [],
    client_poll_interval_ms: 1500,
  })),
}));

// The attach *menu* only exists where the native pickers do (the iOS shell);
// off-shell the `+` opens a file input directly, so the Escape-ordering case
// below would be vacuous without this.
vi.mock('$lib/platform/nativePicker', () => ({
  nativePickersAvailable: vi.fn(() => true),
  takePhoto: vi.fn(),
  pickPhotos: vi.fn(),
  pickDocuments: vi.fn(),
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
}));

import { fetchChatCommands } from '$lib/api';
import { resetCommandCatalogue } from './autocomplete/providers';
import { readDraft, readDraftReply, writeDraft } from '$lib/stores/drafts';
import Composer from './Composer.svelte';

const CATALOGUE = {
  commands: [{ name: 'models', help: 'List model aliases' }],
  model_aliases: [],
};

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue(CATALOGUE);
});

const CITATION = { msgId: 22, role: 'assistant' as const, excerpt: 'the earlier answer' };

function mount(props: Record<string, unknown> = {}) {
  const onSend = vi.fn();
  const onReplyChange = vi.fn();
  const utils = render(Composer, { onSend, onReplyChange, ...props });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea, onSend, onReplyChange };
}

async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
}

describe('composer reply chip', () => {
  it('renders the staged citation', () => {
    const { container } = mount({ replyTo: CITATION });
    const chip = container.querySelector('.reply-chip');
    expect(chip?.textContent).toContain('the earlier answer');
  });

  it('renders nothing when nothing is staged', () => {
    const { container } = mount();
    expect(container.querySelector('.reply-chip')).toBeNull();
  });

  it('the × clears it', async () => {
    const { container, onReplyChange } = mount({ replyTo: CITATION });
    await fireEvent.click(
      container.querySelector<HTMLButtonElement>('[aria-label="Clear reply"]')!,
    );
    expect(onReplyChange).toHaveBeenCalledWith(null);
  });

  it('Escape clears it when no autocomplete is open', async () => {
    const { textarea, onReplyChange } = mount({ replyTo: CITATION });
    await fireEvent.keyDown(textarea, { key: 'Escape' });
    expect(onReplyChange).toHaveBeenCalledWith(null);
  });

  it('Escape dismisses the autocomplete first and leaves the chip alone', async () => {
    const { textarea, onReplyChange } = mount({ replyTo: CITATION });
    await type(textarea, '!mod');
    await tick();
    await fireEvent.keyDown(textarea, { key: 'Escape' });
    // The popover owned that Escape. A second one reaches the chip.
    expect(onReplyChange).not.toHaveBeenCalled();
    await fireEvent.keyDown(textarea, { key: 'Escape' });
    expect(onReplyChange).toHaveBeenCalledWith(null);
  });

  it('Escape closes the attach menu without clearing the chip', async () => {
    // The attach menu deliberately leaves focus in the textarea, so its Escape
    // bubbles through the same handler. Same rule as the popover: a chip that
    // took the key would dismiss the citation while a menu was being closed.
    const { container, textarea, onReplyChange } = mount({ replyTo: CITATION });
    const plus = container.querySelector<HTMLButtonElement>('[aria-label="Attach file"]')!;
    await fireEvent.click(plus);
    await tick();
    expect(container.querySelector('.attach-menu')).not.toBeNull();

    await fireEvent.keyDown(textarea, { key: 'Escape' });
    expect(onReplyChange).not.toHaveBeenCalled();
  });

  it('a send carries the citation and then clears it', async () => {
    const { textarea, onSend, onReplyChange } = mount({ replyTo: CITATION });
    await type(textarea, 'about that');
    // Bare Enter is a newline here; the chord is the keyboard send.
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('about that', [], CITATION);
    expect(onReplyChange).toHaveBeenCalledWith(null);
  });

  it('a citation does not make an empty send sendable', async () => {
    const { textarea, onSend } = mount({ replyTo: CITATION });
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe('composer reply chip — draft round-trip', () => {
  it('stores the citation alongside the text', async () => {
    const { textarea } = mount({ replyTo: CITATION, draftKey: 'carol:room:t1' });
    await type(textarea, 'half a thought');
    cleanup(); // unmount flushes
    expect(readDraft('carol:room:t1')).toBe('half a thought');
    expect(readDraftReply('carol:room:t1')).toBe(22);
  });

  it('restores a stored citation when the room comes back', async () => {
    writeDraft('carol:room:t1', 'half a thought', 22);
    const { onReplyChange } = mount({ draftKey: 'carol:room:t1' });
    await tick();
    expect(onReplyChange).toHaveBeenCalledWith(22);
  });

  it('a room with no stored citation clears whatever was staged', async () => {
    writeDraft('carol:room:t1', 'nothing cited here');
    const { onReplyChange } = mount({ replyTo: CITATION, draftKey: 'carol:room:t1' });
    await tick();
    expect(onReplyChange).toHaveBeenCalledWith(null);
  });

  it('a staged reply with nothing typed is not persisted', async () => {
    mount({ replyTo: CITATION, draftKey: 'carol:room:t1' });
    cleanup();
    // A citation is not itself a message, and `writeDraft` drops the entry on
    // an empty body.
    expect(readDraftReply('carol:room:t1')).toBeUndefined();
  });
});

describe('composer restoreSend', () => {
  it('refills the field with a handed-back send', async () => {
    const atts = [{ path: 'inbox/a.txt', name: 'a.txt', size: 3 }];
    const { textarea, rerender } = mount({ restoreSend: null });
    await rerender({
      onSend: vi.fn(),
      restoreSend: { n: 1, text: 'yes, do that', attachments: atts },
    });
    await tick();
    expect(textarea.value).toBe('yes, do that');
  });

  it('does not refill on the counter it was mounted with', async () => {
    // A remount inherits whatever the page last emitted; treating that as a
    // hand-back would put an already-recovered message back in the field.
    const { textarea } = mount({
      restoreSend: { n: 4, text: 'stale', attachments: [] },
    });
    await tick();
    expect(textarea.value).toBe('');
  });
});
