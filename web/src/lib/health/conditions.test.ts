import { describe, expect, it } from 'vitest';
import type { Diagnosis, Encounter } from '$lib/api';
import {
  conditionOptionLabel,
  encounterOptionLabel,
  linkableConditionOptions,
  linkableEncounterOptions,
  resolveById,
} from './conditions';

function dx(over: Partial<Diagnosis> & { id: number; name: string }): Diagnosis {
  return {
    icd10: null,
    status: 'active',
    date_diagnosed: null,
    date_resolved: null,
    encounter_id: null,
    encounter_ids: [],
    severity: null,
    notes: null,
    ...over,
  };
}

function enc(over: Partial<Encounter> & { id: number }): Encounter {
  return {
    encounter_date: '2026-06-02',
    encounter_type: 'visit',
    provider: null,
    facility: null,
    specialty: null,
    reason: null,
    notes: null,
    ...over,
  };
}

const isoDate = (iso: string) => iso;
const titleCase = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

describe('conditionOptionLabel', () => {
  it('is the bare name', () => {
    expect(conditionOptionLabel(dx({ id: 1, name: 'Hypertension' }))).toBe('Hypertension');
  });

  it('appends the ICD-10 code when there is one', () => {
    expect(conditionOptionLabel(dx({ id: 1, name: 'Hypertension', icd10: 'I10' }))).toBe(
      'Hypertension (I10)',
    );
  });
});

describe('linkableConditionOptions', () => {
  const all = [
    dx({ id: 1, name: 'Hypertension' }),
    dx({ id: 2, name: 'Asthma' }),
    dx({ id: 3, name: 'Eczema' }),
  ];

  it('drops what is already linked or staged', () => {
    expect(linkableConditionOptions(all, [2]).map((o) => o.value)).toEqual(['1', '3']);
  });

  it('keeps the source order', () => {
    expect(linkableConditionOptions(all, []).map((o) => o.label)).toEqual([
      'Hypertension',
      'Asthma',
      'Eczema',
    ]);
  });

  it('offers a condition already linked to a different encounter', () => {
    // The whole point of many-to-many: being on another encounter is not a
    // reason to withhold it here.
    const onAnother = [dx({ id: 9, name: 'Anemia', encounter_ids: [41] })];
    expect(linkableConditionOptions(onAnother, []).map((o) => o.value)).toEqual(['9']);
  });

  it('is empty when everything is linked', () => {
    expect(linkableConditionOptions(all, [1, 2, 3])).toEqual([]);
  });
});

describe('encounterOptionLabel', () => {
  it('is date and type', () => {
    const e = enc({ id: 1, encounter_date: '2026-06-02', encounter_type: 'visit' });
    expect(encounterOptionLabel(e, isoDate, titleCase)).toBe('2026-06-02 · Visit');
  });

  it('adds the provider when known', () => {
    const e = enc({ id: 1, provider: 'Dr. Smith' });
    expect(encounterOptionLabel(e, isoDate, titleCase)).toBe('2026-06-02 · Visit · Dr. Smith');
  });

  it('falls back to the facility', () => {
    const e = enc({ id: 1, facility: 'Riverside Clinic' });
    expect(encounterOptionLabel(e, isoDate, titleCase)).toBe(
      '2026-06-02 · Visit · Riverside Clinic',
    );
  });
});

describe('linkableEncounterOptions', () => {
  const all = [enc({ id: 1 }), enc({ id: 2 }), enc({ id: 3 })];
  const label = (e: Encounter) => `E${e.id}`;

  it('drops what is already linked', () => {
    expect(linkableEncounterOptions(all, [2], label).map((o) => o.value)).toEqual(['1', '3']);
  });

  it('labels through the supplied formatter', () => {
    expect(linkableEncounterOptions(all, [], label).map((o) => o.label)).toEqual([
      'E1',
      'E2',
      'E3',
    ]);
  });
});

describe('resolveById', () => {
  const all = [dx({ id: 1, name: 'A' }), dx({ id: 2, name: 'B' })];

  it('resolves in the order the ids were given', () => {
    expect(resolveById(all, [2, 1]).map((d) => d.name)).toEqual(['B', 'A']);
  });

  it('drops ids outside the pool rather than inventing a placeholder', () => {
    expect(resolveById(all, [1, 99]).map((d) => d.name)).toEqual(['A']);
  });
});
