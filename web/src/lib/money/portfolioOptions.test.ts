import { describe, it, expect } from 'vitest';
import {
  ASSET_CLASSES,
  GEOGRAPHIES,
  assetClassOptions,
  subClassOptions,
  geographyOptions,
} from './portfolioOptions';

describe('portfolioOptions', () => {
  it('offers the canonical asset classes when nothing extra is in use', () => {
    expect(assetClassOptions([])).toEqual(ASSET_CLASSES);
  });

  it('appends in-use values the canonical list does not know, sorted after it', () => {
    const opts = assetClassOptions(['Stocks', 'Options', 'Collectibles']);
    expect(opts.slice(0, ASSET_CLASSES.length)).toEqual(ASSET_CLASSES);
    expect(opts.slice(ASSET_CLASSES.length)).toEqual(['Collectibles', 'Options']);
    // no duplicate for the canonical value already in use
    expect(opts.filter((o) => o === 'Stocks')).toHaveLength(1);
  });

  it('ignores blank in-use values', () => {
    expect(geographyOptions([''])).toEqual(GEOGRAPHIES);
  });

  it('scopes sub-classes to the chosen asset class', () => {
    const rows = [
      { asset_class: 'Stocks', sub_class: 'Tech Levered' },
      { asset_class: 'Commodities', sub_class: 'Platinum' },
      { asset_class: 'Stocks', sub_class: '' },
    ];
    const opts = subClassOptions('Stocks', rows);
    expect(opts).toContain('Total Market');
    expect(opts).toContain('Tech Levered');
    expect(opts).not.toContain('Platinum');
  });

  it('still offers a row-carried sub-class under an unknown asset class', () => {
    const rows = [{ asset_class: 'Options', sub_class: 'Call' }];
    expect(subClassOptions('Options', rows)).toEqual(['Call']);
  });

  it('unions geographies with values already in use (the seed ships e.g. Brazil)', () => {
    const opts = geographyOptions(['Brazil', 'US']);
    expect(opts.slice(0, GEOGRAPHIES.length)).toEqual(GEOGRAPHIES);
    expect(opts).toContain('Brazil');
  });
});
