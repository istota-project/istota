import { getContext, setContext } from 'svelte';
import type { ServiceCard } from '$lib/api';

const MONEY_SERVICES = Symbol('money-module-services');

/**
 * The money module's connected services, fetched once by the settings layout
 * and read by the section that renders them.
 *
 * The layout has to fetch `/settings/module-services/money` anyway — it is what
 * answers "is this module switched off", which gates every section — so the
 * Connections page reads that answer rather than asking again. `services` is a
 * getter so the layout can back it with `$state` and a reload still reaches the
 * page.
 */
export interface MoneyServicesContext {
  readonly services: ServiceCard[];
  /** Re-fetch after a credential write, so a card's status is current. */
  reload: () => Promise<void>;
}

export function setMoneyServices(ctx: MoneyServicesContext): void {
  setContext(MONEY_SERVICES, ctx);
}

export function getMoneyServices(): MoneyServicesContext {
  const ctx = getContext<MoneyServicesContext | undefined>(MONEY_SERVICES);
  if (!ctx) throw new Error('getMoneyServices() outside the money settings layout');
  return ctx;
}
