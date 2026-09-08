/**
 * Date, date-time and duration rendering, in one place.
 *
 * Twenty-odd components each carried their own copy of these four or five
 * lines, and the copies had drifted: nine health and location pages appended
 * `T00:00:00` only when the string did not already carry a `T`, while three
 * money pages appended it unconditionally. Handed a full timestamp the second
 * shape builds `2026-09-05T14:22:31ZT00:00:00`, which is an Invalid Date — and
 * the `try/catch` every copy wrapped itself in cannot help, because `new Date`
 * of a nonsense string does not throw, it returns a value.
 *
 * The guard is what survived. Everything else that differed between the copies
 * — the `Intl` option set, what an empty input renders as — is a parameter, so
 * converting a call site changes nothing it renders.
 *
 * `formatRelative` arrived later and on different terms. The four ladders it
 * replaced (`ui/NotificationItem`, `/admin`, `/location`, `DeviceTrackerCard`)
 * rendered four different things — floor against round, four threshold sets,
 * one falling back to an absolute timestamp past a day — so folding them was a
 * decision about what a relative timestamp should say rather than an
 * extraction, and it changes what three of the four surfaces show. What it
 * settles is written at that function.
 */

/**
 * `Intl` options plus what to render for a missing value.
 *
 * The `Intl` half **replaces** the module default rather than merging with it,
 * so a caller asking for `{ month: '2-digit' }` gets month and nothing else.
 * Merging would make the default un-overridable — two call sites want a date
 * with no year — but the corollary is that a caller wanting only a
 * non-component option (`timeZone`, `hour12`) has to restate the components
 * beside it.
 */
export type DateFormatOptions = Intl.DateTimeFormatOptions & {
  /** Rendered for `null`, `undefined` and `''`. Defaults to `''`. */
  empty?: string;
  /**
   * A fixed locale, where a surface has one. Almost nothing does — the two
   * feed components are the only callers, and they pin `en-US` because that is
   * what they rendered before this module existed. Everything else omits it and
   * gets the reader's own locale.
   */
  locale?: string;
};

const DEFAULT_DATE_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
};

/** A bare calendar date: `2026-09-05`. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** SQLite's `datetime('now')` default: `2026-09-05 14:22:31`. */
const SQLITE_TIMESTAMP = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/;

/**
 * The `T` separator ISO wants, where a value came out of SQLite without one.
 *
 * `datetime('now')` is the schema DEFAULT on several `created_at` columns and
 * it separates with a space. V8 accepts that; the standard does not, and the
 * other engines have not always.
 *
 * **The pattern is anchored, and that is the whole of the correctness here.** A
 * loose "does it contain a time part" test is also true of an RFC 822 date —
 * `Tue, 05 Sep 2026 14:22:31 GMT`, which is what a feed entry carries when
 * feedparser could not normalise it — and replacing the first space of one
 * produces `Tue,T05 …`, an Invalid Date out of a string `new Date` parses
 * correctly untouched.
 */
function separator(iso: string): string {
  return SQLITE_TIMESTAMP.test(iso) ? iso.replace(' ', 'T') : iso;
}

/** What `formatDate` hands `new Date`, for the two shapes it gets wrong. */
function coerce(iso: string): string {
  // A bare date is parsed as UTC midnight, so it renders as the day before
  // anywhere west of Greenwich; the suffix makes it local midnight instead.
  if (DATE_ONLY.test(iso)) return `${iso}T00:00:00`;
  return separator(iso);
}

/**
 * A calendar date, from either a bare `YYYY-MM-DD` or a full timestamp.
 *
 * `coerce` above decides what the platform is handed; nothing else here does.
 *
 * A value that will not parse is returned as it arrived, which is what every
 * copy of this did inside its `catch`: an unrenderable date should read as the
 * raw string a reader can report, never as `Invalid Date`.
 */
export function formatDate(iso: string | null | undefined, opts: DateFormatOptions = {}): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  const d = new Date(coerce(iso));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale, Object.keys(intl).length > 0 ? intl : DEFAULT_DATE_OPTIONS);
}

/**
 * A date and a time together, for a value that is always a full timestamp.
 *
 * Separate from `formatDate` rather than an option on it, because it must not
 * carry the midnight suffix: a caller reaching for this has a timestamp, and
 * appending to one is the defect above. It shares the separator rule, which
 * only ever normalises a value that is already a timestamp — four of these
 * callers render a `created_at`-shaped column and the four local copies this
 * replaced each parsed one on V8's tolerance alone.
 */
export function formatDateTime(
  iso: string | null | undefined,
  opts: DateFormatOptions = {},
): string {
  const { empty = '', locale, ...intl } = opts;
  if (!iso) return empty;
  const d = new Date(separator(iso));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale, Object.keys(intl).length > 0 ? intl : undefined);
}

/** What a relative timestamp may vary by. Nothing else about it is a choice. */
export type RelativeFormatOptions = {
  /** Rendered for `null`, `undefined` and `''`. Defaults to `''`. */
  empty?: string;
  /**
   * Render `42s ago` under a minute instead of `just now`.
   *
   * `/admin` and nothing else. That page refreshes on a timer and the figure
   * is a liveness readout — whether the scheduler ran *just* now or fifty
   * seconds ago is the question being asked of it, and `just now` answers
   * neither. Every other surface is read once and the second is noise.
   */
  seconds?: boolean;
  /** The clock, for tests. Defaults to now. */
  now?: Date;
};

/**
 * Past this the relative form stops being readable and an absolute date is
 * more use. `/admin`'s own column CSS was sized for `1234d ago`, which is what
 * an unbounded day count eventually produces.
 */
const ABSOLUTE_AFTER_DAYS = 30;

/**
 * How long ago something happened: `just now`, `5m ago`, `3h ago`, `12d ago`,
 * and an absolute date past a month.
 *
 * One ladder for every surface, replacing four that disagreed on three axes.
 * What each was decided as, since each was a real difference rather than
 * drift:
 *
 * **Floor, never round.** `DeviceTrackerCard` rounded, so 119 seconds read as
 * "2 min ago" — a figure larger than the time that had actually elapsed. A
 * relative timestamp is a lower bound on an age; rounding up makes it a claim
 * the data does not support.
 *
 * **A future timestamp reads as `just now`.** Only `/admin` guarded this. The
 * others rendered `-1m ago` for a row stamped by a clock a minute ahead of the
 * reader's, which is every deployment with two machines in it.
 *
 * **An unparseable value comes back as it arrived**, like `formatDate` and
 * `formatDateTime` above. `/location` had no guard, and since every comparison
 * against `NaN` is false its value fell through the whole ladder and rendered
 * `NaNd ago`. `NotificationItem` returned `''`, which hides the value from the
 * reader who would have to report it.
 *
 * The seconds tier is the one surviving per-surface difference and it is an
 * option; see `RelativeFormatOptions.seconds`. `DeviceTrackerCard`'s fallback
 * to an absolute timestamp past a *day* did not survive — the exact instant
 * moved to that row's `title` instead, which is where a figure nobody reads at
 * a glance belongs.
 */
export function formatRelative(
  iso: string | null | undefined,
  opts: RelativeFormatOptions = {},
): string {
  const { empty = '', seconds = false, now } = opts;
  if (!iso) return empty;
  // `coerce`, the same thing `formatDate` is handed, and that agreement is the
  // point rather than an incidental choice: the branch below renders through
  // `formatDate`, so parsing here by a different rule would pick the rung off
  // one instant and print the date of another. The two differ only for a bare
  // `YYYY-MM-DD` — UTC midnight against local — which no caller passes today
  // and which would be a silent day-boundary error the first time one did.
  // For a timestamp, which is what every caller holds, `coerce` *is*
  // `separator`; it appends the midnight suffix only to an anchored bare date.
  const d = new Date(coerce(iso));
  if (Number.isNaN(d.getTime())) return iso;
  const diff = ((now ?? new Date()).getTime() - d.getTime()) / 1000;
  if (diff < 60) return seconds && diff >= 0 ? `${Math.floor(diff)}s ago` : 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  const days = Math.floor(diff / 86400);
  return days < ABSOLUTE_AFTER_DAYS ? `${days}d ago` : formatDate(iso);
}

/**
 * A coarse duration in seconds: `6d 2h`, `1h 04m`, `12m`, `45s`.
 *
 * Two units at most. A reader is deciding whether to wait rather than timing
 * anything, so seconds of precision six hours out is noise.
 *
 * `doctor._duration` and `commands._usage_age` state the same rule in Python
 * and `usageFormat.parity.test.ts` holds the three in step. The minutes are
 * zero-padded for the same reason they are there: the field sits in a column
 * and `1h 4m` and `1h 04m` are different widths.
 */
export function formatDuration(seconds: number): string {
  const total = Math.floor(Math.max(0, Number.isFinite(seconds) ? seconds : 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
  if (minutes) return `${minutes}m`;
  return `${secs}s`;
}

/**
 * A duration already measured in whole minutes: `45m`, `2h`, `2h 30m`.
 *
 * A different rule from `formatDuration`, not a unit conversion of it — an
 * exact hour renders as `2h` rather than `2h 00m`, which is what both location
 * surfaces show for a visit that happens to land on the hour.
 */
export function formatMinutes(minutes: number | null | undefined, empty = ''): string {
  if (minutes == null || !Number.isFinite(minutes)) return empty;
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}
