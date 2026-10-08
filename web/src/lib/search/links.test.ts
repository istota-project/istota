import { base } from '$app/paths';
import { describe, expect, it } from 'vitest';
import { hrefFor } from './links';
describe('search links', () => {
  it('encodes route params and rejects unknown routes', () => {
    expect(hrefFor({ type: 'route', path: '/chat/', params: { room: 'a&b', msg: '42' } })).toBe(
      `${base}/chat/?room=a%26b&msg=42`,
    );
    expect(hrefFor({ type: 'route', path: '//example.com/', params: {} })).toBeNull();
  });
  it('leaves files to the viewer and handles absent links', () => {
    expect(hrefFor({ type: 'file', path: '/Users/alice/memories/note.md' })).toBeNull();
    expect(hrefFor(null)).toBeNull();
  });
});
