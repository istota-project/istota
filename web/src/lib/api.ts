import { base } from '$app/paths';
import { uploadFromPath, type Picked } from '$lib/platform/nativePicker';

class AuthError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'AuthError';
  }
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
  if (timeoutMs > 0 && !init?.signal) {
    controller = new AbortController();
    timer = setTimeout(() => controller?.abort(), timeoutMs);
  }
  try {
    const resp = await fetch(`${base}/api${path}`, {
      ...init,
      credentials: 'same-origin',
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (resp.status === 401) throw new AuthError();
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp.json();
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

export interface User {
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
    google_workspace: boolean;
    google_workspace_enabled: boolean;
    admin: boolean;
  };
  // null when the operator hasn't enabled encrypted token storage.
  nextcloud_token?: NextcloudTokenStatus | null;
}

export async function disconnectNextcloudToken(): Promise<{ ok: boolean; was_connected: boolean }> {
  return apiFetch('/settings/nextcloud-token', { method: 'DELETE' });
}

export interface AdminStatsUserSource {
  count: number;
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
  tasks_failed_24h: number;
  last_active: string | null;
}

export interface AdminStatsJob {
  id: number;
  user_id: string;
  name: string;
  cron: string;
  enabled: boolean;
  last_run_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
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
  };
  storage: {
    db_size_bytes: number;
    backups_count: number;
    last_backup: string | null;
    nextcloud_configured: boolean;
    nextcloud_mount_healthy: boolean;
  };
  runtime?: {
    mode: 'standalone' | 'server';
    caveats: { title: string; detail: string }[];
  };
  models?: {
    brain_kind: string;
    default_model: string;
    default_effort: string | null;
    roles: { role: string; resolved: string }[];
    endpoint?: string;
    provider?: string;
    source_type_overrides?: Record<string, string>;
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

export async function getMe(): Promise<User> {
  return apiFetch<User>('/me');
}

export async function getFeeds(params?: Record<string, string>): Promise<FeedsResponse> {
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  return apiFetch<FeedsResponse>(`/feeds${qs}`);
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
  accuracy: number;
  place: string | null;
  speed: number | null;
  battery: number | null;
  activity_type: string | null;
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
  location: string;
  location_source: string | null;
  arrived: string;
  departed: string;
  ping_count: number;
  lat: number;
  lon: number;
}

export interface DaySummary {
  date: string;
  timezone: string;
  ping_count: number;
  transit_pings: number;
  stops: DaySummaryStop[];
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
}

export interface ServiceCard {
  service: string;
  label: string;
  status: 'configured' | 'partial' | 'missing' | 'unavailable';
  fields: SettingsField[];
  configured_keys: string[];
  last_updated: string | null;
  used_by?: string[];
  oauth?: boolean;
  custom_ui?: boolean; // render a bespoke card (garmin) instead of fields
  connected?: boolean; // google_workspace OAuth state
  enabled?: boolean; // google_workspace module flag
}

export interface ServicesResponse {
  services: ServiceCard[];
}

export async function getSettingsServices(): Promise<ServicesResponse> {
  return apiFetch<ServicesResponse>('/settings/services');
}

// --- modules + per-module services ---

export interface ModulesResponse {
  modules: string[];
  disabled: string[];
  enabled_for_user: Record<string, boolean>;
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

export interface GeneratedIngestToken {
  ok: boolean;
  /** Returned once and never again — the secret is write-only from here on. */
  token: string;
  webhook_url: string;
}

/**
 * Mint a location ingest token, rotating any existing one.
 *
 * The only call in this file whose response carries a secret. The token has to
 * reach a phone, and the alternative is the user transcribing 43 random
 * characters; the page renders it as a QR and never asks for it again.
 */
export async function generateIngestToken(): Promise<GeneratedIngestToken> {
  // Not apiFetch: this endpoint's refusal is a 409 whose *message* is the
  // whole point ("the location module is off for this user, so an ingest
  // token would not be accepted"). apiFetch discards the body and throws
  // "API error: 409", which tells the user nothing they can act on.
  const resp = await fetch(`${base}/api/settings/secrets/overland/ingest_token/generate`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    const detail = await resp
      .json()
      .then((body: { detail?: unknown }) => (typeof body?.detail === 'string' ? body.detail : ''))
      .catch(() => '');
    throw new Error(detail || `Could not generate a token (HTTP ${resp.status}).`);
  }
  return resp.json();
}

export async function setSecret(
  service: string,
  key: string,
  value: string,
): Promise<{ ok: boolean; configured: boolean }> {
  return apiFetch(`/settings/secrets/${service}/${key}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
}

export async function deleteSecret(
  service: string,
  key: string,
): Promise<{ ok: boolean; deleted: boolean }> {
  return apiFetch(`/settings/secrets/${service}/${key}`, {
    method: 'DELETE',
  });
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
  routing: Record<string, string>;
  briefing_email_html: boolean;
  // Read-only hint from the server: surfaces available for delivery routing.
  delivery_surfaces?: string[];
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
  managed: 'config' | 'db';
  id?: number;
  name: string;
  cron: string;
  // Display title for the rendered briefing (email subject, archive entry).
  // Blank means "derive from the name"; the run date is appended on render.
  title: string;
  conversation_token: string;
  // A delivery surface (talk / email / ntfy) or a comma/surface:channel
  // descriptor; the dropdown is driven by the server's `outputs` list.
  output: string;
  enabled: boolean;
}

export interface BriefingRoomOption {
  token: string;
  name: string;
}

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
  return healthFetch('/panels/upload', { method: 'POST', body: form });
}

export async function extractHealthPanel(panelId: number): Promise<{
  biomarkers: Partial<Biomarker>[];
  drawn_at: string | null;
  lab_name: string | null;
  panel_type: string | null;
  warnings: string[];
  raw_text: string;
}> {
  return healthFetch(`/panels/${panelId}/extract`, { method: 'POST' });
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

export interface BloodworkMatrixMarker {
  name: string;
  display_name: string;
  unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  category: string;
}

export interface BloodworkMatrixCategory {
  name: string;
  markers: BloodworkMatrixMarker[];
}

export interface BloodworkMatrixPanel {
  id: number;
  drawn_at: string;
  lab_name: string | null;
  panel_type: string | null;
}

export interface BloodworkMatrix {
  categories: BloodworkMatrixCategory[];
  panels: BloodworkMatrixPanel[];
  values: Record<string, Record<string, { value: number; unit: string; flag: string | null }>>;
}

export async function getBloodworkMatrix(): Promise<BloodworkMatrix> {
  return healthFetch('/bloodwork/matrix');
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
  return healthFetch('/encounters/extract', { method: 'POST', body: form });
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
  return healthFetch('/immunizations/extract', { method: 'POST', body: form });
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

// ---- Documents -----------------------------------------------------------

export type DocumentEntity = 'encounter' | 'diagnosis' | 'immunization';

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

export async function listDocuments(entity?: EntityRef): Promise<{
  documents: HealthDocument[];
}> {
  const q = new URLSearchParams();
  if (entity) {
    q.set('entity_type', entity.type);
    q.set('entity_id', String(entity.id));
  }
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

export interface ChatRoom {
  id: number;
  token: string;
  name: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  /** Surface the room was created on. Talk-origin rooms surface here
   * automatically once the bot is messaged in them. */
  origin?: 'web' | 'talk';
  /** Set once the room has been promoted to (or originated as) a Talk room. */
  talk_token?: string;
  /** Unread bot/system messages on the web surface (server-computed; excludes
   * the user's own turns). Absent on older backends → treat as 0. */
  unread_count?: number;
  /** Standing per-room model default (canonical model id), shared across Talk
   * and web. null / absent → inherit the instance default. */
  model?: string | null;
  /** Standing per-room effort level (low/medium/high/xhigh/max). */
  effort?: string | null;
}

export interface ChatConfig {
  max_prompt_chars: number;
  max_attachment_mb: number;
  attachment_extensions: string[];
  client_poll_interval_ms: number;
}

export interface ChatHistoryMessage {
  role: 'user' | 'assistant' | 'system';
  text: string;
  // Task-backed turns carry task_id; bot-delivered notifications carry notif_id.
  task_id?: number;
  notif_id?: number;
  status?: string;
  confirmation?: boolean;
  created_at: string;
  // Finished task-backed turns carry their tool-use descriptions (in order)
  // and wall-clock duration so the action strip + timing persist across
  // reloads (ISSUE-122).
  tools?: string[];
  // Ordered, interleaved segment list (`text` / `tool`) for a finished turn,
  // derived server-side from the execution trace, so history reconstructs the
  // same interleaved layout as the live stream. `tools` is kept as a fallback.
  segments?: { kind: 'text' | 'tool'; text: string }[];
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
export type SendFailure = 'unreachable' | 'timeout' | 'auth' | 'rate_limit' | 'rejected';

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
}

export interface TaskEventDTO {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export function getChatConfig(): Promise<ChatConfig> {
  return apiFetch<ChatConfig>('/chat/config');
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

export interface ChatCommand {
  name: string;
  help: string;
}

export interface ChatModelAlias {
  alias: string;
  target: string | null;
  effort: string | null;
}

export interface ChatCommands {
  commands: ChatCommand[];
  model_aliases: ChatModelAlias[];
}

/** Command + model-alias catalogue powering the composer autocomplete. */
export function fetchChatCommands(): Promise<ChatCommands> {
  return apiFetch<ChatCommands>('/chat/commands');
}

export function getChatRooms(timeoutMs = 0): Promise<{ rooms: ChatRoom[] }> {
  return apiFetch<{ rooms: ChatRoom[] }>('/chat/rooms', undefined, timeoutMs);
}

export function createChatRoom(name: string): Promise<ChatRoom> {
  return apiFetch<ChatRoom>('/chat/rooms', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

export function updateChatRoom(
  id: number,
  patch: { name?: string; archived?: boolean; model?: string | null; effort?: string | null },
): Promise<ChatRoom> {
  return apiFetch<ChatRoom>(`/chat/rooms/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** Create a real Nextcloud Talk conversation for a web-origin room and bind it
 * ("Also open in Talk"). Returns the updated room (now carrying talk_token). */
export function promoteChatRoom(id: number): Promise<ChatRoom> {
  return apiFetch<ChatRoom>(`/chat/rooms/${id}/promote`, {
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

export function getRoomMessages(
  id: number,
  opts: { limit?: number; before?: { ts: string; id: number } | null; timeoutMs?: number } = {},
): Promise<ChatHistory> {
  const limit = opts.limit ?? 50;
  const params = new URLSearchParams({ limit: String(limit) });
  // Both cursor params travel together (the backend rejects a half-cursor) and
  // `before.ts` is the raw stored created_at, passed back verbatim.
  if (opts.before) {
    params.set('before_ts', opts.before.ts);
    params.set('before_id', String(opts.before.id));
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
  const body = JSON.stringify({ text, attachments, attachment_names: attachmentNames });

  try {
    const resp = await fetch(`${base}/api/chat/rooms/${roomId}/messages`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body,
      signal: controller.signal,
    });

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
      data = await resp.json();
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
        failure: 'rejected',
        error: data.error || `error ${resp.status}`,
      };
    }
    // `data` spread first: the endpoint's own payload carries a `status`
    // ("pending"), which would otherwise shadow the numeric HTTP status this
    // type promises — and that status is now rendered to the user on failure.
    return { ...data, ok: true, status: resp.status };
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
    return { ok: false, status: 0, failure: timedOut ? 'timeout' : 'unreachable' };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function getTaskEvents(taskId: number, sinceSeq = 0): Promise<{ events: TaskEventDTO[] }> {
  return apiFetch<{ events: TaskEventDTO[] }>(`/chat/tasks/${taskId}/events?since_seq=${sinceSeq}`);
}

export function confirmChatTask(taskId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/confirm`, { method: 'POST' });
}

export function cancelChatTask(taskId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/chat/tasks/${taskId}/cancel`, { method: 'POST' });
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
  path: string;
  name: string;
  size: number;
  // Where the same file is reachable through `chatFileUrl`, or null when it
  // isn't (a deployment with no local workspace). Distinct from `path`, which
  // is the host path the brain reads and the download endpoint won't take.
  workspace_path?: string | null;
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
  const resp = await fetch(`${base}/api${CHAT_ATTACHMENT_PATH}`, {
    method: 'POST',
    credentials: 'same-origin',
    body: form,
  });
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
  const { status, body } = await uploadFromPath(item, url);
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

export interface BriefingArchiveItem {
  id: number;
  briefing_name: string;
  subject: string;
  generated_at: string;
  task_id: number | null;
  delivered_to: string[];
  body_md?: string;
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

export interface BriefingConfigResponse {
  briefings: { name: string; blocks: BriefingBlock[] }[];
  schedule_names: string[];
  source_kinds: string[];
  structured_kinds: string[];
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

export async function deleteBriefingArchiveItem(id: number): Promise<void> {
  await apiFetch(`/briefings/archive/${id}`, { method: 'DELETE' });
}

export async function getBriefingConfig(): Promise<BriefingConfigResponse> {
  return apiFetch<BriefingConfigResponse>('/briefings/config');
}

export async function putBriefingBlock(
  payload: Record<string, unknown>,
): Promise<{ status: string; block?: BriefingBlock }> {
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

export interface SharedBlockStatus {
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

export async function runSharedBlock(
  name: string,
): Promise<{ status: string; error: string | null; block_status: SharedBlockStatus }> {
  return apiFetch(`/briefings/shared-blocks/${encodeURIComponent(name)}/run`, { method: 'POST' });
}

export async function getSharedBlockOptions(): Promise<{ options: SharedBlockOption[] }> {
  return apiFetch('/briefings/shared-block-options');
}

export { AuthError };
