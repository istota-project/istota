/**
 * Typed fetchers for the Apps management routes (`/istota/api/apps/`).
 *
 * The shapes are the server's (`src/istota/apps/routes.py`). Every call goes
 * through the app's one `apiFetch`, so a 401 is an `AuthError` and the
 * connectivity store hears about every request. A refusal is rethrown as an
 * `AppsApiError` carrying the status and, for a validation failure, the list
 * of errors the server returned, since those are shown beside the draft rather
 * than as one message.
 */
import { AuthError, apiFetch } from '$lib/api';

export type Narrowest = 'phone' | 'tablet' | 'desktop';
export type AppOrigin = 'draft' | 'starter';

export interface AppSummary {
  app_id: string;
  name: string;
  icon: string;
  description: string;
  origin: AppOrigin;
  current_version: number | null;
  narrowest: Narrowest | null;
  pending_id: string | null;
  /** Capabilities the current version requests and has not been granted. */
  ungranted: string[];
}

export interface PendingSummary {
  pending_id: string;
  app_id: string;
  name: string;
  origin: string;
  size: number;
  created_at: string;
}

export interface DraftSummary {
  app_id: string;
  name: string | null;
  has_manifest: boolean;
  state: 'new' | 'changed';
}

export interface StarterEntry {
  app_id: string;
  state: 'installed' | 'dismissed' | 'unavailable' | 'conflict';
}

export interface AppsList {
  apps: AppSummary[];
  pending: PendingSummary[];
  drafts: DraftSummary[];
  starter: StarterEntry[];
  kit_built: boolean;
}

export interface CapabilityRow {
  capability: string;
  scope: string | null;
  description: string;
  sensitive: boolean;
}

export interface PendingCapability extends CapabilityRow {
  new: boolean;
  always_granted: boolean;
}

export interface AppManifest {
  id: string;
  name: string;
  icon: string;
  entry: string;
  description: string;
  capabilities: string[];
  narrowest: Narrowest;
  reason: string;
}

export interface AppDetail {
  app_id: string;
  name: string;
  icon: string;
  description: string;
  origin: AppOrigin;
  current_version: number;
  installed_at: string;
  approved_at: string | null;
  manifest: AppManifest;
  narrowest: Narrowest;
  reason: string;
  warnings: string[];
  pending_id: string | null;
  grants: CapabilityRow[];
  ungranted: CapabilityRow[];
}

export interface FileDiff {
  path: string;
  status: 'added' | 'removed' | 'changed' | 'unchanged';
  diff: string;
}

export interface PendingInstall {
  pending_id: string;
  app_id: string;
  origin: string;
  created_at: string;
  manifest: AppManifest;
  narrowest: Narrowest;
  reason: string;
  current_version: number | null;
  capabilities: PendingCapability[];
  new_capabilities: string[];
  one_button: boolean;
  size: number;
  warnings: string[];
  diff: FileDiff[];
}

export interface AppVersion {
  version: number;
  current: boolean;
  installed_at: string;
  approved_at: string | null;
  narrowest: Narrowest;
  capabilities: string[];
  grants: string[];
}

export interface AppVersions {
  app_id: string;
  current_version: number | null;
  versions: AppVersion[];
}

export interface AppStorage {
  app_id: string;
  name: string;
  kv_bytes: number;
  /** Null when `data/` is a link or blocked: not measured, never followed. */
  data_bytes: number | null;
}

/** A refusal from the apps routes. `errors` is set for a 422 validation failure. */
export class AppsApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly errors: string[] = [],
    readonly body: unknown = null,
  ) {
    super(message);
    this.name = 'AppsApiError';
  }
}

async function appsFetch<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    return await apiFetch<T>(`/apps${path}`, init);
  } catch (e) {
    const status = (e as { status?: unknown }).status;
    // An expired session stays the shared `AuthError`, which the pages check for.
    if (e instanceof AuthError || typeof status !== 'number') throw e;
    const body = (e as { body?: unknown }).body ?? null;
    const raw = (body as { errors?: unknown } | null)?.errors;
    const errors = Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [];
    const message = errors.length ? errors.join(' ') : (e as Error).message;
    throw new AppsApiError(status, message, errors, body);
  }
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return appsFetch<T>(path, {
    method: 'POST',
    ...(body === undefined
      ? {}
      : { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }),
  });
}

const seg = encodeURIComponent;

export function getApps(): Promise<AppsList> {
  return appsFetch<AppsList>('/');
}

export function getAppStorage(): Promise<{ apps: AppStorage[] }> {
  return appsFetch('/storage');
}

export function getApp(appId: string): Promise<AppDetail> {
  return appsFetch(`/${seg(appId)}`);
}

/** Build a pending install from the draft folder; the approval screen is next. */
export function createPending(appId: string): Promise<{ pending_id: string; app_id: string }> {
  return post(`/drafts/${seg(appId)}/pending`);
}

export function getPending(appId: string, pendingId: string): Promise<PendingInstall> {
  return appsFetch(`/${seg(appId)}/pending/${seg(pendingId)}`);
}

export function approvePending(
  appId: string,
  pendingId: string,
  newCapabilities: string[],
): Promise<{ app_id: string; version: number }> {
  return post(`/${seg(appId)}/pending/${seg(pendingId)}/approve`, {
    new_capabilities: newCapabilities,
  });
}

export function deletePending(appId: string, pendingId: string): Promise<void> {
  return appsFetch(`/${seg(appId)}/pending/${seg(pendingId)}`, { method: 'DELETE' });
}

export function getVersions(appId: string): Promise<AppVersions> {
  return appsFetch(`/${seg(appId)}/versions`);
}

export function makeCurrent(
  appId: string,
  version: number,
): Promise<{ app_id: string; current_version: number }> {
  return post(`/${seg(appId)}/versions/${version}/current`);
}

export function deleteVersion(appId: string, version: number): Promise<void> {
  return appsFetch(`/${seg(appId)}/versions/${version}`, { method: 'DELETE' });
}

export function grantCapability(
  appId: string,
  capability: string,
  scope: string | null,
): Promise<{ capability: string; scope: unknown }> {
  return post(`/${seg(appId)}/grants`, { capability, scope });
}

export function uninstallApp(appId: string, deleteData: boolean): Promise<void> {
  const query = deleteData ? '?delete_data=1' : '';
  return appsFetch(`/${seg(appId)}${query}`, { method: 'DELETE' });
}

export function reinstallStarter(appId: string): Promise<{ app_id: string; status: string }> {
  return post(`/starter/${seg(appId)}/reinstall`);
}

/** Where the approval screen for a pending install lives. */
export function installPath(appId: string, pendingId: string): string {
  return `/apps/${seg(appId)}/install/${seg(pendingId)}/`;
}

/** Where an installed app opens. */
export function appPath(appId: string): string {
  return `/apps/${seg(appId)}/`;
}
