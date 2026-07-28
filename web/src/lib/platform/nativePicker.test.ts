import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  nativePickersAvailable,
  nativeUploadAvailable,
  takePhoto,
  pickPhotos,
  pickDocuments,
  uploadFromPath,
} from './nativePicker';

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
  IstotaUploader?: Record<string, ReturnType<typeof vi.fn>>;
}

function installPlugins(p: FakePlugins): FakePlugins {
  (globalThis as Record<string, unknown>).Capacitor = { Plugins: p };
  return p;
}

function base64Of(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

/**
 * A Filesystem that actually holds files and honours offset/length, so a test
 * can watch a read being taken in pieces rather than in one string.
 */
function fakeFilesystem(files: Record<string, number> = { 'file:///tmp/a.jpg': 3 }) {
  const contents = new Map<string, Uint8Array>();
  for (const [path, size] of Object.entries(files)) {
    const bytes = new Uint8Array(size);
    for (let i = 0; i < size; i++) bytes[i] = i % 251;
    contents.set(path, bytes);
  }
  return {
    contents,
    stat: vi.fn(async ({ path }: { path: string }) => {
      const bytes = contents.get(path);
      if (!bytes) throw new Error(`no such file: ${path}`);
      return { size: bytes.length, type: 'file' };
    }),
    readFile: vi.fn(async ({ path, offset, length }: FakeRead) => {
      const bytes = contents.get(path);
      if (!bytes) throw new Error(`no such file: ${path}`);
      const from = offset ?? 0;
      const to = length && length > 0 ? from + length : bytes.length;
      return { data: base64Of(bytes.slice(from, to)) };
    }),
    deleteFile: vi.fn(async ({ path }: { path: string }) => {
      contents.delete(path);
    }),
  };
}

interface FakeRead {
  path: string;
  offset?: number;
  length?: number;
}

/** Everything present, in the shape the real bridge hands over. */
function fullBridge(files?: Record<string, number>): FakePlugins {
  return installPlugins({
    Camera: {
      getPhoto: vi
        .fn()
        .mockResolvedValue({ path: 'file:///tmp/a.jpg', webPath: 'x', format: 'jpeg' }),
      pickImages: vi.fn().mockResolvedValue({
        photos: [{ path: 'file:///tmp/a.jpg', webPath: 'capacitor://x/a.jpg', format: 'jpeg' }],
      }),
      // Present on the bridge and easy to reach for, but it answers with
      // `{ results: [{ uri }] }` — see the pickPhotos suite.
      chooseFromGallery: vi.fn().mockResolvedValue({
        results: [{ type: 0, uri: 'file:///tmp/a.heic', saved: false }],
      }),
    },
    Filesystem: fakeFilesystem(files) as unknown as Record<string, ReturnType<typeof vi.fn>>,
    IstotaDocumentPicker: { pick: vi.fn().mockResolvedValue({ files: [] }) },
  });
}

/** The same bridge, plus the shell that can post a file from disk itself. */
function bridgeWithUploader(files?: Record<string, number>): FakePlugins {
  const p = fullBridge(files);
  p.IstotaUploader = {
    upload: vi.fn().mockResolvedValue({ status: 200, body: '{"path":"inbox/a.jpg"}' }),
  };
  return p;
}

beforeEach(() => {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.4.0`);
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
    // `uri` rather than `base64`: a base64 result is the whole photo in one
    // string across the bridge, which is the cost this module is built to
    // avoid. A path is read in pieces instead.
    expect(options.resultType).toBe('uri');
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
  it('picks through pickImages, not chooseFromGallery', async () => {
    // The two are not interchangeable, and picking the wrong one is silent.
    // `chooseFromGallery` answers `{ results: [{ uri }] }` — reading `.photos`
    // off that gives `undefined`, which reads exactly like a cancelled pick, so
    // the row opened a picker and then attached nothing at all.
    const p = fullBridge();

    const files = await pickPhotos();

    expect(p.Camera!.pickImages).toHaveBeenCalled();
    expect(p.Camera!.chooseFromGallery).not.toHaveBeenCalled();
    expect(files).toHaveLength(1);
  });

  it('reads each picked path through Filesystem', async () => {
    // The gallery pick is the one call that returns paths rather than bytes,
    // and a capacitor:// path cannot be fetched from an https origin.
    const p = fullBridge({ 'file:///tmp/a.jpg': 3, 'file:///tmp/b.png': 3 });
    p.Camera!.pickImages.mockResolvedValue({
      photos: [
        { path: 'file:///tmp/a.jpg', format: 'jpeg' },
        { path: 'file:///tmp/b.png', format: 'png' },
      ],
    });

    const files = await pickPhotos();

    const read = p.Filesystem!.readFile.mock.calls.map((c) => c[0].path);
    expect(read).toContain('file:///tmp/a.jpg');
    expect(read).toContain('file:///tmp/b.png');
    expect(files.map((f) => f.type)).toEqual(['image/jpeg', 'image/png']);
  });

  it('asks for a JPEG the size of the original, turned the right way up', async () => {
    // pickImages re-encodes to JPEG whatever the library holds, so a HEIC
    // arrives as something the upload endpoint recognises.
    const p = fullBridge();
    await pickPhotos();
    const options = p.Camera!.pickImages.mock.calls[0][0];
    expect(options.quality).toBe(90);
    expect(options.correctOrientation).toBe(true);
  });

  it('names each file distinctly', async () => {
    const p = fullBridge({ 'file:///tmp/a.jpg': 3, 'file:///tmp/b.jpg': 3 });
    p.Camera!.pickImages.mockResolvedValue({
      photos: [
        { path: 'file:///tmp/a.jpg', format: 'jpeg' },
        { path: 'file:///tmp/b.jpg', format: 'jpeg' },
      ],
    });
    const files = await pickPhotos();
    expect(files[0].name).not.toBe(files[1].name);
  });

  it('treats a cancelled pick as nothing picked', async () => {
    const p = fullBridge();
    p.Camera!.pickImages.mockRejectedValue(new Error('User cancelled photos app'));
    await expect(pickPhotos()).resolves.toEqual([]);
  });

  it('is inert on a bridge without the picker', async () => {
    installPlugins({ Camera: {} });
    await expect(pickPhotos()).resolves.toEqual([]);
  });
});

describe('reading a picked file', () => {
  const CHUNK = 3 * 1024 * 1024;

  it('takes a large file in pieces rather than one string', async () => {
    // The whole reason for the ceiling that used to sit here: a base64 string
    // of the entire file lands on the JS heap in one allocation, and at that
    // point the file has cost about three times its size before the upload has
    // even started. Reading a window at a time makes the peak the window.
    const size = CHUNK * 2 + 17;
    const p = fullBridge({ 'file:///tmp/a.jpg': size });

    const [file] = await pickPhotos();

    const reads = p.Filesystem!.readFile.mock.calls.map((c) => c[0]);
    expect(reads.map((r) => r.offset)).toEqual([0, CHUNK, CHUNK * 2]);
    expect(reads.every((r) => r.length <= CHUNK)).toBe(true);
    expect(file.size).toBe(size);
  });

  it('reassembles the bytes in order', async () => {
    const size = CHUNK + 512;
    const p = fullBridge({ 'file:///tmp/a.jpg': size });
    const expected = (
      p.Filesystem as unknown as { contents: Map<string, Uint8Array> }
    ).contents.get('file:///tmp/a.jpg')!;

    const [file] = await pickPhotos();

    const got = new Uint8Array(await file.blob!.arrayBuffer());
    expect(got.length).toBe(expected.length);
    expect(got[0]).toBe(expected[0]);
    expect(got[CHUNK - 1]).toBe(expected[CHUNK - 1]);
    expect(got[CHUNK]).toBe(expected[CHUNK]);
    expect(got[size - 1]).toBe(expected[size - 1]);
  });

  it('deletes the copy once it has been read', async () => {
    // Both pickers hand over a copy in the app's own temp directory. Left
    // behind they are dead weight the size of every file ever attached.
    const p = fullBridge();

    await pickPhotos();

    expect(p.Filesystem!.deleteFile).toHaveBeenCalledWith({ path: 'file:///tmp/a.jpg' });
  });

  it('still produces the file when the copy cannot be deleted', async () => {
    const p = fullBridge();
    p.Filesystem!.deleteFile.mockRejectedValue(new Error('read-only'));

    const files = await pickPhotos();

    expect(files).toHaveLength(1);
  });

  it('copes with a Filesystem that ignores the range', async () => {
    // offset/length arrived in @capacitor/filesystem 8.1. An older one answers
    // the whole file to every read, which without this guard is an endless loop
    // appending the same bytes.
    const p = fullBridge({ 'file:///tmp/a.jpg': CHUNK * 2 });
    const whole = (p.Filesystem as unknown as { contents: Map<string, Uint8Array> }).contents.get(
      'file:///tmp/a.jpg',
    )!;
    p.Filesystem!.readFile.mockResolvedValue({ data: base64Of(whole) });

    const [file] = await pickPhotos();

    expect(p.Filesystem!.readFile).toHaveBeenCalledTimes(1);
    expect(file.size).toBe(CHUNK * 2);
  });

  it('falls back to a whole-file read with no stat to size it', async () => {
    const p = fullBridge();
    delete (p.Filesystem as unknown as Record<string, unknown>).stat;

    const [file] = await pickPhotos();

    expect(file.size).toBe(3);
    expect(p.Filesystem!.readFile).toHaveBeenCalledWith({ path: 'file:///tmp/a.jpg' });
  });
});

describe('with the shell able to upload from disk', () => {
  it('never reads the file at all', async () => {
    // The point of the whole exercise. The file goes from the picker to the
    // server without entering the page: no base64, no bridge traffic, no copy
    // on the JS heap. Anything read here would be read for nothing.
    const p = bridgeWithUploader({ 'file:///tmp/a.jpg': 40 * 1024 * 1024 });

    const [picked] = await pickPhotos();

    expect(p.Filesystem!.readFile).not.toHaveBeenCalled();
    expect(picked.nativePath).toBe('file:///tmp/a.jpg');
    expect(picked.blob).toBeUndefined();
  });

  it('sizes the file with stat so it can still be checked against the limit', async () => {
    // The composer refuses an oversized file before uploading it, and without
    // reading the bytes a stat is the only way to know what it weighs.
    const p = bridgeWithUploader({ 'file:///tmp/a.jpg': 12345 });

    const [picked] = await pickPhotos();

    expect(p.Filesystem!.stat).toHaveBeenCalledWith({ path: 'file:///tmp/a.jpg' });
    expect(picked.size).toBe(12345);
  });

  it('takes the size the document picker already reported, without a stat', async () => {
    const p = bridgeWithUploader();
    p.IstotaDocumentPicker!.pick.mockResolvedValue({
      files: [
        { name: 'notes.pdf', mimeType: 'application/pdf', size: 4096, path: 'file:///tmp/n.pdf' },
      ],
    });

    const [picked] = await pickDocuments();

    expect(picked.size).toBe(4096);
    expect(p.Filesystem!.stat).not.toHaveBeenCalled();
  });

  it('reads the bytes instead when there is no uploader behind it', async () => {
    const p = fullBridge();

    const [picked] = await pickPhotos();

    expect(nativeUploadAvailable()).toBe(false);
    expect(picked.nativePath).toBeUndefined();
    expect(picked.blob?.size).toBe(3);
    expect(p.Filesystem!.readFile).toHaveBeenCalled();
  });

  it('hands the shell an absolute URL and the file details', async () => {
    const p = bridgeWithUploader();
    const [picked] = await pickPhotos();

    const result = await uploadFromPath(picked, 'https://example.test/istota/api/chat/attachments');

    expect(p.IstotaUploader!.upload).toHaveBeenCalledWith({
      url: 'https://example.test/istota/api/chat/attachments',
      path: 'file:///tmp/a.jpg',
      name: picked.name,
      mimeType: 'image/jpeg',
      fieldName: 'file',
    });
    expect(result.status).toBe(200);
  });

  it('deletes the copy whether the upload worked or not', async () => {
    const p = bridgeWithUploader();
    p.IstotaUploader!.upload.mockRejectedValue(new Error('offline'));
    const [picked] = await pickPhotos();

    await expect(uploadFromPath(picked, 'https://example.test/up')).rejects.toThrow('offline');

    expect(p.Filesystem!.deleteFile).toHaveBeenCalledWith({ path: 'file:///tmp/a.jpg' });
  });
});

describe('pickDocuments', () => {
  it('reads the path the plugin hands back', async () => {
    const p = fullBridge({ 'file:///tmp/notes.pdf': 3 });
    p.IstotaDocumentPicker!.pick.mockResolvedValue({
      files: [{ name: 'notes.pdf', mimeType: 'application/pdf', path: 'file:///tmp/notes.pdf' }],
    });

    const [file] = await pickDocuments();

    expect(file.name).toBe('notes.pdf');
    expect(file.type).toBe('application/pdf');
    expect(file.size).toBe(3);
  });

  it('still takes base64 from a shell too old to send a path', async () => {
    // 0.3.0 sent bytes. The web half deploys in minutes and the binary lags a
    // TestFlight cycle, so both shapes have to work at once.
    const p = fullBridge();
    p.IstotaDocumentPicker!.pick.mockResolvedValue({
      files: [{ name: 'notes.pdf', mimeType: 'application/pdf', data: THREE_BYTES }],
    });

    const [file] = await pickDocuments();

    expect(file.name).toBe('notes.pdf');
    expect(file.size).toBe(3);
    expect(p.Filesystem!.readFile).not.toHaveBeenCalled();
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
