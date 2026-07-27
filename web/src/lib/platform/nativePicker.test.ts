import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { nativePickersAvailable, takePhoto, pickPhotos, pickDocuments } from './nativePicker';

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1';

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

/** base64 for the three bytes 0x01 0x02 0x03. */
const THREE_BYTES = 'AQID';

interface FakePlugins {
  Camera?: Record<string, ReturnType<typeof vi.fn>>;
  Filesystem?: Record<string, ReturnType<typeof vi.fn>>;
  IstotaDocumentPicker?: Record<string, ReturnType<typeof vi.fn>>;
}

function installPlugins(p: FakePlugins): FakePlugins {
  (globalThis as Record<string, unknown>).Capacitor = { Plugins: p };
  return p;
}

/** Everything present, in the shape the real bridge hands over. */
function fullBridge(): FakePlugins {
  return installPlugins({
    Camera: {
      getPhoto: vi.fn().mockResolvedValue({ base64String: THREE_BYTES, format: 'jpeg' }),
      chooseFromGallery: vi
        .fn()
        .mockResolvedValue({ photos: [{ path: '/tmp/a.jpg', format: 'jpeg' }] }),
    },
    Filesystem: { readFile: vi.fn().mockResolvedValue({ data: THREE_BYTES }) },
    IstotaDocumentPicker: { pick: vi.fn().mockResolvedValue({ files: [] }) },
  });
}

beforeEach(() => {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.3.0`);
});

afterEach(() => {
  delete (globalThis as Record<string, unknown>).Capacitor;
  setUserAgent(PLAIN_SAFARI);
  vi.restoreAllMocks();
});

describe('nativePickersAvailable', () => {
  it('is false in an ordinary browser', () => {
    setUserAgent(PLAIN_SAFARI);
    fullBridge();
    expect(nativePickersAvailable()).toBe(false);
  });

  it('is false on a shell too old to carry the plugins', () => {
    // The two halves ship on different clocks — the web deploys in minutes, the
    // binary lags a TestFlight cycle — so this is the case that has to be inert
    // rather than broken.
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.2.0`);
    fullBridge();
    expect(nativePickersAvailable()).toBe(false);
  });

  it('is false when the shell is new enough but the bridge is not there', () => {
    expect(nativePickersAvailable()).toBe(false);
  });

  it('is true with both the version and the plugins', () => {
    fullBridge();
    expect(nativePickersAvailable()).toBe(true);
  });
});

describe('takePhoto', () => {
  it('asks the camera directly, never the prompt', async () => {
    // `PROMPT` is the source that raises the action sheet this whole module
    // exists to skip.
    const p = fullBridge();
    await takePhoto();
    const options = p.Camera!.getPhoto.mock.calls[0][0];
    expect(options.source).toBe('CAMERA');
    expect(options.resultType).toBe('base64');
  });

  it('returns the photo as a File with its bytes decoded', async () => {
    fullBridge();
    const [file] = await takePhoto();
    expect(file.type).toBe('image/jpeg');
    expect(file.name).toMatch(/^photo-\d{8}-\d{6}\.jpg$/);
    expect(file.size).toBe(3);
  });

  it('treats a cancelled shot as nothing picked', async () => {
    // The plugin rejects rather than resolving empty when the user backs out,
    // and "Could not attach" is the wrong thing to say to someone who changed
    // their mind.
    const p = fullBridge();
    p.Camera!.getPhoto.mockRejectedValue(new Error('User cancelled photos app'));
    await expect(takePhoto()).resolves.toEqual([]);
  });

  it('still reports a real failure', async () => {
    const p = fullBridge();
    p.Camera!.getPhoto.mockRejectedValue(new Error('Camera unavailable'));
    await expect(takePhoto()).rejects.toThrow('Camera unavailable');
  });
});

describe('pickPhotos', () => {
  it('reads each picked path through Filesystem', async () => {
    // The gallery pick is the one call that returns paths rather than bytes,
    // and a capacitor:// path cannot be fetched from an https origin.
    const p = fullBridge();
    p.Camera!.chooseFromGallery.mockResolvedValue({
      photos: [
        { path: '/tmp/a.jpg', format: 'jpeg' },
        { path: '/tmp/b.png', format: 'png' },
      ],
    });

    const files = await pickPhotos();

    expect(p.Filesystem!.readFile).toHaveBeenCalledWith({ path: '/tmp/a.jpg' });
    expect(p.Filesystem!.readFile).toHaveBeenCalledWith({ path: '/tmp/b.png' });
    expect(files.map((f) => f.type)).toEqual(['image/jpeg', 'image/png']);
  });

  it('asks for more than one', async () => {
    const p = fullBridge();
    await pickPhotos();
    expect(p.Camera!.chooseFromGallery.mock.calls[0][0].allowMultipleSelection).toBe(true);
  });

  it('names each file distinctly', async () => {
    const p = fullBridge();
    p.Camera!.chooseFromGallery.mockResolvedValue({
      photos: [
        { path: '/tmp/a.jpg', format: 'jpeg' },
        { path: '/tmp/b.jpg', format: 'jpeg' },
      ],
    });
    const files = await pickPhotos();
    expect(files[0].name).not.toBe(files[1].name);
  });

  it('treats a cancelled pick as nothing picked', async () => {
    const p = fullBridge();
    p.Camera!.chooseFromGallery.mockRejectedValue(new Error('User cancelled photos app'));
    await expect(pickPhotos()).resolves.toEqual([]);
  });
});

describe('pickDocuments', () => {
  it('maps the plugin result onto Files', async () => {
    const p = fullBridge();
    p.IstotaDocumentPicker!.pick.mockResolvedValue({
      files: [{ name: 'notes.pdf', mimeType: 'application/pdf', data: THREE_BYTES }],
    });

    const [file] = await pickDocuments();

    expect(file.name).toBe('notes.pdf');
    expect(file.type).toBe('application/pdf');
    expect(file.size).toBe(3);
  });

  it('returns nothing for a cancelled pick', async () => {
    // The plugin resolves empty rather than rejecting on cancel, which is the
    // shape this side prefers — no message to pattern-match.
    const p = fullBridge();
    p.IstotaDocumentPicker!.pick.mockResolvedValue({ files: [] });
    await expect(pickDocuments()).resolves.toEqual([]);
  });

  it('is inert with no plugin behind it', async () => {
    installPlugins({});
    await expect(pickDocuments()).resolves.toEqual([]);
  });
});
