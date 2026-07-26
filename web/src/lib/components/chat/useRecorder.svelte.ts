// Voice-message recorder for the composer. Wraps getUserMedia + MediaRecorder
// and hands back a File the caller uploads through the ordinary attachment
// path — the executor's `_pre_transcribe_attachments` transcribes any audio
// attachment on the way into the prompt, so this needs no dedicated endpoint
// (same shape as a Talk voice message).
//
// Reactive state lives here (a `.svelte.ts` runes module) so a component
// reading the getters re-renders as the timer ticks.

/** Container types in preference order, with the extension we name the file.
 *  Chrome/Firefox produce webm/opus; iOS Safari only offers audio/mp4, which
 *  is an AAC-in-MP4 stream — `.m4a` is its correct audio-only extension and is
 *  already an allowed chat attachment type, so iOS needs no server change. */
const MIME_CANDIDATES: [string, string][] = [
  ['audio/webm;codecs=opus', 'webm'],
  ['audio/webm', 'webm'],
  ['audio/mp4', 'm4a'],
  ['audio/ogg;codecs=opus', 'ogg'],
  ['audio/mpeg', 'mp3'],
];

function pickMime(): [string, string] | null {
  if (typeof MediaRecorder === 'undefined') return null;
  for (const [mime, ext] of MIME_CANDIDATES) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return [mime, ext];
    } catch {
      // isTypeSupported throws on some older engines; treat as unsupported.
    }
  }
  // A browser with MediaRecorder but no recognised type still records in its
  // own default; let it, and name the file .webm.
  return ['', 'webm'];
}

/** True when recording is actually possible here. getUserMedia is undefined
 *  outside a secure context, so this is also what makes the mic button vanish
 *  on a plain-http LAN address instead of failing on tap. */
export function recordingSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== 'undefined'
  );
}

export interface Recorder {
  /** False when the browser/context can't record — hide the mic entirely. */
  readonly supported: boolean;
  readonly recording: boolean;
  /** True between the tap and the first byte (mic permission prompt). */
  readonly starting: boolean;
  readonly elapsedMs: number;
  /** Human-readable failure (permission denied, no device). '' when fine. */
  readonly error: string;
  start(): Promise<void>;
  /** Stop and hand the recording to onComplete. */
  stop(): void;
  /** Stop and throw the recording away. */
  cancel(): void;
  /** Release the mic (component teardown) — without this the browser keeps
   *  showing its recording indicator after the page moves on. */
  dispose(): void;
}

export interface RecorderOptions {
  onComplete: (file: File) => void;
}

export function createRecorder(opts: RecorderOptions): Recorder {
  let recording = $state(false);
  let starting = $state(false);
  let elapsedMs = $state(0);
  let error = $state('');

  let stream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let chunks: Blob[] = [];
  let ext = 'webm';
  let timer: ReturnType<typeof setInterval> | null = null;
  let startedAt = 0;
  let discard = false;

  function releaseStream() {
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  function stopTimer() {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  function reset() {
    stopTimer();
    recording = false;
    starting = false;
    elapsedMs = 0;
    recorder = null;
    chunks = [];
    releaseStream();
  }

  function fileName(): string {
    const d = new Date();
    const p = (n: number) => String(n).padStart(2, '0');
    return `voice-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${ext}`;
  }

  async function start() {
    if (recording || starting) return;
    error = '';
    if (!recordingSupported()) {
      error = 'Recording is not available in this browser.';
      return;
    }
    starting = true;
    discard = false;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      starting = false;
      releaseStream();
      const name = e instanceof DOMException ? e.name : '';
      error =
        name === 'NotAllowedError' || name === 'SecurityError'
          ? 'Microphone access denied.'
          : name === 'NotFoundError'
            ? 'No microphone found.'
            : 'Could not start recording.';
      return;
    }

    const picked = pickMime();
    ext = picked?.[1] ?? 'webm';
    const mime = picked?.[0] ?? '';
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      starting = false;
      releaseStream();
      error = 'Could not start recording.';
      return;
    }

    chunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    recorder.onstop = () => {
      const parts = chunks;
      const type = recorder?.mimeType || mime || 'audio/webm';
      const wanted = !discard && parts.length > 0;
      const name = fileName();
      reset();
      if (wanted) {
        const blob = new Blob(parts, { type });
        opts.onComplete(new File([blob], name, { type }));
      }
    };
    recorder.onerror = () => {
      error = 'Recording failed.';
      discard = true;
      try {
        recorder?.stop();
      } catch {
        reset();
      }
    };

    recorder.start();
    starting = false;
    recording = true;
    startedAt = Date.now();
    elapsedMs = 0;
    timer = setInterval(() => {
      elapsedMs = Date.now() - startedAt;
    }, 200);
  }

  function finish(keep: boolean) {
    if (!recording || !recorder) return;
    discard = !keep;
    stopTimer();
    try {
      recorder.stop(); // onstop does the rest
    } catch {
      reset();
    }
  }

  return {
    get supported() {
      return recordingSupported();
    },
    get recording() {
      return recording;
    },
    get starting() {
      return starting;
    },
    get elapsedMs() {
      return elapsedMs;
    },
    get error() {
      return error;
    },
    start,
    stop: () => finish(true),
    cancel: () => finish(false),
    dispose: () => {
      discard = true;
      if (recorder && recording) {
        try {
          recorder.stop();
        } catch {
          /* fall through to reset */
        }
      }
      reset();
    },
  };
}

/** mm:ss for the recording readout. */
export function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
