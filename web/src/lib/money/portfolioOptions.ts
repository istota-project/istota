// Dropdown option sets for symbol classifications. The DB columns are free
// text (the bundled seed already ships values like "Brazil"), so these are
// not validation — they are the canonical vocabulary the pickers offer,
// unioned with whatever values the user's rows already carry so an existing
// classification always shows and stays selectable.

export const ASSET_CLASSES = [
  'Stocks',
  'Fixed Income',
  'Cash & Equivalents',
  'Commodities',
  'Real Estate',
  'Alternative',
  'Unclassified',
];

export const SUB_CLASSES: Record<string, string[]> = {
  Stocks: [
    'Total Market',
    'Large Cap',
    'Large Cap Growth',
    'Small Cap',
    'Technology',
    'Dividend',
    'Developed Markets',
    'Emerging Markets',
  ],
  'Fixed Income': ['Short-Term', 'Intermediate', 'Long-Term', 'TIPS', 'Municipal', 'High Yield'],
  'Cash & Equivalents': ['Cash', 'Money Market', 'CD'],
  Commodities: ['Gold', 'Silver', 'Broad Basket'],
  'Real Estate': ['REIT'],
  Alternative: ['Cryptocurrency', 'Private Equity', 'SPAC'],
};

export const GEOGRAPHIES = [
  'US',
  'International',
  'Developed Markets',
  'Emerging Markets',
  'Global',
  'Europe',
  'Asia-Pacific',
];

/** Canonical values first (in canonical order), then any extras sorted. */
function union(canonical: string[], extras: Iterable<string>): string[] {
  const seen = new Set(canonical);
  const added: string[] = [];
  for (const value of extras) {
    if (!value || seen.has(value)) continue;
    seen.add(value);
    added.push(value);
  }
  return [...canonical, ...added.sort()];
}

export function assetClassOptions(inUse: Iterable<string>): string[] {
  return union(ASSET_CLASSES, inUse);
}

/**
 * Sub-classes are scoped to the chosen asset class: the canonical list for
 * that class plus sub-classes the user's rows already use under it. An
 * unknown class still offers whatever its rows carry.
 */
export function subClassOptions(
  assetClass: string,
  rows: Iterable<{ asset_class: string; sub_class: string }>,
): string[] {
  const inUse: string[] = [];
  for (const row of rows) {
    if (row.asset_class === assetClass && row.sub_class) inUse.push(row.sub_class);
  }
  return union(SUB_CLASSES[assetClass] ?? [], inUse);
}

export function geographyOptions(inUse: Iterable<string>): string[] {
  return union(GEOGRAPHIES, inUse);
}
