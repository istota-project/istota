import type { SelectOption } from '$lib/components/ui';

/**
 * The full IANA timezone list from the browser (no hardcoded list, no extra
 * dependency), as `Select` options. Older engines may not implement
 * `supportedValuesOf`, so they get UTC alone. Shared by `/settings` and the
 * admin user editor, so both offer the same zones.
 */
export function timezoneOptions(): SelectOption[] {
  let zones: string[];
  try {
    zones = (Intl as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.(
      'timeZone',
    ) ?? ['UTC'];
  } catch {
    zones = ['UTC'];
  }
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  return zones.map((z) => ({ value: z, label: z }));
}
