import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { encode } from 'uqr';
import QrCode from './QrCode.svelte';

const PAYLOAD = JSON.stringify({ v: 1, endpoint: 'https://example.invalid/webhooks/location' });

afterEach(cleanup);

function draw(value: string) {
  const { container } = render(QrCode, { value });
  const svg = container.querySelector('svg')!;
  return { svg, rects: [...svg.querySelectorAll('rect')] };
}

describe('QrCode', () => {
  it('draws one rect per dark module, plus the background', () => {
    const expected = encode(PAYLOAD).data.flat().filter(Boolean).length;
    const { rects } = draw(PAYLOAD);
    expect(rects).toHaveLength(expected + 1);
  });

  it('leaves the quiet zone the spec requires, inside the viewBox', () => {
    // Four modules on every side. As padding it would be the page's colour
    // rather than the code's background, which is not a quiet zone at all.
    const { svg } = draw(PAYLOAD);
    expect(svg.getAttribute('viewBox')).toBe(
      `0 0 ${encode(PAYLOAD).size + 8} ${encode(PAYLOAD).size + 8}`,
    );
  });

  it('draws dark on light regardless of theme', () => {
    // An inverted code does not decode on most readers, so these two are the
    // format rather than the palette.
    const { rects } = draw(PAYLOAD);
    expect(rects[0].getAttribute('fill')).toBe('#ffffff');
    expect(rects[1].getAttribute('fill')).toBe('#000000');
  });

  it('re-encodes when the value changes', () => {
    const a = draw(PAYLOAD).rects.length;
    cleanup();
    const b = draw(PAYLOAD + 'x'.repeat(200)).rects.length;
    expect(b).not.toBe(a);
  });

  it('carries an accessible name', () => {
    const { svg } = draw(PAYLOAD);
    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.getAttribute('aria-label')).toBe('QR code');
  });
});
