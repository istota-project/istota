/**
 * The one place a billing status maps to a `Badge` variant, for the work and
 * invoice lists. It replaced the money shell's own status chip classes, which
 * both pages hung on their status cells.
 */
export type BillingStatus = 'draft' | 'posted' | 'paid';

export function billingStatusVariant(status: BillingStatus): 'neutral' | 'warn' | 'success' {
  if (status === 'paid') return 'success';
  if (status === 'posted') return 'warn';
  return 'neutral';
}
