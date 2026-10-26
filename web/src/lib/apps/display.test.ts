import { describe, expect, it } from 'vitest';
import { fitsRange } from './display';

describe('fitsRange', () => {
  it('fits a phone app in every pane from 320px', () => {
    expect(fitsRange('phone', 320)).toBe(true);
    expect(fitsRange('phone', 641)).toBe(true);
    expect(fitsRange('phone', 1024)).toBe(true);
  });

  it('needs more than 640px for a tablet app', () => {
    expect(fitsRange('tablet', 320)).toBe(false);
    expect(fitsRange('tablet', 640)).toBe(false);
    expect(fitsRange('tablet', 641)).toBe(true);
    expect(fitsRange('tablet', 1024)).toBe(true);
  });

  it('needs 1024px or more for a desktop app', () => {
    expect(fitsRange('desktop', 320)).toBe(false);
    expect(fitsRange('desktop', 641)).toBe(false);
    expect(fitsRange('desktop', 1023)).toBe(false);
    expect(fitsRange('desktop', 1024)).toBe(true);
  });

  it('reads a pane not measured yet as fitting, and a missing range as phone', () => {
    expect(fitsRange('desktop', null)).toBe(true);
    expect(fitsRange(null, 320)).toBe(true);
  });
});
