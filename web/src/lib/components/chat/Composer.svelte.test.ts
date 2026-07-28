import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
  chatConfigOnce: vi.fn(),
}));

// The pickers have their own unit tests (nativePicker.test.ts). Here the seam
// is what matters: which one a row reaches for, and whether the menu is offered
// at all.
vi.mock('$lib/platform/nativePicker', () => ({
  nativePickersAvailable: vi.fn(() => true),
  takePhoto: vi.fn(async () => []),
  pickPhotos: vi.fn(async () => []),
  pickDocuments: vi.fn(async () => []),
  // Not a seam — the real one, so a File handed to upload() behaves the way it
  // does in the app.
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
}));

import { uploadChatAttachment, fetchChatCommands, chatConfigOnce } from '$lib/api';
import {
  nativePickersAvailable,
  takePhoto,
  pickPhotos,
  pickDocuments,
} from '$lib/platform/nativePicker';
import { resetCommandCatalogue } from './autocomplete/providers';
import Composer from './Composer.svelte';

const upload = uploadChatAttachment as ReturnType<typeof vi.fn>;
const chatConfig = chatConfigOnce as ReturnType<typeof vi.fn>;
const hasNative = nativePickersAvailable as ReturnType<typeof vi.fn>;

/** A File that claims a size without allocating it. */
function sizedFile(name: string, bytes: number, type = 'image/jpeg'): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}
const native = {
  camera: takePhoto as ReturnType<typeof vi.fn>,
  photos: pickPhotos as ReturnType<typeof vi.fn>,
  documents: pickDocuments as ReturnType<typeof vi.fn>,
};

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
  chatConfig.mockReset();
  chatConfig.mockResolvedValue({
    max_prompt_chars: 32000,
    max_attachment_mb: 25,
    attachment_extensions: ['jpg', 'jpeg', 'png', 'pdf', 'webm'],
    client_poll_interval_ms: 1500,
  });
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

const menu = (c: HTMLElement) => c.querySelector('[role="menu"]');

const picker = (c: HTMLElement, kind: string) =>
  c.querySelector(`input[data-picker="${kind}"]`) as HTMLInputElement;

beforeEach(() => {
  hasNative.mockReturnValue(true);
  for (const fn of Object.values(native)) {
    fn.mockReset();
    fn.mockResolvedValue([]);
  }
});

describe('Composer attachment menu', () => {
  it('opens our own menu rather than the system sheet', async () => {
    const { container } = mount();
    expect(menu(container)).toBeNull();

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(menu(container)).toBeTruthy();
    for (const label of ['Photo Library', 'Take Photo', 'Choose File']) {
      expect(btn(container, label)).toBeTruthy();
    }
  });

  it('reaches no file input just to show the menu', async () => {
    // The whole point. WebKit's sheet is what takes the keyboard down, and it
    // goes up the moment a file input is activated — so the tap that opens the
    // menu must not touch one.
    const { container } = mount();
    const opened = vi.fn();
    for (const input of container.querySelectorAll('input[type="file"]')) {
      input.addEventListener('click', opened);
    }

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(opened).not.toHaveBeenCalled();
  });

  it('skips the menu entirely in a plain browser', async () => {
    // Without native pickers every row would end at WebKit's sheet anyway, so
    // the menu would be a step added rather than removed. The button goes
    // straight to the file input, exactly as it did before the menu existed.
    hasNative.mockReturnValue(false);
    const { container } = mount();
    const open = vi.spyOn(picker(container, 'file'), 'click');

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(menu(container)).toBeNull();
    expect(open).toHaveBeenCalled();
  });

  it('keeps the field focused when a menu row is tapped', async () => {
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await fireEvent.click(btn(container, 'Attach file')!);

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    btn(container, 'Photo Library')!.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(textarea);
  });

  it('sits inside the composer, where the global dismiss leaves it alone', async () => {
    // installKeyboardDismiss exempts `.composer` — a menu rendered anywhere else
    // would drop the keyboard on the way to being tapped.
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);
    expect(menu(container)!.closest('.composer')).toBeTruthy();
  });

  it('sends each row to the source it names', async () => {
    // The row landing where it says it will is the whole reason for the native
    // pickers — routing these back through a file input would put our menu in
    // front of WebKit's rather than instead of it.
    const rows: [string, ReturnType<typeof vi.fn>][] = [
      ['Photo Library', native.photos],
      ['Take Photo', native.camera],
      ['Choose File', native.documents],
    ];
    for (const [label, fn] of rows) {
      const { container, unmount } = mount();
      await fireEvent.click(btn(container, 'Attach file')!);
      await fireEvent.click(btn(container, label)!);
      expect(fn, label).toHaveBeenCalled();
      unmount();
    }
  });

  it('uploads what the picker hands back', async () => {
    native.photos.mockResolvedValue([new File(['x'], 'a.jpg', { type: 'image/jpeg' })]);
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalled();
  });

  it('refuses a file bigger than the server takes, without uploading it', async () => {
    // The server answers 413 only after reading the whole body, so without this
    // the user waits out an upload of a file that was never going to land. The
    // limit is the server's own, read from /chat/config.
    native.photos.mockResolvedValue([sizedFile('huge.jpg', 30 * 1024 * 1024)]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain('25 MB');
  });

  it('refuses a type the server does not accept', async () => {
    native.documents.mockResolvedValue([
      sizedFile('payload.exe', 1024, 'application/octet-stream'),
    ]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Choose File')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain('.exe');
  });

  it('carries on with the files that do fit', async () => {
    // One refusal in a batch is not a reason to drop the rest.
    native.photos.mockResolvedValue([
      sizedFile('huge.jpg', 30 * 1024 * 1024),
      sizedFile('fine.jpg', 1024),
    ]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
    expect((upload.mock.calls[0][0] as File).name).toBe('fine.jpg');
  });

  it('lets the server decide when the limits never arrived', async () => {
    // /chat/config is best-effort. Failing to reach it must not turn into a
    // client-side refusal of files the server would have taken.
    chatConfig.mockRejectedValue(new Error('offline'));
    native.photos.mockResolvedValue([sizedFile('huge.jpg', 900 * 1024 * 1024)]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
  });

  it('uploads nothing when the pick was cancelled', async () => {
    // A cancel comes back as an empty list rather than an error, so there is
    // nothing to report and nothing to send.
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')).toBeNull();
  });

  it('says so when the picker itself fails', async () => {
    native.documents.mockRejectedValue(new Error('no picker'));
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Choose File')!);
    await tick();

    expect(container.querySelector('.attach-error')?.textContent).toContain('picker');
  });

  it('closes the menu as the picker opens', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);

    expect(menu(container)).toBeNull();
  });

  it('closes on Escape', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.keyDown(window, { key: 'Escape' });

    expect(menu(container)).toBeNull();
  });

  it('closes on a tap outside it', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    await tick();

    expect(menu(container)).toBeNull();
  });

  it('closes rather than reopening when attach is tapped again', async () => {
    // The outside-tap listener sees the attach button's own pointerdown first.
    // Closing there and reopening on the click would leave it stuck open.
    const { container } = mount();
    const attach = btn(container, 'Attach file')!;
    await fireEvent.click(attach);

    attach.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    await fireEvent.click(attach);

    expect(menu(container)).toBeNull();
  });

  it('says whether it is open', async () => {
    const { container } = mount();
    const attach = btn(container, 'Attach file')!;
    expect(attach.getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(attach);
    expect(attach.getAttribute('aria-expanded')).toBe('true');
  });
});

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

  it('leaves the return key labelled as a return key', () => {
    // It inserts a newline, so it must not be labelled "send" — the label was
    // right back when Enter submitted, and is now the opposite of the truth.
    const { textarea } = mount();
    expect(textarea.getAttribute('enterkeyhint')).not.toBe('send');
  });

  it('inserts a newline on a bare Enter instead of sending', async () => {
    // A paragraph break used to submit the message, so anything longer than one
    // line could not be typed straight through.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'first line');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
    // Left to the browser — the default action is what inserts the newline.
    expect(notPrevented).toBe(true);
  });

  it('leaves Shift+Enter a newline as well', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'first line');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('sends on Cmd+Enter', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('hi', []);
  });

  it('sends on Ctrl+Enter', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
    expect(onSend).toHaveBeenCalledWith('hi', []);
  });

  it('drops the keyboard after a send on a touch device', async () => {
    // The reply arrives behind the keyboard otherwise, and getting it out of
    // the way was a second deliberate gesture every time.
    softKeyboard(true);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
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

  it('holds the keyboard up while a tap on send is still resolving', async () => {
    // The two-tap send. iOS takes focus off the field when a button takes the
    // tap, and the keyboard leaving reflows the composer down out from under
    // the finger — so the click that follows is hit-tested against the new
    // layout and lands on nothing. The first tap dismissed the keyboard and
    // sent nothing; the send needed a second one. Suppressing the default
    // focus shift keeps the field focused through the click, and submit()
    // drops the keyboard itself once the message has actually gone.
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    btn(container, 'Send')!.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(textarea);
  });

  it('holds it up for the other composer tools too', async () => {
    // Same reflow, and the attach sheet or the mic would open against a moving
    // target for the same reason.
    enableMic();
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();

    for (const label of ['Attach file', 'Record voice message']) {
      const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
      btn(container, label)!.dispatchEvent(down);
      expect(down.defaultPrevented).toBe(true);
    }
    expect(document.activeElement).toBe(textarea);
  });

  it('leaves a tap on the field itself alone', async () => {
    // Only the buttons suppress the focus shift. The textarea needs its own
    // mousedown default — that is what places the caret.
    softKeyboard(true);
    const { textarea } = mount();

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    textarea.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(false);
  });

  it('keeps focus where there is a hardware keyboard', async () => {
    // On a desktop the next message is typed straight away, and re-focusing is
    // a mouse trip the user did not ask for.
    softKeyboard(false);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
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
