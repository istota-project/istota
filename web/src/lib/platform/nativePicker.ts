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
 * Files cross the bridge as base64 rather than as paths. The WebView is pointed
 * at this deployment, so its origin is https, and a `capacitor://localhost`
 * file URL read from here is a cross-origin fetch that gets refused
 * (ionic-team/capacitor-plugins#705). That is invisible in every example
 * written for a bundled app, where the origin is `capacitor://localhost` too.
 */

import { shellAtLeast } from './native';

/** The shell that first carried @capacitor/camera and IstotaDocumentPicker. */
const SHELL_WITH_PICKERS = '0.3.0';

/** Cost ceiling before base64 doubles it on the way across. Mirrors the
 *  document plugin's own limit so both halves refuse the same file. */
const MAX_BYTES = 25 * 1024 * 1024;

interface CapacitorPlugins {
  Camera?: {
    getPhoto(options: Record<string, unknown>): Promise<{ base64String?: string; format?: string }>;
    chooseFromGallery?(options: Record<string, unknown>): Promise<{
      photos?: { path?: string; format?: string }[];
    }>;
  };
  Filesystem?: {
    readFile(options: { path: string }): Promise<{ data: string | Blob }>;
  };
  IstotaDocumentPicker?: {
    pick(): Promise<{ files?: { name?: string; mimeType?: string; data?: string }[] }>;
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

function fileFromBase64(base64: string, name: string, type: string): File {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new File([bytes], name, { type });
}

/** `photo-20260727-143015.jpg` — the pickers hand back paths, not names. */
function photoName(format: string, index: number): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  const suffix = index > 0 ? `-${index + 1}` : '';
  return `photo-${stamp}${suffix}.${format || 'jpg'}`;
}

function tooBig(file: File): boolean {
  return file.size > MAX_BYTES;
}

/** Take a photo with the camera. One shot, so at most one file. */
export async function takePhoto(): Promise<File[]> {
  const camera = plugins()?.Camera;
  if (!camera) return [];
  try {
    const photo = await camera.getPhoto({
      source: 'CAMERA',
      resultType: 'base64',
      quality: 90,
      correctOrientation: true,
      // The plugin's own crop UI is a second decision to make before the photo
      // is even attached; the composer can already remove and retake.
      allowEditing: false,
    });
    if (!photo.base64String) return [];
    const format = photo.format || 'jpeg';
    const file = fileFromBase64(
      photo.base64String,
      photoName(format === 'jpeg' ? 'jpg' : format, 0),
      `image/${format}`,
    );
    return tooBig(file) ? [] : [file];
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
 */
export async function pickPhotos(): Promise<File[]> {
  const p = plugins();
  const gallery = p?.Camera?.chooseFromGallery;
  if (!gallery || !p?.Camera) return [];
  try {
    const picked = await gallery.call(p.Camera, {
      allowMultipleSelection: true,
      quality: 90,
      correctOrientation: true,
    });
    const photos = picked.photos ?? [];
    const files: File[] = [];
    for (let i = 0; i < photos.length; i++) {
      const path = photos[i].path;
      if (!path) continue;
      const format = photos[i].format || 'jpeg';
      const data = await p.Filesystem?.readFile({ path });
      if (typeof data?.data !== 'string') continue;
      const file = fileFromBase64(
        data.data,
        photoName(format === 'jpeg' ? 'jpg' : format, i),
        `image/${format}`,
      );
      if (!tooBig(file)) files.push(file);
    }
    return files;
  } catch (e) {
    if (isCancellation(e)) return [];
    throw e;
  }
}

/** Pick anything else — the Files browser, straight to it. */
export async function pickDocuments(): Promise<File[]> {
  const picker = plugins()?.IstotaDocumentPicker;
  if (!picker) return [];
  try {
    const result = await picker.pick();
    const files: File[] = [];
    for (const f of result.files ?? []) {
      if (!f.data) continue;
      files.push(
        fileFromBase64(f.data, f.name || 'file', f.mimeType || 'application/octet-stream'),
      );
    }
    return files;
  } catch (e) {
    if (isCancellation(e)) return [];
    throw e;
  }
}
