import type { WorkEntryInput } from './api';

/** Which quantity field a service's rate rule actually reads. */
export type QuantityField = 'qty' | 'amount' | 'none';

export function quantityFieldFor(serviceType: string | undefined): QuantityField {
  const type = serviceType || 'hours';
  if (type === 'flat') return 'none';
  if (type === 'other') return 'amount';
  return 'qty';
}

/**
 * What a save actually sends: the identity fields always, the quantity fields
 * only when the form has something meaningful to say about them.
 */
export type WorkEntrySavePayload = Partial<WorkEntryInput> &
  Pick<WorkEntryInput, 'date' | 'client' | 'service'>;

export interface WorkEntryFormState {
  date: string;
  client: string;
  service: string;
  /** The entry's service when the form opened; '' when adding. */
  initialService: string;
  /** Which of qty/amount the form actually rendered an input for. */
  wants: QuantityField;
  qty: number | null;
  amount: number | null;
  discount: number;
  description: string;
  entity: string;
}

/**
 * Build the request body for a work-entry save.
 *
 * The rule that matters: a quantity field the form never rendered is
 * **omitted**, not nulled. `entry_line_item` reads `amount` as the fallback
 * for an hours/days entry with no qty (the `work add -a 500 -s dev` shape),
 * and the form shows no Amount box for such an entry — so sending `amount:
 * null` zeroed a legitimately priced entry the user only meant to retitle.
 * The same path fired for every entry when the service list failed to load,
 * since an unresolved service reads as the default hours type.
 *
 * The one case where clearing is right is a service the user actually
 * changed: the old quantity no longer prices the entry and must not linger
 * as a fallback. Adding sends both fields, since there is nothing to preserve.
 */
export function buildWorkEntryPayload(state: WorkEntryFormState): WorkEntrySavePayload {
  const payload: WorkEntrySavePayload = {
    date: state.date,
    client: state.client,
    service: state.service,
    discount: state.discount,
    description: state.description,
    entity: state.entity,
  };

  // Adding has no stored value to protect; a changed service invalidates the
  // one that's there. Either way both fields are sent explicitly.
  const clearUnused = !state.initialService || state.service !== state.initialService;

  if (state.wants === 'qty') {
    payload.qty = state.qty;
  } else if (clearUnused) {
    payload.qty = null;
  }

  if (state.wants === 'amount') {
    payload.amount = state.amount;
  } else if (clearUnused) {
    payload.amount = null;
  }

  return payload;
}
