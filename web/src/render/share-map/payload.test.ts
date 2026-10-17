import { describe, it, expect } from 'vitest';
import { parseSnapshotPayload } from './payload';

function observation(overrides: Record<string, unknown> = {}) {
  return {
    precision: 'exact',
    lat: 52.52,
    lon: 13.405,
    accuracy_m: 40,
    observed_at: '2026-10-16T14:02:31+00:00',
    freshness: 'fresh',
    owner_user_id: 'bob',
    ...overrides,
  };
}

function basemap(overrides: Record<string, unknown> = {}) {
  return {
    provider: 'osm',
    kind: 'raster',
    dark: 'https://tiles.example.test/{z}/{x}/{y}.png',
    light: 'https://tiles.example.test/{z}/{x}/{y}.png',
    attribution: 'OSM contributors',
    needs_key: false,
    fell_back: false,
    warning: '',
    ...overrides,
  };
}

function payload(overrides: Record<string, unknown> = {}) {
  return { observation: observation(), basemap: basemap(), owner: 'Bob', ...overrides };
}

describe('parseSnapshotPayload', () => {
  it('accepts a well-formed payload and keeps only what the map draws', () => {
    const parsed = parseSnapshotPayload({ ...payload(), session_id: 'abc' });
    expect(parsed).not.toBeNull();
    expect(parsed?.ownerLabel).toBe('Bob');
    expect(parsed?.observation).toEqual(observation());
    expect(parsed?.basemap.kind).toBe('raster');
    expect(parsed).not.toHaveProperty('session_id');
  });

  it('falls back to the owner id when no label is given', () => {
    expect(parseSnapshotPayload(payload({ owner: undefined }))?.ownerLabel).toBe('bob');
  });

  it('accepts an area share with no accuracy', () => {
    const parsed = parseSnapshotPayload(
      payload({ observation: observation({ precision: 'area', accuracy_m: null }) }),
    );
    expect(parsed?.observation.precision).toBe('area');
    expect(parsed?.observation.accuracy_m).toBeNull();
  });

  it.each([
    ['not an object', null],
    ['no observation', payload({ observation: undefined })],
    ['no basemap', payload({ basemap: undefined })],
    ['an unknown precision', payload({ observation: observation({ precision: 'street' }) })],
    ['a non-finite latitude', payload({ observation: observation({ lat: Number.NaN }) })],
    ['a latitude out of range', payload({ observation: observation({ lat: 91 }) })],
    ['a longitude out of range', payload({ observation: observation({ lon: -181 }) })],
    ['a string coordinate', payload({ observation: observation({ lon: '13.4' }) })],
    ['a negative accuracy', payload({ observation: observation({ accuracy_m: -1 }) })],
    ['no observed time', payload({ observation: observation({ observed_at: 7 }) })],
    ['no owner id', payload({ observation: observation({ owner_user_id: '' }) })],
    ['an unknown basemap kind', payload({ basemap: basemap({ kind: 'vector' }) })],
    ['a non-http basemap URL', payload({ basemap: basemap({ light: 'file:///etc/passwd' }) })],
    ['a missing light URL', payload({ basemap: basemap({ light: '' }) })],
  ])('refuses %s', (_label, raw) => {
    expect(parseSnapshotPayload(raw)).toBeNull();
  });
});
