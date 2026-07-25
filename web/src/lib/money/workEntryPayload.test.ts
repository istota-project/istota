import { describe, it, expect } from 'vitest';
import {
  buildWorkEntryPayload,
  quantityFieldFor,
  type WorkEntryFormState,
} from './workEntryPayload';

function state(overrides: Partial<WorkEntryFormState> = {}): WorkEntryFormState {
  return {
    date: '2026-03-01',
    client: 'acme',
    service: 'dev',
    initialService: 'dev',
    wants: 'qty',
    qty: 3,
    amount: null,
    discount: 0,
    description: 'API work',
    entity: '',
    ...overrides,
  };
}

describe('quantityFieldFor', () => {
  it('maps service types onto the field the rate rule reads', () => {
    expect(quantityFieldFor('hours')).toBe('qty');
    expect(quantityFieldFor('days')).toBe('qty');
    expect(quantityFieldFor('other')).toBe('amount');
    expect(quantityFieldFor('flat')).toBe('none');
  });

  it('defaults an unresolved service to the hourly shape', () => {
    expect(quantityFieldFor(undefined)).toBe('qty');
    expect(quantityFieldFor('')).toBe('qty');
  });
});

describe('buildWorkEntryPayload', () => {
  it('always carries the identity and prose fields', () => {
    expect(buildWorkEntryPayload(state())).toMatchObject({
      date: '2026-03-01',
      client: 'acme',
      service: 'dev',
      discount: 0,
      description: 'API work',
      entity: '',
    });
  });

  it('sends the quantity field the form rendered', () => {
    expect(buildWorkEntryPayload(state({ qty: 7.5 })).qty).toBe(7.5);
    expect(
      buildWorkEntryPayload(
        state({
          service: 'expenses',
          initialService: 'expenses',
          wants: 'amount',
          qty: null,
          amount: 320,
        }),
      ).amount,
    ).toBe(320);
  });

  it('omits the quantity field it did not render', () => {
    const payload = buildWorkEntryPayload(state({ amount: 500 }));
    expect('amount' in payload).toBe(false);
  });

  it('preserves an amount on an hours entry priced by the amount fallback', () => {
    const payload = buildWorkEntryPayload(state({ qty: null, amount: 500 }));
    expect(payload.qty).toBeNull();
    expect('amount' in payload).toBe(false);
  });

  it('preserves both on a flat service, which renders neither', () => {
    const payload = buildWorkEntryPayload(
      state({
        service: 'retainer',
        initialService: 'retainer',
        wants: 'none',
        qty: 99,
        amount: 12,
      }),
    );
    expect('qty' in payload).toBe(false);
    expect('amount' in payload).toBe(false);
  });

  it('preserves an amount when the service could not be resolved', () => {
    // A failed config load reads every entry as the default hours type.
    const payload = buildWorkEntryPayload(
      state({
        service: 'expenses',
        initialService: 'expenses',
        wants: 'qty',
        qty: null,
        amount: 320,
      }),
    );
    expect('amount' in payload).toBe(false);
  });

  it('clears the stale field when the service changed', () => {
    const payload = buildWorkEntryPayload(
      state({ service: 'dev', initialService: 'expenses', wants: 'qty', qty: 4, amount: 320 }),
    );
    expect(payload.qty).toBe(4);
    expect(payload.amount).toBeNull();
  });

  it('sends both fields when adding', () => {
    const payload = buildWorkEntryPayload(state({ initialService: '', qty: 2, amount: null }));
    expect(payload.qty).toBe(2);
    expect(payload.amount).toBeNull();
  });
});
