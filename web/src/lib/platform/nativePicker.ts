/**
 * Attachment pickers, when the native shell can provide them.
 *
 * A `<input type="file">` cannot be aimed at a source on iOS. Tapping one
 * raises WebKit's own action sheet — Photo Library / Take Photo / Choose File —
 * and the page has no say in it: `capture` is the only hint HTML carries, and
 * `accept` has been ignored or half-applied on iOS since iOS 9
 * (rdar://36726477). So the composer's own attachment menu, which exists so the
 * keyboard survives the tap that opens it, ended at a second menu offering the
 * same three things. This module is how a row lands where it says it will.
 *
 * Everything here is gated on the shell version that ships the plugins, and
 * everything degrades to "not available" so a browser — or an older shell —
 * falls back to the file input it always had. See `native.ts` for why the User
 * Agent rather than `window.Capacitor` is the signal.
 *
 * What the pickers hand over is a path, and the bytes are read from it here a
 * window at a time. Nothing ever holds a whole file, which is why there is no
 * size at which this stops working — see `fileFromPath`.
 *
 * The reading goes through @capacitor/filesystem rather than a `fetch` of the
 * `webPath` the plugins also return. The WebView is pointed at this deployment,
 * so its origin is https, and a `capacitor://localhost` file URL read from here
 * is a cross-origin fetch that gets refused
 * (ionic-team/capacitor-plugins#705). That is invisible in every example
 * written for a bundled app, where the origin is `capacitor://localhost` too.
 */

import { shellAtLeast } from './native';

/** The shell that first carried @capacitor/camera and IstotaDocumentPicker. */
const SHELL_WITH_PICKERS = '0.3.0';

/**
 * How much of a file to read at once.
 *
 * Whatever this is, it is what the transfer costs at its peak — the reason
 * there is no longer a size ceiling here at all. A 3 MB window is 4 MB of
 * base64, which is small enough to be uninteresting and large enough that a
 * 200 MB file is not seventy round trips. Divisible by three, so no chunk
 * needs base64 padding.
 */
const CHUNK_BYTES = 3 * 1024 * 1024;

interface CapacitorPlugins {
  Camera?: {
    getPhoto(options: Record<string, unknown>): Promise<{
      path?: string;
      base64String?: string;
      format?: string;
    }>;
    pickImages?(options: Record<string, unknown>): Promise<{
      photos?: { path?: string; format?: string }[];
    }>;
  };
  Filesystem?: {
    readFile(options: {
      path: string;
      offset?: number;
      length?: number;
    }): Promise<{ data: string | Blob }>;
    stat?(options: { path: string }): Promise<{ size?: number }>;
    deleteFile?(options: { path: string }): Promise<void>;
  };
  IstotaDocumentPicker?: {
    pick(): Promise<{
      files?: { name?: string; mimeType?: string; path?: string; data?: string }[];
    }>;
  };
}

function plugins(): CapacitorPlugins | null {
  if (typeof window === 'undefined') return null;
  return (window as { Capacitor?: { Plugins?: CapacitorPlugins } }).Capacitor?.Plugins ?? null;
}

/** Can this client pick without going through a file input? */
export function nativePickersAvailable(): boolean {
  if (!shellAtLeast(SHELL_WITH_PICKERS)) return false;
  const p = plugins();
  return !!p?.Camera && !!p?.IstotaDocumentPicker;
}

/**
 * A cancelled pick is not a failure.
 *
 * The Camera plugin rejects rather than resolving empty when the user backs
 * out, and the message is the only thing distinguishing that from a real error.
 * Matching on it is unlovely, but the alternative is showing "Could not attach"
 * to someone who simply changed their mind.
 */
function isCancellation(e: unknown): boolean {
  const message = e instanceof Error ? e.message : String(e ?? '');
  return /cancel|no image picked|no images picked/i.test(message);
}

function bytesFromBase64(base64: string): Uint8Array<ArrayBuffer> {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function fileFromBase64(base64: string, name: string, type: string): File {
  return new File([bytesFromBase64(base64)], name, { type });
}

/**
 * Read a file the picker left on disk, a window at a time.
 *
 * Nothing here holds more than one chunk of base64 at once. The pieces go into
 * a File, which WebKit keeps in its blob store rather than on the JS heap, so
 * the assembled file costs far less than the string that used to carry it.
 *
 * `offset`/`length` arrived in @capacitor/filesystem 8.1. An older one ignores
 * them and answers with the whole file, which without a guard would be an
 * endless loop appending the same bytes — so a read longer than it was asked
 * for is treated as the entire file and ends the loop. With no `stat` to size
 * the file there is nothing to iterate against, and it falls back to one read.
 */
async function fileFromPath(path: string, name: string, type: string): Promise<File | null> {
  const fs = plugins()?.Filesystem;
  if (!fs) return null;
  try {
    const total = fs.stat ? ((await fs.stat({ path })).size ?? 0) : 0;
    const parts: BlobPart[] = [];
    if (!fs.stat || total <= 0) {
      const whole = await fs.readFile({ path });
      if (typeof whole.data !== 'string') return null;
      parts.push(bytesFromBase64(whole.data));
    } else {
      for (let offset = 0; offset < total;) {
        const length = Math.min(CHUNK_BYTES, total - offset);
        const chunk = await fs.readFile({ path, offset, length });
        if (typeof chunk.data !== 'string') return null;
        const bytes = bytesFromBase64(chunk.data);
        parts.push(bytes);
        if (bytes.length > length) break;
        if (bytes.length === 0) break;
        offset += bytes.length;
      }
    }
    return new File(parts, name, { type });
  } finally {
    // The copy has served its purpose either way. Left behind, the app's temp
    // directory grows by every file ever attached.
    await fs.deleteFile?.({ path }).catch(() => {});
  }
}

/** `photo-20260727-143015.jpg` — the pickers hand back paths, not names. */
function photoName(format: string, index: number): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  const suffix = index > 0 ? `-${index + 1}` : '';
  return `photo-${stamp}${suffix}.${format || 'jpg'}`;
}

/**
 * Nothing here checks a size.
 *
 * These pickers used to drop an oversized file on the floor, which looked
 * exactly like a cancelled pick. The composer's upload path checks every file
 * it is handed — from a picker, the file input, a paste or a drop — against the
 * limit the server publishes, and says so. One check, one number, one message.
 */

/** Take a photo with the camera. One shot, so at most one file. */
export async function takePhoto(): Promise<File[]> {
  const camera = plugins()?.Camera;
  if (!camera) return [];
  try {
    const photo = await camera.getPhoto({
      source: 'CAMERA',
      // `uri`, not `base64`: a 48-megapixel photo as one base64 string is the
      // allocation this module exists to avoid. The path is read in pieces.
      resultType: 'uri',
      quality: 90,
      correctOrientation: true,
      // The plugin's own crop UI is a second decision to make before the photo
      // is even attached; the composer can already remove and retake.
      allowEditing: false,
    });
    const format = photo.format || 'jpeg';
    const name = photoName(format === 'jpeg' ? 'jpg' : format, 0);
    const type = `image/${format}`;
    if (photo.path) {
      const file = await fileFromPath(photo.path, name, type);
      return file ? [file] : [];
    }
    // A shell older than 0.4.0 answers a `uri` request with base64 anyway,
    // because it asked for base64 — nothing to read from disk.
    if (photo.base64String) return [fileFromBase64(photo.base64String, name, type)];
    return [];
  } catch (e) {
    if (isCancellation(e)) return [];
    throw e;
  }
}

/**
 * Pick existing photos, more than one if wanted.
 *
 * The gallery pick is the one call that does not return its own bytes — it
 * hands back paths — so each one is read through Filesystem. That is the whole
 * reason the shell carries @capacitor/filesystem.
 *
 * `pickImages` rather than its replacement `chooseFromGallery`, despite the
 * deprecation. They are different pickers, not two names for one:
 *
 * - `pickImages` presents PHPickerViewController, re-encodes to JPEG at the
 *   asked-for quality, corrects orientation, and writes each file to the app's
 *   own temp directory — the `path` is documented as readable through
 *   Filesystem. That is the whole contract this module was built on.
 * - `chooseFromGallery` presents the plugin's own SwiftUI grid, needs full
 *   library authorisation to fill it, ignores `quality` and
 *   `correctOrientation` entirely, and returns `{ results: [{ uri }] }` — a
 *   `uri` pointing at the untouched original inside the Photos container, so a
 *   HEIC stays a HEIC at full size.
 *
 * When `pickImages` does go, the replacement is a HEIC→JPEG step on this side,
 * not a rename of the call.
 */
export async function pickPhotos(): Promise<File[]> {
  const p = plugins();
  const pick = p?.Camera?.pickImages;
  if (!pick || !p?.Camera) return [];
  try {
    const picked = await pick.call(p.Camera, {
      quality: 90,
      correctOrientation: true,
      // `limit: 0` is unlimited. Size is the composer's business, not the
      // picker's — see the note above takePhoto.
      limit: 0,
    });
    const photos = picked.photos ?? [];
    const files: File[] = [];
    for (let i = 0; i < photos.length; i++) {
      const path = photos[i].path;
      if (!path) continue;
      const format = photos[i].format || 'jpeg';
      const file = await fileFromPath(
        path,
        photoName(format === 'jpeg' ? 'jpg' : format, i),
        `image/${format}`,
      );
      if (file) files.push(file);
    }
    return files;
  } catch (e) {
    if (isCancellation(e)) return [];
    throw e;
  }
}

/**
 * Pick anything else — the Files browser, straight to it.
 *
 * Two shapes, because the two halves ship on different clocks. From 0.4.0 the
 * plugin hands over a path to the copy it already made and the bytes are read
 * from disk; 0.3.0 sent base64 in the result itself, which is what the 25 MB
 * ceiling in the Objective-C existed to bound. Feature-detected rather than
 * version-gated: the answer says which one it is.
 */
export async function pickDocuments(): Promise<File[]> {
  const picker = plugins()?.IstotaDocumentPicker;
  if (!picker) return [];
  try {
    const result = await picker.pick();
    const files: File[] = [];
    for (const f of result.files ?? []) {
      const name = f.name || 'file';
      const type = f.mimeType || 'application/octet-stream';
      if (f.path) {
        const file = await fileFromPath(f.path, name, type);
        if (file) files.push(file);
      } else if (f.data) {
        files.push(fileFromBase64(f.data, name, type));
      }
    }
    return files;
  } catch (e) {
    if (isCancellation(e)) return [];
    throw e;
  }
}
