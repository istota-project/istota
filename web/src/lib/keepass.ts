/** KeePass 2.0 key files, generated only in the browser. */
export async function generateKeyfile(): Promise<string> {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const hex = (value: Uint8Array) =>
    Array.from(value, (byte) => byte.toString(16).padStart(2, '0'))
      .join('')
      .toUpperCase();
  return `<?xml version="1.0" encoding="utf-8"?><KeyFile><Meta><Version>2.0</Version></Meta><Key><Data Hash="${hex(digest.slice(0, 4))}">${hex(bytes).match(/.{8}/g)!.join(' ')}</Data></Key></KeyFile>`;
}

export { downloadBlob } from '$lib/download';
