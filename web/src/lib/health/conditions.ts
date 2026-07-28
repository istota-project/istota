/**
 * Presentation helpers for linking conditions and encounters.
 *
 * The relationship is many-to-many: a condition is diagnosed by a GP, referred
 * to a specialist, and reviewed at a follow-up, and all three are links on the
 * same condition. Both link pickers — "add a condition to this encounter" and
 * "add an encounter to this condition" — are the same shape, so their option
 * building lives here and is unit testable without mounting a page.
 */

import type { Diagnosis, Encounter } from '$lib/api';
import type { SelectOption } from '$lib/components/ui';

/** One condition as it reads in a link picker. */
export function conditionOptionLabel(d: Diagnosis): string {
  return d.icd10 ? `${d.name} (${d.icd10})` : d.name;
}

/**
 * Conditions still available to link, in the order the API returned them
 * (active, then chronic, then resolved — each newest first).
 *
 * `linkedIds` covers both what is already on the record and what is already
 * staged in the form, so the picker never offers a no-op.
 */
export function linkableConditionOptions(all: Diagnosis[], linkedIds: number[]): SelectOption[] {
  const linked = new Set(linkedIds);
  return all
    .filter((d) => !linked.has(d.id))
    .map((d) => ({ value: String(d.id), label: conditionOptionLabel(d) }));
}

/** One encounter as it reads in a link picker: "12 Jul 2026 · Visit". */
export function encounterOptionLabel(
  e: Encounter,
  formatDate: (iso: string) => string,
  typeLabel: (t: string) => string,
): string {
  const who = e.provider || e.facility;
  const base = `${formatDate(e.encounter_date)} · ${typeLabel(e.encounter_type)}`;
  return who ? `${base} · ${who}` : base;
}

/** Encounters still available to link to a condition. */
export function linkableEncounterOptions(
  all: Encounter[],
  linkedIds: number[],
  label: (e: Encounter) => string,
): SelectOption[] {
  const linked = new Set(linkedIds);
  return all
    .filter((e) => !linked.has(e.id))
    .map((e) => ({ value: String(e.id), label: label(e) }));
}

/**
 * Resolve ids to records for rendering staged/linked chips.
 *
 * Ids that name nothing in the pool are dropped rather than rendered as a
 * placeholder: the pools are capped, so an unresolvable id usually means the
 * record is simply outside the page fetched, and "#41" reads as a bug.
 */
export function resolveById<T extends { id: number }>(pool: T[], ids: number[]): T[] {
  const byId = new Map(pool.map((x) => [x.id, x]));
  const out: T[] = [];
  for (const id of ids) {
    const hit = byId.get(id);
    if (hit) out.push(hit);
  }
  return out;
}
