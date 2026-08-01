/**
 * The one disclaimer for the tax estimator, following the health module's
 * `_DISCLAIMER`: a single constant, rendered persistently and not dismissible.
 *
 * Naming what is *not* modeled is the part that does real work. A generic "this
 * is not advice" line is noise — it tells a reader nothing they can act on,
 * and it is the sentence people learn to skip. Each item below is a real gap
 * that could move the number, and every one of them is a thing the estimate
 * quietly assumes away.
 *
 * Keep this in step with what the calculator actually does. If a gap here gets
 * modeled, take it out; if a new one appears, add it. A disclaimer that lists
 * a limitation the code no longer has trains the reader to distrust the rest.
 */
export const TAX_DISCLAIMER_TITLE = 'An estimate, not tax advice';

export const TAX_DISCLAIMER_LEAD =
  'These are estimated quarterly payments. Verify them against the IRS and your ' +
  'state authority before sending money.';

/** The specific gaps. Rendered as a list, so keep each item to one clause. */
export const TAX_DISCLAIMER_GAPS: readonly string[] = [
  'Local and city income taxes — eleven states have county or municipal add-ons, and none are included.',
  'Tax credits of every kind, the Alternative Minimum Tax, and exemption phase-outs.',
  'The 2025 federal deductions for tips, overtime and seniors, and the charitable deduction for non-itemizers.',
  'State conformity beyond a single starting-point setting — benefit recapture, state-specific credits and deductions are not modeled.',
  'Itemized deductions: the estimate always assumes you take the standard deduction.',
  'Filing statuses other than married-filing-jointly and single.',
];
