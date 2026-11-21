import type { SelectOption } from '@minot/ui';
import type { DateFormatOptions } from '@minot/ui/dateFormat';

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

function browserLoads(zone: string): boolean {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

/**
 * `Timestamp` options for a date and time shown in `zone` and named by it ("Mon 23
 * Nov, 13:20 EST"), so a time is never read in the browser's own zone. A zone this
 * browser cannot load shows the instant in UTC, named as UTC, rather than throwing.
 */
export function zonedDateTimeOptions(zone: string): DateFormatOptions {
  return {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: zone && browserLoads(zone) ? zone : 'UTC',
    timeZoneName: 'short',
  };
}
