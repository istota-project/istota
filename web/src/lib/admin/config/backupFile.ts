import type { AdminConfigView } from '$lib/api';

/** `20261113T091500Z`, the stamp the server puts in an encrypted backup's name. */
export function backupStamp(at: Date = new Date()): string {
  return at
    .toISOString()
    .replace(/\.\d{3}Z$/, 'Z')
    .replace(/[-:]/g, '');
}

export type PlainBackup =
  { ok: true; bytes: Uint8Array<ArrayBuffer>; filename: string } | { ok: false; message: string };

/**
 * The plain download built from a fresh view: the server's preamble and the
 * redacted file, measured in UTF-8 bytes against the server's own limit
 * before anything is handed to the browser. Without a limit there is no
 * download, rather than an unchecked one.
 */
export function plainBackup(view: AdminConfigView, at: Date = new Date()): PlainBackup {
  const limits = view.backup_limits;
  if (!limits || typeof limits.config_bytes !== 'number') {
    return {
      ok: false,
      message: 'This server did not say how large a backup may be. Reload the page and try again.',
    };
  }
  if (view.raw_toml === null) {
    return {
      ok: false,
      message: 'config.toml could not be read, so there is no saved configuration to download.',
    };
  }
  const bytes = new TextEncoder().encode(limits.config_preamble + view.raw_toml);
  if (bytes.length > limits.config_bytes) {
    return {
      ok: false,
      message:
        `The configuration is ${bytes.length} bytes, over the backup limit of ` +
        `${limits.config_bytes} bytes. Nothing was downloaded.`,
    };
  }
  return { ok: true, bytes, filename: `istota-config-${backupStamp(at)}.toml` };
}

/** The bytes of a base64 string the server sent. */
export function fromBase64(text: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(text), (char) => char.charCodeAt(0));
}
