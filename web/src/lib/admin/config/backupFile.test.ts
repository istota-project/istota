import { describe, expect, it } from 'vitest';
import { configView } from '$lib/test/adminConfig';
import { backupStamp, fromBase64, plainBackup } from './backupFile';

const at = new Date('2026-11-13T09:15:00.123Z');
const preamble = '# header\n\n';

function view(raw: string | null, limit: number) {
  return configView([], {
    raw_toml: raw,
    backup_limits: { config_bytes: limit, config_preamble: preamble },
  });
}

describe('plainBackup', () => {
  it('is the preamble and the redacted file, named by the UTC time', () => {
    const built = plainBackup(view('[web]\nport = 1\n', 1000), at);
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(new TextDecoder().decode(built.bytes)).toBe(`${preamble}[web]\nport = 1\n`);
    expect(built.filename).toBe('istota-config-20261113T091500Z.toml');
  });

  it('counts UTF-8 bytes with the preamble, inclusive at the limit', () => {
    // "é" is two bytes and one character.
    const raw = 'name = "é"\n';
    const size = new TextEncoder().encode(preamble + raw).length;
    expect(size).toBe(preamble.length + raw.length + 1);
    expect(plainBackup(view(raw, size), at).ok).toBe(true);
    const over = plainBackup(view(raw, size - 1), at);
    expect(over).toEqual({
      ok: false,
      message: `The configuration is ${size} bytes, over the backup limit of ${size - 1} bytes. Nothing was downloaded.`,
    });
  });

  it('refuses a 300 KiB file against the server limit', () => {
    const raw = `x = "${'a'.repeat(300 * 1024)}"\n`;
    expect(plainBackup(view(raw, 256 * 1024), at).ok).toBe(false);
  });

  it('offers nothing without the server limit or a readable file', () => {
    const noLimit = plainBackup(configView([], { raw_toml: 'a = 1\n', backup_limits: null }), at);
    expect(noLimit.ok).toBe(false);
    expect(!noLimit.ok && noLimit.message).toMatch(/Reload the page/);
    const absent = configView([], { raw_toml: 'a = 1\n' });
    delete absent.backup_limits;
    expect(plainBackup(absent, at).ok).toBe(false);
    expect(plainBackup(view(null, 1000), at).ok).toBe(false);
  });
});

it('stamps and decodes as the server does', () => {
  expect(backupStamp(at)).toBe('20261113T091500Z');
  expect(Array.from(fromBase64(btoa('\u0000ÿ')))).toEqual([0, 255]);
});
