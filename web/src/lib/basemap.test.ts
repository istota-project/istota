import { describe, expect, it } from 'vitest';
import {
  BASEMAP_DARK_LAYER,
  BASEMAP_LIGHT_LAYER,
  DEFAULT_BASEMAP,
  buildRasterStyle,
  isRaster,
  styleUrlFor,
} from './basemap';
import type { BasemapSpec } from './basemap';

function raster(overrides: Partial<BasemapSpec> = {}): BasemapSpec {
  return {
    provider: 'carto',
    kind: 'raster',
    dark: 'https://tiles.example.test/dark/{z}/{x}/{y}.png',
    light: 'https://tiles.example.test/light/{z}/{x}/{y}.png',
    attribution: '&copy; someone',
    needs_key: false,
    fell_back: false,
    warning: '',
    ...overrides,
  };
}

function vector(overrides: Partial<BasemapSpec> = {}): BasemapSpec {
  return {
    provider: 'openfreemap',
    kind: 'style',
    dark: 'https://tiles.example.test/styles/dark',
    light: 'https://tiles.example.test/styles/light',
    attribution: '',
    needs_key: false,
    fell_back: false,
    warning: '',
    ...overrides,
  };
}

describe('the shipped fallback', () => {
  it('needs no key, so a failed config fetch still renders a map', () => {
    expect(DEFAULT_BASEMAP.needs_key).toBe(false);
    expect(DEFAULT_BASEMAP.kind).toBe('style');
  });

  it('names no vendor that requires a credential', () => {
    expect(DEFAULT_BASEMAP.dark).not.toContain('cartocdn');
    expect(DEFAULT_BASEMAP.light).not.toContain('cartocdn');
  });

  it('gives each theme its own style', () => {
    expect(DEFAULT_BASEMAP.dark).not.toEqual(DEFAULT_BASEMAP.light);
  });
});

describe('isRaster', () => {
  it('splits the two ways a basemap is delivered', () => {
    expect(isRaster(raster())).toBe(true);
    expect(isRaster(vector())).toBe(false);
  });
});

describe('buildRasterStyle', () => {
  it('carries both themes so a switch is a visibility toggle, not a reload', () => {
    const style = buildRasterStyle(raster(), 'dark');
    const ids = style.layers.map((l) => l.id);
    expect(ids).toContain(BASEMAP_DARK_LAYER);
    expect(ids).toContain(BASEMAP_LIGHT_LAYER);
  });

  it('shows only the requested theme', () => {
    const style = buildRasterStyle(raster(), 'light');
    const byId = Object.fromEntries(style.layers.map((l) => [l.id, l]));
    expect(byId[BASEMAP_LIGHT_LAYER].layout?.visibility).toBe('visible');
    expect(byId[BASEMAP_DARK_LAYER].layout?.visibility).toBe('none');
  });

  it('puts each theme tiles into its own source', () => {
    const style = buildRasterStyle(raster(), 'dark');
    const urls = Object.values(style.sources).flatMap((s) => s.tiles ?? []);
    expect(urls).toContain('https://tiles.example.test/dark/{z}/{x}/{y}.png');
    expect(urls).toContain('https://tiles.example.test/light/{z}/{x}/{y}.png');
  });

  it('shares one source when both themes are the same tiles', () => {
    // osm has no dark flavour. Two sources would fetch every tile twice.
    const spec = raster({
      light: 'https://same.test/{z}/{x}/{y}.png',
      dark: 'https://same.test/{z}/{x}/{y}.png',
    });
    const style = buildRasterStyle(spec, 'dark');
    expect(Object.keys(style.sources)).toHaveLength(1);
    expect(style.layers).toHaveLength(2);
  });

  it('passes the attribution through to every source', () => {
    const style = buildRasterStyle(raster(), 'dark');
    for (const source of Object.values(style.sources)) {
      expect(source.attribution).toBe('&copy; someone');
    }
  });

  it('leaves the tile template placeholders untouched', () => {
    const style = buildRasterStyle(raster(), 'dark');
    const urls = Object.values(style.sources).flatMap((s) => s.tiles ?? []);
    for (const url of urls) {
      expect(url).toContain('{z}/{x}/{y}');
    }
  });

  it('is a valid v8 style', () => {
    const style = buildRasterStyle(raster(), 'dark');
    expect(style.version).toBe(8);
  });

  it('keeps a key that is already in the tile url', () => {
    const spec = raster({ dark: 'https://t.test/{z}/{x}/{y}.png?api_key=abc' });
    const style = buildRasterStyle(spec, 'dark');
    const urls = Object.values(style.sources).flatMap((s) => s.tiles ?? []);
    expect(urls.some((u) => u.includes('api_key=abc'))).toBe(true);
  });
});

describe('styleUrlFor', () => {
  it('picks the url for the active theme', () => {
    expect(styleUrlFor(vector(), 'dark')).toBe('https://tiles.example.test/styles/dark');
    expect(styleUrlFor(vector(), 'light')).toBe('https://tiles.example.test/styles/light');
  });

  it('falls back to the other theme rather than returning nothing', () => {
    const spec = vector({ light: '' });
    expect(styleUrlFor(spec, 'light')).toBe('https://tiles.example.test/styles/dark');
  });
});
