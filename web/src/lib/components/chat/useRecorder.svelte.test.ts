import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { createRecorder, formatElapsed, recordingSupported } from './useRecorder.svelte';

/** Minimal MediaRecorder double: records the handlers the module installs and
 *  lets a test drive stop() by hand. */
class FakeMediaRecorder {
  static supported: string[] = ['audio/webm;codecs=opus'];
  static isTypeSupported = (t: string) => FakeMediaRecorder.supported.includes(t);
  static last: FakeMediaRecorder | null = null;

  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  state = 'inactive';
  mimeType: string;

  constructor(
    public stream: unknown,
    opts?: { mimeType?: string },
  ) {
    this.mimeType = opts?.mimeType ?? 'audio/webm';
    FakeMediaRecorder.last = this;
  }
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['xx'], { type: this.mimeType }) });
    this.onstop?.();
  }
}

const tracks = { stopped: 0 };
function installMediaStack(getUserMedia?: () => Promise<unknown>) {
  tracks.stopped = 0;
  (globalThis as Record<string, unknown>).MediaRecorder = FakeMediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia:
        getUserMedia ??
        (async () => ({
          getTracks: () => [
            {
              stop() {
                tracks.stopped++;
              },
            },
          ],
        })),
    },
  });
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).MediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: undefined,
  });
  FakeMediaRecorder.supported = ['audio/webm;codecs=opus'];
  FakeMediaRecorder.last = null;
});

describe('formatElapsed', () => {
  it('renders mm:ss with a zero-padded seconds field', () => {
    expect(formatElapsed(0)).toBe('0:00');
    expect(formatElapsed(4200)).toBe('0:04');
    expect(formatElapsed(65_000)).toBe('1:05');
    expect(formatElapsed(600_000)).toBe('10:00');
  });
});

describe('recordingSupported', () => {
  it('is false without mediaDevices — an insecure context hides the mic', () => {
    expect(recordingSupported()).toBe(false);
  });

  it('is true once getUserMedia and MediaRecorder exist', () => {
    installMediaStack();
    expect(recordingSupported()).toBe(true);
  });
});

describe('createRecorder', () => {
  beforeEach(() => installMediaStack());

  it('records and hands back a File named for the negotiated container', async () => {
    const onComplete = vi.fn();
    const rec = createRecorder({ onComplete });
    await rec.start();
    expect(rec.recording).toBe(true);

    rec.stop();
    expect(onComplete).toHaveBeenCalledTimes(1);
    const file = onComplete.mock.calls[0][0] as File;
    expect(file).toBeInstanceOf(File);
    expect(file.name).toMatch(/^voice-\d{8}-\d{6}\.webm$/);
    expect(rec.recording).toBe(false);
    // The mic is released, or the browser keeps showing its recording chip.
    expect(tracks.stopped).toBe(1);
  });

  it('names an iOS recording .m4a — audio/mp4 is the only type Safari offers', async () => {
    FakeMediaRecorder.supported = ['audio/mp4'];
    const onComplete = vi.fn();
    const rec = createRecorder({ onComplete });
    await rec.start();
    rec.stop();
    expect((onComplete.mock.calls[0][0] as File).name).toMatch(/\.m4a$/);
  });

  it('cancel discards the audio but still frees the mic', async () => {
    const onComplete = vi.fn();
    const rec = createRecorder({ onComplete });
    await rec.start();
    rec.cancel();
    expect(onComplete).not.toHaveBeenCalled();
    expect(rec.recording).toBe(false);
    expect(tracks.stopped).toBe(1);
  });

  it('reports a denied permission instead of starting', async () => {
    installMediaStack(async () => {
      throw new DOMException('no', 'NotAllowedError');
    });
    const rec = createRecorder({ onComplete: vi.fn() });
    await rec.start();
    expect(rec.recording).toBe(false);
    expect(rec.error).toMatch(/denied/i);
  });

  it('distinguishes a missing microphone from a refusal', async () => {
    installMediaStack(async () => {
      throw new DOMException('no', 'NotFoundError');
    });
    const rec = createRecorder({ onComplete: vi.fn() });
    await rec.start();
    expect(rec.error).toMatch(/no microphone/i);
  });

  it('dispose during a recording drops the audio and releases the mic', async () => {
    const onComplete = vi.fn();
    const rec = createRecorder({ onComplete });
    await rec.start();
    rec.dispose();
    expect(onComplete).not.toHaveBeenCalled();
    expect(tracks.stopped).toBe(1);
  });
});
