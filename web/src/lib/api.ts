import { base } from '$app/paths';
import { uploadFromPath, nativeUploadAvailable, type Picked } from '$lib/platform/nativePicker';
import { noteTransport } from '$lib/stores/connectivity';
import type { BasemapSpec } from '$lib/basemap';

class AuthError extends Error {
  readonly status = 401;
  constructor() {
    super('Not authenticated');
    this.name = 'AuthError';
  }
}

/**
 * What a non-2xx actually said, falling back to the status alone.
 *
 * This used to be `API error: ${status}` unconditionally, which discarded the
 * body — and on a FastAPI route the body is where the reason lives. Three
 * call sites had already dropped out of `apiFetch` to get it back
 * (`generateIngestToken`, `draftAction`, `putAvatar`), each with a comment
 * saying why, which is three copies of a fix that belongs here. The vault's
 * master-password field is what made it worth doing: its 400 and its 409 are
 * both entirely message — the passphrase floor, and the refusal to mint a
 * second one over a file already encrypted under the first — and the user was
 * shown `API error: 400` for both.
 *
 * `detail` is FastAPI's spelling; `error` is what this app's hand-rolled JSON
 * refusals use. Structured details (`{code, message}`) are unwrapped to their
 * message, which is the only half a person can read.
 *
 * Never throws and never blocks on a body that will not arrive: the status is
 * always an answer, so a body that is HTML, empty, or still in flight simply
 * leaves the old message in place. The read is bounded by the caller's own
 * timeout because it happens inside the same `try`.
 */
async function errorMessage(resp: Response, body?: unknown): Promise<string> {
  const fallback = `API error: ${resp.status}`;
  if (body === undefined) {
    try {
      body = await resp.json();
    } catch {
      return fallback;
    }
  }
  const raw = (body as { detail?: unknown; error?: unknown } | null) ?? null;
  const detail = raw?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  if (typeof raw?.error === 'string' && raw.error.trim()) return raw.error;
  return fallback;
}

async function apiFetch<T>(path: string, init?: RequestInit, timeoutMs = 0): Promise<T> {
  // `fetch` has no timeout of its own: a stalled connection (a mobile handover,
  // a proxy holding the socket) hangs until the OS gives up, which can be
  // minutes or never. A caller whose own state machine is blocked on the
  // result — the chat room stream's recovery routine — passes a bound so the
  // request rejects and the state is released, and so a late resolve can never
  // clobber whatever replaced it in the meantime.
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let timedOut = false;
  if (timeoutMs > 0 && !init?.signal) {
    controller = new AbortController();
    timer = setTimeout(() => {
      timedOut = true;
      controller?.abort();
    }, timeoutMs);
  }
  try {
    let resp: Response;
    try {
      resp = await fetch(`${base}/api${path}`, {
        ...init,
        credentials: 'same-origin',
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (e) {
      // Every request in the app comes through here, which makes this the one
      // place that sees what the network is actually doing (ISSUE-202). A
      // `fetch` rejection is a transport failure or an abort, and only the
      // first is evidence about the server: a caller that cancelled its own
      // request has said nothing about the connection, so it must not raise
      // the offline banner. No caller passes a signal today — the same
      // condition the timeout guard above is written against — and the check
      // is here so the first one to do so cannot introduce that quietly.
      if (!init?.signal?.aborted) noteTransport(false, timedOut ? 'timeout' : 'unreachable');
      throw e;
    }
    // Whatever it says, something answered. A 401 or a 500 is a server, and
    // the connectivity store's whole job is to keep that apart from silence.
    // Reported before the status branches, since a status is an answer.
    noteTransport(true);
    if (resp.status === 401) throw new AuthError();
    if (!resp.ok) {
      const detail = await resp.json().catch(() => null);
      throw Object.assign(new Error(await errorMessage(resp, detail)), {
        status: resp.status,
        field: typeof detail?.field === 'string' ? detail.field : null,
        // The parsed refusal, for a route whose error body carries more than a
        // message (the Nextcloud sharing save's per-row `failed`).
        body: detail as unknown,
      });
    }
    try {
      // **Awaited**, so the read happens inside this `try` and ahead of the
      // `finally` below. `return resp.json()` cleared the timer on the headers
      // alone and left the body unbounded — a proxy that flushes headers and
      // then stalls is the same hang the bound exists for, which is what
      // `sendChatMessage` has always said about its own body read. It also
      // reported that stall to the connectivity store as a reachable server,
      // which is worse than saying nothing: the probe rides on this call, so a
      // half-delivered answer would have cleared the offline banner.
      return await resp.json();
    } catch (e) {
      // Only our own abort. A body that is not JSON is an error page, which
      // the status already described, and says nothing about the connection.
      if (timedOut) noteTransport(false, 'timeout');
      throw e;
    }
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export interface NextcloudTokenStatus {
  connected: boolean;
  expires_at: string | null;
}

/** Ways to reach the bot outside the web UI, as actually deployed. */
export interface UserContact {
  // The user's plus-addressed inbound address, or null when email is off.
  email: string | null;
  talk: boolean;
}

export interface UserAuth {
  method: 'email' | 'nextcloud' | 'none';
  email: string | null;
  can_change_password: boolean;
  // No password yet: Settings offers a link to set one.
  can_set_password: boolean;
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ signed_out: true }> {
  return apiFetch('/account/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

export interface User {
  // Absent in older cached identities; treat as false.
  show_nav_unread_badges?: boolean;
  // Absent on cached records from before email authentication.
  auth?: UserAuth;
  username: string;
  display_name: string;
  bot_name: string;
  is_admin: boolean;
  // Absent on a server that predates the field.
  contact?: UserContact;
  features: {
    chat: boolean;
    feeds: boolean;
    location: boolean;
    money: boolean;
    health: boolean;
    briefings: boolean;
    workspace?: boolean;
    /** The Files page: a workspace, a connection, or shares from other servers (#849). */
    files?: boolean;
    groups?: boolean;
    google_workspace: boolean;
    google_workspace_enabled: boolean;
    admin: boolean;
    /** The "Shared with Istota" card; absent on a server that predates it. */
    sharing?: boolean;
    /** Open Cloud Mesh folder shares are received on this server. */
    sharing_ocm?: boolean;
  };
  // null when the operator hasn't enabled encrypted token storage.
  nextcloud_token?: NextcloudTokenStatus | null;
  // Content hashes for the two identities every page renders, so the client
  // can build an immutable URL for each without a round trip. Null means no
  // picture is stored — render the fallback and issue no request. Absent on a
  // server that predates the field.
  avatars?: { user: string | null; bot: string | null };
}

/**
 * Remove the stored Nextcloud token. With `unshare`, the bot's shares are
 * removed first and the token is kept when any is left: the 409 then carries
 * `still_shared` (names) on the thrown error's `body`, or only the listing's
 * message when Nextcloud could not be asked.
 */
export async function disconnectNextcloudToken(
  unshare = false,
): Promise<{ ok: boolean; was_connected: boolean }> {
  const query = unshare ? '?unshare=true' : '';
  return apiFetch(`/settings/nextcloud-token${query}`, { method: 'DELETE' });
}

// --- Nextcloud calendar and address-book sharing ---
//
// The state lives in Nextcloud: each level is read back from the collection's
// own share list, so a share made in the Nextcloud apps shows here too. Ids,
// never URLs, cross to the browser.

export type NextcloudShareLevel = 'off' | 'readonly' | 'full';
export type NextcloudShareKind = 'calendars' | 'contacts';

export interface NextcloudKindState {
  /** The operator's limit for raising access. Lowering is always allowed. */
  ceiling: NextcloudShareLevel;
  /** False when the bot could not use a share of this kind on this instance. */
  available: boolean;
}

export interface NextcloudCollection {
  id: string;
  kind: NextcloudShareKind;
  name: string;
  level: NextcloudShareLevel;
}

export interface NextcloudSharing {
  /** The bot's own Nextcloud account, the sharee. */
  bot: string;
  kinds: Record<NextcloudShareKind, NextcloudKindState>;
  collections: NextcloudCollection[];
}

export interface NextcloudShareFailure {
  id: string;
  name: string;
  reason: string;
}

export interface NextcloudSharingSaveResult extends NextcloudSharing {
  failed: NextcloudShareFailure[];
}

export async function getNextcloudSharing(): Promise<NextcloudSharing> {
  return apiFetch<NextcloudSharing>('/settings/nextcloud-sharing');
}

/**
 * Send only the changed rows. The server re-reads after writing, so the result
 * is what Nextcloud now holds. A save where every change failed is a 502 that
 * still carries the re-read list and `failed`; that is resolved like a partial
 * failure rather than thrown, since the rows are what the card has to show.
 */
export async function saveNextcloudSharing(
  changes: Record<string, NextcloudShareLevel>,
): Promise<NextcloudSharingSaveResult> {
  try {
    return await apiFetch<NextcloudSharingSaveResult>('/settings/nextcloud-sharing', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes }),
    });
  } catch (e) {
    const err = e as { status?: number; body?: Partial<NextcloudSharingSaveResult> | null };
    const body = err.body;
    if (
      err.status === 502 &&
      body &&
      Array.isArray(body.collections) &&
      Array.isArray(body.failed)
    ) {
      return body as NextcloudSharingSaveResult;
    }
    throw e;
  }
}

export interface AdminStatsUserSource {
  count: number;
  no_model?: number;
  failed: number;
  avg_duration_seconds: number | null;
}

export interface AdminStatsUser {
  username: string;
  display_name: string;
  is_admin: boolean;
  tasks_total: number;
  tasks_last_24h: number;
  tasks_avg_per_day: number;
  tasks_by_source_24h: Record<string, AdminStatsUserSource>;
  tasks_interactive_24h: number;
  tasks_automated_24h: number;
  // Command and skill tasks (module pollers, CRON.md commands), apart from
  // both buckets above. Optional while an older backend may answer.
  tasks_no_model_24h?: number;
  tasks_failed_24h: number;
  last_active: string | null;
  // Usage sits on the same row as the task counts, so "how much is this user
  // costing" is answered where "how much is this user running" already is.
  //
  // `usage_cost_*` is a map keyed by cost basis, never a scalar: one user's
  // rows can span bases when an operator switches the CLI's auth mid-window,
  // and nothing sums across them. `usage_rows_24h` can exceed
  // `tasks_last_24h` by design — it includes spend with no task row at all
  // (a nightly sleep cycle, health OCR), which `usage_by_origin_24h` is what
  // makes legible.
  usage_tokens_24h: number;
  usage_tokens_30d: number;
  usage_cost_24h: Record<string, number>;
  usage_cost_30d: Record<string, number>;
  usage_by_origin_24h: Record<string, { rows: number; tokens: number }>;
  usage_avg_initial_context: number | null;
  usage_avg_peak_context: number | null;
  usage_cache_hit_rate_24h: number;
  usage_rows_24h: number;
  usage_unmeasured_24h: number;
}

export interface AdminUsageTotals {
  rows: number;
  measured_rows: number;
  billed_input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  cost_by_basis: Record<string, number>;
  avg_initial_context_tokens: number | null;
  avg_peak_context_tokens: number | null;
  context_rows: number;
}

export interface AdminUsageGroup extends AdminUsageTotals {
  key: string;
}

export interface AdminStatsUsage {
  totals_24h?: AdminUsageTotals;
  totals_30d?: AdminUsageTotals;
  by_model_30d?: AdminUsageGroup[];
  // The model list is capped at five; this is how many it left out.
  by_model_30d_omitted?: number;
  by_brain_30d?: AdminUsageGroup[];
  by_origin_24h?: AdminUsageGroup[];
  unmeasured_tasks_24h?: number;
  context_unmeasured_rows_30d?: number;
  // Best-effort like every other section: a failure is an error string in the
  // payload rather than a 500 on the whole dashboard.
  error?: string;
}

/** One rate-limit window of the Claude Code plan, as the endpoint reports it. */
export interface AdminSubscriptionWindow {
  /** Stable id — `session`, `weekly_all`, `weekly_scoped:fable`. */
  key: string;
  label: string;
  /** 0–100, clamped server-side. */
  percent: number;
  resets_at: string | null;
  /** Floored at 0; null when the window has no scheduled reset. */
  resets_in_seconds: number | null;
  /** The server's own severity scale. Carried, never acted on — see the card. */
  severity: string;
  is_active: boolean | null;
}

/** Pay-as-you-go credits beyond the plan, in minor units with a currency. */
export interface AdminSubscriptionSpend {
  enabled: boolean;
  used_minor: number;
  limit_minor: number;
  currency: string;
  /** Minor units per major = 10 ** exponent. Never assume 2. */
  exponent: number;
  percent: number;
}

/**
 * Plan utilization for the Claude Code subscription.
 *
 * `available` is true wherever this key is present at all, kept so the shape
 * does not change under a client that still reads it. A reading the server
 * could not obtain omits the key instead, so there is no card to draw and
 * `runtime.subscription_usage` carries the reason as a SKIP. Every field is
 * optional because the whole section degrades to `{error}` when it fails — the
 * same best-effort shape `usage` has.
 */
export interface AdminSubscription {
  available?: boolean;
  windows?: AdminSubscriptionWindow[];
  /** Null when the payload carried no credit block at all. */
  spend?: AdminSubscriptionSpend | null;
  fetched_at?: string | null;
  /** Real numbers from an earlier fetch, plus the failure that made them old. */
  stale?: boolean;
  /** The resolver's branch name (`env` / `file` / `keychain`), never a token. */
  token_source?: string;
  /** The operator's own thresholds. The card tints by these and never by a
   *  literal of its own, or a configured threshold is silently ignored. */
  warn_percent?: number;
  high_percent?: number;
  error?: string;
}

export interface AdminStatsJob {
  id: number;
  user_id: string;
  name: string;
  cron: string;
  /** What the user asked for in CRON.md. Not on its own whether the job runs. */
  enabled: boolean;
  /** When the scheduler suspended the job after N consecutive failures, or
   *  null. The daemon's own column, kept apart from `enabled` because CRON.md
   *  overwrites that one on every sync tick. A job runs only when it is
   *  enabled and not suspended. */
  auto_disabled_at: string | null;
  last_run_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
}

/** A provider on the admin Models card. Never carries a key or a full URL. */
export interface AdminModelProvider {
  id: string;
  kind: string;
  /** The base URL's host. */
  host: string;
  /** Whether the daemon fetches its model list. */
  discovers: boolean;
  /** When that list was last fetched, ISO 8601 UTC, or null. */
  discovered_at: string | null;
  last_error: string | null;
  /** Current models the catalogue holds for it. */
  models: number;
  /** How many of those `[models] enabled` admits. */
  enabled: number;
}

export interface AdminStats {
  system: {
    version: string;
    uptime_seconds: number;
    db_size_bytes: number;
    python_version: string;
    last_scheduler_run: string | null;
    scheduler_healthy: boolean;
  };
  users: AdminStatsUser[];
  scheduler: {
    jobs_total: number;
    jobs_active: number;
    jobs_paused: number;
    jobs: AdminStatsJob[];
    last_errors: { job_name: string; error: string; timestamp: string | null }[];
  };
  modules: Record<string, Record<string, unknown>>;
  usage: AdminStatsUsage;
  /** Absent unless there is a card to draw: Claude Code is the brain or the
   *  fallback, and the endpoint returned windows. See `_admin_subscription_section`. */
  subscription?: AdminSubscription;
  tasks: {
    total: number;
    last_24h: number;
    avg_per_day_30d: number;
    by_source: Record<string, number>;
    failed_by_source_24h: Record<string, number>;
    avg_duration_seconds: number;
    error_rate_24h: number;
    failed_24h: number;
    interactive_24h: number;
    automated_24h: number;
    interactive_avg_per_day_30d: number;
    automated_avg_per_day_30d: number;
    no_model_24h?: number;
    no_model_avg_per_day_30d?: number;
  };
  storage: {
    db_size_bytes: number;
    backups_count: number;
    last_backup: string | null;
    nextcloud_configured: boolean;
    nextcloud_mount_healthy: boolean;
    /** Where the workspace lives; host-folder is only ever the local Mac VM.
     *  Absent from an older backend. */
    mode?: 'host-folder' | 'nextcloud' | 'local';
    /** The bot's own Nextcloud account, for the bot-icon control's copy. */
    nextcloud_username?: string | null;
  };
  models?: {
    brain_kind: string;
    default_model: string;
    default_effort: string | null;
    roles: { role: string; resolved: string }[];
    endpoint?: string;
    provider?: string;
    source_type_overrides?: Record<string, string>;
    /** Brain kinds a room may pin (`[brain] room_selectable`, intersected with
     * the kinds this build can construct). Absent when the operator has listed
     * none, which is the shipped default. */
    room_selectable?: string[];
    /** One row per configured provider, from the discovery caches. */
    providers?: AdminModelProvider[];
    error?: string;
  };
  brain_status?: {
    degraded: boolean;
    active: string | null;
    primary: string;
    reason?: string | null;
    error?: string;
  };
  error?: string;
}

export async function getAdminStats(): Promise<AdminStats> {
  return apiFetch<AdminStats>('/admin/stats');
}

export interface AdminScheduledJobs {
  jobs_total: number;
  jobs_active: number;
  jobs_paused: number;
  jobs: AdminStatsJob[];
}

/** Where a disable or enable landed. `db_only` means CRON.md could not be
 *  rewritten, so the next sync puts the job back as the file says. */
export type AdminJobOutcome = 'file' | 'db_only' | 'module';

export function getAdminScheduledJobs(): Promise<AdminScheduledJobs> {
  return apiFetch<AdminScheduledJobs>('/admin/scheduled-jobs');
}

export function setAdminScheduledJobEnabled(
  id: number,
  enabled: boolean,
): Promise<{ outcome: AdminJobOutcome; job: AdminStatsJob | null }> {
  return groupRequest(`/admin/scheduled-jobs/${id}/${enabled ? 'enable' : 'disable'}`, 'POST');
}

export function deleteAdminScheduledJob(
  id: number,
  confirm: string,
  step_up: StepUpProof,
): Promise<{ deleted: boolean; id: number }> {
  return groupRequest(`/admin/scheduled-jobs/${id}`, 'DELETE', { confirm, step_up });
}

// ---- Admin logs + configuration (ISSUE-203) ----

export interface AdminLogSource {
  id: string;
  label: string;
  kind: 'file' | 'db';
  description: string;
  available: boolean;
  detail: string;
  /** Whether this source's timestamps are UTC or the server's local clock.
   *  Labelled rather than converted — a log line's stamp is what the server
   *  wrote, and silently shifting it makes it un-greppable against the file. */
  time_basis: 'utc' | 'server-local';
  path: string | null;
  bytes: number;
  files: number;
}

export interface AdminLogRecord {
  /** Opaque and monotonic within a source: a byte position for the file
   *  source, a row id for the DB source. Never a path. */
  cursor: string;
  timestamp: string | null;
  level: string;
  logger: string | null;
  message: string;
  task_id: number | null;
  user_id: string | null;
  source_type: string | null;
}

export interface AdminLogPage {
  /** Oldest-first, i.e. reading order. */
  records: AdminLogRecord[];
  /** Cursor for the previous (older) page; null once the start is reached. */
  next_before: string | null;
  /** Where a live tail should resume from. */
  tail_cursor: string | null;
  /** The scan budget was spent before `limit` was filled — distinct from
   *  "nothing older exists", which is `next_before === null`. */
  truncated: boolean;
}

export interface AdminLogTail {
  records: AdminLogRecord[];
  cursor: string;
  /** The source restarted under us (the live file rotated), so the client
   *  should clear rather than append. */
  reset: boolean;
}

export interface AdminLogFilters {
  level?: string;
  q?: string;
  logger?: string;
  user_id?: string;
  task_id?: number;
}

function adminLogParams(filters: AdminLogFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.level) params.set('level', filters.level);
  if (filters.q) params.set('q', filters.q);
  if (filters.logger) params.set('logger', filters.logger);
  if (filters.user_id) params.set('user_id', filters.user_id);
  if (filters.task_id !== undefined) params.set('task_id', String(filters.task_id));
  return params;
}

export async function getAdminLogSources(): Promise<{ sources: AdminLogSource[] }> {
  return apiFetch<{ sources: AdminLogSource[] }>('/admin/logs/sources');
}

export async function getAdminLogPage(
  sourceId: string,
  opts: AdminLogFilters & { limit?: number; before?: string } = {},
): Promise<AdminLogPage> {
  const params = adminLogParams(opts);
  if (opts.limit) params.set('limit', String(opts.limit));
  if (opts.before) params.set('before', opts.before);
  const qs = params.toString();
  return apiFetch<AdminLogPage>(`/admin/logs/${encodeURIComponent(sourceId)}${qs ? `?${qs}` : ''}`);
}

/** URL for the live-tail SSE stream. Built here so the query-param spelling
 *  lives beside the page fetcher's and the two cannot drift. */
export function adminLogStreamUrl(
  sourceId: string,
  cursor: string,
  filters: AdminLogFilters = {},
): string {
  const params = adminLogParams(filters);
  params.set('cursor', cursor);
  return `${base}/api/admin/logs/${encodeURIComponent(sourceId)}/stream?${params.toString()}`;
}

/** The control a config field is edited with. `readonly` when it is not
 *  edited here at all; `read_only_reason` says why. */
export type ConfigEditorKind =
  | 'bool'
  | 'int'
  | 'float'
  | 'str'
  | 'path'
  | 'list'
  | 'choice'
  | 'multi_choice'
  | 'model'
  | 'model_patterns'
  | 'secret'
  | 'table'
  | 'readonly';

/** What a `model` field may name: provider ids, whether it takes `:effort`,
 *  whether aliases are offered. */
export interface ConfigModelField {
  sources: string[];
  effort: boolean;
  aliases: boolean;
}

export interface ConfigCatalogueModel {
  ref: string;
  name: string;
  family: string | null;
  deprecated: boolean;
  reasoning: boolean;
  /** The effort levels the model takes; empty when it takes none. */
  efforts: string[];
  context_window: number;
  /** In `[models] enabled`, which a non-admin may pick from. */
  enabled: boolean;
}

export interface ConfigCatalogueSource {
  id: string;
  kind: string;
  label: string;
  discovered_at: string | null;
  last_error: string | null;
  models: ConfigCatalogueModel[];
}

export interface ConfigCatalogueAlias {
  name: string;
  /** The ref it resolves to on the CLI brains; null for the brain's own model. */
  target: string | null;
  effort: string | null;
  efforts: string[] | null;
}

/** What the config page's model pickers choose from, once per view. */
export interface ConfigModelCatalogue {
  sources: ConfigCatalogueSource[];
  aliases: ConfigCatalogueAlias[];
  /** How many models each stored `[models] enabled` entry matches. */
  pattern_counts: Record<string, number>;
}

export interface AdminConfigField {
  /** Dotted display label — `web.oauth2_client_secret`. Never an address: a
   *  segment can hold a dot, so the editor addresses a field by `path`. */
  key: string;
  name: string;
  value: unknown;
  type: string;
  /** A credential: `value` is always null. `set` says whether it is configured. */
  secret: boolean;
  set: boolean;
  path: string[];
  /** The dataclass default. Absent for a credential. */
  default?: unknown;
  /** Whether config.toml sets the key; when it does not, the default applies. */
  in_file: boolean;
  choices: string[] | null;
  multi_choices: string[] | null;
  doc: string | null;
  /** How a credential is changed: a secrets file by its name, or an inline
   *  value by its dotted label. Null for a plain field. */
  credential: { kind: 'file' | 'inline'; name: string } | null;
  /** The environment variable that sets the key over the file right now. */
  env_override: string | null;
  editor: ConfigEditorKind;
  read_only_reason: string | null;
  risky: boolean;
  /** A `table` field's current file value as TOML, secrets as sentinels. */
  toml?: string;
  /** A `model` field's sources and effort, read against `model_catalogue`. */
  model_field?: ConfigModelField;
}

export interface AdminConfigSection {
  key: string;
  label: string;
  fields: AdminConfigField[];
}

export interface ConfigProblem {
  kind: 'discarded' | 'unknown' | 'retired' | 'warning' | 'error' | 'model' | 'model_unknown';
  path: string[];
  message: string;
  blocking: boolean;
  /** In the file before this change; the change can fix it, and must. */
  pre_existing?: boolean;
  /** `unset` when removing the key at `path` resolves it: an unknown or
   *  retired key, or a file credential also set inline. `move` for a file
   *  credential held only inline, which the host can move into its file. */
  action?: 'unset' | 'move';
}

export interface ConfigManagedEntry {
  managed: boolean;
  source: string | null;
  at: string | null;
}

export type ConfigResource = 'config' | 'secrets' | 'admins';

/** Where a credential is held: its secret file, inline in config.toml, a
 *  variable set from outside the stack (read-only here), or nowhere.
 *  `unknown` is a file only the daemon mounts, before it has said. */
export type ConfigSecretSource = 'file' | 'inline' | 'environment' | 'none' | 'unknown';

export interface ConfigSecretFile {
  name: string;
  label: string;
  config_key: string | null;
  /** For a file no config key reads, the section it is shown in. */
  section?: string | null;
  set: boolean;
  source: ConfigSecretSource;
  /** Why a new value cannot be set here; a move may still be offered. */
  read_only_reason: string | null;
  /** Held inline with nothing managed, so it can move to its secret file. */
  movable: boolean;
  risky: boolean;
}

/** A credential the host moves from config.toml into its secret file. */
export interface ConfigMove {
  label: string;
  file: string;
}

export interface AdminConfigView {
  config_path: string | null;
  editable: boolean;
  sections: AdminConfigSection[];
  applier: { kind: 'host' | 'none'; available: boolean; reason: string };
  managed: Record<ConfigResource, ConfigManagedEntry>;
  /** sha256 of the config.toml bytes this view was built from. */
  document_digest: string | null;
  /** When the web process started. Moves when an applied change restarts it. */
  started_at: string | null;
  problems: ConfigProblem[];
  has_comments: boolean;
  /** The whole file for the raw editor, every secret leaf the sentinel
   *  `"<unchanged>"`. Null when there is no file or it does not parse. */
  raw_toml: string | null;
  /** One entry per deployment secrets file, never a value. */
  secrets: ConfigSecretFile[];
  /** Null when the catalogue could not be read; model fields are then text. */
  model_catalogue?: ConfigModelCatalogue | null;
  /** `candidates`: every user with a live sign-in identity, who may be picked. */
  admins: { users: string[]; candidates?: string[]; read_only_reason: string | null };
}

export type ConfigOp =
  | { op: 'set'; path: string[]; value: unknown }
  | { op: 'unset'; path: string[] }
  | { op: 'set_toml'; path: string[]; toml: string };

/** What preview and submit take. `secrets` maps a credential name to its new
 *  value, `""` clearing it; `admins` is the whole new list, or absent.
 *  `move_to_file` names credentials (by dotted label) the host moves from
 *  config.toml into their secret files; it carries no value. */
export interface ConfigChangeSet {
  base_digest: string;
  ops: ConfigOp[];
  raw: string | null;
  secrets: Record<string, string>;
  move_to_file?: string[];
  admins?: string[];
  confirm?: string[];
  step_up?: StepUpProof;
}

export interface ConfigPreview {
  diff: string[];
  problems: { blocking: ConfigProblem[]; warnings: ConfigProblem[] };
  errors: string[];
  changed: boolean;
  changed_sections: string[];
  changed_keys: number;
  /** Labels of the risky keys the change touches, `admins` for the list. */
  risky_keys: string[];
  secret_names: string[];
  /** Credentials the change moves from config.toml into their secret files. */
  moves: ConfigMove[];
  admins_changed: boolean;
  /** Tasks running now, which the restart after applying interrupts. */
  running_tasks: number;
  base_digest: string;
}

export type ConfigRequestStatus =
  'pending' | 'taken' | 'building' | 'applied' | 'unchanged' | 'refused' | 'failed' | 'cancelled';

export interface ConfigRequest {
  id: number;
  status: ConfigRequestStatus;
  admin_id: string;
  base_digest: string;
  document_toml: string;
  admins: string[] | null;
  admins_changed: boolean;
  changed_sections: string[];
  secret_names: string[];
  /** Labels of the credentials the request moves into their secret files. */
  moves: string[];
  diff_text: string;
  output_text: string | null;
  created_at: string;
  taken_at: string | null;
  finished_at: string | null;
}

/**
 * A refused config editor request.
 *
 * `error` is the route's code (`config_changed`, `request_open`,
 * `confirm_required`, …) and `extra` carries what comes with it: `digest`,
 * `id`, `keys`, `resources`, `problems`, `errors`. A refused step-up proof has
 * no `error`; it has `field` (`'code'` or `'password'`) and a `reason`, which
 * is what `StepUpDialog` reads. `status` 0 is a request that never reached the server.
 */
export class ConfigChangeError extends Error {
  readonly status: number;
  readonly error: string | null;
  readonly field: string | null;
  readonly reason: string | null;
  readonly extra: Record<string, unknown>;
  constructor(
    message: string,
    status: number,
    error: string | null,
    extra: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'ConfigChangeError';
    this.status = status;
    this.error = error;
    this.extra = extra;
    this.field = typeof extra.field === 'string' ? extra.field : null;
    this.reason = typeof extra.reason === 'string' ? extra.reason : null;
  }
}

/** Outside `apiFetch` because a refusal's code and its extras have to reach
 *  the review dialog, and `apiFetch` keeps only the message. */
async function configChangeWrite<T>(path: string, body: unknown): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api/admin/config${path}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    noteTransport(false, 'unreachable');
    throw new ConfigChangeError('Istota could not be reached. Nothing was sent.', 0, null);
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  let payload: unknown = null;
  try {
    payload = await resp.json();
  } catch {
    payload = null;
  }
  if (!resp.ok) {
    const { error, detail, ...extra } = (payload ?? {}) as Record<string, unknown>;
    const message =
      typeof detail === 'string' && detail.trim() ? detail : `API error: ${resp.status}`;
    throw new ConfigChangeError(
      message,
      resp.status,
      typeof error === 'string' ? error : null,
      extra,
    );
  }
  return payload as T;
}

/** The redacted diff, problems and risky keys a change set would produce.
 *  Stores nothing and needs no step-up. */
export function previewConfigChange(changeSet: ConfigChangeSet): Promise<ConfigPreview> {
  return configChangeWrite<ConfigPreview>('/preview', changeSet);
}

/** Store the change for the host applier. Needs `step_up`, and `confirm` for
 *  every risky key the preview named. */
export function submitConfigChange(changeSet: ConfigChangeSet): Promise<{ id: number }> {
  return configChangeWrite<{ id: number }>('/requests', changeSet);
}

export function getConfigRequest(id: number): Promise<ConfigRequest> {
  return apiFetch<ConfigRequest>(`/admin/config/requests/${id}`);
}

/** The newest requests first, with their redacted diffs and host output. */
export async function listConfigRequests(limit = 50): Promise<ConfigRequest[]> {
  const { requests } = await apiFetch<{ requests: ConfigRequest[] }>(
    `/admin/config/requests?limit=${limit}`,
  );
  return requests;
}

/** What restoring an earlier request would do. Nothing is stored: the
 *  admin submits `change_set` like any other change. */
export interface ConfigRestore {
  change_set: ConfigChangeSet;
  preview: ConfigPreview;
  note: string;
}

export function restoreConfigRequest(id: number): Promise<ConfigRestore> {
  return configChangeWrite<ConfigRestore>(`/requests/${id}/restore`, {});
}

/** Withdraw a request the host has not taken yet. */
export function cancelConfigRequest(id: number): Promise<{ id: number; status: 'cancelled' }> {
  return configChangeWrite(`/requests/${id}/cancel`, {});
}

export interface AdminUser {
  user_id: string;
  display_name: string;
  is_admin: boolean;
  state: 'nextcloud_only' | 'passwordless' | 'password_set';
  identity: { email: string; disabled: boolean; last_login_at: string | null } | null;
}

export interface AdminUsers {
  users: AdminUser[];
  orphans: AdminUser[];
  email_enabled: boolean;
}

export function getAdminUsers() {
  return apiFetch<AdminUsers>('/admin/users');
}

export function createAdminUser(user: { user_id: string; email: string; display_name?: string }) {
  return apiFetch<{ sent: boolean }>('/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  });
}

export type AdminUserAction = 'invite' | 'reset' | 'disable' | 'enable' | 'logout-all' | 'remove';

export function adminUserAction(userId: string, action: AdminUserAction) {
  const path = `/admin/users/${encodeURIComponent(userId)}`;
  if (action === 'remove') return apiFetch(path, { method: 'DELETE' });
  const disable = action === 'disable' || action === 'enable';
  return apiFetch(`${path}/${disable ? 'disable' : action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    ...(disable ? { body: JSON.stringify({ disabled: action === 'disable' }) } : {}),
  });
}

/** The profile fields an admin may set (`user_profiles.ADMIN_EDITABLE_FIELDS`). */
export interface AdminUserProfile {
  display_name: string;
  timezone: string;
  email_addresses: string[];
  trusted_email_senders: string[];
  quiet_email_senders: string[];
  outbound_approval: '' | 'off' | 'untrusted' | 'all';
  disabled_skills: string[];
  disabled_modules: string[];
  /** `0` is the deployment default. */
  max_foreground_workers: number;
  max_background_workers: number;
  sms_phone_number: string;
  /** Skills, and the `web` token, that mail somebody else wrote may also use. */
  correspondent_scopes: string[];
}

export type AdminWhatsAppStatus = 'unbound' | 'awaiting_first_message' | 'enrolled' | 'opted_out';

/** One user's settings, as `GET /admin/users/{id}` returns them. */
export interface AdminUserDetail {
  user_id: string;
  is_admin: boolean;
  identity: {
    email: string;
    disabled: boolean;
    last_login_at: string | null;
    state: 'passwordless' | 'password_set';
  } | null;
  profile: AdminUserProfile;
  channels: { log_channel: string; alerts_channel: string };
  whatsapp: {
    /** In full: the admin set it and needs to see what they set. */
    number: string;
    status: AdminWhatsAppStatus;
    /** Masked server-side, or null when nothing has enrolled. */
    identity: string | null;
    provider: string | null;
    last_seen_at: string | null;
  };
  /** Fields the deployment writes on every converge; the PATCH refuses them. */
  managed: string[];
  options: {
    modules: string[];
    skills: string[];
    /** What may be added. The server refuses `files`, `memory` and the rest regardless. */
    correspondent_scopes: string[];
    /** What such mail keeps with nothing added. */
    correspondent_default_scopes: string[];
    outbound_approval: AdminUserProfile['outbound_approval'][];
    outbound_approval_floor: 'off' | 'untrusted' | 'all';
    /** The `[email]` transport. */
    email_enabled: boolean;
    /** Email sign-in for the web UI — what the Login section needs. */
    email_login_enabled: boolean;
    sms_enabled: boolean;
    whatsapp_enabled: boolean;
  };
  /** On an identity PUT whose address append was skipped. */
  addresses_skipped?: 'managed';
}

/** A partial update: any profile field, plus the WhatsApp number (`""` clears it). */
export type AdminUserPatch = Partial<AdminUserProfile> & { whatsapp_number?: string };

/** A refused admin PATCH. `fields` names the inputs it is about (a managed
 *  field, a malformed value, a value another user holds); empty for a refusal
 *  of the whole request. */
export class AdminUserWriteError extends Error {
  readonly fields: string[];
  constructor(message: string, fields: string[]) {
    super(message);
    this.name = 'AdminUserWriteError';
    this.fields = fields;
  }
}

export function getAdminUser(userId: string) {
  return apiFetch<AdminUserDetail>(`/admin/users/${encodeURIComponent(userId)}`);
}

/** Outside `apiFetch` for the reason `credentialWrite` is: a refusal's
 *  `fields` has to reach the form, and `apiFetch` keeps only the message. */
export async function updateAdminUser(
  userId: string,
  patch: AdminUserPatch,
): Promise<AdminUserDetail> {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api/admin/users/${encodeURIComponent(userId)}`, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  } catch {
    noteTransport(false, 'unreachable');
    throw new AdminUserWriteError('Istota could not be reached. Nothing was saved.', []);
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  let payload: unknown = null;
  try {
    payload = await resp.json();
  } catch {
    payload = null;
  }
  if (!resp.ok) {
    const raw = (payload ?? {}) as { detail?: unknown; fields?: unknown };
    const message =
      typeof raw.detail === 'string' && raw.detail.trim()
        ? raw.detail
        : `API error: ${resp.status}`;
    const fields = Array.isArray(raw.fields)
      ? raw.fields.filter((f): f is string => typeof f === 'string')
      : [];
    throw new AdminUserWriteError(message, fields);
  }
  return payload as AdminUserDetail;
}

/** Keep the number; forget the latched identity, the window and the opt-out. */
export function resetAdminUserWhatsApp(userId: string) {
  return apiFetch<AdminUserDetail>(`/admin/users/${encodeURIComponent(userId)}/whatsapp/reset`, {
    method: 'POST',
  });
}

/** Attach or change the login email. A change signs the user out everywhere. */
export function setAdminUserIdentity(
  userId: string,
  body: { email: string; invite: boolean; add_to_addresses: boolean },
) {
  return apiFetch<AdminUserDetail>(`/admin/users/${encodeURIComponent(userId)}/identity`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getAdminConfig(): Promise<AdminConfigView> {
  return apiFetch<AdminConfigView>('/admin/config');
}

/** One runtime self-check. Same shape as `istota doctor --json` emits. */
export interface DoctorCheck {
  /** Stable dotted id, e.g. `developer.forge_binaries.gh`. */
  name: string;
  status: 'ok' | 'warn' | 'fail' | 'skip';
  /** What was observed. Redacted server-side; never carries a credential. */
  detail: string;
  /** What to do about it. Always present on `warn` and `fail`. */
  remedy: string;
  /** `image` = answerable from the built artifact; `deployment` = needs this install. */
  scope: 'image' | 'deployment';
}

export interface DoctorReport {
  /** Worst status present: `fail` > `warn` > `ok`. Skips don't count. */
  status: 'ok' | 'warn' | 'fail';
  summary: { ok: number; warn: number; fail: number; skip: number };
  /** Whether the daemon's last deep run is included. */
  deep: boolean;
  checks: DoctorCheck[];
  /** When the daemon's last run finished (UTC, `…Z`), or null before its first. */
  checked_at: string | null;
  /** The request that run answered, if it answered one. */
  request_id: string | null;
  deep_checked_at: string | null;
  deep_request_id: string | null;
  /** A run asked for and not yet picked up by the daemon. */
  pending: { id: string; deep: boolean } | null;
  /** The last request the daemon finished, whatever its runs found. */
  answered: { id: string; deep: boolean } | null;
}

/**
 * The daemon's last doctor runs. Nothing is checked in the web process: on the
 * Docker stack it is another container, without the daemon's sandbox.
 */
export async function getAdminDoctor(): Promise<DoctorReport> {
  return apiFetch<DoctorReport>('/admin/doctor');
}

/**
 * Ask the daemon for a run on its next tick. A request still pending is joined,
 * so the id returned is the one to wait for in `answered`.
 */
export async function requestDoctorRun(deep: boolean): Promise<{ id: string; deep: boolean }> {
  return apiFetch<{ id: string; deep: boolean }>('/admin/doctor/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deep }),
  });
}

/**
 * What the bridge says about the sidecar link, or `null`.
 *
 * `null` means **this process cannot see the bridge** — never that the bridge
 * is down. On the canonical Ansible deployment the web unit and the scheduler
 * are separate units and the bridge lives in the scheduler, so `null` is the
 * only answer that surface ever gives.
 */
export interface AdminConnectionLink {
  listening: boolean;
  connected: boolean;
  ready: boolean;
  /** The sidecar's last fatal, already reduced to a bounded label server-side. */
  fatal_reason: string | null;
  /** The session is unlinked and will not come back without a re-pair. */
  fatal_is_permanent: boolean;
  /** Another client keeps replacing the connection and the sidecar has
   *  stopped reconnecting (ISSUE-553). With `fatal_is_permanent` it has also
   *  given up retrying. */
  connection_replaced_latched: boolean;
  restarts: number | null;
}

/**
 * The pairing row's closed states, mirroring `db.WHATSAPP_PAIRING_TERMINAL_STATES`.
 *
 * Here rather than in the page because a set of state names written twice is a
 * set that drifts, and this is the module that already mirrors the server's
 * vocabulary. **It is a fallback, not the authority**: `AdminPairingState`
 * carries `terminal`, computed server-side from `row_state` by the same rule
 * the start route's own guard reads, and that field is what a caller should
 * gate on. These names are for a live stream frame, which carries no
 * `terminal` of its own. Pinned against the Python constant by
 * `pairingTerminalStates.parity.test.ts`.
 */
export const WHATSAPP_PAIRING_TERMINAL_STATES: ReadonlySet<string> = new Set([
  'paired',
  'expired',
  'failed',
]);

/** The durable pairing row, plus the live window's state where one is publishing. */
export interface AdminPairingState {
  window_id: string | null;
  /** `requested` | `servicing` | `awaiting_sidecar` | `awaiting_scan` |
   *  `sidecar_absent` | `paired` | `expired` | `failed`. */
  state: string | null;
  row_state: string | null;
  terminal: boolean;
  requested_by: string | null;
  requested_at: string | null;
  expires_at: string | null;
  expires_at_epoch: number | null;
  message: string | null;
  force: boolean;
  /** Bumped on every rotation. The only thing that says a new code exists. */
  qr_seq: number;
  /** Whether `qr.svg` would draw something now. Never the code itself. */
  qr_available: boolean;
}

/** The deployment's WhatsApp link, as `GET /admin/connections` lists it. */
export interface AdminWhatsAppConnection {
  id: 'whatsapp';
  label: string;
  enabled: boolean;
  provider: string;
  number: string;
  /** The adapter has a QR at all — false for Meta's Cloud API. */
  pairing_supported: boolean;
  pairing_enabled: boolean;
  restart_interval_seconds: number;
  credential_errors: string[];
  link: AdminConnectionLink | null;
  pairing: AdminPairingState | null;
  /** Why a start would be refused before it is attempted, or `null`. */
  pairing_blocked_reason: string | null;
}

/**
 * The daemon's last `nextcloud.connection` check, redacted again by the web
 * process. `stale` means the report is older than the Health pane's bound.
 */
export interface AdminNextcloudCheck {
  status: 'ok' | 'warn' | 'fail' | 'skip';
  detail: string;
  remedy: string;
  at: string | null;
  stale: boolean;
}

/** The Nextcloud instance the bot signs in to. Never carries the app password. */
export interface AdminNextcloudConnection {
  id: 'nextcloud';
  label: string;
  configured: boolean;
  /** The base URL with any userinfo removed; empty when unconfigured. */
  url: string;
  username: string;
  talk_enabled: boolean;
  talk_bot_username: string;
  storage: 'nextcloud' | 'local';
  /** Null when the daemon has recorded no such check yet. */
  check: AdminNextcloudCheck | null;
}

/** Google application configuration; never carries the client secret. */
export interface AdminGoogleWorkspaceConnection {
  id: 'google_workspace';
  label: string;
  enabled: boolean;
  client_id: string;
  client_secret_set: boolean;
  redirect_uri: string | null;
  services: GoogleOfferedService[];
  additional_scopes: string[];
  missing: ('client_id' | 'client_secret' | 'site_hostname' | 'scopes')[];
  status: 'disabled' | 'incomplete' | 'configured';
}

export type ServarrApp = 'sonarr' | 'radarr' | 'lidarr' | 'prowlarr';

/** One arr app of the install connection. Never carries the API key. */
export interface AdminServarrApp {
  app: ServarrApp;
  /** The configured URL with any userinfo removed; empty for an incomplete table. */
  url: string;
  /** Null for a table the loader dropped, where it is unknown. */
  api_key_set: boolean | null;
  /** The table is missing its url or api_key, so the skill ignores it. */
  incomplete: boolean;
  /** A config problem doctor reports without a request, or null. */
  problem: string | null;
  /** The daemon's last `servarr.connection.<app>` row; null when none is recorded. */
  check: AdminNextcloudCheck | null;
}

/** The servarr skill's install-wide connection; configured is no claim the keys work. */
export interface AdminServarrConnection {
  id: 'servarr';
  label: string;
  status: 'disabled' | 'incomplete' | 'configured';
  apps: AdminServarrApp[];
  /** Known apps with no table. */
  missing: ServarrApp[];
  requesters: string[];
  /** The add defaults per app, as configured. */
  defaults: Partial<Record<ServarrApp, Record<string, string>>>;
}

/** Deployment connections in order: Nextcloud, WhatsApp, Google Workspace, Servarr. */
export type AdminConnection =
  | AdminNextcloudConnection
  | AdminWhatsAppConnection
  | AdminGoogleWorkspaceConnection
  | AdminServarrConnection;

export interface AdminBrowsers {
  status: 'ok' | 'disabled' | 'unavailable';
  console_configured: boolean;
  instances: { user: string; slot: number; idle_seconds: number; url: string }[];
  /** When the scheduler read the pool; absent when the web process asked it directly. */
  as_of?: string;
}

export async function getAdminBrowsers(): Promise<AdminBrowsers> {
  return apiFetch<AdminBrowsers>('/admin/browsers', undefined, 8_000);
}

/** A single-use ticket for the browser console relay, valid for `expires_in`
 *  seconds. Through `groupRequest`, like the other admin step-up routes, so a
 *  refusal arrives as a `GroupApiError` carrying its `code` (`console_limit`,
 *  `admin_identity_required`, …) or a step-up `reason`. */
export function getAdminBrowserConsoleTicket(
  user: string,
  stepUp?: StepUpProof,
): Promise<{ ticket: string; expires_in: number }> {
  return groupRequest('/admin/browsers/console-ticket', 'POST', { user, step_up: stepUp });
}

/** The relay's websocket URL, on this page's own origin. */
export function adminBrowserConsoleUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}${base}/api/admin/browsers/console`;
}

export async function getAdminConnections(): Promise<{ connections: AdminConnection[] }> {
  return apiFetch<{ connections: AdminConnection[] }>('/admin/connections');
}

export async function getWhatsAppPairing(): Promise<{ pairing: AdminPairingState | null }> {
  return apiFetch<{ pairing: AdminPairingState | null }>('/admin/connections/whatsapp/pairing');
}

/**
 * Ask for a pairing window.
 *
 * **Both flags are always sent and neither is ever inferred**, which is why
 * they are required rather than optional: the rule is enforced by the type
 * instead of by each caller remembering it. `force` means "I accept
 * disconnecting a session that is working", and the server answers `force`
 * without `confirm_disconnect` with a 400, so the destructive path cannot be
 * reached by one flipped boolean. The unforced call is safe against a live
 * session whatever this browser believes: the bridge refuses anything with no
 * latched permanent fault and records that refusal on the row.
 */
export async function startWhatsAppPairing(opts: {
  force: boolean;
  confirmDisconnect: boolean;
}): Promise<{ window_id: string; state: string; force: boolean }> {
  return apiFetch('/admin/connections/whatsapp/pairing', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      force: opts.force === true,
      confirm_disconnect: opts.confirmDisconnect === true,
    }),
  });
}

export async function cancelWhatsAppPairing(): Promise<{
  cancelled: boolean;
  reason: string;
  window_id?: string | null;
}> {
  return apiFetch('/admin/connections/whatsapp/pairing', { method: 'DELETE' });
}

/** URL for the pairing SSE stream. Built here for `adminLogStreamUrl`'s reason. */
export function whatsAppPairingStreamUrl(): string {
  return `${base}/api/admin/connections/whatsapp/pairing/stream`;
}

/**
 * URL of the server-rendered pairing code.
 *
 * `seq` is a cache-buster rather than a selector — the server draws whichever
 * code is live and names it in `X-Pairing-Qr-Seq` — so it is what makes the
 * browser refetch on a rotation and hold the image still between them. The code
 * reaches the page only as this response's bytes: nothing puts it in a string.
 */
export function whatsAppPairingQrUrl(seq: number): string {
  return `${base}/api/admin/connections/whatsapp/pairing/qr.svg?seq=${encodeURIComponent(String(seq))}`;
}

export interface FeedCategory {
  id: number;
  title: string;
}

export interface Feed {
  id: number;
  title: string;
  site_url: string;
  category: FeedCategory;
}

export interface FeedEntry {
  id: number;
  title: string;
  url: string;
  content: string;
  images: string[];
  /** Images the server hid because a newer entry already showed them. */
  duplicate_image_count: number;
  /**
   * Canonical watch page for playable media (an Are.na Embed block); empty
   * otherwise. The provider's own iframe is deliberately not stored, so the
   * reader rebuilds a player from this via `$lib/feeds/embed`.
   */
  embed_url: string;
  /**
   * A downloadable document the entry is about (an Are.na Attachment, in
   * practice a PDF); empty otherwise. Distinct from `embed_url`: this one is
   * opened rather than played, and its presence is what stops the reader
   * treating a PDF's cover page as an ordinary gallery image.
   */
  file_url: string;
  /**
   * A media file played inline with a native `<video>` / `<audio>` — a
   * Mastodon video attachment, a podcast enclosure; empty otherwise. Neither
   * of the two above: `embed_url` is a provider watch page we rebuild a
   * player for, `file_url` is opened elsewhere. Such a URL used to arrive in
   * `images` and render as an `<img>` that never decodes (ISSUE-356).
   */
  media_url: string;
  /** MIME type for `media_url` (e.g. `video/mp4`); empty otherwise. */
  media_type: string;
  feed: Feed;
  status: string;
  starred: boolean;
  starred_at: string;
  published_at: string;
  created_at: string;
}

export interface FeedsResponse {
  feeds: Feed[];
  entries: FeedEntry[];
  total: number;
}

/**
 * How long `/me` may run before it counts as a gap.
 *
 * Bounded, unlike most reads here, because the root layout gates the whole app
 * on this one and then asks the connectivity store which kind of failure it
 * was (ISSUE-354). Unbounded, a stalled request outlives the store's own 5s
 * probe: the probe finds the connection back and sets the store online, the
 * stalled `/me` rejects minutes later, and the layout reads "online" for a
 * request that plainly was not — which is the error page again, on a device
 * that is working. The bound is what keeps the answer close to the failure.
 *
 * Generous rather than probe-sized: this is the request the app is waiting on,
 * not one we will repeat in five seconds, and failing a slow-but-working
 * connection would cost more than it saves.
 */
export const ME_TIMEOUT_MS = 20_000;

export interface NavUnreadCounts {
  enabled: boolean;
  chat: number | null;
  briefings: number | null;
  feeds: number | null;
}

export function getNavUnreadCounts(): Promise<NavUnreadCounts> {
  return apiFetch<NavUnreadCounts>('/nav/unread', undefined, 10_000);
}

export async function getMe(timeoutMs = ME_TIMEOUT_MS): Promise<User> {
  return apiFetch<User>('/me', undefined, timeoutMs);
}

export async function getFeeds(params?: Record<string, string>): Promise<FeedsResponse> {
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  return apiFetch<FeedsResponse>(`/feeds${qs}`);
}

export interface FeedCounts {
  unread: number;
  starred: number;
  /** Unread entries per feed id; a feed with none is absent. */
  unread_by_feed: Record<string, number>;
}

export function getFeedCounts(): Promise<FeedCounts> {
  return apiFetch<FeedCounts>('/feeds/counts');
}

export async function getFeedEntry(id: number): Promise<FeedEntry> {
  return apiFetch<FeedEntry>(`/feeds/entries/${id}`);
}

export async function updateEntryStatus(id: number, status: string): Promise<void> {
  await apiFetch(`/feeds/entries/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });
}

export async function updateEntriesStatus(ids: number[], status: string): Promise<void> {
  await apiFetch('/feeds/entries/batch', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entry_ids: ids, status }),
  });
}

export async function updateEntryStarred(id: number, starred: boolean): Promise<void> {
  await apiFetch(`/feeds/entries/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  });
}

export type MarkAsReadScope = 'all' | 'feed' | 'category';

export async function markAsRead(
  scope: MarkAsReadScope,
  opts?: { id?: number; before_id?: number },
): Promise<{ status: string; updated: number }> {
  const body: Record<string, unknown> = { scope };
  if (opts?.id != null) body.id = opts.id;
  if (opts?.before_id != null) body.before_id = opts.before_id;
  return apiFetch('/feeds/mark-as-read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// Feeds settings types

export interface FeedsConfigCategory {
  slug: string;
  title?: string;
}

export interface FeedsConfigFeed {
  url: string;
  title?: string;
  category?: string;
  poll_interval_minutes?: number;
}

export interface FeedsConfigSettings {
  default_poll_interval_minutes?: number;
  /** Look-back window for hiding repeated images; 0 disables. */
  image_dedupe_window_days?: number;
  /** Days a read entry is kept after it was added; 0 disables age pruning. */
  entry_retention_days?: number;
  /** Maximum stored entries per feed, except stars; 0 disables the cap. */
  max_entries_per_feed?: number;
}

export interface FeedsConfigPayload {
  settings: FeedsConfigSettings;
  categories: FeedsConfigCategory[];
  feeds: FeedsConfigFeed[];
}

export interface FeedsDiagnostics {
  total_feeds: number;
  total_entries: number;
  unread_entries: number;
  error_feeds: number;
  last_poll_at: string | null;
}

export interface FeedsFeedState {
  url: string;
  last_fetched_at: string | null;
  last_error: string | null;
  error_count: number;
}

export interface FeedsConfigResponse {
  config: FeedsConfigPayload;
  diagnostics: FeedsDiagnostics;
  feed_state: FeedsFeedState[];
}

export interface FeedsImportResult {
  status: string;
  feeds_added: number;
  feeds_updated: number;
  categories_added: number;
  rewritten_bridger_urls: number;
}

export async function getFeedsConfig(): Promise<FeedsConfigResponse> {
  return apiFetch<FeedsConfigResponse>('/feeds/config');
}

export async function putFeedsConfig(config: FeedsConfigPayload): Promise<{
  status: string;
  sync: { categories_added: number; feeds_added: number; feeds_updated: number };
}> {
  return apiFetch('/feeds/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config }),
  });
}

export async function importOpml(file: File): Promise<FeedsImportResult> {
  const fd = new FormData();
  fd.append('file', file);
  const resp = await fetch(`${base}/api/feeds/import-opml`, {
    method: 'POST',
    credentials: 'same-origin',
    body: fd,
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let msg = `Import failed: ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.error) msg = body.error;
    } catch {
      // ignore
    }
    throw new Error(msg);
  }
  return resp.json();
}

export function exportOpmlUrl(): string {
  return `${base}/api/feeds/export-opml`;
}

export async function refreshFeeds(): Promise<{ status: string; feeds_queued: number }> {
  return apiFetch('/feeds/refresh', { method: 'POST' });
}

// Location types

export interface LocationPing {
  timestamp: string;
  lat: number;
  lon: number;
  /**
   * Metres as the device reported them; what they are measured against varies
   * by source and is not recorded. Null on three counts: a horizontal fix with
   * no vertical one, a fix the device flagged as vertically invalid, and a
   * point the client declared rather than measured (its wifi-zone feature).
   * The last of those covers whole intervals rather than scattered samples, so
   * do not reason about coverage from a per-ping rate.
   */
  altitude: number | null;
  accuracy: number;
  place: string | null;
  speed: number | null;
  battery: number | null;
  activity_type: string | null;
  /**
   * 'overland' for the phone, 'garmin' for an imported watch track. Present on
   * `history` pings; the current-location payload does not carry it.
   */
  source?: string;
}

export interface CurrentLocation {
  last_ping: LocationPing | null;
  current_visit: {
    place_name: string;
    entered_at: string;
    duration_minutes: number | null;
    ping_count: number;
  } | null;
}

export interface DaySummaryStop {
  type: 'stop';
  location: string;
  location_source: string | null;
  arrived: string;
  departed: string;
  ping_count: number;
  lat: number;
  lon: number;
}

/** A recorded activity (an imported watch track), kept out of the stops. */
export interface DaySummaryActivity {
  type: 'activity';
  activity: string | null;
  source: string;
  start: string;
  end: string;
  duration_minutes: number | null;
  distance_km: number;
  ping_count: number;
  start_place: string | null;
  end_place: string | null;
  start_lat: number;
  start_lon: number;
  end_lat: number;
  end_lon: number;
}

export interface DaySummary {
  date: string;
  timezone: string;
  ping_count: number;
  transit_pings: number;
  stops: DaySummaryStop[];
  activities: DaySummaryActivity[];
}

export interface PingsResponse {
  pings: LocationPing[];
  count: number;
}

export interface Place {
  id: number;
  name: string;
  lat: number;
  lon: number;
  radius_meters: number;
  category: string;
  notes?: string | null;
}

export interface PlacesResponse {
  places: Place[];
}

export interface PlaceStats {
  place_id: number;
  total_visits: number;
  first_visit: string | null;
  last_visit: string | null;
  avg_duration_min: number | null;
  total_duration_min: number | null;
  longest_visit_min: number | null;
}

export interface DiscoveredCluster {
  lat: number;
  lon: number;
  total_pings: number;
  first_seen: string;
  last_seen: string;
  radius_meters?: number;
}

export interface DiscoverResponse {
  clusters: DiscoveredCluster[];
}

export interface DismissedCluster {
  id: number;
  lat: number;
  lon: number;
  radius_meters: number;
  dismissed_at: string;
}

export interface DismissedClustersResponse {
  dismissed: DismissedCluster[];
}

// Location API

function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}

function withBrowserTz(params: Record<string, string>): URLSearchParams {
  const qs = new URLSearchParams(params);
  const tz = browserTz();
  if (tz && !qs.has('tz')) qs.set('tz', tz);
  return qs;
}

export async function getLocationCurrent(): Promise<CurrentLocation> {
  return apiFetch<CurrentLocation>('/location/current');
}

export async function getLocationPings(params: Record<string, string>): Promise<PingsResponse> {
  const qs = withBrowserTz(params).toString();
  return apiFetch<PingsResponse>(`/location/pings?${qs}`);
}

export async function getDaySummary(date?: string): Promise<DaySummary> {
  const params: Record<string, string> = {};
  if (date) params.date = date;
  const qs = withBrowserTz(params).toString();
  return apiFetch<DaySummary>(`/location/day-summary${qs ? '?' + qs : ''}`);
}

export async function getLocationPlaces(): Promise<PlacesResponse> {
  return apiFetch<PlacesResponse>('/location/places');
}

export async function createPlace(data: {
  name: string;
  lat: number;
  lon: number;
  radius_meters?: number;
  category?: string;
  notes?: string | null;
}): Promise<Place> {
  return apiFetch<Place>('/location/places', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function updatePlace(
  id: number,
  data: Partial<Pick<Place, 'name' | 'lat' | 'lon' | 'radius_meters' | 'category' | 'notes'>>,
): Promise<Place> {
  return apiFetch<Place>(`/location/places/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deletePlace(id: number): Promise<void> {
  await apiFetch(`/location/places/${id}`, { method: 'DELETE' });
}

export async function getPlaceStats(placeId: number): Promise<PlaceStats> {
  return apiFetch<PlaceStats>(`/location/places/${placeId}/stats`);
}

export async function discoverPlaces(minPings?: number): Promise<DiscoverResponse> {
  const qs = minPings ? `?min_pings=${minPings}` : '';
  return apiFetch<DiscoverResponse>(`/location/discover-places${qs}`);
}

export async function listDismissedClusters(): Promise<DismissedClustersResponse> {
  return apiFetch<DismissedClustersResponse>('/location/dismissed-clusters');
}

export async function dismissCluster(data: {
  lat: number;
  lon: number;
  radius_meters: number;
}): Promise<DismissedCluster> {
  return apiFetch<DismissedCluster>('/location/dismissed-clusters', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function restoreDismissedCluster(id: number): Promise<void> {
  await apiFetch(`/location/dismissed-clusters/${id}`, { method: 'DELETE' });
}

// ---- Settings (Phase 5) ----

export interface SettingsField {
  key: string;
  label: string;
  type: 'text' | 'email' | 'password' | 'url';
  // A setting stored beside the credentials; its value comes back in `values`.
  plain?: boolean;
}

export interface ServiceCard {
  service: string;
  label: string;
  status: 'configured' | 'partial' | 'missing' | 'unavailable';
  fields: SettingsField[];
  configured_keys: string[];
  // Stored values of the `plain` fields only; credentials never come back.
  values?: Record<string, string>;
  last_updated: string | null;
  used_by?: string[];
  // Optional prose from the service schema: where to get the credential, or
  // what setting it changes. Absent for most services.
  hint?: string;
  oauth?: boolean; // auth is an OAuth redirect rather than writable fields
  // Render a bespoke card (garmin, google_workspace) instead of fields.
  custom_ui?: boolean;
  connected?: boolean; // google_workspace OAuth state
  enabled?: boolean; // google_workspace module flag
}

export interface ServicesResponse {
  services: ServiceCard[];
}

export async function getSettingsServices(): Promise<ServicesResponse> {
  return apiFetch<ServicesResponse>('/settings/services');
}

// --- Google Workspace (ISSUE-240) ---
//
// The instance scope list is a ceiling, not a request: it names what the
// operator's Google Cloud project has enabled. `offered` therefore names every
// service, with `max_level: 'off'` for the ones this instance does not offer —
// which the card has to render differently from "you did not grant it".

export type GoogleScopeLevel = 'off' | 'readonly' | 'full';

export interface GoogleOfferedService {
  service: string;
  label: string;
  max_level: GoogleScopeLevel;
}

export interface GoogleGrantedService {
  service: string;
  label: string;
  level: Exclude<GoogleScopeLevel, 'off'>;
  scopes: string[];
  /** False when the grant holds only part of what that level needs — Google's
   *  consent screen lets a user deselect individual boxes. */
  complete: boolean;
  /** Granted scopes of this service below the reported level. In the map, so
   *  never "unrecognised"; outside the reported level's set, so they would
   *  otherwise appear nowhere. */
  also: string[];
}

export interface GoogleStatus {
  enabled: boolean;
  connected: boolean;
  offered: GoogleOfferedService[];
  granted: GoogleGrantedService[];
  unrecognized_scopes: string[];
  /** Ceiling scopes with no service row. Requested unconditionally — no picker
   *  row can turn one off — so the card names them. */
  unoffered_scopes: string[];
  /** Clamped to the current ceiling, so it never names a level the picker's
   *  own option list no longer offers. The stored value is left untouched. */
  selection: Record<string, GoogleScopeLevel>;
  /** False when the user never chose — `selection` is then the whole ceiling. */
  selection_set: boolean;
  requested_scopes: string[];
  /** Requested but not granted: reconnect to apply. */
  missing_scopes: string[];
  /** Granted but no longer requested: the grant outlived a narrowing. */
  extra_scopes: string[];
}

export interface GoogleScopesSaveResponse {
  ok: boolean;
  selection: Record<string, GoogleScopeLevel>;
  requested_scopes: string[];
  reconnect_required: boolean;
}

export async function getGoogleStatus(): Promise<GoogleStatus> {
  return apiFetch<GoogleStatus>('/google/status');
}

export async function saveGoogleScopes(
  selection: Record<string, GoogleScopeLevel>,
): Promise<GoogleScopesSaveResponse> {
  return apiFetch<GoogleScopesSaveResponse>('/google/scopes', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selection }),
  });
}

export async function disconnectGoogle(): Promise<void> {
  await apiFetch('/google/disconnect', { method: 'DELETE' });
}

// --- modules + per-module services ---

export interface ModulesResponse {
  modules: string[];
  disabled: string[];
  enabled_for_user: Record<string, boolean>;
  /** What the Disabled skills picker offers: the skills this user's tasks can load. */
  skills: string[];
}

export async function getModules(): Promise<ModulesResponse> {
  return apiFetch<ModulesResponse>('/settings/modules');
}

export interface ModuleServicesResponse {
  module: string;
  module_enabled: boolean;
  services: ServiceCard[];
}

export async function getModuleServices(module: string): Promise<ModuleServicesResponse> {
  return apiFetch<ModuleServicesResponse>(`/settings/module-services/${module}`);
}

export interface LocationSettingsInfo {
  webhook_url: string;
  module_enabled: boolean;
  place_detection: {
    accuracy_threshold_m: number;
    visit_exit_minutes: number;
  };
}

export async function getLocationSettingsInfo(): Promise<LocationSettingsInfo> {
  return apiFetch<LocationSettingsInfo>('/location/settings-info');
}

/** One token that can post to `/webhooks/location`. Never the token itself. */
export interface LocationDevice {
  id: number;
  /** `app`: minted by the tracker card for its own phone. `code`: shown as a QR code or URL. */
  kind: 'app' | 'code';
  /** The tracker's device UUID for an `app` row; null for a `code` row. */
  device_id: string | null;
  label: string;
  token_tail: string;
  created_at: string;
  last_used_at: string | null;
}

export interface LocationDevicesResponse {
  devices: LocationDevice[];
  /** Whether the shared token from before per-device tokens still exists. */
  legacy_token: boolean;
}

export type CreateLocationDeviceRequest =
  { kind: 'app'; device_id: string; label?: string } | { kind: 'code'; label?: string };

export interface CreatedLocationDevice {
  id: number;
  /** Returned once and never again; the server keeps only its hash. */
  token: string;
  /** `webhook_url` without its query string, for a tracker that sends a Bearer header. */
  endpoint: string;
  webhook_url: string;
}

export function listLocationDevices(): Promise<LocationDevicesResponse> {
  return apiFetch('/location/devices');
}

/**
 * Mint an ingest token for one device. Touches no other device's token.
 *
 * The only call in this file whose response carries a location token: it has
 * to reach a tracker or a QR code. A 400 or 409 refusal is all message, and
 * `apiFetch` throws with the server's `detail` as the error text.
 */
export function createLocationDevice(
  req: CreateLocationDeviceRequest,
): Promise<CreatedLocationDevice> {
  return apiFetch('/location/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
}

export function deleteLocationDevice(id: number): Promise<{ ok: boolean }> {
  return apiFetch(`/location/devices/${id}`, { method: 'DELETE' });
}

/** Remove the shared token from before per-device tokens. */
export function deleteLegacyIngestToken(): Promise<{ ok: boolean }> {
  return apiFetch('/location/devices/legacy', { method: 'DELETE' });
}

/** A capability a device token carries. `notes` saves quick notes, `voice` sends voice tasks. */
export type DeviceScope = 'notes' | 'voice';

/**
 * One phone's device token for native actions. Never the token itself;
 * `token_tail` tells two rows apart. Separate from the location ingest tokens
 * above, which a different process checks.
 */
export interface DeviceToken {
  id: number;
  scopes: string[];
  token_tail: string;
  created_at: string;
  last_used_at: string | null;
}

/** A location token as a phone entry carries it: the `app` row for that device. */
export type PhoneLocationToken = Omit<LocationDevice, 'kind' | 'device_id'>;

/**
 * One phone in Settings → Devices, with every token it holds. The server joins
 * the device token and the location `app` row on the device id; either may be
 * missing. `last_used_at` is the newer of the two.
 */
export interface Phone {
  /** The app's device UUID, upper-cased. */
  device_id: string;
  label: string | null;
  token: DeviceToken | null;
  location: PhoneLocationToken | null;
  created_at: string;
  last_used_at: string | null;
}

export interface DevicesResponse {
  devices: Phone[];
  /** Location `code` tokens (Overland, a QR code), which name no device. */
  location_trackers: PhoneLocationToken[];
  /** Whether the shared location token from before per-device tokens still exists. */
  legacy_location_token: boolean;
}

export interface CreatedDeviceToken {
  id: number;
  /** Returned once and never again; the server keeps only its hash. */
  token: string;
  scopes: string[];
}

export function listDevices(): Promise<DevicesResponse> {
  return apiFetch('/devices');
}

/** Revoke every token one phone holds, in one server transaction. */
export function revokePhone(
  deviceId: string,
): Promise<{ ok: boolean; token: boolean; location: boolean }> {
  return apiFetch(`/devices/phones/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
}

/**
 * Mint this device's token, replacing any it had: the old token stops working
 * the moment this answers. Refusals are `{error, code}`: `unknown_scope` (400),
 * `notes_folder_unset` and `too_many_devices` (409).
 */
export function createDevice(req: {
  device_id: string;
  label?: string;
  scopes: DeviceScope[];
}): Promise<CreatedDeviceToken> {
  return apiFetch('/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
}

/** Revoke one device token, leaving the phone's location token alone. 404 for a row that is not the caller's. */
export function deleteDevice(id: number): Promise<{ ok: boolean }> {
  return apiFetch(`/devices/${id}`, { method: 'DELETE' });
}

// The basemap spec embeds the calling user's stored CARTO key in the tile URL,
// so writing that key invalidates the cached spec. Without this, pasting a key
// on /location/settings and navigating to /location is a client-side
// navigation: the module stays loaded, the stale spec is served, and the map
// does not change until a full reload — which is not what the settings card
// says will happen, and gives the user no reason to suspect a reload.
function invalidateSecretDerivedCaches(service: string): void {
  if (service === 'carto') resetBasemapCache();
}

export async function setSecret(
  service: string,
  key: string,
  value: string,
): Promise<{ ok: boolean; configured: boolean }> {
  const result = await apiFetch<{ ok: boolean; configured: boolean }>(
    `/settings/secrets/${service}/${key}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    },
  );
  invalidateSecretDerivedCaches(service);
  return result;
}

export async function deleteSecret(
  service: string,
  key: string,
): Promise<{ ok: boolean; deleted: boolean }> {
  const result = await apiFetch<{ ok: boolean; deleted: boolean }>(
    `/settings/secrets/${service}/${key}`,
    { method: 'DELETE' },
  );
  invalidateSecretDerivedCaches(service);
  return result;
}

/**
 * Derive Monarch session cookies from email+password and store them.
 *
 * The plaintext credentials never persist on the server — they're used
 * once to call api.monarch.com/auth/login/, the resulting session_id +
 * csrftoken get written to the encrypted secrets table, and the password
 * is dropped at the end of the request. The MFA code (if any) is the
 * *current* 6-digit TOTP, not the secret.
 */
/** What a login attempt can come back as.
 *
 * `challenge` is deliberately not an error: Monarch accepted the password and
 * wants a one-time code, which is a step in the flow rather than a failure.
 * Throwing for it is what produced the old "login failed" message on a
 * perfectly good password.
 */
export type MonarchLoginResult =
  | { status: 'ok' }
  | { status: 'challenge'; kind: 'email_otp' | 'mfa'; message: string }
  | {
      status: 'error';
      kind: 'auth' | 'captcha' | 'cloudflare' | 'blocked' | 'other';
      message: string;
    };

/** Read `detail` out of a FastAPI error body, structured or plain. */
function monarchDetail(body: unknown): { code: string; message: string } {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === 'object') {
    const d = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof d.code === 'string' ? d.code : '',
      message: typeof d.message === 'string' ? d.message : '',
    };
  }
  return { code: '', message: typeof detail === 'string' ? detail : '' };
}

export async function monarchLogin(
  email: string,
  password: string,
  codes: { mfaTotp?: string; emailOtp?: string } = {},
): Promise<MonarchLoginResult> {
  // Not apiFetch: this endpoint's whole contract is in the response *body*
  // (which apiFetch discards) and it answers a wrong Monarch password with
  // 401 (which apiFetch turns into an AuthError, bouncing the user to the
  // istota login page as though their own session had expired).
  const resp = await fetch(`${base}/api/money/monarch/login`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email,
      password,
      mfa_totp: codes.mfaTotp ?? '',
      email_otp: codes.emailOtp ?? '',
    }),
  });

  if (resp.ok) return { status: 'ok' };

  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    // A proxy error page or an empty body — fall through to the status-based
    // wording rather than surfacing a JSON parse failure to the user.
  }
  const { code, message } = monarchDetail(body);

  if (resp.status === 412) {
    return {
      status: 'challenge',
      kind: code === 'mfa_required' ? 'mfa' : 'email_otp',
      message,
    };
  }
  if (resp.status === 401) {
    return {
      status: 'error',
      kind: 'auth',
      message: message || 'Monarch rejected that email and password.',
    };
  }
  if (resp.status === 503) {
    const lower = message.toLowerCase();
    const kind = lower.includes('captcha')
      ? 'captcha'
      : lower.includes('cloudflare')
        ? 'cloudflare'
        : 'blocked';
    return { status: 'error', kind, message };
  }
  return {
    status: 'error',
    kind: 'other',
    message: message || `Login failed (HTTP ${resp.status}).`,
  };
}

// --- Phase 6: profile + resources ---

export interface UserProfile {
  // Read-only: fields the deployment re-writes on every converge. The PUT
  // refuses an edit to one (409 `managed_by_provisioning`), so the settings
  // page renders them disabled.
  managed?: string[];
  user_id: string;
  display_name: string;
  timezone: string;
  email_addresses: string[];
  trusted_email_senders: string[];
  quiet_email_senders: string[];
  log_channel: string;
  alerts_channel: string;
  disabled_skills: string[];
  disabled_modules: string[];
  max_foreground_workers: number;
  max_background_workers: number;
  default_destination: string;
  // The room a destination naming no room of its own lands in — a canonical
  // room token, '' for unset, when the per-surface default is worked out
  // instead. One token for both surfaces: a promoted room answers on web and on
  // Talk without being named twice (ISSUE-477).
  default_room: string;
  routing: Record<string, string>;
  briefing_email_html: boolean;
  // Opt-in: follow the GPS timezone on travel (ISSUE-096). Off by default —
  // it overwrites the timezone the user chose above.
  timezone_follow_location: boolean;
  show_nav_unread_badges: boolean;
  push_chat_alerts?: 'alerts' | 'badge' | 'off';
  push_preview?: boolean;
  // Missing category IDs remain enabled, including newly registered sources.
  push_inbox_categories?: Record<string, boolean>;
  // How much of a turn that arrived from outside the room (today, mirrored
  // email) the transcript shows. The row is always there whatever this says —
  // `hidden` withholds the body, never the turn.
  external_turn_display: ExternalTurnDisplay;
  // Where a relay question from another user reaches this one. '' defers to
  // the asker's choice and ends at the default room.
  relay_delivery: RelayDelivery;
  // Read-only: which of the values above would reach this user now, asked of
  // the same checks the relay resolver runs. An unavailable preference still
  // saves, and the resolver then falls back to the default room.
  relay_delivery_options?: RelayDeliveryOption[];
  // Read-only hint from the server: surfaces available for delivery routing.
  delivery_surfaces?: string[];
  // Read-only: the rooms a `web:<token>` route can name, oldest first. `default`
  // marks the one a bare `web` route lands in, so the picker can say which room
  // that is rather than leaving it unnamed (ISSUE-473).
  web_rooms?: WebRoomOption[];
  // Read-only: which of this profile's own web pins will swallow a delivery —
  // the user is a member and the room is archived, gone or hidden. Its own key
  // rather than a test of `web_rooms`, because absence from that list also
  // covers a room with no handle yet and a failed lookup, both of which deliver
  // perfectly well (ISSUE-478).
  unavailable_web_rooms?: string[];
  // Read-only: this profile's `default_room` when it is not being honoured — the
  // room is archived, gone or no longer theirs, so the bare-`web` heuristic
  // answers instead. Its own key rather than a place in the list above, and its
  // own mark in the picker: an unavailable route swallows the delivery, while an
  // ignored default room only means the delivery lands somewhere the user did
  // not choose (ISSUE-479). A pin that is merely hidden is not reported — the
  // next delivery un-hides the room.
  ignored_default_room?: string;
  // Read-only: the name of the room a bare `web` delivery would land in now,
  // read without creating it; null when the server could not say. The iOS
  // voice sheet shows it as a label (`voiceRoom`).
  default_room_name?: string | null;
  // Read-only: the Talk conversations a `talk:<token>` route can name — the two
  // the bot provisioned, then every Talk-bound room the user is in. No
  // `default` flag: a bare `talk` resolves per purpose, and each row says where
  // in its own surface label (ISSUE-475).
  talk_rooms?: TalkRoomOption[];
  // Where a quick note is saved: a folder relative to My files (`Notes/Inbox`),
  // null when unset. The folder need not exist yet; the first note creates it.
  // A value the server cannot use is a 422 with `code: 'invalid_folder'`.
  quick_notes_folder?: string | null;
}

export interface WebRoomOption {
  token: string;
  name: string;
  /** A bare `web` route lands here. */
  default: boolean;
  /** Somebody else is in it — the picker marks it. */
  shared: boolean;
  /** The user's machine-owned log or alerts room. */
  channel: boolean;
}

export interface TalkRoomOption {
  token: string;
  name: string;
  /** One of the two the bot provisioned — the picker marks it. */
  channel: boolean;
}

/**
 * How much of an external-origin turn the chat transcript shows.
 *
 * The type lives here with the payload shapes that carry it; the normalizer and
 * the value list live in `$lib/stores/externalTurns`, because a function on this
 * module is mocked away by every store test that replaces `$lib/api`.
 */
export type ExternalTurnDisplay = 'full' | 'collapsed' | 'hidden';

export type RelayDelivery = '' | 'room' | 'whatsapp' | 'sms';

export interface RelayDeliveryOption {
  value: RelayDelivery;
  available: boolean;
}

export async function getProfile(): Promise<{ profile: UserProfile | null }> {
  return apiFetch<{ profile: UserProfile | null }>('/settings/profile');
}

export async function updateProfile(
  patch: Partial<UserProfile>,
): Promise<{ ok: boolean; fields: string[] }> {
  return apiFetch('/settings/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

// --- Phase 7b: briefings ---

export interface UserBriefingRow {
  // Always 'db'; kept on the wire for one release so a cached client works.
  managed?: 'db';
  id: number;
  name: string;
  cron: string;
  // Display title for the rendered briefing (email subject, archive entry).
  // Blank means "derive from the name"; the run date is appended on render.
  title: string;
  conversation_token: string;
  // A delivery surface (talk / email / ntfy) or a comma/surface:channel
  // descriptor; the dropdown is driven by the server's `outputs` list.
  output: string;
  // The owner's own choice. A hold below keeps the briefing from running
  // whatever this says.
  enabled: boolean;
  // Set while an administrator holds the briefing; only an administrator
  // lifts it. `by_label` is a display name.
  held?: BriefingHold | null;
}

export interface BriefingHold {
  at: string;
  by_label: string;
}

// The same list the routing rows offer — `/settings/briefings` and
// `/settings/profile` read one `_user_talk_rooms` rather than two answers to
// "which Talk conversations" (ISSUE-475).
export type BriefingRoomOption = TalkRoomOption;

export async function getBriefings(): Promise<{
  briefings: UserBriefingRow[];
  rooms: BriefingRoomOption[];
  outputs: string[];
}> {
  return apiFetch('/settings/briefings');
}

export async function upsertBriefing(payload: {
  name: string;
  cron: string;
  title?: string;
  conversation_token?: string;
  output?: string;
  enabled?: boolean;
}): Promise<{ ok: boolean; id: number; state: 'created' | 'updated' | 'noop' }> {
  return apiFetch('/settings/briefings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefing(id: number): Promise<{ ok: boolean; deleted: boolean }> {
  return apiFetch(`/settings/briefings/${id}`, { method: 'DELETE' });
}

// --- Health (experimental module) ---

export interface HealthStat {
  id: number;
  measured_at: string;
  metric: string;
  value: number;
  unit: string;
  source: string;
  source_ref?: number | null;
  notes: string | null;
}

export interface HealthPanel {
  id: number;
  drawn_at: string;
  lab_name: string | null;
  panel_type: string | null;
  biomarker_count: number;
  flagged_count: number;
  draft: boolean;
  notes: string | null;
  has_source: boolean;
  encounter_id: number | null;
  specimen: Specimen | null;
}

/** What a lab panel was run on; `health.db.SPECIMENS` on the server. */
export const SPECIMENS = ['blood', 'urine', 'stool', 'saliva', 'other'] as const;
export type Specimen = (typeof SPECIMENS)[number];

export function specimenLabel(s: Specimen): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export interface Biomarker {
  id: number;
  panel_id: number;
  name: string;
  display_name: string | null;
  value: number;
  unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  flag: string | null;
}

export interface BiomarkerTrendPoint {
  drawn_at: string;
  value: number;
  unit: string;
  flag: string | null;
}

export interface BiomarkerTrend {
  name: string;
  display_name: string;
  points: BiomarkerTrendPoint[];
  unit_mismatch: boolean;
  ref_range_low: number | null;
  ref_range_high: number | null;
  unit: string | null;
}

export interface BiomarkerSummaryEntry {
  name: string;
  latest: { drawn_at: string; value: number; unit: string; flag: string | null };
  previous: { drawn_at: string; value: number; unit: string; flag: string | null } | null;
  direction: 'up' | 'down' | 'flat';
  sample_count: number;
}

export interface BiomarkerRef {
  name: string;
  display_name: string;
  category: string;
  default_unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  ref_range_low_m: number | null;
  ref_range_high_m: number | null;
  ref_range_low_f: number | null;
  ref_range_high_f: number | null;
  aliases: string[];
  description: string | null;
}

export interface DisplayUnits {
  weight: 'kg' | 'lb';
  height: 'cm' | 'ft_in';
  temp: 'C' | 'F';
}

export interface HealthSettings {
  dob: string | null;
  height_cm: number | null;
  sex: 'M' | 'F' | null;
  display_units: DisplayUnits;
}

export interface HealthDashboard {
  latest_stats: Record<string, HealthStat>;
  bmi: number | null;
  recent_panels: HealthPanel[];
  alerts: (Biomarker & { panel_id: number; drawn_at: string; lab_name: string | null })[];
  settings: HealthSettings;
  active_diagnoses_count?: number;
  recent_encounters?: Encounter[];
}

export interface Encounter {
  id: number;
  encounter_date: string;
  encounter_type: string;
  provider: string | null;
  facility: string | null;
  specialty: string | null;
  reason: string | null;
  notes: string | null;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface Diagnosis {
  id: number;
  name: string;
  icd10: string | null;
  status: 'active' | 'resolved' | 'chronic';
  date_diagnosed: string | null;
  date_resolved: string | null;
  /**
   * Deprecated. A condition is seen at several encounters — GP, specialist,
   * follow-up — so `encounter_ids` is the real answer. The server still emits
   * this legacy single id, but nothing here should read it.
   */
  encounter_id: number | null;
  /** Every encounter this condition has been seen at, newest first. */
  encounter_ids: number[];
  severity: 'mild' | 'moderate' | 'severe' | null;
  notes: string | null;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface HistorySummary {
  active_diagnoses: Diagnosis[];
  chronic_diagnoses: Diagnosis[];
  recent_encounters: Encounter[];
  recent_procedures: Encounter[];
  medications: Medication[];
}

async function healthFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${base}/api/health${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let body: { error?: string } = {};
    try {
      body = await resp.json();
    } catch {
      // ignore
    }
    throw new Error(body.error || `Health API error: ${resp.status}`);
  }
  return resp.json();
}

/** How often, and for how long, the page waits on a daemon extraction. */
const EXTRACT_POLL_MS = 1500;
const EXTRACT_DEADLINE_MS = 10 * 60 * 1000;

type ExtractState<T> =
  | { state: 'pending' }
  | { state: 'unknown' }
  | { state: 'answered'; result: T & { error?: string } };

/**
 * Start a document extraction and wait for the daemon's answer. The extract
 * routes only hand off (ISSUE-697): the daemon runs the model, and the answer
 * is read back from `/extract-requests/{id}`.
 */
async function runHealthExtraction<T>(path: string, init: RequestInit): Promise<T> {
  const { id } = await healthFetch<{ id: string }>(path, init);
  const deadline = Date.now() + EXTRACT_DEADLINE_MS;
  while (Date.now() < deadline) {
    const answer = await healthFetch<ExtractState<T>>(
      `/extract-requests/${encodeURIComponent(id)}`,
    );
    if (answer.state === 'answered') {
      if (answer.result.error) throw new Error(answer.result.error);
      return answer.result;
    }
    if (answer.state === 'unknown') throw new Error('The extraction request was lost.');
    await new Promise((resolve) => setTimeout(resolve, EXTRACT_POLL_MS));
  }
  throw new Error('The extraction is taking too long. Try again later.');
}

export async function listHealthStats(
  params: { metric?: string; since?: string; until?: string; limit?: number } = {},
): Promise<{ stats: HealthStat[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/stats${suffix}`);
}

export async function createHealthStat(body: {
  metric: string;
  value: number;
  unit: string;
  measured_at?: string;
  notes?: string;
}): Promise<{ status: string; id: number }> {
  return healthFetch('/stats', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteHealthStat(id: number): Promise<{ status: string }> {
  return healthFetch(`/stats/${id}`, { method: 'DELETE' });
}

export async function healthStatsLatest(): Promise<{ stats: Record<string, HealthStat> }> {
  return healthFetch('/stats/latest');
}

export async function healthStatsSeries(
  metric: string,
  params: { since?: string; until?: string } = {},
): Promise<{ metric: string; points: { measured_at: string; value: number; unit: string }[] }> {
  const q = new URLSearchParams({ metric });
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  return healthFetch(`/stats/series?${q.toString()}`);
}

export async function listHealthPanels(
  params: { since?: string; until?: string; include_drafts?: number; limit?: number } = {},
): Promise<{ panels: HealthPanel[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/panels${suffix}`);
}

export async function getHealthPanel(id: number): Promise<{
  panel: HealthPanel;
  biomarkers: Biomarker[];
  source: { available: boolean; mime: string | null };
}> {
  return healthFetch(`/panels/${id}`);
}

export async function createHealthPanel(body: {
  drawn_at: string;
  lab_name?: string;
  panel_type?: string;
  notes?: string;
  encounter_id?: number | null;
  specimen?: Specimen | null;
}): Promise<{
  status: string;
  id: number;
  collision?: { existing_id: number; drawn_at: string; lab_name: string | null };
}> {
  return healthFetch('/panels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function updateHealthPanel(
  id: number,
  body: Partial<{
    drawn_at: string;
    lab_name: string;
    panel_type: string;
    notes: string;
    draft: boolean;
    encounter_id: number | null;
    specimen: Specimen | null;
  }>,
): Promise<{ status: string }> {
  return healthFetch(`/panels/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteHealthPanel(id: number): Promise<{ status: string }> {
  return healthFetch(`/panels/${id}`, { method: 'DELETE' });
}

export async function saveHealthBiomarkers(
  panelId: number,
  biomarkers: Partial<Biomarker>[],
  confirm: boolean,
): Promise<{ status: string; count: number }> {
  return healthFetch(`/panels/${panelId}/biomarkers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ biomarkers, confirm }),
  });
}

export async function uploadHealthPanel(
  file: File,
  drawn_at: string,
  lab_name?: string,
  panel_type?: string,
  specimen?: Specimen | null,
): Promise<{
  status: string;
  id: number;
  collision?: { existing_id: number; drawn_at: string; lab_name: string | null };
}> {
  const form = new FormData();
  form.append('file', file);
  form.append('drawn_at', drawn_at);
  if (lab_name) form.append('lab_name', lab_name);
  if (panel_type) form.append('panel_type', panel_type);
  if (specimen) form.append('specimen', specimen);
  return healthFetch('/panels/upload', { method: 'POST', body: form });
}

export async function extractHealthPanel(panelId: number): Promise<{
  biomarkers: Partial<Biomarker>[];
  drawn_at: string | null;
  lab_name: string | null;
  panel_type: string | null;
  specimen: Specimen | null;
  warnings: string[];
  raw_text: string;
}> {
  return runHealthExtraction(`/panels/${panelId}/extract`, { method: 'POST' });
}

export function healthPanelSourceUrl(panelId: number): string {
  return `${base}/api/health/panels/${panelId}/source`;
}

export interface CsvImportSummary {
  status: string;
  panels_created: number;
  panels_skipped_identical: number;
  panels_needs_review: number;
  biomarkers_created: number;
  rows_processed: number;
  warnings: string[];
}

export async function importHealthCsv(file: File): Promise<CsvImportSummary> {
  const form = new FormData();
  form.append('file', file);
  return healthFetch('/csv/import', { method: 'POST', body: form });
}

export function healthCsvExportUrl(): string {
  return `${base}/api/health/csv/export`;
}

export async function healthBiomarkerTrend(
  name: string,
  params: { since?: string; until?: string } = {},
): Promise<BiomarkerTrend> {
  const q = new URLSearchParams({ name });
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  return healthFetch(`/biomarkers/trend?${q.toString()}`);
}

export async function healthBiomarkerSummary(): Promise<{ summary: BiomarkerSummaryEntry[] }> {
  return healthFetch('/biomarkers/summary');
}

export async function healthBiomarkerRefs(): Promise<{ refs: BiomarkerRef[] }> {
  return healthFetch('/biomarkers/refs');
}

export interface LabsMatrixMarker {
  name: string;
  display_name: string;
  unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  category: string;
}

export interface LabsMatrixCategory {
  name: string;
  markers: LabsMatrixMarker[];
}

export interface LabsMatrixPanel {
  id: number;
  drawn_at: string;
  lab_name: string | null;
  panel_type: string | null;
  specimen: Specimen | null;
}

export interface LabsMatrix {
  categories: LabsMatrixCategory[];
  panels: LabsMatrixPanel[];
  values: Record<string, Record<string, { value: number; unit: string; flag: string | null }>>;
}

export async function getLabsMatrix(specimen?: Specimen | null): Promise<LabsMatrix> {
  const q = specimen ? `?specimen=${encodeURIComponent(specimen)}` : '';
  return healthFetch(`/labs/matrix${q}`);
}

export interface BiomarkerExplainer {
  name: string;
  display_name: string;
  direction: 'high' | 'low';
  summary: string;
  causes: string[];
  mitigations: string[];
  disclaimer: string;
  source: 'cache' | 'generated' | 'fallback';
  generated_at: string | null;
}

export async function getBiomarkerExplainer(
  name: string,
  direction: 'high' | 'low',
): Promise<BiomarkerExplainer> {
  const q = new URLSearchParams({ direction });
  return healthFetch(`/biomarkers/${encodeURIComponent(name)}/explainer?${q.toString()}`);
}

export async function getHealthSettings(): Promise<{ settings: HealthSettings }> {
  return healthFetch('/settings');
}

export async function putHealthSettings(
  body: Partial<HealthSettings>,
): Promise<{ status: string; settings: HealthSettings }> {
  return healthFetch('/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getHealthDashboard(): Promise<HealthDashboard> {
  return healthFetch('/dashboard');
}

// ---- Encounters / diagnoses / history ------------------------------------

export async function listEncounters(
  params: { since?: string; until?: string; type?: string; limit?: number; offset?: number } = {},
): Promise<{ encounters: Encounter[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/encounters${suffix}`);
}

export async function createEncounter(
  body: Partial<Encounter> & { encounter_date: string; encounter_type: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/encounters', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getEncounter(id: number): Promise<{
  encounter: Encounter;
  diagnoses: Diagnosis[];
  panels: HealthPanel[];
  documents: HealthDocument[];
  medications: Medication[];
}> {
  return healthFetch(`/encounters/${id}`);
}

export async function updateEncounter(
  id: number,
  body: Partial<Encounter>,
): Promise<{ status: string }> {
  return healthFetch(`/encounters/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteEncounter(id: number): Promise<{ status: string }> {
  return healthFetch(`/encounters/${id}`, { method: 'DELETE' });
}

export interface ParsedDiagnosis {
  name: string;
  icd10: string | null;
  status: 'active' | 'resolved' | 'chronic';
  severity: 'mild' | 'moderate' | 'severe' | null;
}

export interface ParsedEncounter {
  encounter_date: string | null;
  encounter_type: string;
  provider: string | null;
  facility: string | null;
  specialty: string | null;
  reason: string | null;
  notes: string | null;
  diagnoses: ParsedDiagnosis[];
  confidence: 'high' | 'medium' | 'low' | 'manual';
}

export async function extractEncounters(file: File): Promise<{
  rows: ParsedEncounter[];
  mode: 'text' | 'vision';
  warnings: string[];
  /** The kept upload. Null when storing it failed — see `warnings`. */
  document_id: number | null;
}> {
  const form = new FormData();
  form.append('file', file);
  return runHealthExtraction('/encounters/extract', { method: 'POST', body: form });
}

export async function bulkInsertEncounters(
  rows: ParsedEncounter[],
  documentId?: number | null,
): Promise<{
  status: string;
  ids: number[];
  count: number;
  diagnosis_ids: number[];
  document_id: number | null;
}> {
  return healthFetch('/encounters/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, document_id: documentId ?? null }),
  });
}

export async function listDiagnoses(
  params: { status?: string; limit?: number; offset?: number } = {},
): Promise<{ diagnoses: Diagnosis[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/diagnoses${suffix}`);
}

export async function createDiagnosis(
  body: Partial<Diagnosis> & { name: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/diagnoses', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function linkDiagnosisEncounter(
  diagnosisId: number,
  encounterId: number,
): Promise<{ status: string; created: boolean }> {
  return healthFetch(`/diagnoses/${diagnosisId}/encounters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ encounter_id: encounterId }),
  });
}

export async function unlinkDiagnosisEncounter(
  diagnosisId: number,
  encounterId: number,
): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${diagnosisId}/encounters/${encounterId}`, {
    method: 'DELETE',
  });
}

export async function getDiagnosis(
  id: number,
): Promise<{ diagnosis: Diagnosis; encounters: Encounter[]; documents: HealthDocument[] }> {
  return healthFetch(`/diagnoses/${id}`);
}

export async function updateDiagnosis(
  id: number,
  body: Partial<Diagnosis>,
): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteDiagnosis(id: number): Promise<{ status: string }> {
  return healthFetch(`/diagnoses/${id}`, { method: 'DELETE' });
}

export async function getHistorySummary(): Promise<HistorySummary> {
  return healthFetch('/history/summary');
}

// ---- Immunizations -------------------------------------------------------

export interface Immunization {
  id: number;
  name: string;
  product_name: string | null;
  date_given: string;
  manufacturer: string | null;
  dose_label: string | null;
  lot_number: string | null;
  route: string | null;
  site: string | null;
  administered_by: string | null;
  facility: string | null;
  encounter_id: number | null;
  cvx_code: string | null;
  notes: string | null;
  source: string;
  created_at?: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

export interface ImmunizationRef {
  name: string;
  display_name: string;
  category: 'routine' | 'booster' | 'risk_based' | 'travel';
  schedule: string;
  interval_days: number | null;
  primary_series_doses: number | null;
  aliases: string[];
  description: string | null;
  typical_age_range: string | null;
}

export type ImmunizationStatus =
  | 'up_to_date'
  | 'due_soon'
  | 'overdue'
  | 'series_incomplete'
  | 'never_recorded'
  | 'expired'
  | 'risk_based'
  | 'recorded'; // "Other" bucket

export interface CoverageEntry {
  name: string;
  display_name: string;
  category: string;
  status: ImmunizationStatus;
  last_given: string | null;
  dose_count: number;
  next_due: string | null;
  is_overdue: boolean;
  days_until_due: number | null;
}

export interface ParsedImmunization {
  name: string;
  product_name: string | null;
  date_given: string | null;
  source_line: string;
  confidence: 'high' | 'medium' | 'low' | 'manual';
  notes: string | null;
}

export interface ImmunizationExplainer {
  name: string;
  display_name: string;
  status: ImmunizationStatus;
  summary: string;
  why_it_matters: string[];
  disclaimer: string;
  source: 'static' | 'fallback';
  generated_at: string | null;
}

export async function listImmunizations(
  params: { name?: string; since?: string; until?: string; limit?: number; offset?: number } = {},
): Promise<{ immunizations: Immunization[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/immunizations${suffix}`);
}

export async function createImmunization(
  body: Partial<Immunization> & { name: string; date_given: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/immunizations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getImmunization(id: number): Promise<{
  immunization: Immunization;
  encounter: Encounter | null;
  documents: HealthDocument[];
}> {
  return healthFetch(`/immunizations/${id}`);
}

export async function updateImmunization(
  id: number,
  body: Partial<Immunization>,
): Promise<{ status: string }> {
  return healthFetch(`/immunizations/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function deleteImmunization(id: number): Promise<{ status: string }> {
  return healthFetch(`/immunizations/${id}`, { method: 'DELETE' });
}

export async function listImmunizationRefs(): Promise<{ refs: ImmunizationRef[] }> {
  return healthFetch('/immunizations/refs');
}

export async function getImmunizationCoverage(): Promise<{
  coverage: CoverageEntry[];
  other: CoverageEntry[];
}> {
  return healthFetch('/immunizations/coverage');
}

export async function parseImmunizations(text: string): Promise<{ rows: ParsedImmunization[] }> {
  return healthFetch('/immunizations/parse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
}

export async function extractImmunizations(file: File): Promise<{
  rows: ParsedImmunization[];
  mode: 'text' | 'vision';
  warnings: string[];
  /** The kept upload. Null when storing it failed — see `warnings`. */
  document_id: number | null;
}> {
  const form = new FormData();
  form.append('file', file);
  return runHealthExtraction('/immunizations/extract', { method: 'POST', body: form });
}

export async function bulkInsertImmunizations(
  rows: ParsedImmunization[],
  documentId?: number | null,
): Promise<{ status: string; ids: number[]; count: number; document_id: number | null }> {
  return healthFetch('/immunizations/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, document_id: documentId ?? null }),
  });
}

export async function getImmunizationExplainer(name: string): Promise<ImmunizationExplainer> {
  return healthFetch(`/immunizations/${encodeURIComponent(name)}/explainer`);
}

// ---- Medications ---------------------------------------------------------

export type MedicationCategory = 'prescription' | 'otc' | 'supplement';
export type MedicationStatus = 'active' | 'stopped' | 'all';

/** `medication_to_dict` in `src/istota/health/serialize.py`, in its key order. */
export interface Medication {
  id: number;
  name: string;
  generic_name: string | null;
  dose: string | null;
  dose_unit: string | null;
  frequency: string | null;
  route: string | null;
  category: MedicationCategory;
  prescriber: string | null;
  pharmacy: string | null;
  reason: string | null;
  start_date: string | null;
  end_date: string | null;
  active: boolean;
  encounter_id: number | null;
  notes: string | null;
  source: string;
  created_at: string;
  /** Attached-document count; present on list responses. */
  document_count?: number;
}

/** The writable fields. `null` clears a nullable one; an absent key leaves it alone. */
export type MedicationFields = Partial<
  Omit<Medication, 'id' | 'source' | 'created_at' | 'document_count'>
>;

export async function listMedications(
  params: {
    status?: MedicationStatus;
    category?: MedicationCategory;
    name?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<{ medications: Medication[] }> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v));
  }
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return healthFetch(`/medications${suffix}`);
}

export async function createMedication(
  body: MedicationFields & { name: string },
): Promise<{ status: string; id: number }> {
  return healthFetch('/medications', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function getMedication(id: number): Promise<{
  medication: Medication;
  encounter: Encounter | null;
  documents: HealthDocument[];
}> {
  return healthFetch(`/medications/${id}`);
}

export async function updateMedication(
  id: number,
  body: MedicationFields,
): Promise<{ status: string }> {
  return healthFetch(`/medications/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Without `endDate` the server uses today in the user's timezone. */
export async function discontinueMedication(
  id: number,
  endDate?: string,
): Promise<{ status: string; end_date: string }> {
  return healthFetch(`/medications/${id}/discontinue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(endDate ? { end_date: endDate } : {}),
  });
}

export async function deleteMedication(id: number): Promise<{ status: string }> {
  return healthFetch(`/medications/${id}`, { method: 'DELETE' });
}

// ---- Documents -----------------------------------------------------------

export type DocumentEntity = 'encounter' | 'diagnosis' | 'immunization' | 'medication';

export interface HealthDocument {
  id: number;
  filename: string;
  original_filename: string | null;
  mime: string;
  byte_size: number;
  source: 'manual' | 'import' | 'agent';
  notes: string | null;
  created_at: string;
  /** Auth-gated stream route. Never a filesystem path. */
  url: string;
  /**
   * What this document is attached to. Present on every `documents: [...]`
   * listing, absent on the `POST /documents` upload acknowledgement — that
   * one reports what the create did (`created`, `linked`) rather than
   * describing the row, and every caller re-lists afterwards anyway.
   */
  links?: DocumentLink[];
}

export interface DocumentLink {
  entity_type: DocumentEntity;
  entity_id: number;
  label: string;
}

export interface EntityRef {
  type: DocumentEntity;
  id: number;
}

export async function listDocuments(
  entity?: EntityRef,
  page?: { limit?: number; offset?: number },
): Promise<{ documents: HealthDocument[] }> {
  const q = new URLSearchParams();
  if (entity) {
    q.set('entity_type', entity.type);
    q.set('entity_id', String(entity.id));
  }
  // Only meaningful on the unfiltered branch — the entity-scoped one returns
  // every document on that record and takes no bound. The server defaults to
  // 200 and caps at 1000, so a caller wanting all of them has to page.
  if (page?.limit !== undefined) q.set('limit', String(page.limit));
  if (page?.offset !== undefined) q.set('offset', String(page.offset));
  const qs = q.toString();
  return healthFetch(`/documents${qs ? `?${qs}` : ''}`);
}

export async function getDocument(
  id: number,
): Promise<{ document: HealthDocument; links: DocumentLink[] }> {
  return healthFetch(`/documents/${id}`);
}

export async function uploadDocument(
  file: File,
  entity?: EntityRef,
  notes?: string,
): Promise<HealthDocument & { status: string; created: boolean; linked: boolean }> {
  const form = new FormData();
  form.append('file', file);
  if (entity) {
    form.append('entity_type', entity.type);
    form.append('entity_id', String(entity.id));
  }
  if (notes) form.append('notes', notes);
  return healthFetch('/documents', { method: 'POST', body: form });
}

export async function linkDocument(
  id: number,
  entity: EntityRef,
): Promise<{ status: string; created: boolean }> {
  return healthFetch(`/documents/${id}/links`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_type: entity.type, entity_id: entity.id }),
  });
}

export async function unlinkDocument(
  id: number,
  entity: EntityRef,
): Promise<{ status: string; removed: boolean }> {
  return healthFetch(`/documents/${id}/links/${entity.type}/${entity.id}`, {
    method: 'DELETE',
  });
}

export async function deleteDocument(id: number): Promise<{ status: string }> {
  return healthFetch(`/documents/${id}`, { method: 'DELETE' });
}

// ---- Garmin --------------------------------------------------------------

export interface GarminStatus {
  connected: boolean;
  email: string | null;
  last_sync: string | null;
  error: string | null;
}

export interface GarminConnectResponse {
  status: 'ok' | 'mfa_required' | 'error';
  prompt?: string;
  error?: string;
}

export interface GarminSyncResponse {
  inserted: number;
  skipped: number;
  errored: number;
  days_processed: number;
  errors: string[];
  auth_error: boolean;
}

// Garmin auth is a cross-module connected service: its routes live at
// /api/garmin/*, not under /api/health. (Daily-summary sync stays health-
// owned — see syncGarmin below.)
async function garminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${base}/api/garmin${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let body: { error?: string } = {};
    try {
      body = await resp.json();
    } catch {
      // ignore
    }
    throw new Error(body.error || `Garmin API error: ${resp.status}`);
  }
  return resp.json();
}

export async function getGarminStatus(): Promise<GarminStatus> {
  return garminFetch('/status');
}

export async function connectGarmin(
  email: string,
  password: string,
): Promise<GarminConnectResponse> {
  return garminFetch('/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
}

export async function submitGarminMfa(code: string): Promise<GarminConnectResponse> {
  return garminFetch('/mfa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

export async function disconnectGarmin(): Promise<{ status: string }> {
  return garminFetch('/disconnect', { method: 'POST' });
}

export interface GarminImportResponse {
  dry_run: boolean;
  inserted: number;
  activities: number;
  details: Array<{
    activity_id: number;
    type: string;
    start: string;
    distance_m: number | null;
    fetched: number;
    inserted: number;
    shadowed: number;
  }>;
}

export async function importGarminTracks(days_back = 7): Promise<GarminImportResponse> {
  return garminFetch('/import-tracks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days_back }),
  });
}

export async function syncGarmin(days_back = 7): Promise<GarminSyncResponse> {
  return healthFetch('/garmin/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days_back }),
  });
}

// ---- Web chat ----

export type RoomNotifications = 'default' | 'alerts' | 'badge' | 'off';

export interface ChatRoom {
  id: number;
  token: string;
  name: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  /** Set at creation and never changed: no memory in, nothing into long-term
   * memory out, one member, web only. */
  incognito?: boolean;
  /** Surface the room was created on. Talk-origin rooms surface here
   * automatically once the bot is messaged in them. */
  origin?: 'web' | 'talk' | 'sms' | 'whatsapp' | 'email';
  /** The bound Talk conversation, or null when the room is web-only. Sent on
   * every room the listing returns, not only on a fresh promote response — the
   * room-list refresh writes this key unconditionally, so a listing that
   * omitted it erased the promote's own answer on the next poll. */
  talk_token?: string | null;
  /** Unread bot/system messages on the web surface (server-computed; excludes
   * the user's own turns). Absent on older backends → treat as 0. */
  unread_count?: number;
  /** This user's starred transcript messages across all pages. */
  starred_count?: number;
  /** Sidebar tint: a `ROOM_COLORS` name, or null for none. Per-*user*, unlike
   * the model/effort/brain trio below — it lives on this user's room handle,
   * so the two members of a shared Talk room can tint it differently. */
  color?: string | null;
  /** Personal notification override. Absent follows the account default. */
  push_notifications?: RoomNotifications;
  /** Standing per-room model default (canonical model id), shared across Talk
   * and web. null / absent → inherit the instance default. */
  model?: string | null;
  /** Standing per-room effort level (low/medium/high/xhigh/max). */
  effort?: string | null;
  /** Standing per-room brain kind, shared across Talk and web. null / absent →
   * inherit the deployment's own routing. Writing it is admin-only, and the
   * kinds on offer are an operator allowlist (`selectable_brains` below), so a
   * client that has none renders no control rather than an empty dropdown. */
  brain?: string | null;
  /** ISO-UTC stamp of the room's newest message, falling back to the room's
   * creation time when nobody has spoken in it. The sidebar's sort key — it is
   * normalized the same way a message row's `created_at` is, so an arriving
   * row's stamp can be written straight onto the room. Absent on older
   * backends → the room keeps whatever position the server gave it. */
  last_activity?: string;
  /** The phone surface this room is bound to, a WhatsApp group included, or
   * `email` for the user's private email room; null for every other room, an
   * email thread room included. What the sidebar badge and the settings line
   * read. */
  phone_surface?: 'sms' | 'whatsapp' | 'email' | null;
  /** The room is bound to SMS or WhatsApp, a WhatsApp group included, or is
   * the user's private email room or an email thread: web reads it and the
   * server refuses a send into it, so no composer. True for every member, since adding a reader does
   * not make the thread writable. */
  read_only?: boolean;
  /** The phone binding is a WhatsApp group, not the creator's private thread.
   * Its parked questions are answered from web and it takes members; only the
   * composer is gone (ISSUE-585). */
  phone_group?: boolean;
  /** The room is an email thread: a view of the mail, read-only here with
   * `read_only` set. The reply is drafted from the user's private chat. */
  email_thread?: boolean;
  /** The viewer chose to show this email thread in the main room list. An
   * email thread without it sits in the collapsed "Email threads" group
   * (`isHiddenRoom`). Per user, like `color`; false for every other room. */
  listed?: boolean;
  /** More than one human reads this room, a Talk guest included. */
  shared?: boolean;
  /** The caller keeps private notes about this room ("My notes"). With
   * `shared`, what makes the room menu offer Room notes and My notes. */
  has_my_notes?: boolean;
  /** The policy of a shared room; null for a room one human reads. */
  policy?: RoomPolicyView | null;
  /** Set while the room is switched off (multiplayer D8): nothing in it is
   * recorded or answered. null while it is on. */
  off?: RoomOffView | null;
  /** The group whose current members are this room's members, or null for a
   * room with its own roster. Membership is managed in Groups. */
  managed_by?: RoomManagedBy | null;
}

/** A group-owned room's group, as the Chat panes show it. `is_owner` is
 * display data; the server decides every write. */
export interface RoomManagedBy {
  group_id: string;
  display_name: string;
  /** The Groups page for this group, already under the app's base. */
  manage_url: string;
  is_owner: boolean;
}

export interface RoomOffView {
  at: string;
  /** Everyone who switched it off, in order; empty when the bot was removed
   * from a WhatsApp group, which does not say by whom. `agreed` is a vetoer
   * who has since asked for it back on. */
  by: { name: string; guest: boolean; agreed: boolean }[];
  /** The server's own sentence saying how to switch it back on. */
  way_back: string;
}

/** How guests' turns are answered (multiplayer D11). */
export type GuestReply = 'off' | 'held' | 'direct';

/** When the bot answers an unaddressed turn in a shared room (ISSUE-640). */
export type SpeechMode = 'mention' | 'classifier' | 'off';

/** How wide the classifier reads "for the bot" in a room (ISSUE-654). */
export type SpeechDisposition = 'reserved' | 'friendly';

export interface RoomPolicyView {
  /** The host's user id, or null once the room has lost its host. */
  host: string | null;
  is_host: boolean;
  guest_reply: GuestReply;
  /** The room's own speech mode, or null when it follows the deployment. */
  speech_mode?: SpeechMode | null;
  /** What the room is on now, by its own mode or the deployment's. */
  effective_speech_mode?: string;
  /** The deployment's `[speech_gate] mode`, which "Follow deployment" means. */
  deployment_speech_mode?: string;
  /** The room's own disposition, or null when it follows the deployment. */
  disposition?: SpeechDisposition | null;
  /** What the classifier uses here now, by the room's value or the deployment's. */
  effective_disposition?: string;
  /** The deployment's `[speech_gate] disposition`. */
  deployment_disposition?: string;
  /** An email thread room: no guest mode, so the guest reply setting is
   * not shown. */
  email_thread?: boolean;
  /** Why the caller may not change the room-wide settings, or null when they
   * may. The server's own refusal text, so it is shown verbatim. */
  settings_refusal: string | null;
}

export interface ChatConfig {
  max_prompt_chars: number;
  max_attachment_mb: number;
  attachment_extensions: string[];
  client_poll_interval_ms: number;
  /**
   * The authenticated caller's username, which the send queue's `localStorage`
   * key is built from — a shared Talk room has one token across every member,
   * so a bare token would hand one person's queued message to another on a
   * browser profile two people take turns using. Published here rather than
   * read from `getMe()` because the chat store awaits this config before
   * anything else, and `getMe()` resolves after the restore needs it. Absent
   * on an older backend, in which case the queue is in memory only.
   */
  user_id?: string;
  /** How an external-origin turn's body renders. */
  external_turn_display?: ExternalTurnDisplay;
  /** The caller's own raw setting, where `''` means "follow the operator". */
  outbound_approval?: string;
  /**
   * False when the stored value is not one the server recognizes — a
   * hand-edited row. The server resolves such a value to the floor, so a pane
   * must show it as unrecognized rather than as a live selection.
   */
  outbound_approval_valid?: boolean;
  /** What `outbound_approval` resolves to once the operator floor applies. */
  outbound_approval_effective?: string;
  /** The floor the user may tighten past but not loosen below. */
  outbound_approval_floor?: string;
}

/**
 * The message a turn replies to, resolved server-side.
 *
 * Two shapes in one type, discriminated by `deleted`: a live parent carries its
 * role and a display excerpt, a hard-deleted one carries only the id it named.
 * The dead form is kept rather than dropped — the citation records that the
 * turn had a referent, and erasing it would rewrite the conversation.
 */
export interface ReplyCitation {
  msg_id: number;
  role?: 'user' | 'assistant' | 'system';
  excerpt?: string;
  // Who wrote a `user` parent, when that is not the viewer (#764).
  author?: string;
  deleted: boolean;
}

export interface ChatHistoryMessage {
  role: 'user' | 'assistant' | 'system';
  text: string;
  // Task-backed turns carry task_id; bot-delivered notifications carry notif_id.
  task_id?: number;
  notif_id?: number;
  status?: string;
  // A user turn the bot may still decline (#675): its task is not followed,
  // and an answer arrives as its own stored row.
  declinable?: boolean;
  confirmation?: boolean;
  created_at: string;
  // Finished task-backed turns carry their tool-use descriptions (in order)
  // and wall-clock duration so the action strip + timing persist across
  // reloads (ISSUE-122).
  tools?: string[];
  // Ordered, interleaved segment list (`text` / `tool`) for a finished turn,
  // derived server-side from the execution trace, so history reconstructs the
  // same interleaved layout as the live stream. `tools` is kept as a fallback.
  // A `gate` is the question a task parked on, with how it was answered
  // (ISSUE-592); `outcome` is absent while it is still open.
  segments?: {
    kind: 'text' | 'tool' | 'gate';
    text: string;
    outcome?: 'approved' | 'declined';
  }[];
  duration_seconds?: number | null;
  // The model that produced this answer (canonical ID), null when unknown.
  model?: string | null;
  // Durable (messages-store-backed) rows carry the message's stable id and the
  // requesting user's star flag; aux (tasks-only) turns carry neither and are
  // not starrable.
  msg_id?: number;
  starred?: boolean;
  // Aggregate-view rows (GET /chat/messages) additionally carry their room.
  room_token?: string;
  room_name?: string;
  // Display names of files a user turn carried, so the attachment chips
  // survive a transcript rebuild (the composer's in-memory names don't).
  attachments?: string[];
  // Positional against `attachments`: the workspace path to open each chip at,
  // or null for one this user can't be served (see `chatFileUrl`).
  attachment_paths?: (string | null)[];
  // Present only on a turn that cites a parent.
  reply_to?: ReplyCitation;
  // Who wrote a user row, when it was not the reader — today, the sender of an
  // email mirrored into the room it continues. Absent means the viewer, which
  // is what every user row was assumed to be.
  author?: string;
  // The writer's istota user id, which is what the avatar endpoint keys on —
  // `author` is a display label and two people may share one. Set only where
  // the writer is an istota user who is not the reader, so an external
  // sender's turn carries a name and no id, and nothing here is requested for
  // it. The picture's content hash deliberately does not ride along: this row
  // is on the byte-budgeted room-event stream, and the client pays one
  // conditional request per author per session instead (D13).
  author_id?: string;
  // The surface a user row entered from, when it is not one the room itself
  // lives on — today `'email'` alone. Absent is the signal for "from inside
  // this conversation", so a co-member's Talk or web turn carries no key.
  origin?: string;
  // The email's subject line, lifted out of the wrapper the display body
  // strips. What a collapsed external turn shows in place of the body.
  subject?: string;
  // The phone surface a user turn was texted in from. The turn is the
  // writer's own, so this is a provenance mark and not the external treatment.
  via?: string;
  // False on a row in a shared room that is not the viewer's to delete — the
  // delete endpoint's own owner rule, asked ahead of time. Absent means the
  // endpoint would accept it.
  deletable?: boolean;
  // A private reply about a shared room (ISSUE-608), in the viewer's own
  // private room. `name` is null when the viewer is no longer in that room,
  // and the chip then names nothing and opens nothing.
  about_room?: { token: string; name: string | null };
  // On an assistant row, the mail this answer sent into an email thread room
  // (ISSUE-612); `body` is present only when the mailed text differs from
  // `text`. On an email note (a system row), the thread row's mail, read
  // live, with `body` always present.
  mail?: OutgoingMail;
  // User rows in a mail room (an email thread room or the private email
  // room): the mail as the incoming-mail card renders it. On an email note
  // that stored its parts, the thread's incoming mail, for the same card.
  received_mail?: ReceivedMail;
  // An email note's parts (ISSUE-644): the code-built header and outcome line
  // and the bot's own remark. Absent on a note from before them, which then
  // renders from `text`.
  email_note?: EmailNoteParts;
  // Assistant rows: the location shares this answer carries a map of, in the
  // order they were attached. Each is the projected observation the answer's
  // text already discloses, plus its map link.
  location_shares?: LocationShare[];
}

/** One location share card as `_location_shares_field` serializes it. */
export interface LocationShare {
  card_id: number;
  owner_user_id: string;
  owner_label: string;
  precision: 'exact' | 'area';
  lat: number;
  lon: number;
  accuracy_m: number | null;
  observed_at: string;
  freshness: string;
  map_url: string;
}

export interface EmailNoteParts {
  header: string;
  outcome: string;
  remark: string;
}

export type OutgoingMailState = 'sent' | 'held' | 'failed' | 'discarded';

export interface OutgoingMail {
  to: string[];
  cc: string[];
  subject?: string;
  state: OutgoingMailState;
  body?: string;
  // The body is markdown converted from the HTML part the mail went out with,
  // which the recipient's client shows rendered (#831). Absent otherwise.
  markdown?: boolean;
  // In a mail room: how to name an address other than by itself ("you" for
  // the viewer's own, the bot's name for the bot's), keyed lowercased.
  labels?: Record<string, string>;
  // In a mail room: the viewer's private room at this turn's task, where the
  // mail is discussed. Stage 3 of hidden email threads links the room only; a
  // held mail is approved from its notification until the note carries it.
  note_path?: string;
  // An email thread's card with no note: "Discuss in private chat" opens
  // `room` with the composer linked to `about` (section 0c).
  discuss?: MailDiscuss;
}

/** The private room to discuss a thread's mail in, and the thread itself. */
export interface MailDiscuss {
  room: string;
  about: string;
}

export interface MailAddress {
  name: string;
  address: string;
}

export interface MailAttachment {
  filename: string;
  size?: number;
  // A workspace path `chatFileUrl` serves, present only for a copy in the
  // viewer's own workspace.
  path?: string;
}

/** A mail that came into a mail room, from stored metadata only. */
export interface ReceivedMail {
  from: MailAddress;
  to: MailAddress[];
  cc: MailAddress[];
  date: string;
  subject: string;
  attachments: MailAttachment[];
  // The text the sender wrote now, and the rest (signature, quoted history).
  new_text: string;
  rest: string;
  labels: Record<string, string>;
  note_path?: string;
  discuss?: MailDiscuss;
  // A row from before the metadata was stored: From, Subject and Date from
  // the prompt wrapper alone, and none of the fields below.
  fallback?: boolean;
  message_id?: string;
  in_reply_to?: string;
  sender_check?: 'verified' | 'failed' | 'none';
  trusted?: boolean;
}

/** Cross-room aggregate views (sidebar All / Unread / Starred). */
export type ChatView = 'all' | 'unread' | 'starred';

export interface ChatHistory {
  messages: ChatHistoryMessage[];
  // Oldest in-flight task (back-compat). Prefer active_tasks.
  active_task: { id: number; status: string } | null;
  // All in-flight tasks for the room, oldest-first. The room runs them one at
  // a time; the client resumes the first and queues the rest in this order.
  active_tasks?: { id: number; status: string }[];
  // Older history exists below this page (ISSUE-131). Absent on a pre-paging
  // backend, so the client treats `undefined` as "no more".
  has_more?: boolean;
  truncated?: boolean;
  // Pass back as before_ts/before_id to fetch the next older page. `ts` is the
  // RAW stored created_at (`YYYY-MM-DD HH:MM:SS`), never the display value —
  // the keyset breaks if it's round-tripped through a normalized timestamp.
  oldest_cursor?: { ts: string; id: number } | null;
}

/**
 * Why a send didn't land.
 *
 * Kept separable rather than flattened into one message string because the
 * distinction outlives the sentence: an offline outbox (ISSUE-202) holds an
 * `unreachable` send for later and fails a `rejected` one outright, and `auth`
 * is the one failure a retry can never resolve.
 */
export type SendFailure =
  | 'unreachable'
  | 'timeout'
  | 'auth'
  | 'rate_limit'
  | 'rejected'
  // The cited parent is gone or was never in this room. Its own member because
  // the recovery is unlike every other failure's: Retry cannot work (it would
  // re-POST the same dead id), so the text goes back to the composer instead.
  | 'reply_target_gone';

/** Trailing options on a send. New fields belong here, not as positionals. */
export interface SendOptions {
  /** Canonical `messages.id` this message replies to. */
  replyToMsgId?: number;
  /**
   * An email thread room this message is about, with no row to reply to
   * ("Discuss in private chat" with no note). The server checks it and
   * refuses with 400; a `replyToMsgId` outranks it, so it is not sent beside one.
   */
  aboutRoom?: string;
}

export interface SendResult {
  ok: boolean;
  status: number;
  // Set only when `ok` is false.
  failure?: SendFailure;
  retry_after?: number;
  task_id?: number | null;
  inline_result?: string;
  // Structured payload from an inline !command (e.g. !search result cards);
  // null/absent for plain-text commands. Rendered as a dedicated component.
  command_data?: Record<string, unknown> | null;
  stream_url?: string;
  error?: string;
  // The speech gate stored the turn and nobody is answering it: no task, no
  // stream. `message_id` is the stored row's canonical id.
  recorded?: boolean;
  message_id?: number;
}

export interface TaskEventDTO {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

/**
 * The server's chat limits.
 *
 * `timeoutMs` is for the connectivity probe (`stores/connectivity.ts`), which
 * uses this call as its "is the server there" question and cannot afford to
 * wait out a stall — a probe with no bound would leave the app believing it is
 * offline for as long as the OS takes to give up on the socket.
 */
export function getChatConfig(timeoutMs = 0): Promise<ChatConfig> {
  return apiFetch<ChatConfig>('/chat/config', undefined, timeoutMs);
}

/**
 * `getChatConfig`, shared across callers.
 *
 * The limits in here are the server's, and they are what the composer checks a
 * file against before spending an upload on it. Several places want them and
 * none of them can change them, so one request per page load is enough.
 *
 * A failure is not cached: the config is best-effort, and a client that gave up
 * permanently on one bad response would keep enforcing nothing (or, worse, a
 * stale guess) for the life of the page.
 */
let chatConfigInFlight: Promise<ChatConfig> | null = null;

export function chatConfigOnce(): Promise<ChatConfig> {
  chatConfigInFlight ??= getChatConfig().catch((e) => {
    chatConfigInFlight = null;
    throw e;
  });
  return chatConfigInFlight;
}

/** Test seam: drop the cached config so the next call fetches again. */
export function resetChatConfigCache(): void {
  chatConfigInFlight = null;
}

export function getBasemap(): Promise<BasemapSpec> {
  return apiFetch<BasemapSpec>('/map/basemap');
}

/**
 * `getBasemap`, shared across callers and across maps.
 *
 * Every map surface asks the same question and none of them can change the
 * answer, so one request per page load covers the location page, the history
 * page and anything that mounts a second map beside them.
 *
 * A failure is not cached, for the same reason `chatConfigOnce` does not cache
 * one: the caller falls back to the keyless default and a later navigation
 * should get another chance at the real answer.
 */
let basemapInFlight: Promise<BasemapSpec> | null = null;

export function basemapOnce(): Promise<BasemapSpec> {
  basemapInFlight ??= getBasemap().catch((e) => {
    basemapInFlight = null;
    throw e;
  });
  return basemapInFlight;
}

/** Test seam: drop the cached basemap so the next call fetches again. */
export function resetBasemapCache(): void {
  basemapInFlight = null;
}

export interface ChatCommand {
  name: string;
  help: string;
}

export interface ChatModelAlias {
  alias: string;
  target: string | null;
  effort: string | null;
  /** The effort levels the alias's model takes, lowest first; `[]` for a
   *  model without reasoning, null (or absent, from an older server) when
   *  that is not known. */
  efforts?: string[] | null;
}

/** One model the room picker may offer this viewer, from the catalogue. */
export interface ChatModel {
  /** `provider/id`: what a pick stores. */
  ref: string;
  name: string;
  provider: string;
  family: string | null;
  reasoning: boolean;
  /** The effort levels it takes, lowest first; empty without reasoning. */
  efforts: string[];
  context_window: number;
  input: string[];
  /** USD per million tokens, or null where no price is known. */
  price: { input: number; output: number } | null;
  /** In `[models] enabled`. Only an admin is sent models where this is false. */
  enabled: boolean;
}

/** What the picker makes of a room's stored model, for this viewer. */
export interface ChatRoomModel {
  value: string;
  /** `set_by_admin`: outside the viewer's set, and still runs for every
   *  member. `unavailable`: does not resolve on the room's brain. */
  status: 'listed' | 'set_by_admin' | 'unavailable';
  ref: string | null;
  efforts: string[] | null;
}

/** A hidden command alias: `dispatch` resolves `alias` to `target` before it
 *  looks `target` up. Deliberately absent from `commands`, which is what feeds
 *  `!help` and the composer autocomplete. Optional so a client built against
 *  an older server still type-checks. */
export interface ChatCommandAlias {
  alias: string;
  target: string;
}

/** A brain kind, as the room settings modal shows it. `model_namespace` is
 *  what the modal compares to decide whether a pending change will clear the
 *  room's model pin. The server publishes all three brain fields to admins
 *  only, so an empty `selectable_brains` means "no control" whether that is
 *  because the operator listed no kinds or because this user may not write
 *  one. Optional so a client built against an older server still type-checks. */
export interface SelectableBrain {
  kind: string;
  label: string;
  model_namespace: string;
}

/** A provider a native room may run on, for an admin's provider select
 *  (model-catalogue D22). `default` marks native's default provider, which a
 *  room with no provider of its own runs on. */
export interface ChatProvider {
  id: string;
  kind: string;
  default: boolean;
  /** What a room on this provider runs with no model of its own. */
  default_model: string | null;
}

export interface ChatCommands {
  commands: ChatCommand[];
  command_aliases?: ChatCommandAlias[];
  model_aliases: ChatModelAlias[];
  /** The models this viewer may pick on the scoped brain: the whole catalogue
   *  for an admin, the enabled set otherwise. Grouped by provider, enabled
   *  first. Optional so a client built against an older server type-checks. */
  models?: ChatModel[];
  /** Sent with `room_id` when the room stores a model. */
  room_model?: ChatRoomModel;
  /** The canonical id an unpinned room runs, resolved through its brain.
   *  `null` when the brain names none and the backend picks its own. */
  default_model?: string | null;
  /** What a room may be pinned to. */
  selectable_brains?: SelectableBrain[];
  /** Every *known* kind's model namespace, not only the offered ones — the
   *  brain a change moves *away from* need not be on the menu. It is the
   *  inherited one when the room pins nothing, and it can be a kind the
   *  operator has since dropped from the allowlist.
   *
   *  Known rather than buildable (ISSUE-417): a namespace is a property of the
   *  kind, not of the server's ability to construct the brain, so a kind whose
   *  construction fails still has one here — and the server's own clearing rule
   *  answers the same way, which is what keeps the two agreeing. */
  brain_namespaces?: Record<string, string>;
  /** What a room with no pin of its own runs, on the web surface. Null only for
   *  a non-admin, or where the deployment's kind is not a known one. */
  inherited_brain?: SelectableBrain | null;
  /** The room's stored provider, with `room_id`; null is native's default. */
  room_provider?: string | null;
  /** The providers a native room may be moved to. Sent to an admin only, and
   *  only for a native room, so emptiness is the whole test for the control. */
  providers?: ChatProvider[];
}

/** Command, model-alias and brain catalogue powering the composer autocomplete
 *  and the room settings modal.
 *
 *  `roomId` scopes `model_aliases` to that room's own brain — a room pinned to
 *  another model namespace must not be offered names it cannot run. Omit it for
 *  the deployment default, which is what the composer's `!model` completion
 *  wants and what every caller got before rooms could pin a brain. */
export function fetchChatCommands(roomId?: number, brain?: string): Promise<ChatCommands> {
  const params = new URLSearchParams();
  if (roomId !== undefined) params.set('room_id', String(roomId));
  // The kind the caller is *considering*, which `room_id` cannot express: the
  // room holds its old brain until the save lands, so the settings modal asks
  // for the pending selection's models (ISSUE-417). Ignored server-side for a
  // non-admin or a kind the operator has not listed.
  if (brain) params.set('brain', brain);
  const q = params.toString();
  return apiFetch<ChatCommands>(`/chat/commands${q ? `?${q}` : ''}`);
}

export function getChatRooms(timeoutMs = 0): Promise<{ rooms: ChatRoom[] }> {
  return apiFetch<{ rooms: ChatRoom[] }>('/chat/rooms', undefined, timeoutMs);
}

export function createChatRoom(
  name: string,
  options: { incognito?: boolean } = {},
): Promise<ChatRoom> {
  return apiFetch<ChatRoom>('/chat/rooms', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options.incognito ? { name, incognito: true } : { name }),
  });
}

export interface RoomPatch {
  name?: string;
  archived?: boolean;
  model?: string | null;
  effort?: string | null;
  brain?: string | null;
  /** A native room's provider, admin only; null is native's default. */
  provider?: string | null;
  /** A `ROOM_COLORS` name, or null to clear. Absent leaves it untouched. */
  color?: string | null;
  /** Personal override; default restores inheritance, absent leaves it unchanged. */
  push_notifications?: RoomNotifications;
  /** Host only, like the other room-wide settings. */
  guest_reply?: GuestReply;
  /** Show an email thread in the main room list. The server refuses it (400)
   * on any other room. */
  listed?: boolean;
  /** Host only. Null follows the deployment's mode again. */
  speech_mode?: SpeechMode | null;
  /** Host only, like `speech_mode`. Null follows the deployment again. */
  disposition?: SpeechDisposition | null;
}

/** The PATCH response is the room, plus one field that is not room state:
 *  `cleared` names the standing defaults this request dropped because the
 *  brain change crossed a model namespace — `["model"]`, or `["model",
 *  "effort"]`, since the two are cleared as the pair they were set as. Present
 *  only when something was dropped, and the store takes it off before merging
 *  the rest into its record. */
export type UpdatedChatRoom = ChatRoom & { cleared?: string[] };

export function updateChatRoom(id: number, patch: RoomPatch): Promise<UpdatedChatRoom> {
  return apiFetch<UpdatedChatRoom>(`/chat/rooms/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** What a promote did. Not all of these are errors, which is why the endpoint
 * answers with a status rather than a bare room: `live` and `unreachable` both
 * left the room untouched, and rendering them as a failure is what made a room
 * with a dead binding look like a button that does nothing (ISSUE-401). */
export type PromoteStatus =
  /** Promoted a room that was not bound to Talk before. */
  | 'ok'
  /** The bound conversation was gone; the room now points at a new one. */
  | 'reconnected'
  /** Already bound to a conversation that still exists. Nothing changed. */
  | 'live'
  /** The conversation is there, but the bot was removed from it. Adding the bot
   * back is the repair — replacing the binding would fork a live room. */
  | 'bot_removed'
  /** Nextcloud could not be asked about the existing binding. Nothing changed. */
  | 'unreachable'
  /** Another request bound the room first. Its conversation stands. */
  | 'raced';

export interface PromoteResult {
  status: PromoteStatus;
  /** The updated room, or null where nothing usable came back. */
  room: ChatRoom | null;
}

/** Create a real Nextcloud Talk conversation for a web-origin room and bind it
 * ("Also open in Talk"), or repair a binding whose conversation was deleted. */
export function promoteChatRoom(id: number): Promise<PromoteResult> {
  return apiFetch<PromoteResult>(`/chat/rooms/${id}/promote`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/** A room can't be deleted while a task is still running in it (HTTP 409). */
export class ChatRoomBusyError extends Error {
  constructor() {
    super('room has a task in progress');
    this.name = 'ChatRoomBusyError';
  }
}

export async function deleteChatRoom(id: number): Promise<{ status: string }> {
  const resp = await fetch(`${base}/api/chat/rooms/${id}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (resp.status === 409) throw new ChatRoomBusyError();
  // 404 → already gone; idempotent from the caller's view.
  if (resp.status === 404) return { status: 'gone' };
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

/** `!room host` from the web: claim a room that has lost its host. A present
 * host is never replaced, which the server answers with 409. */
export function claimRoomHost(
  id: number,
): Promise<{ outcome: string; shared: boolean; policy: RoomPolicyView | null }> {
  return apiFetch(`/chat/rooms/${id}/host`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export interface RoomMember {
  user_id: string;
  display_name: string;
  is_owner: boolean;
}

export interface RoomMembers {
  members: RoomMember[];
  /** Only the creator manages membership, and never of a Talk-backed room. */
  can_manage: boolean;
  /** Transcript rows an add discloses to the new member. */
  message_count: number;
  /** An email thread room belongs to its host and takes no members. */
  email_thread?: boolean;
  /** A group-owned room's people are its group's, managed in Groups. */
  managed_by?: RoomManagedBy | null;
}

export function getRoomMembers(id: number): Promise<RoomMembers> {
  return apiFetch<RoomMembers>(`/chat/rooms/${id}/members`);
}

/** `acknowledge_history` is the creator's statement that the whole transcript
 * is disclosed; the endpoint refuses an add without it. */
export function addRoomMember(id: number, userId: string): Promise<{ member: RoomMember }> {
  return apiFetch(`/chat/rooms/${id}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, acknowledge_history: true }),
  });
}

export async function removeRoomMember(id: number, userId: string): Promise<void> {
  const resp = await fetch(`${base}/api/chat/rooms/${id}/members/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    throw Object.assign(new Error(await errorMessage(resp, detail)), {
      status: resp.status,
      field: typeof detail?.field === 'string' ? detail.field : null,
    });
  }
}

export interface DirectoryUser {
  user_id: string;
  display_name: string;
}

export function getChatUsers(): Promise<{ users: DirectoryUser[] }> {
  return apiFetch('/chat/users');
}

/** The group a room is linked to (multiplayer Stage 27). `choices` are the
 * caller's own groups, and empty unless the caller may set the link. */
export interface RoomGroupLink {
  group_id: string | null;
  group_name: string | null;
  can_set: boolean;
  refusal: string | null;
  choices: { group_id: string; display_name: string }[];
  /** The room's membership follows the group, so the link is fixed. */
  managed?: boolean;
  /** The Groups page for a managed room's group. */
  manage_url?: string | null;
}

export function getRoomGroup(id: number): Promise<RoomGroupLink> {
  return apiFetch<RoomGroupLink>(`/chat/rooms/${id}/group`);
}

/** Link the room to one of the caller's groups, or `null` to unlink it. */
export function putRoomGroup(id: number, groupId: string | null): Promise<RoomGroupLink> {
  return apiFetch<RoomGroupLink>(`/chat/rooms/${id}/group`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ group_id: groupId }),
  });
}

/** A room's `CHANNEL.md` — the standing instructions every task in the room
 * is given. `revision` is opaque and must be handed back on save. */
export interface ChatRoomMemory {
  room_id: number;
  token: string;
  content: string;
  /** False when the file is absent or whitespace-only; both are the empty state. */
  exists: boolean;
  /** More than one human reads the room, so a save is read by all of them. */
  shared: boolean;
  /** Server-supplied starting text for the empty state. */
  template: string;
  revision: string;
}

export function getRoomMemory(id: number): Promise<ChatRoomMemory> {
  return apiFetch<ChatRoomMemory>(`/chat/rooms/${id}/memory`);
}

/** A text file moved under its editor between load and save: reload before
 * overwriting. The editor keeps the user's text on this one. */
export class TextFileConflictError extends Error {
  constructor(message = 'the file changed since it was loaded') {
    super(message);
    this.name = 'TextFileConflictError';
  }
}

/** Another writer held the file's lock past the timeout. Retry. */
export class TextFileLockedError extends Error {
  constructor(message = 'the file is being written right now') {
    super(message);
    this.name = 'TextFileLockedError';
  }
}

/** The daemon would not load the file as written; `errors` says why. */
export class TextFileInvalidError extends Error {
  readonly errors: string[];
  constructor(errors: string[]) {
    super('the daemon would not load this file as written');
    this.name = 'TextFileInvalidError';
    this.errors = errors;
  }
}

/** The file moved under the editor — an agent write landed between load and
 * save. Reload before overwriting. */
export class ChatMemoryConflictError extends TextFileConflictError {
  constructor() {
    super('channel memory changed since it was loaded');
    this.name = 'ChatMemoryConflictError';
  }
}

/** Save refused because a task is in flight in the room, or because another
 * writer held the file lock. Both clear on their own; retry is the answer. */
export class ChatMemoryBusyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ChatMemoryBusyError';
  }
}

/** The caller's own notes about a room ("My notes"): a file in their
 * workspace that only their own turns in the room read. Same revision rule
 * as the room memory. */
export interface ChatRoomNotes {
  room_id: number;
  token: string;
  content: string;
  exists: boolean;
  revision: string;
}

export function getRoomNotes(id: number): Promise<ChatRoomNotes> {
  return apiFetch<ChatRoomNotes>(`/chat/rooms/${id}/notes`);
}

export function saveRoomNotes(
  id: number,
  content: string,
  revision: string,
): Promise<{ status: string; revision: string }> {
  return saveRoomFile(`${base}/api/chat/rooms/${id}/notes`, content, revision);
}

export function saveRoomMemory(
  id: number,
  content: string,
  revision: string,
): Promise<{ status: string; revision: string }> {
  return saveRoomFile(`${base}/api/chat/rooms/${id}/memory`, content, revision);
}

async function saveRoomFile(
  url: string,
  content: string,
  revision: string,
): Promise<{ status: string; revision: string }> {
  const resp = await fetch(url, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, revision }),
  });
  if (resp.status === 409) {
    const body = await resp.json().catch(() => ({}));
    if (body.code === 'conflict') throw new ChatMemoryConflictError();
    throw new ChatMemoryBusyError(body.error || 'room is busy');
  }
  if (resp.status === 413) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(`Too long — the limit is ${Math.floor((body.max_bytes ?? 0) / 1024)} KB.`);
  }
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

export function getRoomMessages(
  id: number,
  opts: {
    limit?: number;
    before?: { ts: string; id: number } | null;
    until?: { ts: string; id: number };
    timeoutMs?: number;
  } = {},
): Promise<ChatHistory> {
  const limit = opts.limit ?? 50;
  const params = new URLSearchParams({ limit: String(limit) });
  // Both cursor params travel together (the backend rejects a half-cursor) and
  // `before.ts` is the raw stored created_at, passed back verbatim.
  if (opts.before) {
    params.set('before_ts', opts.before.ts);
    params.set('before_id', String(opts.before.id));
  }
  if (opts.until) {
    params.set('until_ts', opts.until.ts);
    params.set('until_id', String(opts.until.id));
  }
  return apiFetch<ChatHistory>(
    `/chat/rooms/${id}/messages?${params.toString()}`,
    undefined,
    opts.timeoutMs ?? 0,
  );
}

/** One page of the cross-room message stream for an aggregate view. Same
 * message shape as the per-room endpoint plus room_token / room_name; same
 * keyset-cursor contract (`before.ts` is the raw stored created_at). */
export function getChatMessagesView(
  view: ChatView,
  opts: { limit?: number; before?: { ts: string; id: number } | null } = {},
): Promise<ChatHistory> {
  const params = new URLSearchParams({ view, limit: String(opts.limit ?? 50) });
  if (opts.before) {
    params.set('before_ts', opts.before.ts);
    params.set('before_id', String(opts.before.id));
  }
  return apiFetch<ChatHistory>(`/chat/messages?${params.toString()}`);
}

/** Star / unstar a durable message for the current user. */
export function setChatMessageStarred(
  msgId: number,
  starred: boolean,
): Promise<{ ok: boolean; starred: boolean }> {
  return apiFetch<{ ok: boolean; starred: boolean }>(`/chat/messages/${msgId}/star`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  });
}

/** A message can't be deleted while its turn is still running (HTTP 409) —
 * the scheduler writes the assistant row at completion, so the delete would
 * silently undo itself. Distinguished from a generic failure because "try
 * again once it finishes" is actionable and "couldn't delete" isn't. */
export class ChatMessageBusyError extends Error {
  constructor() {
    super('message belongs to a task in progress');
    this.name = 'ChatMessageBusyError';
  }
}

/** Hard-delete one transcript row. Gone from every read path at once, and —
 * in a Talk-bound room — best-effort from the mirrored Talk message too. */
export async function deleteChatMessage(msgId: number): Promise<{ status: string }> {
  const resp = await fetch(`${base}/api/chat/messages/${msgId}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (resp.status === 409) throw new ChatMessageBusyError();
  // 404 → already gone (another tab, a repeat click); idempotent from here.
  if (resp.status === 404) return { status: 'gone' };
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

/** Advance every room's web read cursor at once (the header mark-all chip). */
export function markAllRoomsRead(): Promise<{ ok: boolean; updated: number }> {
  return apiFetch<{ ok: boolean; updated: number }>('/chat/rooms/read-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Mark a room read on the web surface — clears its sidebar unread badge by
 * advancing the per-user read cursor to the room's newest message. */
export function markRoomRead(id: number): Promise<{ ok: boolean; last_read_message_id: number }> {
  return apiFetch<{ ok: boolean; last_read_message_id: number }>(`/chat/rooms/${id}/read`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * How long a send may stay open before we call it dead.
 *
 * `fetch` has no timeout of its own, and this caller's state machine blocks on
 * the result — without a bound, a stalled connection (a mobile handover, a
 * proxy holding the socket) parks the composer in "sending" for as long as the
 * OS takes to give up, which can be minutes or never.
 *
 * Generously above the slowest legitimate send rather than tight: the endpoint
 * does real work before it answers (`record_inbound`, plus a Talk mirror that
 * is itself bounded at ~5s), so a short bound would fail sends that were about
 * to succeed.
 */
export const SEND_TIMEOUT_MS = 30_000;

export async function sendChatMessage(
  roomId: number,
  text: string,
  attachments: string[] = [],
  // Display labels, positional against `attachments`. The stored filename
  // carries a random collision suffix, so the name the user picked is only
  // knowable here — the server persists these for the transcript's chips.
  attachmentNames: string[] = [],
  timeoutMs = SEND_TIMEOUT_MS,
  // Client-minted identity for this message, carried by every attempt at it.
  // The server answers a repeat with the task the first attempt created, which
  // is what makes a retry of a send it silently accepted produce one turn
  // rather than two. Optional: omitted, the endpoint behaves as it always did.
  idempotencyKey?: string,
  // Anything added from here on goes in the options object rather than as a
  // further positional — six is already more than a call site can read.
  options: SendOptions = {},
): Promise<SendResult> {
  const controller = new AbortController();
  let timedOut = false;
  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;

  // Serialized outside the try so the catch below can honestly claim every
  // rejection it sees is the network — otherwise a body that can't be
  // stringified would be reported to the user as an unreachable server.
  const body = JSON.stringify({
    text,
    attachments,
    attachment_names: attachmentNames,
    ...(idempotencyKey ? { client_msg_id: idempotencyKey } : {}),
    // Only the id: the server reads the parent's text from the row it already
    // holds, so nothing here can dictate what the model is told it said.
    ...(options.replyToMsgId ? { reply_to_msg_id: options.replyToMsgId } : {}),
    ...(options.aboutRoom && !options.replyToMsgId ? { about_room: options.aboutRoom } : {}),
  });

  try {
    const resp = await fetch(`${base}/api/chat/rooms/${roomId}/messages`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: controller.signal,
    });

    // Something answered, whatever it went on to say (ISSUE-202). Reported
    // before the status branches below, since a 401 and a 429 are as much
    // evidence of a reachable server as a 200 is.
    noteTransport(true);

    // Also returned rather than thrown as `AuthError`: only `getMe` in the root
    // layout catches that, so from here it took the silent path below.
    if (resp.status === 401) return { ok: false, status: 401, failure: 'auth' };
    if (resp.status === 429) {
      // A `Retry-After` may legally be an HTTP-date, and an intermediary
      // (a CDN, an nginx limiter) is not obliged to send the seconds form —
      // so an unparseable value must not reach the transcript as "wait NaNs".
      const parsed = Number(resp.headers.get('Retry-After'));
      const retry = Number.isFinite(parsed) && parsed > 0 ? parsed : 60;
      return { ok: false, status: 429, failure: 'rate_limit', retry_after: retry };
    }
    // Inside the try, and ahead of clearTimeout, so the abort still covers the
    // body: a proxy that flushes headers and then stalls is the same hang the
    // bound exists for, and clearing on the headers alone would leave it open.
    let data: { error?: string } & Record<string, unknown> = {};
    try {
      const parsed = await resp.json();
      // Shape-checked rather than assigned. `resp.json()` resolves to `null`
      // for a body that is the literal `null` and to an array for a JSON list,
      // neither of which throws — so the inner catch never fired and reading
      // `data.error` below threw a TypeError that escaped to the outer catch,
      // where a plain 400 was reported to the user as an unreachable server.
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) data = parsed;
    } catch (e) {
      // A body that isn't JSON is an error page — nginx answers its own HTML
      // for a 502 or an over-cap upload — and the status below already
      // describes it. An *aborted* read is the stall this bound exists for, so
      // it has to reach the classifier instead of passing for an empty body:
      // swallowing it returned `{ok: true}` with no task id, which the caller
      // reads as an inline command that produced nothing.
      if (controller.signal.aborted) throw e;
    }
    if (!resp.ok) {
      return {
        ok: false,
        status: resp.status,
        // A 404 on a send that carried a citation is almost always the server
        // refusing the *citation*. The endpoint answers 404 for an unknown
        // room too, which is reachable if another client deletes the room
        // mid-send — the recovery is the right one either way (the text comes
        // back rather than being stranded on a dead row), so the cost of
        // conflating them is a wrong sentence, not lost work. Classified on
        // the intent rather than on the error string, which is prose.
        failure: resp.status === 404 && options.replyToMsgId ? 'reply_target_gone' : 'rejected',
        error: data.error || `error ${resp.status}`,
      };
    }
    // `data` spread first: the endpoint's own payload carries a `status`
    // ("pending"), which would otherwise shadow the numeric HTTP status this
    // type promises — and that status is now rendered to the user on failure.
    // The one payload status the caller needs is "recorded", so it gets a
    // field of its own before the shadowing.
    return {
      ...data,
      ...(data.status === 'recorded' ? { recorded: true } : {}),
      ok: true,
      status: resp.status,
    };
  } catch {
    // A rejection here is the network, never the server: connection refused,
    // DNS, a dropped socket, a stalled body, or our own abort. There is no
    // status to report.
    //
    // Returned rather than thrown, unlike every other call in this file. The
    // caller renders this failure onto the message it belongs to, so it needs
    // the same shape as a rejection the server did answer — and a throw from
    // here is what used to escape an un-awaited caller and leave the composer
    // locked with nothing on screen (ISSUE-200).
    //
    // The same distinction the offline outbox turns on, so the connectivity
    // store hears it here rather than re-deriving it from the returned shape.
    const failure: SendFailure = timedOut ? 'timeout' : 'unreachable';
    noteTransport(false, failure);
    return { ok: false, status: 0, failure };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function getTaskEvents(taskId: number, sinceSeq = 0): Promise<{ events: TaskEventDTO[] }> {
  return apiFetch<{ events: TaskEventDTO[] }>(`/chat/tasks/${taskId}/events?since_seq=${sinceSeq}`);
}

/**
 * Outbound mail the approval gate is holding for the caller.
 *
 * Three shapes in one type, and the card must branch in this order:
 *
 * - `unreadable` — a stored column does not parse. Carries an id and nothing
 *   else, because the columns that would carry content are the ones that failed
 *   to read. Discard is the only action that works on it.
 * - `truncated` — the row exists but the stream frame spent its byte budget
 *   before reaching it. Enough to place a card; the body comes from
 *   `listOutboundDrafts()`.
 * - the full row.
 *
 * Every text field is model-composed or comes off a stranger's mail. Render it,
 * never inject it as markup.
 */
export interface OutboundDraft {
  id: number;
  /** Absent on an unreadable row, which is the only field it does carry. */
  status?: 'pending' | 'sending';
  room_token?: string | null;
  task_id?: number | null;
  subject?: string;
  body?: string;
  html?: boolean;
  to?: string[];
  cc?: string[];
  bcc?: string[];
  attachments?: string[];
  hold_reason?: string;
  /** "Label (address)" for a draft held from a connected mail account; null for the bot box. */
  from_line?: string | null;
  created_at?: string | null;
  /** What else the task that composed this draft did, as rendered strings. */
  actions_taken?: string[];
  /** Set when the stream frame carried a stub instead of the row. */
  truncated?: boolean;
  /** Set when the stored row could not be parsed. */
  unreadable?: boolean;
}

export function listOutboundDrafts(): Promise<{ drafts: OutboundDraft[] }> {
  return apiFetch<{ drafts: OutboundDraft[] }>('/chat/drafts');
}

/**
 * Why an approve, edit or discard did not happen — the card wording turns on
 * this, and the four cases call for four different sentences.
 *
 * `conflict` carries `state`, because "your mail is going out right now" and
 * "someone already discarded this" are opposite messages behind one 409.
 * `sent_unrecorded` is the one failure that must never offer a retry: the mail
 * left the building and only the bookkeeping failed.
 */
export type DraftFailure = 'gone' | 'conflict' | 'permanent' | 'transient' | 'sent_unrecorded';

export interface DraftActionResult {
  ok: boolean;
  status: number;
  failure?: DraftFailure;
  /** On `conflict`: what the row is now. */
  state?: 'pending' | 'sending' | 'sent' | 'discarded' | 'gone';
  error?: string;
  message_id?: string;
  /** The re-read row a successful PATCH returns. */
  draft?: OutboundDraft;
}

/**
 * One place the draft routes' failures become a `DraftFailure`.
 *
 * `apiFetch` throws a bare `API error: <status>` on any non-2xx, which discards
 * the body — and the body is where `state` and `retryable` live. So these three
 * verbs go through `fetch` directly, the way the send path already does for the
 * same reason.
 */
async function draftAction(
  path: string,
  init: RequestInit = { method: 'POST' },
): Promise<DraftActionResult> {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api${path}`, {
      ...init,
      credentials: 'same-origin',
    });
  } catch {
    // The request never landed, so nothing was decided. Safe to offer again.
    return { ok: false, status: 0, failure: 'transient' };
  }
  if (resp.status === 401) throw new AuthError();
  let data: Record<string, unknown> = {};
  try {
    data = await resp.json();
  } catch {
    /* an empty or non-JSON body still has to classify by status */
  }
  if (resp.ok) return { ...data, ok: true, status: resp.status };
  const error = typeof data.error === 'string' ? data.error : `error ${resp.status}`;
  if (resp.status === 404) return { ok: false, status: 404, failure: 'gone', error };
  if (resp.status === 409) {
    // A missing `state` stays undefined rather than defaulting to `gone`. The
    // caller drops the card silently for a settled state and keeps it for
    // `sending`, so a default of `gone` turns "we don't know" into the branch
    // that reports the action as having worked.
    const state = data.state;
    return {
      ok: false,
      status: 409,
      failure: 'conflict',
      state: typeof state === 'string' ? (state as DraftActionResult['state']) : undefined,
      error,
    };
  }
  if (data.sent === true) {
    return {
      ok: false,
      status: resp.status,
      failure: 'sent_unrecorded',
      message_id: typeof data.message_id === 'string' ? data.message_id : undefined,
      error,
    };
  }
  // The server's own transient/permanent split, not a guess from the status:
  // it alone knows whether the relay refused or the instance is misconfigured.
  return {
    ok: false,
    status: resp.status,
    failure: data.retryable === true ? 'transient' : 'permanent',
    error,
  };
}

export function approveOutboundDraft(draftId: number): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}/approve`);
}

export function discardOutboundDraft(draftId: number): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}/discard`);
}

export function editOutboundDraft(draftId: number, body: string): Promise<DraftActionResult> {
  return draftAction(`/chat/drafts/${draftId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body }),
  });
}

/** `room` is the room the card rendered in. A relay question, room post or
 *  guest proposal is approved only from the private room showing its
 *  preview, and the server answers 409 without it (#624). */
export function confirmChatTask(taskId: number, room?: string): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/confirm`, {
    method: 'POST',
    ...(room
      ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ room }) }
      : {}),
  });
}

export function cancelChatTask(taskId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/cancel`, { method: 'POST' });
}

export interface RetryTaskResult {
  status: 'queued';
  task_id: number;
  retried_task_id: number;
  // True when the task was a scheduled job or briefing, which runs its current
  // definition rather than a copy of the failed task.
  run_now: boolean;
  message: string;
}

/** Re-run a failed or cancelled task (ISSUE-631). A refusal is a 409 whose
 * message is the one `!retry` gives, thrown as the Error's message. */
export function retryChatTask(
  taskId: number,
  mode: 'retry' | 'resume' = 'retry',
): Promise<RetryTaskResult> {
  return apiFetch<RetryTaskResult>(`/chat/tasks/${taskId}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  });
}

export function chatStreamUrl(taskId: number): string {
  return `${base}/api/chat/tasks/${taskId}/stream`;
}

/** A row from the live room-event stream: the same shape the history endpoints
 * emit, plus the room it belongs to, so `buildHistoryMessage` can construct a
 * streamed row and a reloaded row through one code path. */
export type ChatRoomEvent = ChatHistoryMessage;

export interface ChatRoomEventsPage {
  events: ChatRoomEvent[];
  /** The cursor to adopt. On `gap` this is the max id the server *scanned*,
   * not the last one it sent — adopting the latter would strand the rows the
   * server chose not to replay. */
  cursor: number;
  /** The delta was too large to replay; reload instead (rooms list + the open
   * room), then adopt `cursor`. */
  gap: boolean;
  /** Messages deleted since `since_deletion_id`. A separate tail because a
   * delete is hard: there is no `messages` row left for the id-ordered event
   * tail to carry, so the ledger is what tells another open tab. */
  deletions?: ChatMessageDeletion[];
  /** Cursor to adopt for the deletion tail — its own sequence, unrelated to
   * `cursor`. */
  deletion_cursor?: number;
  /** Held outbound mail, as a whole-set snapshot rather than a tail: a draft's
   * transitions are not all insertions, so an id cursor would carry one of
   * three. Always present on a healthy read, even when empty. */
  drafts?: OutboundDraft[];
  /** The drafts read failed. Distinct from an empty set, which is why the key
   * above is always sent — a client reading absence as "none held" would clear
   * every card on one transient lock. */
  drafts_unavailable?: boolean;
}

export interface ChatMessageDeletion {
  msg_id: number;
  room_token: string;
}

/** Snapshot of the room-event tail — the polling fallback behind the room
 * stream. `limit=1` asks only for a fresh cursor (used after a reload). */
export function getRoomEvents(
  sinceId = 0,
  limit = 0,
  timeoutMs = 0,
  sinceDeletionId = 0,
): Promise<ChatRoomEventsPage> {
  const params = new URLSearchParams({ since_id: String(sinceId) });
  if (limit > 0) params.set('limit', String(limit));
  if (sinceDeletionId > 0) params.set('since_deletion_id', String(sinceDeletionId));
  return apiFetch<ChatRoomEventsPage>(`/chat/events?${params.toString()}`, undefined, timeoutMs);
}

/** SSE endpoint carrying every message visible to the user, across all rooms. */
export function chatRoomStreamUrl(sinceId: number, sinceDeletionId = 0): string {
  return `${base}/api/chat/stream?since_id=${sinceId}&since_deletion_id=${sinceDeletionId}`;
}

export interface ChatAttachment {
  // The host path the brain reads, or **null for a chip whose bytes are still
  // in this browser** — a file attached with no connection, waiting for the
  // outbox to upload it (ISSUE-202). A null path never reaches the wire: the
  // drain resolves every pending chip to a real path before it POSTs.
  path: string | null;
  name: string;
  size: number;
  // Where the same file is reachable through `chatFileUrl`, or null when it
  // isn't (a deployment with no local workspace). Distinct from `path`, which
  // is the host path the brain reads and the download endpoint won't take.
  workspace_path?: string | null;
  // Key into the offline `blobs` object store, set on a pending chip and on no
  // other. Its presence is what `path: null` means.
  pendingBlobId?: string;
  // The picked file's type, carried only on a pending chip so the queue entry
  // that records it has one. A file already uploaded has no use for it — the
  // server holds the file and the client only ever names it.
  mimeType?: string;
}

/**
 * An attachment upload that never reached the server.
 *
 * Distinguished from every other upload failure for the reason the send path
 * distinguishes `unreachable` from `rejected`: nothing has been refused, so a
 * queued message keeps its bytes and waits rather than failing (ISSUE-202).
 * Everything the server actually answered — a 413, a disallowed extension —
 * throws a plain `Error` carrying its message.
 */
export class UploadUnreachableError extends Error {
  constructor(message = 'Couldn’t upload — the server is unreachable.') {
    super(message);
    this.name = 'UploadUnreachableError';
  }
}

/**
 * Download URL for one of the caller's own workspace files.
 *
 * The whole point of routing a chip here rather than at a Nextcloud share is
 * that nothing becomes public: the endpoint serves the file inside the logged-in
 * session, so the user opens a file they already own.
 */
export function chatFileUrl(path: string): string {
  return `${base}/api/chat/files?path=${encodeURIComponent(path)}`;
}

export interface FilePreview {
  name: string;
  size: number;
  modified: string;
  kind: 'image' | 'audio' | 'video' | 'pdf' | 'text' | 'binary';
  media_type?: string;
  text?: string;
  truncated: boolean;
}

export function previewChatFile(path: string, signal?: AbortSignal): Promise<FilePreview> {
  return apiFetch(`/chat/files/preview?path=${encodeURIComponent(path)}`, { signal });
}

/** Bytes of a file in the caller's own Nextcloud, read with their personal connection. */
export function nextcloudFileUrl(path: string): string {
  return `${base}/api/workspace/nextcloud/file?path=${encodeURIComponent(path)}`;
}

export function previewNextcloudFile(path: string, signal?: AbortSignal): Promise<FilePreview> {
  return apiFetch(`/workspace/nextcloud/preview?path=${encodeURIComponent(path)}`, { signal });
}

/** Bytes of a file in a share from another server; `path` is relative to the share, `''` for a file share. */
export function shareFileUrl(place: string, path: string): string {
  return `${base}/api/workspace/share/file?${new URLSearchParams({ place, path })}`;
}

export function previewShareFile(
  place: string,
  path: string,
  signal?: AbortSignal,
): Promise<FilePreview> {
  return apiFetch(`/workspace/share/preview?${new URLSearchParams({ place, path })}`, { signal });
}

/**
 * What the file picker offers for a profile picture.
 *
 * Mirrors `avatars.ACCEPT_ATTRIBUTE` in `src/istota/avatars.py`, and is
 * deliberately narrower than `image/*` — that matches TIFF, BMP, AVIF and SVG,
 * all of which the server refuses, and the user would find out only after
 * choosing one.
 */
export const AVATAR_ACCEPT = 'image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif';

/**
 * Where one identity's picture is served from.
 *
 * A plain cookie-authenticated URL to drop into `src`, exactly like
 * `chatFileUrl`. `version` is a content hash: with it the server answers
 * `immutable` for the caller's own picture and the bot icon, so every later
 * render in a transcript is a cache hit with no request. Without it the
 * answer is `no-cache` plus an `ETag`, which is what a third party's picture
 * gets — one conditional request per author per session.
 */
export function avatarUrl(kind: 'user' | 'bot', userId?: string, version?: string | null): string {
  // `userId` is optional for the bot branch alone, and nothing in the type
  // enforces the pairing. Without this, a missing id builds a URL with an
  // empty last segment, which matches no route at all — so the browser gets
  // FastAPI's own 404 rather than the one the endpoint sends, and the caller
  // sees an image that failed with no way to tell why.
  if (kind === 'user' && !userId) throw new Error('avatarUrl: a user avatar needs a userId');
  const path =
    kind === 'bot' ? '/api/avatars/bot' : `/api/avatars/user/${encodeURIComponent(userId!)}`;
  const query = version ? `?v=${encodeURIComponent(version)}` : '';
  return `${base}${path}${query}`;
}

export interface AvatarUpload {
  /** sha256 of the *normalized* bytes — what `?v` and the ETag carry. */
  hash: string;
  mime: string;
  bytes: number;
}

/**
 * PUT one picture as multipart, to whichever avatar endpoint owns it.
 *
 * Shaped like `uploadChatAttachment` rather than routed through `apiFetch`,
 * because every refusal here is one the user has to read: the server sends
 * `{error}` with a 413 for a file over the cap and a 415 for a format it will
 * not decode, and `apiFetch` would collapse both into `API error: 413`.
 */
async function putAvatar(path: string, file: File): Promise<AvatarUpload> {
  const form = new FormData();
  form.append('file', file);
  let resp: Response;
  try {
    resp = await fetch(`${base}${path}`, {
      method: 'PUT',
      credentials: 'same-origin',
      body: form,
    });
  } catch {
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `upload failed (${resp.status})`);
  return data as AvatarUpload;
}

/** Replace the caller's own profile picture. */
export async function uploadAvatar(file: File): Promise<AvatarUpload> {
  return putAvatar('/api/settings/avatar', file);
}

/**
 * Remove the caller's uploaded picture, revealing any imported one.
 *
 * `{deleted: false}` when there was none — idempotent, not a 404.
 */
export async function deleteAvatar(): Promise<{ deleted: boolean }> {
  return apiFetch('/settings/avatar', { method: 'DELETE' });
}

/**
 * Replace the deployment's bot icon. Admin only.
 *
 * Deployment-wide and one row, so two admins setting it at once resolve as
 * last writer wins. It is *not* the bot's Nextcloud Talk avatar and cannot
 * change it — the daemon holds an app password, and Nextcloud's avatar route
 * is session-and-CSRF-guarded — which is why the control says so.
 */
export async function uploadBotAvatar(file: File): Promise<AvatarUpload> {
  return putAvatar('/api/admin/avatar', file);
}

/**
 * Clear the bot icon, reverting to the amber initial chip.
 *
 * `{deleted: false}` when there was none — idempotent, not a 404.
 */
export async function deleteBotAvatar(): Promise<{ deleted: boolean }> {
  return apiFetch('/admin/avatar', { method: 'DELETE' });
}

const CHAT_ATTACHMENT_PATH = '/chat/attachments';

/**
 * Upload one attachment, by whichever route the file arrived on.
 *
 * A file the shell picked is still on disk and never had to come into the page
 * at all: the shell posts it straight from there with URLSession, and what
 * crosses the bridge is the server's answer. Everything else — a paste, a drop,
 * a file input, a voice memo — is already in memory here and goes out as an
 * ordinary multipart fetch.
 *
 * Both routes end at the same endpoint and read the same JSON back, including
 * the error bodies, so the caller cannot tell them apart and does not need to.
 */
export async function uploadChatAttachment(item: File | Picked): Promise<ChatAttachment> {
  if (!(item instanceof File) && item.nativePath) {
    return uploadFromShell(item);
  }
  const file = item instanceof File ? item : item.blob;
  if (!file) throw new Error('Nothing to upload.');
  const form = new FormData();
  form.append('file', file);
  let resp: Response;
  try {
    resp = await fetch(`${base}/api${CHAT_ATTACHMENT_PATH}`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    });
  } catch {
    // The one failure that says nothing about the file. Reported to the
    // connectivity store for the same reason every other transport completion
    // is: an upload is the request that discovers the gap when it is the first
    // thing the user does after losing signal.
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `upload failed (${resp.status})`);
  return data as ChatAttachment;
}

/**
 * The same upload, done by the shell from the file still on disk.
 *
 * The URL has to be absolute — the shell is building a URLSession request, not
 * resolving one against a document — so the page's own origin is what makes it
 * the same endpoint the fetch above would have reached.
 */
async function uploadFromShell(item: Picked): Promise<ChatAttachment> {
  const url = new URL(`${base}/api${CHAT_ATTACHMENT_PATH}`, location.origin).toString();
  // Asked here rather than left to `uploadFromPath` to throw, so that the
  // catch below is only ever the uploader's own rejection. A missing plugin is
  // a fact about this build, not about the network, and reporting it as a gap
  // would raise the offline banner and stop every room's queue draining over
  // something no request observed.
  if (!nativeUploadAvailable()) throw new Error('No native uploader.');
  let status: number;
  let body: string;
  try {
    ({ status, body } = await uploadFromPath(item, url));
  } catch {
    // `uploadFromPath` returns any HTTP answer as a status, so a throw here is
    // URLSession failing to get one — the same gap the fetch above reports.
    noteTransport(false, 'unreachable');
    throw new UploadUnreachableError();
  }
  noteTransport(true);
  if (status === 401) throw new AuthError();
  let data: { error?: string } & Partial<ChatAttachment> = {};
  try {
    data = JSON.parse(body);
  } catch {
    // A proxy's own error page rather than the app's JSON — an nginx 413 is
    // the one that actually happens. There is no message worth relaying, so
    // the status carries it.
  }
  if (status < 200 || status >= 300) {
    throw new Error(data.error || `upload failed (${status})`);
  }
  return data as ChatAttachment;
}

// ---------------------------------------------------------------------------
// Briefings module
// ---------------------------------------------------------------------------

export type BriefingWidth = 'auto' | 'wide' | 'full';

export interface BriefingDocumentBlock {
  block_id: number;
  title: string;
  kind: string;
  body_md: string;
  presentation: { width: BriefingWidth };
  source_status: 'complete' | 'partial';
}

export interface BriefingDocument {
  version: 1;
  title: string;
  blocks: BriefingDocumentBlock[];
}

export interface BriefingArchiveItem {
  document?: BriefingDocument | null;
  id: number;
  read_at: string | null;
  briefing_name: string;
  subject: string;
  generated_at: string;
  task_id: number | null;
  delivered_to: string[];
  body_md?: string;
  /** The first words as one plain line, for list rows. */
  preview?: string;
}

export interface BriefingArchiveResponse {
  items: BriefingArchiveItem[];
  total: number;
  briefing_names: string[];
}

export interface BriefingSource {
  id: number;
  position: number;
  kind: string;
  config: Record<string, unknown>;
  enabled: boolean;
}

export interface BriefingBlock {
  id: number;
  briefing_name: string;
  position: number;
  title: string;
  directive: string;
  render_mode: string;
  options: Record<string, unknown>;
  sources: BriefingSource[];
}

export interface BriefingSourceDraft {
  id?: number;
  kind: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
}

export interface BriefingBlockSave {
  id?: number;
  briefing_name?: string;
  title?: string;
  directive?: string | null;
  render_mode?: string;
  options?: Record<string, unknown>;
  sources?: BriefingSourceDraft[];
}

export interface BriefingBlockTemplate {
  id: string;
  label: string;
  description: string;
  block: {
    title: string;
    directive: string;
    render_mode: string;
    options: Record<string, unknown>;
    sources: BriefingSourceDraft[];
  };
}

export interface BriefingConfigResponse {
  briefings: { name: string; blocks: BriefingBlock[] }[];
  schedule_names: string[];
  source_kinds: string[];
  structured_kinds: string[];
  block_templates: BriefingBlockTemplate[];
}

export interface BrowsePreset {
  key: string;
  name: string;
  url: string;
}

export interface FeedOptions {
  available: boolean;
  subscriptions: { kind: string; value: number; label: string }[];
  categories: { kind: string; value: number; label: string }[];
}

export async function getBriefingArchive(
  params?: Record<string, string>,
): Promise<BriefingArchiveResponse> {
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  return apiFetch<BriefingArchiveResponse>(`/briefings/archive${qs}`);
}

export async function getBriefingArchiveItem(id: number): Promise<BriefingArchiveItem> {
  return apiFetch<BriefingArchiveItem>(`/briefings/archive/${id}`);
}

export async function markBriefingArchiveRead(
  id: number,
): Promise<{ id: number; read_at: string }> {
  return apiFetch(`/briefings/archive/${id}/read`, { method: 'POST' });
}

export async function markBriefingArchiveAsRead(
  scope: 'all' | 'briefing',
  briefingName?: string,
): Promise<{ status: string; updated: number }> {
  return apiFetch('/briefings/mark-as-read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scope,
      ...(scope === 'briefing' ? { briefing_name: briefingName } : {}),
    }),
  });
}

export async function deleteBriefingArchiveItem(id: number): Promise<void> {
  await apiFetch(`/briefings/archive/${id}`, { method: 'DELETE' });
}

export interface BriefingPreviewResponse {
  body_md: string;
  plain: string;
  html: string;
}

export async function previewBriefing(
  document: BriefingDocument,
): Promise<BriefingPreviewResponse> {
  return apiFetch('/briefings/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  });
}

export async function getBriefingConfig(): Promise<BriefingConfigResponse> {
  return apiFetch<BriefingConfigResponse>('/briefings/config');
}

export function putBriefingBlock(
  payload: BriefingBlockSave,
): Promise<{ status: string; block: BriefingBlock }>;
export function putBriefingBlock(payload: {
  reorder: { briefing_name: string; ordered_ids: number[] };
}): Promise<{ status: string; reordered: number }>;
export async function putBriefingBlock(
  payload: BriefingBlockSave | { reorder: { briefing_name: string; ordered_ids: number[] } },
): Promise<{ status: string; block?: BriefingBlock; reordered?: number }> {
  return apiFetch('/briefings/blocks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefingBlock(id: number): Promise<void> {
  await apiFetch(`/briefings/blocks/${id}`, { method: 'DELETE' });
}

export async function putBriefingSource(
  payload: Record<string, unknown>,
): Promise<{ status: string; id?: number }> {
  return apiFetch('/briefings/sources', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteBriefingSource(id: number): Promise<void> {
  await apiFetch(`/briefings/sources/${id}`, { method: 'DELETE' });
}

export async function getBrowsePresets(): Promise<{ presets: BrowsePreset[] }> {
  return apiFetch('/briefings/browse-presets');
}

export async function getFeedOptions(): Promise<FeedOptions> {
  return apiFetch('/briefings/feed-options');
}

export async function checkBriefingPath(
  path: string,
): Promise<{ ok: boolean; resolved?: string; error?: string }> {
  return apiFetch(`/briefings/path-check?path=${encodeURIComponent(path)}`);
}

export async function getBriefingPathSuggestions(q = ''): Promise<{ paths: string[] }> {
  const query = q.trim();
  return apiFetch(
    query ? `/briefings/path-suggest?q=${encodeURIComponent(query)}` : '/briefings/path-suggest',
  );
}

// --- Shared briefing blocks (admin) + options (any user) ------------------

export interface SharedBlockRunAnswer {
  id: string;
  // An agent block answers `queued` or `in_flight`; its task's outcome arrives
  // later, in `SharedBlockStatus.agent`.
  outcome:
    | 'regenerated'
    | 'kept_prior'
    | 'brain_unavailable'
    | 'failed'
    | 'not_found'
    | 'queued'
    | 'in_flight';
  at: string;
}

export interface SharedBlockAgentRun {
  task_id: number;
  briefing_name: string;
  queued_at: string;
  captured: boolean;
  outcome?: 'regenerated' | 'kept_prior' | 'failed' | 'discarded';
  answered_at?: string;
  consecutive_failures?: number;
}

export interface SharedBlockStatus {
  // Run now is answered by the daemon (ISSUE-700): the pending request, and
  // the daemon's last answer to one.
  run_pending: { id: string; name: string; at: string } | null;
  run_answered: SharedBlockRunAnswer | null;
  // An agent block's last task, queued by cron or Run now.
  agent: SharedBlockAgentRun | null;
  last_run_at: string | null;
  value_updated_at: string | null;
  value_preview: string | null;
  stored_trusted: boolean | null;
  has_content: boolean;
}

export interface SharedBlock {
  name: string;
  cron: string;
  title: string;
  directive: string;
  render_mode: string;
  enabled: boolean;
  trusted: boolean;
  sources: { kind: string; config: Record<string, unknown> }[];
  created_at: string | null;
  updated_at: string | null;
  status: SharedBlockStatus;
}

export interface SharedBlocksResponse {
  shared_blocks: SharedBlock[];
  allowed_source_kinds: string[];
  render_modes: string[];
  shared_block_timezone: string;
}

export interface SharedBlockOption {
  name: string;
  updated_at: string | null;
  has_content: boolean;
  source: 'config' | 'custom';
}

export async function getSharedBlocks(): Promise<SharedBlocksResponse> {
  return apiFetch('/briefings/shared-blocks');
}

export async function putSharedBlock(
  payload: Record<string, unknown>,
): Promise<{ status: string; shared_block?: SharedBlock }> {
  return apiFetch('/briefings/shared-blocks', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function deleteSharedBlock(name: string, deleteValue = false): Promise<void> {
  const qs = deleteValue ? '?delete_value=true' : '';
  await apiFetch(`/briefings/shared-blocks/${encodeURIComponent(name)}${qs}`, { method: 'DELETE' });
}

export async function runSharedBlock(name: string): Promise<{ id: string }> {
  return apiFetch(`/briefings/shared-blocks/${encodeURIComponent(name)}/run`, { method: 'POST' });
}

/**
 * A mail account a `my_email` briefing source can read: the user's bot address,
 * Gmail, or one of their connected mail accounts (`imap:<id>`).
 */
export interface EmailAccount {
  id: 'bot' | 'google' | `imap:${number}`;
  label: string;
  address: string | null;
  available: boolean;
  reason: string;
}

export async function getEmailAccounts(): Promise<{ accounts: EmailAccount[] }> {
  return apiFetch('/briefings/email-accounts');
}

export async function getSharedBlockOptions(): Promise<{ options: SharedBlockOption[] }> {
  return apiFetch('/briefings/shared-block-options');
}

// --- the notification inbox ------------------------------------------------

/** One button on a notification.
 *
 * `endpoint` is an existing producer API path (`/chat/tasks/12/confirm`),
 * apiFetch-relative — the same form every other call in this file takes. There
 * is deliberately no generic dispatcher endpoint on the server: those handlers
 * already own their authorization, and a dispatcher would be a second gate with
 * less context. `href` is an in-app route, relative to `base`. */
export interface NotificationAction {
  id: string;
  label: string;
  kind: 'primary' | 'default' | 'danger';
  method: 'POST' | 'LINK';
  endpoint: string | null;
  href: string | null;
  /** Offered only in the detail modal, beside the full body it acts on. */
  detail_only?: boolean;
  body?: Record<string, unknown>;
}

/** A stored notification as the panel renders it.
 *
 * Every text field here is either bot-composed or lifted off a stranger's mail —
 * a gated email's sender and subject reach `title` and `body`. Render them as
 * text nodes; none of the five components touching this type uses `{@html}`. */
export interface ResolvedNotification {
  id: number;
  source: string;
  severity: 'info' | 'success' | 'warning' | 'danger';
  actionable: boolean;
  title: string;
  body: string;
  link: string | null;
  occurrences: number;
  created_at: string;
  updated_at: string;
  seen_at: string | null;
  object_type: string | null;
  object_id: string | null;
  actions: NotificationAction[];
  status_note: string | null;
}

export interface NotificationCounts {
  push_badge?: number;
  chat_unread?: number;
  open: number;
  actionable: number;
}

export interface NotificationListing {
  notifications: ResolvedNotification[];
  /** The post-sweep count of the whole open set, not of the returned page.
   *  Both tab labels are derived from this and the rows, so a label can never
   *  claim more than the list below it shows. */
  total_open: number;
}

/** The id and the version the client actually rendered.
 *
 * The pair is the whole point: the server resolves a fire-and-forget row only
 * where the stored `updated_at` still matches, so an occurrence raised between
 * the fetch and this call is not closed by a user who never saw it. */
export interface NotificationSeen {
  id: number;
  updated_at: string;
}

/** Mirrors `notification_sources.SAFE_PATH_RE` on the server.
 *
 * The server validates every URL it emits, at runtime, on every view. This is
 * the second copy rather than a substitute for it: the *browser* is what
 * performs the fetch, with the session cookie attached, off a path the server
 * chose — so the side taking the risk checks it too. Anchored the same way, and
 * deliberately without the `m` flag: `$` would otherwise admit a trailing
 * newline, and a control character reaching a fetch target is what an allowlist
 * is for. */
const SAFE_ACTION_PATH = /^\/[A-Za-z0-9][A-Za-z0-9/_-]*$/;

/** A type predicate, so a caller that checks a nullable `href` also narrows it —
 *  otherwise every call site needs a second truthiness test the compiler can see,
 *  and the two can disagree about which one is the guard. */
export function isSafeActionPath(path: string | null | undefined): path is string {
  return typeof path === 'string' && SAFE_ACTION_PATH.test(path);
}

/** Whether an action path is answered by the server rather than by a page.
 *  `/chat/r/<room>[/t/<task>]` is the deep-link redirect (#624) and
 *  `/files/assignments/<id>` an assignment's folder (#848), `/files/shares` the
 *  index of shares from other servers (#849): the frontend is prerendered with
 *  no route there, so their links must make the client do a full navigation. */
const SERVER_ACTION_PREFIXES = ['/chat/r/', '/files/assignments/', '/files/shares'];

export function isServerActionPath(path: string | null | undefined): boolean {
  return isSafeActionPath(path) && SERVER_ACTION_PREFIXES.some((prefix) => path.startsWith(prefix));
}

/** The deep link into a room by its token, or null for a token that cannot be
 *  one. The server checks membership and lands on a fresh room list, or on the
 *  bare chat page for a room the user is not in. */
export function chatRoomLink(token: unknown): string | null {
  if (typeof token !== 'string' || token === '' || token.includes('/')) return null;
  const path = `/chat/r/${token}`;
  return isSafeActionPath(path) ? path : null;
}

export function getNotificationCounts(): Promise<NotificationCounts> {
  return apiFetch<NotificationCounts>('/notifications/count');
}

export function listNotifications(
  filter: 'all' | 'action' = 'all',
  limit = 50,
): Promise<NotificationListing> {
  return apiFetch<NotificationListing>(`/notifications?filter=${filter}&limit=${limit}`);
}

export function dismissNotification(id: number): Promise<{ status: string }> {
  return apiFetch(`/notifications/${id}/dismiss`, { method: 'POST' });
}

export function markNotificationsSeen(seen: NotificationSeen[]): Promise<{ status: string }> {
  return apiFetch('/notifications/seen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ seen }),
  });
}

/** POST the path a resolver named for this action.
 *
 * Refuses anything the allowlist does not accept rather than fetching it. The
 * path is server-supplied and built by interpolating an `object_id` that is
 * opaque `TEXT` on the row, so `1/../../admin/x` is the shape being refused. */
export async function runNotificationAction(
  endpoint: string,
  body?: Record<string, unknown>,
): Promise<unknown> {
  if (!isSafeActionPath(endpoint)) {
    throw new Error(`refusing an unsafe notification action path: ${endpoint}`);
  }
  return apiFetch(endpoint, {
    method: 'POST',
    ...(body
      ? { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }
      : {}),
  });
}

export { AuthError };

export interface CredentialGrant {
  scope_mode: 'all' | 'rooms';
  rooms: string[];
  allow_scheduled: boolean;
  allow_http?: boolean;
  policy_revision?: number;
}
/** Credentials belong to the user, the deployment, or a generated account. */
export type CredentialSource = 'local' | 'config' | 'generated';
export interface CredentialSummary {
  name: string;
  source: CredentialSource;
  hosts: string[];
  headers: string[];
  revealable: boolean;
  grant: CredentialGrant | null;
  /** Local credentials only: the stored site, which is not secret. */
  url?: string;
  /** Local credentials only: the bound hosts other than the site's, comma-separated,
   *  as the server's parser splits them. */
  extra_hosts?: string;
  /** Local credentials only. The username itself is never sent. */
  username_set?: boolean;
  otp_set?: boolean;
  /** Whether any field in this credential is a two-factor seed. */
  otp?: boolean;
  /** Generated credentials only: whether recovery codes are stored. The codes
   *  are fetched on demand with `showRecoveryCodes`, never listed. */
  recovery?: boolean;
  recovery_remaining?: number | null;
}
export interface CredentialGrantsSettings {
  credentials: CredentialSummary[];
  rooms: { token: string; name: string }[];
  grant_existing_available: boolean;
  sandboxed: boolean;
  /** False only when the deployment withholds the credential store from tasks;
   *  `add_blocked_reason` then says why. */
  can_add: boolean;
  add_blocked_reason: string;
  broker_enabled: boolean;
}
export interface CredentialAccess {
  scope_mode: 'all' | 'rooms';
  rooms: string[];
  allow_scheduled: boolean;
  allow_http: boolean;
}
export interface NewCredential {
  otp?: string;
  name: string;
  value: string;
  username: string;
  url: string;
  extra_hosts: string;
  headers: string;
  revealable: boolean;
  access?: CredentialAccess;
}
/** `value: null` keeps the stored value. `username: null` keeps the stored
 *  username and `''` removes it. The other four are required: the server reads
 *  an absent `url` as a mistake rather than as "clear the site". */
export interface CredentialUpdate {
  otp?: string | null;
  value: string | null;
  username: string | null;
  url: string;
  extra_hosts: string;
  headers: string;
  revealable: boolean;
}
export interface CredentialWriteResult {
  ok: boolean;
  name: string;
  username_name: string | null;
  url_name: string | null;
  grant: CredentialGrant | null;
}

/** A refused credential write. `field` names the input it is about, or is null
 *  for a refusal of the whole request, which the form shows as a banner. The
 *  message is the server's and never carries the value. */
export class CredentialWriteError extends Error {
  readonly field: string | null;
  readonly reason: string | null;
  constructor(message: string, field: string | null, reason: string | null = null) {
    super(message);
    this.name = 'CredentialWriteError';
    this.reason = reason;
    this.field = field;
  }
}

/** Outside `apiFetch` because a refusal's `field` has to reach the form, and
 *  `apiFetch` keeps only the message. */
async function credentialWrite<T = CredentialWriteResult>(
  path: string,
  method: string,
  body: unknown,
  prefix = '/settings/credentials',
  signal?: AbortSignal,
) {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api${prefix}${path}`, {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: method === 'GET' ? undefined : JSON.stringify(body),
      signal,
    });
  } catch {
    noteTransport(false, 'unreachable');
    throw new CredentialWriteError(
      'Istota could not be reached. Check the list before trying again.',
      null,
    );
  }
  noteTransport(true);
  if (resp.status === 401) throw new AuthError();
  let payload: unknown = null;
  try {
    payload = await resp.json();
  } catch {
    payload = null;
  }
  if (!resp.ok) {
    const raw = (payload ?? {}) as { detail?: unknown; field?: unknown; reason?: unknown };
    const message =
      typeof raw.detail === 'string' && raw.detail.trim()
        ? raw.detail
        : `API error: ${resp.status}`;
    throw new CredentialWriteError(
      message,
      typeof raw.field === 'string' ? raw.field : null,
      typeof raw.reason === 'string' ? raw.reason : null,
    );
  }
  return payload as T;
}

export function createCredential(credential: NewCredential): Promise<CredentialWriteResult> {
  return credentialWrite('', 'POST', credential);
}
export function updateLocalCredential(
  name: string,
  update: CredentialUpdate,
): Promise<CredentialWriteResult> {
  return credentialWrite(`/${encodeURIComponent(name)}/local`, 'PATCH', update);
}
export function getCredentialGrants(): Promise<CredentialGrantsSettings> {
  return apiFetch('/settings/credentials');
}
export function saveCredentialGrant(
  name: string,
  grant: CredentialGrant,
): Promise<{ ok: boolean }> {
  return apiFetch(`/settings/credentials/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(grant),
  });
}
export function revokeCredentialGrant(name: string): Promise<{ ok: boolean }> {
  return apiFetch(`/settings/credentials/${encodeURIComponent(name)}`, { method: 'DELETE' });
}
export function deleteCredential(name: string): Promise<{ ok: boolean; deleted: boolean }> {
  return apiFetch(`/settings/credentials/${encodeURIComponent(name)}/value`, { method: 'DELETE' });
}
export function grantExistingCredentials(): Promise<{ ok: boolean; count: number }> {
  return apiFetch('/settings/credentials/grant-existing', { method: 'POST' });
}
export type StepUpAction =
  | 'export'
  | 'recovery_reveal'
  | 'backup_recipient'
  | 'history_restore'
  | 'history_purge'
  | 'admin_config_change'
  | 'admin_briefing_delete'
  | 'admin_cron_delete'
  | 'admin_browser_console';
/** How a sensitive action is confirmed: the account password, or a mailed
 *  code. `methods` is every one the account has, the server's default first;
 *  `method` is the one this request opened. */
export type StepUpMethod = 'email' | 'password';
export type StepUpProof =
  { request_id: string; code: string } | { request_id: string; password: string };
export interface StepUpStart {
  method: StepUpMethod;
  methods?: StepUpMethod[];
  request_id: string;
  expires_at: string;
  email_hint?: string;
}
export interface CredentialHistory {
  id: number;
  name: string;
  op: string;
  actor: string;
  at: string;
  fields: string[];
}
export interface CredentialActivityRow {
  id: number;
  name: string | null;
  action: string;
  actor: string;
  at: string;
  detail: Record<string, string | number | boolean | null> | null;
}

/** Opens a confirmation. With no `method` the server picks its default and
 *  mails nothing unless a code is the only way; `'email'` mails a code. */
export function startStepUp(
  action: StepUpAction,
  name?: string,
  method?: StepUpMethod,
): Promise<StepUpStart> {
  return credentialWrite('', 'POST', { action, name, method }, '/settings/step-up');
}
export function showRecoveryCodes(
  name: string,
  step_up: StepUpProof,
): Promise<{ codes: string[]; spent: number[]; format: string }> {
  return credentialWrite(`/${encodeURIComponent(name)}/recovery`, 'POST', { step_up });
}
export function getCredentialHistory(name: string): Promise<CredentialHistory[]> {
  return credentialWrite(`/${encodeURIComponent(name)}/history`, 'GET', undefined);
}
export function getDeletedCredentials(): Promise<CredentialHistory[]> {
  return credentialWrite('/deleted', 'GET', undefined);
}
export function getCredentialActivity(): Promise<CredentialActivityRow[]> {
  return credentialWrite('/activity', 'GET', undefined);
}
export function restoreCredentialHistory(
  id: number,
  step_up: StepUpProof,
): Promise<{ restored: string[] }> {
  return credentialWrite(`/history/${id}/restore`, 'POST', { step_up });
}
export function purgeCredentialHistory(
  name: string,
  step_up: StepUpProof,
): Promise<{ purged: number }> {
  return credentialWrite(`/${encodeURIComponent(name)}/history`, 'DELETE', { step_up });
}

export interface WalletBilling {
  line1: string;
  line2: string;
  city: string;
  region: string;
  postcode: string;
  country: string;
}
export interface WalletCard {
  id: number;
  label: string;
  brand: string;
  last_four: string;
  exp_month: number;
  exp_year: number;
  name: string;
  billing: WalletBilling;
  state: 'active' | 'paused';
  issuer: string;
}
export type WalletCardDetails = Pick<
  WalletCard,
  'label' | 'exp_month' | 'exp_year' | 'name' | 'billing'
>;
export interface WalletPolicy {
  currency: string;
  auto_limit_cents: number;
  auto_budget_cents: number;
  ceiling_cents: number | null;
  allow_scheduled: boolean;
}
export interface WalletPurchase {
  id: number;
  task_id: number;
  room_token: string | null;
  card_id: number | null;
  card_label: string;
  merchant_host: string;
  amount_cents: number;
  currency: string;
  approval: 'auto' | 'user' | null;
  state:
    | 'refused'
    | 'held'
    | 'authorized'
    | 'filled'
    | 'completed'
    | 'failed'
    | 'expired'
    | 'unreported'
    | 'declined'
    | 'cancelled';
  created_at: string;
}
export interface WalletSettings {
  currency_precision: { default: number; exceptions: Record<string, number> };
  refusal: string | null;
  cards: WalletCard[];
  policy: WalletPolicy;
  purchases: WalletPurchase[];
}
export interface NetworkRefusal {
  entry: string;
  hits: number;
  last_seen: string;
}
export interface NetworkJob {
  digest: string;
  reach: 'private' | 'public';
  prompt: string;
  command: string;
  skill: string;
  skill_args: string;
}
export interface NetworkSubject {
  subject: string;
  kind: string;
  label: string;
  state: 'open' | 'public' | 'limited';
  hosts: string[];
  skills: string[];
  approvals: string[];
  job: NetworkJob | null;
  refused: NetworkRefusal[];
}
export interface NetworkAccess {
  subjects: NetworkSubject[];
  external_skills: string[];
}
export function getNetworkAccess(): Promise<NetworkAccess> {
  return apiFetch('/settings/network');
}
function postNetwork(path: string, body: Record<string, string>): Promise<{ ok: boolean }> {
  return apiFetch(`/settings/network${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
export function addNetworkGrant(subject: string, entry: string): Promise<{ ok: boolean }> {
  return postNetwork('/grants', { subject, entry });
}
export function approveNetworkJob(
  subject: string,
  kind: 'open' | 'public',
  digest: string,
): Promise<{ ok: boolean }> {
  return postNetwork('/approvals', { subject, kind, digest });
}
export function removeNetworkGrant(subject: string, entry: string): Promise<{ ok: boolean }> {
  return postNetwork('/grants/remove', { subject, entry });
}
export function dismissNetworkRefusal(subject: string, entry: string): Promise<{ ok: boolean }> {
  return postNetwork('/refused/dismiss', { subject, entry });
}
export function getWallet(): Promise<WalletSettings> {
  return apiFetch('/settings/wallet');
}
export function addWalletCard(
  card: WalletCardDetails & { number: string; cvc: string },
): Promise<{ id: number }> {
  return apiFetch('/settings/wallet/cards', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(card),
  });
}
export function updateWalletCard(
  id: number,
  card: Partial<WalletCardDetails> & { state?: WalletCard['state'] },
): Promise<{ ok: boolean }> {
  return apiFetch(`/settings/wallet/cards/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(card),
  });
}
export function removeWalletCard(id: number): Promise<{ ok: boolean; cancelled: number }> {
  return apiFetch(`/settings/wallet/cards/${id}`, { method: 'DELETE' });
}
export function saveWalletPolicy(policy: WalletPolicy): Promise<{ ok: boolean }> {
  return apiFetch('/settings/wallet/policy', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(policy),
  });
}
export function cancelWalletPurchase(id: number): Promise<{ ok: boolean }> {
  return apiFetch(`/settings/wallet/purchases/${id}/cancel`, { method: 'POST' });
}

export type MailSecurity = 'tls' | 'starttls';
export interface MailAccountSmtp {
  host: string;
  port: number;
  security: MailSecurity;
  /** Sign in to SMTP with the IMAP username and password. */
  same_login: boolean;
  username: string;
}
/** A mailbox the user connected. Passwords never come back, only whether one is stored. */
export interface MailAccount {
  id: number;
  slug: string;
  label: string;
  permission: 'read' | 'read_send';
  imap_host: string;
  imap_port: number;
  imap_security: MailSecurity;
  imap_username: string;
  default_folder: string;
  smtp: MailAccountSmtp | null;
  from_address: string;
  from_name: string;
  sent_folder: string;
  enabled: boolean;
  imap_password_set: boolean;
  smtp_password_set: boolean;
  pending_drafts: number;
  updated_at: string | null;
}
export interface MailAccountsSettings {
  enabled: boolean;
  allow_send: boolean;
  max_per_user: number;
  /** Why writes are refused on this deployment (the vault isolation gate), else null. */
  refusal: string | null;
  accounts: MailAccount[];
}
/** A create or edit body. A password left out or null keeps the stored one; `smtp: null` removes sending. */
export type MailAccountInput = Partial<
  Pick<
    MailAccount,
    | 'slug'
    | 'label'
    | 'permission'
    | 'imap_host'
    | 'imap_port'
    | 'imap_security'
    | 'imap_username'
    | 'default_folder'
    | 'from_address'
    | 'from_name'
    | 'sent_folder'
    | 'enabled'
  >
> & {
  smtp?: MailAccountSmtp | null;
  imap_password?: string | null;
  smtp_password?: string | null;
};
export interface MailAccountTestResult {
  ok: boolean;
  message: string;
  lines: { step: string; ok: boolean; message: string }[];
}
export type MailAccountTestState =
  | { status: 'pending' }
  | { status: 'answered'; result: MailAccountTestResult }
  | { status: 'unknown' };

/** 404 when the operator has switched connected mail accounts off. */
export function getMailAccounts(): Promise<MailAccountsSettings> {
  return apiFetch('/settings/mail-accounts');
}
export function createMailAccount(input: MailAccountInput): Promise<{ account: MailAccount }> {
  return apiFetch('/settings/mail-accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}
export function updateMailAccount(
  id: number,
  input: MailAccountInput,
): Promise<{ account: MailAccount }> {
  return apiFetch(`/settings/mail-accounts/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}
export function deleteMailAccount(
  id: number,
): Promise<{ deleted: boolean; drafts_discarded: number }> {
  return apiFetch(`/settings/mail-accounts/${id}`, { method: 'DELETE' });
}
/** Leaves a connection test for the daemon; poll `getMailAccountTest` with the id. */
export function requestMailAccountTest(id: number): Promise<{ request_id: string }> {
  return apiFetch(`/settings/mail-accounts/${id}/test`, { method: 'POST' });
}
/** An answered test is removed once read, so a second read of it is `unknown`. */
export function getMailAccountTest(requestId: string): Promise<MailAccountTestState> {
  return apiFetch(`/settings/mail-accounts/test-requests/${encodeURIComponent(requestId)}`);
}

/** A calendar the user shared with Istota by its feed address. The address never comes back. */
export interface SharedFeed {
  id: number;
  host: string;
  /** The calendar's own name, from the provider; null when it has none. */
  name: string | null;
  state: 'active' | 'error';
  /** A fixed reason code for the last failed refresh, such as `http_404`. */
  last_reason: string | null;
  last_ok_at: string | null;
  created_at: string | null;
}

export type CalendarDelivery = 'invite' | 'feed';

/** Istota's own calendar: how its events reach the user, and the feed link's state. */
export interface IstotaCalendarSettings {
  delivery: CalendarDelivery;
  /** What applies now: `feed` whenever the server sends no email. */
  effective_delivery: CalendarDelivery;
  email_enabled: boolean;
  /** The feed link's dates, never the link; null when there is none. */
  feed: { created_at: string; last_used_at: string | null } | null;
  /** False when the server has no public name to put in a link. */
  can_link: boolean;
  /** False on a loopback name, where only this machine can read the feed. */
  reachable_elsewhere: boolean;
}

/** Who offered a share, by federated id and display name. Every field is the sender's words. */
export interface SharedFolderPeople {
  /** The owner's federated id, as the sender gave it. */
  owner: string | null;
  owner_display_name?: string | null;
  /** Who shared it, when that is not the owner; null otherwise. */
  shared_by?: string | null;
  shared_by_display_name?: string | null;
}

/** A folder someone offered to Istota over Open Cloud Mesh. Every field is the sender's words. */
export interface SharedFolderOffer extends SharedFolderPeople {
  id: number;
  name: string | null;
  host: string;
  /** True for a new offer of a folder that is already shared. */
  reoffer: boolean;
  /** Whether the sending server signed the offer; null on one stored before this was recorded. */
  signed?: boolean | null;
  created_at: string | null;
}

/** A folder share the user accepted. Name and owner are the sender's words. */
export interface SharedFolder extends SharedFolderPeople {
  id: number;
  name: string | null;
  host: string;
  /** What the provider last said the share allows. */
  access: 'read' | 'write';
  state: 'active' | 'error';
  /** A file or a folder, once its root has been read; null before. */
  kind?: 'file' | 'folder' | null;
  last_reason: string | null;
  created_at: string | null;
  accepted_at?: string | null;
}

/** Folder shares over OCM: the address to share with, the offers waiting, the shares accepted. */
export interface SharedFolderSettings {
  /** False when the server stopped receiving shares; accepted ones stay removable. */
  enabled?: boolean;
  /** Null while the server receives no shares. */
  federated_id: string | null;
  pending: SharedFolderOffer[];
  shares?: SharedFolder[];
  /** Why accepting is refused here (an unsandboxed multi-user server), or null. */
  accept_refusal?: string | null;
}

export interface SharingSettings {
  feeds: SharedFeed[];
  max_feeds: number;
  /** False when the credential store is not configured, so no address can be stored. */
  can_add: boolean;
  /** Absent on a server that predates Istota's own calendar. */
  calendar?: IstotaCalendarSettings;
  /** Null or absent while the server receives no folder shares. */
  ocm?: SharedFolderSettings | null;
}

export function getSharing(): Promise<SharingSettings> {
  return apiFetch('/settings/sharing');
}
/** Fetches the address once on the server and stores it; a refusal carries a `reason`. */
export function addSharedFeed(url: string): Promise<{ feed: SharedFeed }> {
  return apiFetch('/settings/sharing/feeds', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
}
export function removeSharedFeed(id: number): Promise<{ removed: boolean }> {
  return apiFetch(`/settings/sharing/feeds/${id}`, { method: 'DELETE' });
}
/** Opens the share once to prove it, then stores it; a refusal carries a `reason`. */
export function acceptSharedFolder(
  id: number,
  allowScheduled: boolean,
): Promise<{ share: SharedFolder; notified: boolean }> {
  return apiFetch(`/settings/sharing/folders/${id}/accept`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allow_scheduled: allowScheduled }),
  });
}
export function declineSharedFolder(id: number): Promise<{ notified: boolean }> {
  return apiFetch(`/settings/sharing/folders/${id}/decline`, { method: 'POST' });
}
export function removeSharedFolder(id: number): Promise<{ notified: boolean }> {
  return apiFetch(`/settings/sharing/folders/${id}`, { method: 'DELETE' });
}
/** Makes the Istota calendar feed link, or replaces it; the address is returned this once. */
export function createCalendarFeedLink(): Promise<{ url: string }> {
  return apiFetch('/settings/sharing/calendar-feed', { method: 'POST' });
}
export function removeCalendarFeedLink(): Promise<{ removed: boolean }> {
  return apiFetch('/settings/sharing/calendar-feed', { method: 'DELETE' });
}
export function setCalendarDelivery(
  delivery: CalendarDelivery,
): Promise<{ delivery: CalendarDelivery }> {
  return apiFetch('/settings/sharing/delivery', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ delivery }),
  });
}

export interface KeepassImportItem {
  name: string;
  origin: 'entry' | 'generated';
  fields: string[];
  hosts: string[];
  status: 'new' | 'changed' | 'unchanged' | 'conflict' | 'skipped';
  changed_fields: string[];
  reason: string | null;
  default_selected: boolean;
}

export interface KeepassImportPreview {
  digest: string;
  scoped: boolean;
  truncated: string;
  items: KeepassImportItem[];
  skipped: Record<string, number>;
}

export interface KeepassImportResult {
  imported: string[];
  not_imported: Record<string, string>;
}

function keepassImportForm(file: File, passphrase: string, keyfile: File | null): FormData {
  const form = new FormData();
  form.append('file', file);
  form.append('passphrase', passphrase);
  if (keyfile) form.append('keyfile', keyfile);
  return form;
}

export function previewKeepassImport(
  file: File,
  passphrase: string,
  keyfile: File | null,
  signal?: AbortSignal,
): Promise<KeepassImportPreview> {
  return apiFetch('/settings/credentials/import/preview', {
    method: 'POST',
    body: keepassImportForm(file, passphrase, keyfile),
    signal,
  });
}

export function applyKeepassImport(
  file: File,
  passphrase: string,
  keyfile: File | null,
  selected: string[],
  digest: string,
  signal?: AbortSignal,
): Promise<KeepassImportResult> {
  const form = keepassImportForm(file, passphrase, keyfile);
  form.append('selected', JSON.stringify(selected));
  form.append('digest', digest);
  return apiFetch('/settings/credentials/import', { method: 'POST', body: form, signal });
}

export interface KeepassExport {
  filename: string;
  password: string;
  file: string;
  summary: { credentials: number; generated: number; otp: number; recovery: number };
}
export function exportKeepass(
  keyfile: string | null,
  step_up: StepUpProof,
  signal?: AbortSignal,
): Promise<KeepassExport> {
  return credentialWrite('/export', 'POST', { keyfile, step_up }, '/settings/credentials', signal);
}

export interface CredentialBackupSettings {
  recipient_suffix: string | null;
  last_run: { at: string; outcome: string; reason: string | null; file: string | null } | null;
  interval: number;
  available: boolean;
}
export function getCredentialBackup(): Promise<CredentialBackupSettings> {
  return apiFetch('/settings/credentials/backup');
}
export function setCredentialBackup(
  recipient: string | null,
  step_up: StepUpProof,
): Promise<{ recipient_suffix: string | null }> {
  return credentialWrite('/backup', 'PUT', { recipient, step_up }, '/settings/credentials');
}

export type SearchLink =
  { type: 'route'; path: string; params: Record<string, string> } | { type: 'file'; path: string };
export interface SearchHit {
  cursor?: { ts: string; id: number } | null;
  id: string;
  kind: string;
  title: string;
  subtitle: string | null;
  snippet: string;
  highlights: number[][];
  date: string | null;
  link: SearchLink | null;
  badges: string[];
}
export interface SearchGroup {
  source: string;
  label: string;
  results: SearchHit[];
  has_more: boolean;
  relaxed: boolean;
  error: 'timeout' | 'failed' | null;
  elapsed_ms: number;
}
export interface SearchResponse {
  query: string;
  groups: SearchGroup[];
  on_demand?: { source: string; label: string }[];
}
export function search(
  q: string,
  options: { sources?: string[]; limit?: number; offset?: number; signal?: AbortSignal } = {},
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q });
  if (options.sources) params.set('sources', options.sources.join(','));
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.offset !== undefined) params.set('offset', String(options.offset));
  return apiFetch(`/search?${params}`, { signal: options.signal });
}

// ---- First-run setup (`webui/setup_app.py`) ----
//
// The setup server runs before any config exists and serves only these
// routes. Its errors are `{error: <code>, message, fields?, step?}`, and a 401
// means "locked" rather than "signed out", so these do not go through
// `apiFetch`: that would read the code as the message, send a 401 to the login
// page, and report into a connectivity store the setup page never starts.

/** `configured` is the normal web app's answer: there is nothing to set up. */
export type SetupPhase = 'locked' | 'open' | 'finishing' | 'configured';

export interface SetupState {
  phase: SetupPhase;
  /** A re-run over a configured stack (`istota setup --web --force`). */
  rerun?: boolean;
  /** A re-run's username, sent only to the open session. */
  user_id?: string;
  /** Plain HTTP on this machine (`--local`) rather than the TLS setup port. */
  local?: boolean;
  /** The `Host` the browser used, for the web address screen. */
  host?: string;
  /** Where "Only on this computer" answers: `localhost:<the port the VM forwards>`. */
  local_address?: string;
  /** The module toggles this image can offer; `field` is the answer's name. */
  modules_available?: { field: string; module: string }[];
  memory_total_mib?: number;
  defaults?: {
    bot_name: string;
    storage: string;
    profiles: string[];
    /** Each API-key service's address and default model. */
    models?: SetupModelDefaults;
  };
}

export interface SetupModelService {
  base_url: string;
  model: string;
}
export interface SetupModelDefaults {
  anthropic: SetupModelService;
  openrouter: SetupModelService;
}

/** A credential the server holds: never its value, only whether it is set. */
export interface SetupCredential {
  set: boolean;
}
export type SetupDraftValue = string | number | boolean | string[] | SetupCredential;
export type SetupDraft = Record<string, SetupDraftValue>;
/** A `PUT` merges. `null` drops an answer, but keeps a stored credential. */
export type SetupDraftPatch = Record<string, string | number | boolean | string[] | null>;

export interface SetupFieldError {
  /** An answer field, or `""` for an error about the form as a whole. */
  field: string;
  message: string;
  /** On a refused Finish: the screen that asks this field. */
  step?: string;
}

export type SetupCheckKind = 'model' | 'nextcloud' | 'email' | 'caldav' | 'address';
export interface SetupCheckLine {
  key: string;
  ok: boolean;
  warning: boolean;
  message: string;
}
export interface SetupCheckResult {
  ok: boolean;
  /** Passed, with something the user should read. */
  warning: boolean;
  message: string;
  /** The Nextcloud check's per-step results. */
  lines?: SetupCheckLine[];
  /** The Nextcloud check: whether Talk is installed there. */
  talk_available?: boolean;
}
export interface SetupValidation {
  ok: boolean;
  fields: SetupFieldError[];
}
export interface SetupFinished {
  login_url: string;
  /** Absent on a re-run, which signs nobody in afresh. */
  email?: string;
}

const SETUP_UNREACHABLE = "Can't reach the setup service.";

/**
 * A refused or failed setup request. `status` 0 is no answer at all. `code` is
 * the server's `error`; `fields` go under their inputs and `message` in the
 * screen's banner.
 */
export class SetupApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: SetupFieldError[];
  readonly step: string | null;
  constructor(
    status: number,
    code: string,
    message: string,
    fields: SetupFieldError[] = [],
    step: string | null = null,
  ) {
    super(message);
    this.name = 'SetupApiError';
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.step = step;
  }
}

function setupFieldErrors(raw: unknown): SetupFieldError[] {
  if (!Array.isArray(raw)) return [];
  const fields: SetupFieldError[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const { field, message, step } = item as Record<string, unknown>;
    if (typeof field !== 'string' || typeof message !== 'string') continue;
    fields.push(typeof step === 'string' ? { field, message, step } : { field, message });
  }
  return fields;
}

async function setupFetch<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${base}/api/setup${path}`, {
      method,
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new SetupApiError(0, 'unreachable', SETUP_UNREACHABLE);
  }
  let payload: unknown = null;
  try {
    payload = await resp.json();
  } catch {
    payload = null;
  }
  if (!resp.ok || payload === null) {
    const raw = (payload ?? {}) as Record<string, unknown>;
    const message =
      typeof raw.message === 'string' && raw.message.trim()
        ? raw.message
        : `The setup service answered ${resp.status}.`;
    throw new SetupApiError(
      resp.status,
      typeof raw.error === 'string' ? raw.error : 'failed',
      message,
      setupFieldErrors(raw.fields),
      typeof raw.step === 'string' ? raw.step : null,
    );
  }
  return payload as T;
}

export function getSetupState(): Promise<SetupState> {
  return setupFetch('/state');
}

export async function unlockSetup(code: string): Promise<void> {
  await setupFetch('/unlock', 'POST', { code });
}

export function getSetupDraft(): Promise<{ draft: SetupDraft }> {
  return setupFetch('/draft');
}

export function putSetupDraft(patch: SetupDraftPatch): Promise<{ draft: SetupDraft }> {
  return setupFetch('/draft', 'PUT', patch);
}

/** The saved draft's errors on one screen's fields. */
export function validateSetupStep(step: string): Promise<SetupValidation> {
  return setupFetch('/validate', 'POST', { step });
}

/** OpenRouter's model ids, for suggestions only; `[]` when there are none. */
export async function getSetupModels(): Promise<string[]> {
  const { models } = await setupFetch<{ models: string[] }>('/models');
  return Array.isArray(models) ? models.filter((m) => typeof m === 'string') : [];
}

/** A live test of what the saved draft would write; it takes no body. */
export function checkSetupConnection(kind: SetupCheckKind): Promise<SetupCheckResult> {
  return setupFetch(`/check/${kind}`, 'POST');
}

export function finishSetup(): Promise<SetupFinished> {
  return setupFetch('/finish', 'POST');
}

// Workspace places and safe, one-directory listings.
export type WorkspaceAccess = 'rw' | 'ro' | 'skill' | 'none' | 'conditional';
export interface WorkspacePlace {
  id: string;
  kind: 'home' | 'linked' | 'room' | 'group' | 'drive' | 'nextcloud' | 'share';
  label: string;
  browsable: boolean;
  view_root: string | null;
  /** A share's provider host, last recorded access, and file or folder once read; `share` places only. */
  remote?: { host: string; access: 'read' | 'write'; kind?: 'file' | 'folder' | null };
  reach: {
    private: WorkspaceAccess;
    shared_member: WorkspaceAccess;
    restricted: WorkspaceAccess;
    note: string | null;
  };
}
export interface WorkspaceEntry {
  view_path?: string;
  name: string;
  kind: 'dir' | 'file' | 'link' | 'other';
  size: number | null;
  modified: number;
  hidden: boolean;
  share?: { kind: 'user' | 'group' | 'link'; count: number } | null;
  nextcloud_url?: string | null;
  /** In a share from another server: the path to ask for, relative to the share (`''` for a file share). */
  share_path?: string;
}
export interface WorkspaceListing {
  notes?: string[];
  place: string;
  path: string;
  entries: WorkspaceEntry[];
  truncated: boolean;
  skipped_names: number;
  /** A share from another server: what its provider says it allows, read for this listing. */
  access?: 'read' | 'write';
  root_kind?: 'file' | 'folder';
}
export interface WorkspacePlaces {
  places: WorkspacePlace[];
  notes: string[];
  /** Whether the index of shares from other servers is offered; absent from an older server. */
  share_index?: boolean;
}
export function getWorkspacePlaces(): Promise<WorkspacePlaces> {
  return apiFetch('/workspace/places');
}
export function getWorkspaceListing(place: string, path = ''): Promise<WorkspaceListing> {
  return apiFetch(`/workspace/list?${new URLSearchParams({ place, path })}`);
}

export interface DriveItem {
  id: string;
  name: string;
  mime_type: string;
  modified: string | null;
  size: number | null;
  web_url: string | null;
  is_folder: boolean;
}
export interface DriveListing {
  items: DriveItem[];
  next_page_token: string | null;
}
export function getWorkspaceDriveListing(
  folder = 'root',
  pageToken?: string,
): Promise<DriveListing> {
  return apiFetch(
    `/workspace/drive/list?${new URLSearchParams({ folder, ...(pageToken ? { page_token: pageToken } : {}) })}`,
  );
}

export function deleteWorkspaceEntry(path: string, name: string): Promise<{ status: string }> {
  return apiFetch(`/workspace/files?${new URLSearchParams({ place: 'home', path, name })}`, {
    method: 'DELETE',
  });
}

export function createWorkspaceFolder(path: string, name: string): Promise<{ path: string }> {
  return apiFetch('/workspace/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ place: 'home', path, name }),
  });
}

export function uploadWorkspaceFile(
  path: string,
  file: File,
  onProgress: (percent: number) => void,
): Promise<{ name: string; path: string; size: number }> {
  const form = new FormData();
  form.append('place', 'home');
  form.append('path', path);
  form.append('file', file);
  // XHR exposes upload progress; fetch does not.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${base}/api/workspace/upload`);
    xhr.timeout = 120_000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () => reject(new UploadUnreachableError());
    xhr.ontimeout = () => reject(new Error('Upload timed out.'));
    xhr.onload = async () => {
      if (xhr.status === 401) {
        reject(new AuthError());
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(
          new Error(
            await errorMessage(new Response(xhr.responseText, { status: xhr.status || 500 })),
          ),
        );
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText));
      } catch {
        reject(new Error('Could not read the upload response.'));
      }
    };
    xhr.send(form);
  });
}

export interface WorkspacePinInput {
  provider: 'workspace' | 'drive';
  ref: string;
  name: string;
  mime_type: string | null;
  web_url: string | null;
}
export interface WorkspacePin extends WorkspacePinInput {
  id: number;
  available: boolean;
  is_dir: boolean;
}
export function getWorkspacePins(): Promise<{ pins: WorkspacePin[] }> {
  return apiFetch('/workspace/pins');
}

export function getWorkspaceDriveParent(fileId: string): Promise<{ folder: string }> {
  return apiFetch(`/workspace/drive/parent?${new URLSearchParams({ file_id: fileId })}`);
}
export function putWorkspacePin(pin: WorkspacePinInput): Promise<{ pin: WorkspacePin }> {
  return apiFetch('/workspace/pins', { method: 'PUT', body: JSON.stringify(pin) });
}
export function deleteWorkspacePin(id: number): Promise<{ status: string }> {
  return apiFetch(`/workspace/pins/${id}`, { method: 'DELETE' });
}

export interface WorkspaceScriptRef {
  source: 'cron' | 'heartbeat';
  name: string;
  field: 'command' | 'prompt_file' | 'prompt';
  kind: 'runs' | 'prompt_file' | 'mentions';
  state: 'enabled' | 'disabled' | 'suspended' | 'not_synced' | 'admin_only';
}
export interface WorkspaceScript {
  path: string;
  kind: 'script' | 'prompt';
  size: number;
  modified: string;
  description: string | null;
  referenced_by: WorkspaceScriptRef[];
  mentioned_in: WorkspaceScriptRef[];
}
export interface WorkspaceScriptsIndex {
  scripts_view_path: string;
  files: WorkspaceScript[];
  broken: { path: string; referenced_by: WorkspaceScriptRef[] }[];
  truncated: boolean;
  sources: {
    cron: 'ok' | 'missing' | 'error';
    heartbeat: 'ok' | 'missing' | 'error';
    scripts: 'ok' | 'refused';
  };
}
export function getWorkspaceScripts(): Promise<WorkspaceScriptsIndex> {
  return apiFetch('/workspace/scripts');
}

/** One of the user's own config files: USER.md, CRON.md, TASKS.md,
 * HEARTBEAT.md, or `skills/<skill>` for a skill's overlay. */
export interface WorkspaceConfigFile {
  name: string;
  view_path?: string | null;
  kind: 'config' | 'skill';
  exists: boolean;
  size: number | null;
  /** False when the file is something other than a plain file (a link a
   * task planted), which the server will refuse to write. */
  editable: boolean;
}
export interface WorkspaceConfigFileContent {
  content: string;
  revision: string;
  exists: boolean;
}
function configFilePath(name: string): string {
  return `/workspace/config-files/${name.split('/').map(encodeURIComponent).join('/')}`;
}
export function getWorkspaceConfigFiles(): Promise<{ files: WorkspaceConfigFile[] }> {
  return apiFetch('/workspace/config-files');
}
export function getWorkspaceConfigFile(name: string): Promise<WorkspaceConfigFileContent> {
  return apiFetch(configFilePath(name));
}
/** `applies: 'next_sync'` on a CRON.md save: the scheduler picks the jobs up
 * at its next CRON.md sync, not at once. */
export function saveWorkspaceConfigFile(
  name: string,
  content: string,
  revision: string,
): Promise<{ revision: string; applies?: 'next_sync' }> {
  return putTextFile(configFilePath(name), { content, revision });
}

/** Files above this size open in the viewer only; the server holds the same cap. */
export const WORKSPACE_TEXT_EDIT_MAX_BYTES = 1_048_576;

/** A text file in My files. The server refuses anything that is not text by
 * its bytes (415), a link (422) and the bot's config folder (403). */
export function getWorkspaceTextFile(
  path: string,
  name: string,
): Promise<WorkspaceConfigFileContent> {
  return apiFetch(`/workspace/files?${new URLSearchParams({ place: 'home', path, name })}`);
}
export function saveWorkspaceTextFile(
  path: string,
  name: string,
  content: string,
  revision: string,
): Promise<{ revision: string }> {
  return putTextFile('/workspace/files', { place: 'home', path, name, content, revision });
}

async function putTextFile<T extends { revision: string }>(
  path: string,
  payload: Record<string, string>,
): Promise<T> {
  const resp = await fetch(`${base}/api${path}`, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (resp.status === 401) throw new AuthError();
  if (resp.ok) return resp.json();
  const body = await resp.json().catch(() => ({}));
  if (resp.status === 409) throw new TextFileConflictError();
  if (resp.status === 423) throw new TextFileLockedError();
  if (resp.status === 422 && Array.isArray(body.errors))
    throw new TextFileInvalidError(body.errors);
  if (resp.status === 413 && typeof body.max_bytes === 'number')
    throw new Error(`Too long — the limit is ${Math.floor(body.max_bytes / 1024)} KB.`);
  throw new Error(await errorMessage(resp, body));
}

/** A quick note the server wrote. `path` is relative to My files. */
export interface QuickNoteCreated {
  path: string;
  /** The file name, `Buy milk.md`. Fixed at creation. */
  name: string;
  /** Null on a replay whose file has since been moved or deleted. */
  revision: string | null;
  /** False when `client_note_id` replayed a note already written. */
  created: boolean;
}

/**
 * A quick note refusal with its stable `code`: `empty`, `too_large`,
 * `notes_folder_unset`, `in_progress`, `outside_folder`, `invalid_folder`,
 * `missing`, and the rest the server documents.
 */
export class QuickNoteError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'QuickNoteError';
  }
}

async function quickNoteRequest<T>(method: 'POST' | 'PUT', payload: unknown): Promise<T> {
  try {
    return await apiFetch<T>('/workspace/notes', {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    if (e instanceof AuthError) throw e;
    const status = (e as { status?: unknown }).status;
    if (typeof status !== 'number') throw e;
    const body = (e as { body?: { code?: unknown } | null }).body;
    const code = typeof body?.code === 'string' ? body.code : '';
    if (method === 'PUT' && code === 'conflict') throw new TextFileConflictError();
    if (method === 'PUT' && code === 'locked') throw new TextFileLockedError();
    throw new QuickNoteError(status, code, (e as Error).message);
  }
}

/**
 * Create a note in the quick notes folder. `clientNoteId` makes a retry after
 * a lost response return the note already written instead of a second file.
 */
export function createQuickNote(content: string, clientNoteId?: string): Promise<QuickNoteCreated> {
  return quickNoteRequest('POST', {
    content,
    ...(clientNoteId ? { client_note_id: clientNoteId } : {}),
  });
}

/**
 * Replace a quick note this editor created. A stale revision is a
 * `TextFileConflictError`, a held lock a `TextFileLockedError`; the server
 * never creates a file here.
 */
export function saveQuickNote(
  path: string,
  content: string,
  revision: string,
): Promise<{ revision: string }> {
  return quickNoteRequest('PUT', { path, content, revision });
}

export interface GroupCapabilities {
  member: boolean;
  owner: boolean;
  manage: boolean;
  archive: boolean;
  read_memory: boolean;
  edit_memory: boolean;
  leave: boolean;
  read_briefings: boolean;
  author_briefings: boolean;
  receive_briefings: boolean;
}
export interface GroupSummary {
  group_id: string;
  system_key?: 'all_users' | null;
  display_name: string;
  kind: string;
  revision: number;
  role: 'owner' | 'member' | null;
  capabilities: GroupCapabilities;
}
export interface GroupMember {
  user_id: string;
  display_name: string;
  role: 'owner' | 'member';
}
export interface GroupDisclosure {
  memory: string;
  rooms: string[];
  briefings: string[];
}
/** A room linked to a group that the caller can open. `group` rooms hold the
 * whole group; `room` rooms keep their own roster. `room_id` is the caller's
 * chat handle, null until one has been minted. */
export interface GroupRoom {
  token: string;
  name: string;
  origin: string;
  membership_source?: 'group' | 'room';
  room_id?: number | null;
}
export interface GroupDetail extends GroupSummary {
  members: GroupMember[];
  rooms: GroupRoom[];
  disclosure: GroupDisclosure;
}
export interface GroupRoomCreated {
  group_id: string;
  created: boolean;
  revision: number;
  room: GroupRoom & { room_id: number };
}
export interface GroupMemoryChange {
  id: string;
  user_id?: string;
  ts?: string;
  source?: string;
  task_id?: number | null;
  applied?: { op?: { op?: string }; outcome?: string }[];
  rejected?: { op?: { op?: string }; outcome?: string; reason?: string }[];
}
export interface GroupMemory {
  group_id: string;
  status: 'readable' | 'missing';
  content: string;
  revision: string;
  bytes: number;
  max_bytes: number;
  large: boolean;
  last_change: GroupMemoryChange | null;
}
export interface GroupMembershipInput {
  revision: number;
  role: 'owner' | 'member';
  acknowledge_history?: boolean;
  replacement_owner?: string;
}
export class GroupApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly reason?: string,
    readonly disclosure?: GroupDisclosure,
  ) {
    super(message);
    this.name = 'GroupApiError';
  }
}
// Group forms need refusal codes and disclosure; apiFetch retains only a message.
async function groupRequest<T>(path: string, method = 'GET', payload?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}/api${path}`, {
      method,
      credentials: 'same-origin',
      ...(payload === undefined
        ? {}
        : {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }),
    });
  } catch (error) {
    noteTransport(false, 'unreachable');
    throw error;
  }
  noteTransport(true);
  if (response.status === 401) throw new AuthError();
  const body = await response.json().catch(() => ({}));
  if (response.ok) return body;
  throw new GroupApiError(
    response.status,
    body.error ?? '',
    typeof body.message === 'string' ? body.message : await errorMessage(response, body),
    body.reason,
    body.disclosure,
  );
}
function groupPath(id: string): string {
  return `/groups/${encodeURIComponent(id)}`;
}
export function getGroups(): Promise<{ groups: GroupSummary[] }> {
  return groupRequest('/groups');
}
export function getGroup(id: string): Promise<GroupDetail> {
  return groupRequest(groupPath(id));
}
export function updateGroup(
  id: string,
  payload: { revision: number; display_name?: string; kind?: string },
): Promise<GroupSummary> {
  return groupRequest(groupPath(id), 'PATCH', payload);
}
export function getGroupUserOptions(
  id: string,
): Promise<{ users: { user_id: string; display_name: string }[] }> {
  return groupRequest(`${groupPath(id)}/user-options`);
}
export function putGroupMember(
  id: string,
  user: string,
  payload: GroupMembershipInput,
): Promise<GroupSummary> {
  return groupRequest(`${groupPath(id)}/members/${encodeURIComponent(user)}`, 'PUT', payload);
}
export function deleteGroupMember(
  id: string,
  user: string,
  payload: { revision: number; replacement_owner?: string },
): Promise<GroupSummary> {
  return groupRequest(`${groupPath(id)}/members/${encodeURIComponent(user)}`, 'DELETE', payload);
}
/** Create a room for the whole group. `request_id` makes a retry return the
 * same room, so a caller reuses it until a creation succeeds. */
export function createGroupRoom(
  id: string,
  payload: { name: string; request_id: string; revision: number },
): Promise<GroupRoomCreated> {
  return groupRequest(`${groupPath(id)}/rooms`, 'POST', payload);
}
export function getGroupMemory(id: string): Promise<GroupMemory> {
  return groupRequest(`${groupPath(id)}/memory`);
}
export async function saveGroupMemory(
  id: string,
  content: string,
  revision: string,
): Promise<{ status: 'ok'; revision: string; audit_recorded: boolean }> {
  try {
    return await groupRequest(`${groupPath(id)}/memory`, 'PUT', { content, revision });
  } catch (error) {
    if (error instanceof GroupApiError && error.code === 'memory_conflict')
      throw new TextFileConflictError();
    if (error instanceof GroupApiError && error.code === 'memory_locked')
      throw new TextFileLockedError();
    throw error;
  }
}
export function getGroupMemoryActivity(
  id: string,
  cursor?: string,
): Promise<{ entries: GroupMemoryChange[]; next_cursor: string | null }> {
  const query = cursor ? `?${new URLSearchParams({ cursor })}` : '';
  return groupRequest(`${groupPath(id)}/memory/activity${query}`);
}
export function retryGroupStorage(
  id: string,
): Promise<{ group_id: string; storage_available: boolean }> {
  return groupRequest(`/admin/groups/${encodeURIComponent(id)}/storage`, 'POST', {});
}

export interface AdminGroupSummary extends GroupSummary {
  created_by: string;
  created_at: string;
  archived_at: string | null;
  needs_owner: boolean;
}
export interface AdminGroupDetail extends AdminGroupSummary {
  members: GroupMember[];
  disclosure: GroupDisclosure;
  history: (GroupMember & {
    added_at: string;
    added_by: string;
    ended_at: string | null;
    ended_by: string | null;
  })[];
  events: {
    id: number;
    actor_id: string;
    event_type: string;
    subject_user_id: string | null;
    created_at: string;
    details: Record<string, unknown>;
  }[];
}
export function getAdminGroups(): Promise<{ groups: AdminGroupSummary[] }> {
  return groupRequest('/admin/groups');
}
export function getAdminGroup(id: string): Promise<AdminGroupDetail> {
  return groupRequest(`/admin${groupPath(id)}`);
}
export function getAdminGroupUserOptions(): Promise<{
  users: { user_id: string; display_name: string }[];
}> {
  return groupRequest('/admin/groups/user-options');
}
export function createAdminGroup(payload: {
  revision: 0;
  group_id: string;
  display_name: string;
  kind?: string;
  owners: string[];
}): Promise<{ group_id: string; revision: number; storage_available: boolean }> {
  return groupRequest('/admin/groups', 'POST', payload);
}
export function updateAdminGroup(
  id: string,
  payload:
    | { revision: number; display_name?: string; kind?: string }
    | (Omit<GroupMembershipInput, 'role'> & { user_id: string; role: 'owner' | 'member' | null }),
): Promise<{ group_id: string; revision: number }> {
  return groupRequest(`/admin${groupPath(id)}`, 'PATCH', payload);
}
export function archiveAdminGroup(
  id: string,
  payload: { revision: number; confirm: true },
): Promise<{ group_id: string; revision: number; archived_at: string }> {
  return groupRequest(`/admin${groupPath(id)}/archive`, 'POST', payload);
}

/** Admin › Briefings: definitions and state only, never content. */
export interface AdminPersonalBriefing {
  id: number;
  user_id: string;
  owner_label: string;
  name: string;
  title: string;
  cron: string;
  /** The owner's; null for a row whose user has no profile. */
  timezone: string | null;
  output: string;
  enabled: boolean;
  held: BriefingHold | null;
  last_run_at: string | null;
}
export interface AdminGroupBriefing {
  id: number;
  group_id: string;
  group_label: string;
  system_key: string | null;
  name: string;
  title: string;
  cron: string;
  timezone: string;
  channel: string;
  enabled: boolean;
  held: BriefingHold | null;
  owners: string[];
  last_published_at: string | null;
}
export type AdminBriefingKind = 'personal' | 'group';
export function getAdminBriefings(): Promise<{
  personal: AdminPersonalBriefing[];
  group: AdminGroupBriefing[];
}> {
  return groupRequest('/admin/briefings');
}
export function holdAdminBriefing(
  kind: AdminBriefingKind,
  id: number,
): Promise<AdminPersonalBriefing | AdminGroupBriefing> {
  return groupRequest(`/admin/briefings/${kind}/${id}/hold`, 'POST', {});
}
export function resumeAdminBriefing(
  kind: AdminBriefingKind,
  id: number,
): Promise<AdminPersonalBriefing | AdminGroupBriefing> {
  return groupRequest(`/admin/briefings/${kind}/${id}/resume`, 'POST', {});
}
/** `confirm` is the briefing's name typed back. A refused proof is a 403
 *  `GroupApiError` with `reason` set, which `StepUpDialog` reads. */
export function deleteAdminBriefing(
  kind: AdminBriefingKind,
  id: number,
  confirm: string,
  step_up: StepUpProof,
): Promise<{ ok: true; deleted: true; id: number }> {
  return groupRequest(`/admin/briefings/${kind}/${id}`, 'DELETE', { confirm, step_up });
}
// Group briefing authoring uses the same authenticated API boundary as Groups.
export interface BriefingTarget {
  scope: 'user' | 'group';
  group_id?: string;
  label: string;
  system_key?: string | null;
  member_count?: number;
  capabilities: {
    author_briefings: boolean;
    generate_briefings?: boolean;
    read_briefings?: boolean;
    receive_briefings?: boolean;
  };
}
export interface GroupBriefingSource {
  id: string;
  kind: string;
  config: Record<string, unknown>;
}
export interface GroupBriefingGeneration {
  version: number;
  contains_personal_information?: boolean;
  audience_acknowledged?: boolean;
  prompt?: string;
  research?: { enabled: boolean; prompt: string; allowed_domains?: string[] };
  inputs?: GroupBriefingSource[];
  blocks?: {
    title: string;
    directive: string | null;
    render_mode: string;
    options: Record<string, unknown>;
    sources: GroupBriefingSource[];
  }[];
}
export interface BriefingSourceApproval {
  source_id: string;
  owner_id: string;
  resource: { kind: string; config: Record<string, unknown> };
  approved: boolean;
  warning: string;
  audience_contract: Record<string, unknown>;
  grant_revision: number;
  definition_revision: number;
}
export interface GroupBriefingDefinition {
  excluded_user_ids?: string[];
  next_run?: string | null;
  last_run?: { id: number; state: string; published_at: string | null } | null;
  id: number;
  group_id: string;
  name: string;
  title: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  channel: string;
  revision: number;
  generation_mode?: 'prompt' | 'blocks';
  generation?: GroupBriefingGeneration;
  capabilities: BriefingTarget['capabilities'];
  source_approvals?: BriefingSourceApproval[];
  // Set while an administrator holds the briefing; only an administrator
  // lifts it, and the owners cannot enable it meanwhile.
  held?: BriefingHold | null;
  // An administrator deleted it. Its published editions stay readable under
  // the renamed name; nothing else about it is live.
  deleted?: boolean;
}
export type GroupBriefingPayload = Pick<
  GroupBriefingDefinition,
  | 'name'
  | 'title'
  | 'cron'
  | 'timezone'
  | 'enabled'
  | 'channel'
  | 'revision'
  | 'excluded_user_ids'
  | 'generation_mode'
  | 'generation'
>;
export interface BriefingAudience {
  excluded: number;
  member_count: number;
  deliverable: number;
  muted: number;
  missing_destination: number;
  sources: BriefingSourceApproval[];
}
export interface GroupBriefingRun {
  id: number;
  run_id: number;
  group_id: string;
  definition_id: number;
  mode: 'preview' | 'publish';
  state: string;
  subject?: string;
  body_md?: string;
  document?: BriefingDocument | null;
  error_code?: string | null;
  created_at?: string;
  published_at?: string | null;
}
export interface GroupEditionSummary {
  scope: 'group';
  run_id: number;
  group_id: string;
  group_label: string;
  briefing_name: string;
  briefing_title: string;
  subject: string | null;
  published_at: string;
  preview?: string;
}
export function getGroupEditions(
  params: { group?: string; limit?: number; offset?: number } = {},
): Promise<{ items: GroupEditionSummary[]; total: number }> {
  const query = new URLSearchParams();
  if (params.group) query.set('group', params.group);
  query.set('limit', String(params.limit ?? 20));
  query.set('offset', String(params.offset ?? 0));
  return apiFetch(`/briefings/group-editions?${query}`);
}
export function getBriefingTargets(): Promise<{
  targets: BriefingTarget[];
  channels: string[];
  timezone: string;
  source_scopes: Record<string, string>;
}> {
  return apiFetch('/briefings/targets');
}
function groupBriefingPath(group: string, name?: string) {
  return `/groups/${encodeURIComponent(group)}/briefings${name === undefined ? '' : '/' + encodeURIComponent(name)}`;
}
export function getGroupBriefings(
  group: string,
): Promise<{ items: GroupBriefingDefinition[]; capabilities: BriefingTarget['capabilities'] }> {
  return apiFetch(groupBriefingPath(group));
}
export function getGroupBriefing(group: string, name: string): Promise<GroupBriefingDefinition> {
  return apiFetch(groupBriefingPath(group, name));
}
export function saveGroupBriefing(
  group: string,
  payload: GroupBriefingPayload,
  name?: string,
): Promise<GroupBriefingDefinition> {
  return apiFetch(groupBriefingPath(group, name), {
    method: name === undefined ? 'POST' : 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
/** An owner deletes a disabled definition; an enabled one is refused with
 *  `disable_first`, a stale `revision` with `revision_conflict`. */
export function deleteGroupBriefing(
  group: string,
  name: string,
  revision: number,
): Promise<{ ok: true; deleted: true; id: number }> {
  return groupRequest(groupBriefingPath(group, name), 'DELETE', { revision });
}
export function getBriefingAudience(group: string, name: string): Promise<BriefingAudience> {
  return apiFetch(groupBriefingPath(group, name) + '/audience');
}
export function getBriefingResources(
  group: string,
  query = '',
): Promise<{
  files: string[];
  calendars: { value: string; label: string }[];
  calendar_unavailable: boolean;
}> {
  return apiFetch(
    `/groups/${encodeURIComponent(group)}/briefing-resources?q=${encodeURIComponent(query)}`,
  );
}
export function approveBriefingSource(
  group: string,
  name: string,
  state: BriefingSourceApproval,
  resource?: Record<string, unknown>,
): Promise<BriefingSourceApproval> {
  return apiFetch(
    groupBriefingPath(group, name) + `/sources/${encodeURIComponent(state.source_id)}/approval`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_definition_revision: state.definition_revision,
        expected_grant_revision: state.grant_revision,
        acknowledged: true,
        audience_contract: state.audience_contract,
        ...(resource ? { resource_config: resource } : {}),
      }),
    },
  );
}
export function revokeBriefingSource(
  group: string,
  name: string,
  state: BriefingSourceApproval,
): Promise<BriefingSourceApproval> {
  return apiFetch(
    groupBriefingPath(group, name) + `/sources/${encodeURIComponent(state.source_id)}/approval`,
    {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_grant_revision: state.grant_revision }),
    },
  );
}
export function runGroupBriefing(
  group: string,
  name: string,
  mode: 'preview' | 'publish',
  requestId: string,
): Promise<GroupBriefingRun> {
  return apiFetch(groupBriefingPath(group, name) + '/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, request_id: requestId }),
  });
}
export function getGroupBriefingRun(
  group: string,
  name: string,
  run: number,
): Promise<GroupBriefingRun> {
  return apiFetch(groupBriefingPath(group, name) + `/runs/${run}`);
}

export interface BriefingDelivery {
  reasons?: { excluded: number; muted: number; missing_destination: number };
  counts: Record<string, number>;
  recipients: {
    user_id: string;
    display_name: string;
    channel: string;
    state: string;
    attempts: number;
    error_code: string | null;
  }[];
}
export interface BriefingDeliveryPreferences {
  outbound_muted: boolean;
  in_app_muted: boolean;
}
export function getGroupBriefingRuns(
  group: string,
  name: string,
  offset = 0,
): Promise<{ items: GroupBriefingRun[]; total: number }> {
  return apiFetch(groupBriefingPath(group, name) + `/runs?limit=20&offset=${offset}`);
}
export function getGroupBriefingDelivery(
  group: string,
  name: string,
  run: number,
): Promise<BriefingDelivery> {
  return apiFetch(groupBriefingPath(group, name) + `/runs/${run}/delivery`);
}
export function retryGroupBriefingDelivery(
  group: string,
  name: string,
  run: number,
  acknowledgeDuplicates = false,
): Promise<{ retried: number }> {
  return apiFetch(groupBriefingPath(group, name) + `/runs/${run}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ acknowledge_duplicates: acknowledgeDuplicates }),
  });
}
export function getGroupBriefingPreferences(
  group: string,
  name: string,
): Promise<BriefingDeliveryPreferences> {
  return apiFetch(groupBriefingPath(group, name) + '/delivery');
}
export function saveGroupBriefingPreferences(
  group: string,
  name: string,
  preferences: BriefingDeliveryPreferences,
): Promise<BriefingDeliveryPreferences> {
  return apiFetch(groupBriefingPath(group, name) + '/delivery', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      outbound_muted: preferences.outbound_muted,
      in_app_muted: preferences.in_app_muted,
    }),
  });
}
export function getGroupDeliveryPreferences(group: string): Promise<BriefingDeliveryPreferences> {
  return apiFetch(`/groups/${encodeURIComponent(group)}/briefing-delivery`);
}
export function saveGroupDeliveryPreferences(
  group: string,
  preferences: BriefingDeliveryPreferences,
): Promise<BriefingDeliveryPreferences> {
  return apiFetch(`/groups/${encodeURIComponent(group)}/briefing-delivery`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(preferences),
  });
}
export function groupBriefingDownloadUrl(group: string, name: string, run: number): string {
  return `${base}/api${groupBriefingPath(group, name)}/runs/${run}/download`;
}

export interface PushCategory {
  id: string;
  label: string;
}

export function listPushCategories(): Promise<{ categories: PushCategory[] }> {
  return apiFetch('/push/categories');
}

export interface PushDevice {
  id: number;
  platform: string;
  app_version: string;
  environment: 'production' | 'sandbox';
  token_tail: string;
  created_at: string;
  last_seen_at: string | null;
  last_error: string | null;
}

export function listPushDevices(): Promise<{ devices: PushDevice[] }> {
  return apiFetch('/push/devices');
}

export function registerPushDevice(device: {
  token: string;
  environment: 'production' | 'sandbox';
  platform: 'ios';
  app_version: string;
  /** The generic shell's native connection (0.12.0 and later); older shells send neither. */
  connection_binding?: string;
  connection_generation?: number;
}): Promise<{ id: number }> {
  return apiFetch('/push/devices', { method: 'POST', body: JSON.stringify(device) });
}

export function deletePushDevice(id: number): Promise<{ ok: boolean }> {
  return apiFetch(`/push/devices/${id}`, { method: 'DELETE' });
}
