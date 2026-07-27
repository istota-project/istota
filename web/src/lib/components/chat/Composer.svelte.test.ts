import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
}));

import { uploadChatAttachment, fetchChatCommands } from '$lib/api';
import { resetCommandCatalogue } from './autocomplete/providers';
import Composer from './Composer.svelte';

const upload = uploadChatAttachment as ReturnType<typeof vi.fn>;

/** jsdom leaves scrollHeight at 0, so autoGrow can't tell one line from many.
 *  Feed it a height we control to exercise the wrap threshold.
 *
 *  `narrowScrollHeight` models the width-dependent case: the composer measures
 *  wrapping with the field's wrapper pinned to its single-row width (an inline
 *  flex-basis), so a test can return a different height for that measurement
 *  than for the field's natural width. */
let fakeScrollHeight = 20;
let narrowScrollHeight: number | null = null;
Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
  configurable: true,
  get(this: HTMLTextAreaElement) {
    const pinned = (this.closest('.ta-wrap') as HTMLElement | null)?.style.flexBasis;
    return pinned && narrowScrollHeight !== null ? narrowScrollHeight : fakeScrollHeight;
  },
});

/** jsdom reports every box as 0×0, which makes the single-row width
 *  uncomputable. Give the three measured elements a size. */
Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
  configurable: true,
  get(this: HTMLElement) {
    return this.classList?.contains('composer-row') ? 400 : 0;
  },
});
Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
  configurable: true,
  get(this: HTMLElement) {
    if (this.classList?.contains('plus')) return 36;
    if (this.classList?.contains('tools')) return 72;
    return 0;
  },
});

class FakeMediaRecorder {
  static isTypeSupported = () => true;
  static last: FakeMediaRecorder | null = null;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  mimeType = 'audio/webm';
  constructor() {
    FakeMediaRecorder.last = this;
  }
  start() {}
  stop() {
    this.ondataavailable?.({ data: new Blob(['x'], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function enableMic() {
  (globalThis as Record<string, unknown>).MediaRecorder = FakeMediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
  });
}

/** Which keyboard the composer thinks it is talking to. */
function softKeyboard(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on === q.includes('coarse'),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  cleanup();
  delete (globalThis as Record<string, unknown>).MediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: undefined,
  });
  fakeScrollHeight = 20;
  narrowScrollHeight = null;
});

beforeEach(() => {
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
    commands: [],
    model_aliases: [],
  });
  upload.mockReset();
  upload.mockResolvedValue({ path: 'inbox/voice.webm', name: 'voice.webm', size: 12 });
});

function mount(props: Record<string, unknown> = {}) {
  const utils = render(Composer, { onSend: vi.fn(), ...props });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea };
}

async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
}

const btn = (c: HTMLElement, label: string) =>
  c.querySelector(`[aria-label="${label}"]`) as HTMLButtonElement | null;

describe('Composer send control', () => {
  it('is disabled with an empty field and enables once there is text', async () => {
    const { container, textarea } = mount();
    expect(btn(container, 'Send')!.disabled).toBe(true);
    await type(textarea, 'hello');
    expect(btn(container, 'Send')!.disabled).toBe(false);
  });

  it('stays disabled for whitespace-only input', async () => {
    const { container, textarea } = mount();
    await type(textarea, '   ');
    expect(btn(container, 'Send')!.disabled).toBe(true);
  });

  it('sends the trimmed text and clears the field', async () => {
    const onSend = vi.fn();
    const { container, textarea } = mount({ onSend });
    await type(textarea, '  hi there  ');
    await fireEvent.click(btn(container, 'Send')!);
    expect(onSend).toHaveBeenCalledWith('hi there', []);
    expect(textarea.value).toBe('');
  });

  it('asks for a send key on a soft keyboard', () => {
    // Enter already sends; without this the return key is labelled as a
    // newline, which is the opposite of what it does.
    const { textarea } = mount();
    expect(textarea.getAttribute('enterkeyhint')).toBe('send');
  });

  it('drops the keyboard after a send on a touch device', async () => {
    // The reply arrives behind the keyboard otherwise, and getting it out of
    // the way was a second deliberate gesture every time.
    softKeyboard(true);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(document.activeElement).not.toBe(textarea);
  });

  it('drops it after the send button too, not just the return key', async () => {
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.click(btn(container, 'Send')!);
    expect(document.activeElement).not.toBe(textarea);
  });

  it('keeps focus where there is a hardware keyboard', async () => {
    // On a desktop the next message is typed straight away, and re-focusing is
    // a mouse trip the user did not ask for.
    softKeyboard(false);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(document.activeElement).toBe(textarea);
  });

  it('keeps focus when Enter did not send', async () => {
    softKeyboard(true);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });
    expect(document.activeElement).toBe(textarea);
  });

  it('swaps to a stop control while a task is running', () => {
    const onCancel = vi.fn();
    const { container } = mount({ busy: true, onCancel });
    expect(btn(container, 'Send')).toBeNull();
    expect(btn(container, 'Stop')).toBeTruthy();
  });

  it('keeps the same element across the send/stop flip', async () => {
    const onCancel = vi.fn();
    const { container, rerender } = mount({ busy: false, onCancel });
    const before = btn(container, 'Send');
    await rerender({ busy: true, onCancel });
    // Not merely "a stop button exists": it must be the *same* node. iOS
    // re-hit-tests when it delivers a tap's synthesized click, so a swapped
    // element receives the tap that was aimed at its predecessor — a Send
    // arriving as a Stop.
    expect(btn(container, 'Stop')).toBe(before);
  });

  it('ignores an activation whose mode flipped under it', async () => {
    vi.useFakeTimers();
    try {
      const onSend = vi.fn();
      const onCancel = vi.fn();
      const { container, textarea, rerender } = mount({ onSend, onCancel, busy: false });
      await type(textarea, 'hi');
      const control = btn(container, 'Send')!;
      await fireEvent.click(control);
      expect(onSend).toHaveBeenCalledTimes(1);

      // The parent flips busy inside that same click, and the duplicate
      // delivery lands on the control now reading Stop.
      await rerender({ onSend, onCancel, busy: true });
      await fireEvent.click(control);
      expect(onCancel).not.toHaveBeenCalled();

      // Past the window it is a real tap again.
      vi.advanceTimersByTime(500);
      await fireEvent.click(control);
      expect(onCancel).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('still allows two deliberate sends in quick succession', async () => {
    vi.useFakeTimers();
    try {
      const onSend = vi.fn();
      const { container, textarea } = mount({ onSend });
      await type(textarea, 'one');
      await fireEvent.click(btn(container, 'Send')!);
      await type(textarea, 'two');
      await fireEvent.click(btn(container, 'Send')!);
      // The guard is about a flipped *mode*, not a rate limit.
      expect(onSend).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Composer layout', () => {
  it('keeps one row while the text fits and drops the controls below once it wraps', async () => {
    const { container, textarea } = mount();
    const row = container.querySelector('.composer-row')!;
    expect(row.classList.contains('multiline')).toBe(false);

    fakeScrollHeight = 80;
    await type(textarea, 'a long message that wraps over several lines');
    expect(row.classList.contains('multiline')).toBe(true);

    // Sending empties the field, so the bar collapses back to one row.
    fakeScrollHeight = 20;
    await fireEvent.click(btn(container, 'Send')!);
    await tick();
    await Promise.resolve();
    await tick();
    expect(row.classList.contains('multiline')).toBe(false);
  });

  it('stays wrapped when the text only fits because wrapping widened the field', async () => {
    // The regression: wrapping moves the controls off the field's row, so the
    // field gets wider and the text no longer wraps — measured naively that
    // flips the layout back, and the field alternates one/two rows on every
    // keystroke. Here the text wraps at the single-row width (80) but fits at
    // the full width (20), which is exactly that boundary.
    narrowScrollHeight = 80;
    fakeScrollHeight = 20;

    const { container, textarea } = mount();
    const row = container.querySelector('.composer-row')!;

    for (const value of ['aaaa', 'aaaab', 'aaaabc', 'aaaabcd']) {
      await type(textarea, value);
      expect(row.classList.contains('multiline')).toBe(true);
    }
  });
});

describe('Composer voice message', () => {
  it('hides the mic when the browser cannot record', () => {
    const { container } = mount();
    expect(btn(container, 'Record voice message')).toBeNull();
  });

  it('records, uploads the audio as an attachment, and sends it with the message', async () => {
    enableMic();
    const onSend = vi.fn();
    const { container } = mount({ onSend });

    await fireEvent.click(btn(container, 'Record voice message')!);
    await tick();
    await Promise.resolve();
    await tick();

    // Recording state: readout up, mic replaced by discard + finish.
    expect(container.querySelector('.rec-overlay')).toBeTruthy();
    expect(btn(container, 'Record voice message')).toBeNull();
    expect(btn(container, 'Discard recording')).toBeTruthy();

    await fireEvent.click(btn(container, 'Finish recording')!);
    await tick();
    await Promise.resolve();
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
    expect((upload.mock.calls[0][0] as File).name).toMatch(/\.webm$/);
    expect(container.querySelector('.rec-overlay')).toBeNull();

    // An audio-only message is sendable with no text at all.
    expect(btn(container, 'Send')!.disabled).toBe(false);
    await fireEvent.click(btn(container, 'Send')!);
    expect(onSend).toHaveBeenCalledWith('', [{ path: 'inbox/voice.webm', name: 'voice.webm' }]);
  });

  it('discarding a recording uploads nothing', async () => {
    enableMic();
    const { container } = mount();
    await fireEvent.click(btn(container, 'Record voice message')!);
    await tick();
    await Promise.resolve();
    await tick();
    await fireEvent.click(btn(container, 'Discard recording')!);
    await tick();
    expect(upload).not.toHaveBeenCalled();
    expect(btn(container, 'Record voice message')).toBeTruthy();
  });
});
