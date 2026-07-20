<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import {
		getSettingsServices,
		getModules,
		getProfile,
		updateProfile,
		getResources,
		addResource,
		deleteResource,
		getMe,
		disconnectNextcloudToken,
		type ServiceCard as ServiceCardData,
		type UserProfile,
		type UserResourceRow,
		type ResourceTypeSchema,
		type NextcloudTokenStatus,
	} from '$lib/api';
	import { Button, Modal, Select, type SelectOption } from '$lib/components/ui';
	import {
		ServiceCard,
		GarminCard,
		SettingsLayout,
		SettingsCard,
		SettingsField,
	} from '$lib/components/settings';

	let services: ServiceCardData[] = $state([]);
	let allModules: string[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let info = $state('');
	let oauthBusy = $state(false);
	// null = operator hasn't enabled encrypted token storage → no card.
	let ncToken: NextcloudTokenStatus | null = $state(null);
	let ncTokenBusy = $state(false);

	// Full IANA timezone list from the browser (no hardcoded list / extra dep).
	// Older engines may not implement supportedValuesOf — fall back to UTC.
	const timezoneOptions: SelectOption[] = (() => {
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
	})();

	let profile: UserProfile | null = $state(null);
	let profileSaving = $state(false);
	let profileError = $state('');
	let initialProfileJson = $state('');
	let profileDirty = $derived(
		profile ? JSON.stringify(profile) !== initialProfileJson : false,
	);

	let resourceTypes: ResourceTypeSchema[] = $state([]);
	let resources: UserResourceRow[] = $state([]);
	let newRes = $state({ type: '', path: '', name: '', permissions: 'read', extrasJson: '' });
	let resourceError = $state('');
	let resourceSaving = $state(false);

	const resourceTypeOptions: SelectOption[] = $derived([
		{ value: '', label: 'Type…' },
		...resourceTypes.map((rt) => ({ value: rt.type, label: rt.label })),
	]);
	const permissionOptions: SelectOption[] = [
		{ value: 'read', label: 'read' },
		{ value: 'readwrite', label: 'readwrite' },
	];

	type ConfirmKind = { kind: 'resource'; id: number; label: string };
	let confirmDelete: ConfirmKind | null = $state(null);

	async function refresh() {
		loading = true;
		try {
			const [svcResp, profResp, resResp, modResp, meResp] = await Promise.all([
				getSettingsServices(),
				getProfile(),
				getResources(),
				getModules(),
				getMe(),
			]);
			services = svcResp.services;
			ncToken = meResp.nextcloud_token ?? null;
			profile = profResp.profile;
			if (profile) {
				// Normalize optional routing fields so the bindings are safe.
				profile.routing = profile.routing || {};
				profile.default_destination = profile.default_destination || 'talk';
			}
			initialProfileJson = profile ? JSON.stringify(profile) : '';
			resourceTypes = resResp.types;
			resources = resResp.resources;
			allModules = modResp.modules;
			error = '';
		} catch (e) {
			error = (e as Error).message || 'Failed to load settings';
		} finally {
			loading = false;
		}
	}

	async function reloadServices() {
		try {
			services = (await getSettingsServices()).services;
		} catch (e) {
			error = (e as Error).message || 'Failed to reload services';
		}
	}

	function toggleDisabledModule(name: string) {
		if (!profile) return;
		const next = new Set(profile.disabled_modules || []);
		if (next.has(name)) next.delete(name);
		else next.add(name);
		profile.disabled_modules = [...next];
	}

	// User-routable surfaces only — self-routing (istota_file) and the inline
	// repl surface are held back from the UI; the server's delivery_surfaces
	// list is the source of truth, this is the offline fallback. `web` is the
	// web chat surface: routing logs/alerts there posts into the user's room.
	const BUILTIN_SURFACES = ['talk', 'email', 'ntfy', 'web'];

	function deliverySurfaces(): string[] {
		const s = profile?.delivery_surfaces;
		return s && s.length ? s : BUILTIN_SURFACES;
	}

	// Per-purpose route dropdown. `emptyValue`/`emptyLabel` is the leading no-op
	// option; `talkLabel` spells out where the bare `talk` surface resolves for
	// this purpose (the logs room vs the alerts channel) so it isn't ambiguous.
	// A saved descriptor that isn't one of the offered surfaces (e.g. a
	// CLI-set "talk:<token>" or "talk,email") is kept as an extra option so it
	// shows and isn't silently dropped on re-save.
	function routeOptions(
		current: string,
		opts: { emptyValue?: string; emptyLabel?: string; talkLabel?: string } = {}
	): SelectOption[] {
		const { emptyValue = '', emptyLabel = '(default)', talkLabel = 'talk' } = opts;
		const surfaces = deliverySurfaces();
		const out: SelectOption[] = [{ value: emptyValue, label: emptyLabel }];
		for (const s of surfaces) out.push({ value: s, label: s === 'talk' ? talkLabel : s });
		if (current && current !== emptyValue && !surfaces.includes(current))
			out.push({ value: current, label: current });
		return out;
	}

	// Default destination dropdown: every surface, no no-op option (there is
	// always a default), plus the current value if it's a custom descriptor.
	function destinationOptions(current: string): SelectOption[] {
		const surfaces = deliverySurfaces();
		const out: SelectOption[] = surfaces.map((s) => ({ value: s, label: s }));
		if (current && !surfaces.includes(current)) out.push({ value: current, label: current });
		return out;
	}

	// The execution log is opt-in and (off) must override a provisioned
	// log_channel, so its empty option carries the explicit "none" sentinel. The
	// displayed value reflects the *effective* destination: an explicit
	// routing.log wins, else a provisioned log_channel shows as "talk" (the logs
	// channel), else "(off)".
	function logRouteValue(): string {
		const r = (profile?.routing || {})['log'];
		if (r) return r;
		if (profile?.log_channel) return 'talk';
		return 'none';
	}

	function setRoute(purpose: string, value: string) {
		if (!profile) return;
		const next = { ...(profile.routing || {}) };
		const v = (value || '').trim();
		if (v) next[purpose] = v;
		else delete next[purpose];
		profile.routing = next;
	}

	async function disconnectNextcloud() {
		ncTokenBusy = true;
		try {
			await disconnectNextcloudToken();
			ncToken = { connected: false, expires_at: null };
			info = 'Nextcloud connection removed.';
		} catch (e) {
			error = (e as Error).message || 'Disconnect failed';
		} finally {
			ncTokenBusy = false;
		}
	}

	function connectGoogle() {
		oauthBusy = true;
		// Full-page nav — the OAuth callback redirects back to /istota/.
		window.location.href = `${base}/google/connect`;
	}

	async function disconnectGoogle() {
		oauthBusy = true;
		try {
			await fetch(`${base}/api/google/disconnect`, {
				method: 'DELETE',
				credentials: 'include',
			});
			await reloadServices();
		} catch (e) {
			error = (e as Error).message || 'Disconnect failed';
		} finally {
			oauthBusy = false;
		}
	}

	function profileListString(values: string[]): string {
		return values.join(', ');
	}

	function parseListInput(value: string): string[] {
		return value
			.split(',')
			.map((v) => v.trim())
			.filter((v) => v.length > 0);
	}

	async function saveProfile() {
		if (!profile) return;
		profileSaving = true;
		profileError = '';
		info = '';
		try {
			const patch: Partial<UserProfile> = {
				display_name: profile.display_name,
				timezone: profile.timezone,
				email_addresses: profile.email_addresses,
				trusted_email_senders: profile.trusted_email_senders,
				quiet_email_senders: profile.quiet_email_senders,
				disabled_skills: profile.disabled_skills,
				disabled_modules: profile.disabled_modules,
				default_destination: profile.default_destination || 'talk',
				routing: profile.routing || {},
			};
			await updateProfile(patch);
			info = 'Profile saved.';
			await refresh();
		} catch (e) {
			profileError = (e as Error).message || 'Save failed';
		} finally {
			profileSaving = false;
		}
	}

	async function submitResource(e: SubmitEvent) {
		e.preventDefault();
		resourceError = '';
		const t = newRes.type.trim();
		if (!t) {
			resourceError = 'Pick a resource type.';
			return;
		}
		const spec = resourceTypes.find((rt) => rt.type === t);
		if (spec?.needs_path && !newRes.path.trim()) {
			resourceError = `${spec.label} requires a path.`;
			return;
		}
		let extras: Record<string, unknown> | undefined;
		const rawExtras = newRes.extrasJson.trim();
		if (rawExtras) {
			try {
				const parsed = JSON.parse(rawExtras);
				if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
					resourceError = 'Extras must be a JSON object.';
					return;
				}
				extras = parsed as Record<string, unknown>;
			} catch (err) {
				resourceError = `Extras JSON parse error: ${(err as Error).message}`;
				return;
			}
		}

		resourceSaving = true;
		try {
			await addResource({
				type: t,
				path: newRes.path.trim() || undefined,
				name: newRes.name.trim() || undefined,
				permissions: newRes.permissions || 'read',
				extras,
			});
			newRes = { type: '', path: '', name: '', permissions: 'read', extrasJson: '' };
			await refresh();
		} catch (e) {
			resourceError = (e as Error).message || 'Add failed';
		} finally {
			resourceSaving = false;
		}
	}

	function askRemoveResource(r: UserResourceRow) {
		if (r.id === undefined) return;
		confirmDelete = {
			kind: 'resource',
			id: r.id,
			label: r.name || r.path || r.type,
		};
	}

	async function performDelete() {
		if (!confirmDelete) return;
		const target = confirmDelete;
		confirmDelete = null;
		try {
			if (target.kind === 'resource') {
				await deleteResource(target.id);
				await refresh();
			}
		} catch (e) {
			error = (e as Error).message || 'Delete failed';
		}
	}

	onMount(() => {
		void refresh();
	});

	// /settings/services already filters to connected services (no module-owned
	// monarch/feeds/overland leak through). Skip cards whose status is
	// "unavailable" — historically used to mean "no resource declaration" but
	// now only OAuth services with the global flag off can land there.
	let activeServices = $derived(
		services.filter((s) => s.status !== 'unavailable'),
	);
</script>

<SettingsLayout
	title="Settings"
	description="Profile, resources, and per-service credentials. Secrets are encrypted at rest and never sent back to the browser — secret fields are write-only."
	{loading}
	{error}
	{info}
>
	{#if profile}
		{@const saveBtn = {
			dirty: profileDirty,
			saving: profileSaving,
		}}

		{#snippet profileSaveActions()}
			{#if saveBtn.dirty}
				<span class="dirty-badge">Unsaved changes</span>
			{/if}
			<Button
				variant="primary"
				size="sm"
				onclick={saveProfile}
				disabled={!saveBtn.dirty || saveBtn.saving}
			>
				{saveBtn.saving ? 'Saving…' : 'Save'}
			</Button>
		{/snippet}

		<SettingsCard title="Identity" actions={profileSaveActions}>
			<p class="hint">
				How Istota addresses you. User ID: <code>{profile.user_id}</code>
			</p>

			<SettingsField label="Display name">
				<input type="text" bind:value={profile.display_name} />
			</SettingsField>
			<SettingsField label="Email addresses (comma-separated)">
				<input
					type="text"
					value={profileListString(profile.email_addresses)}
					oninput={(e) => {
						if (profile)
							profile.email_addresses = parseListInput(
								(e.currentTarget as HTMLInputElement).value,
							);
					}}
				/>
			</SettingsField>
			<SettingsField
				label="Timezone (IANA)"
				hint="Setting a timezone here overrides your Nextcloud timezone and is kept across restarts."
			>
				<Select
					value={profile.timezone || 'UTC'}
					options={timezoneOptions}
					ariaLabel="Timezone"
					fullWidth
					onValueChange={(v) => {
						if (profile) profile.timezone = v;
					}}
				/>
			</SettingsField>
		</SettingsCard>

		<SettingsCard
			title="Preferences"
			description="How Istota behaves for your account."
			actions={profileSaveActions}
		>
			<SettingsField label="Trusted email senders (fnmatch patterns, comma-separated)">
				<input
					type="text"
					value={profileListString(profile.trusted_email_senders)}
					oninput={(e) => {
						if (profile)
							profile.trusted_email_senders = parseListInput(
								(e.currentTarget as HTMLInputElement).value,
							);
					}}
				/>
			</SettingsField>
			<SettingsField label="Quiet email senders (filed silently — no task; fnmatch patterns, comma-separated)">
				<input
					type="text"
					value={profileListString(profile.quiet_email_senders)}
					oninput={(e) => {
						if (profile)
							profile.quiet_email_senders = parseListInput(
								(e.currentTarget as HTMLInputElement).value,
							);
					}}
				/>
			</SettingsField>
			<SettingsField label="Disabled skills (comma-separated)">
				<input
					type="text"
					value={profileListString(profile.disabled_skills)}
					oninput={(e) => {
						if (profile)
							profile.disabled_skills = parseListInput(
								(e.currentTarget as HTMLInputElement).value,
							);
					}}
				/>
			</SettingsField>
			{#if allModules.length > 0}
				<div class="field">
					<span>Disabled modules</span>
					<div class="module-toggles">
						{#each allModules as m (m)}
							<label class="module-chip">
								<input
									type="checkbox"
									checked={(profile.disabled_modules || []).includes(m)}
									onchange={() => toggleDisabledModule(m)}
								/>
								<span>{m}</span>
							</label>
						{/each}
					</div>
					<p class="hint">
						Modules are on by default. Tick to opt out — the corresponding
						UI tab and scheduled jobs will be hidden / paused.
					</p>
				</div>
			{/if}
			<SettingsField
				label="Default delivery destination"
				hint="Where your results and notifications go. Alerts can use a separate channel below."
			>
				<Select
					value={profile.default_destination || 'talk'}
					options={destinationOptions(profile.default_destination || 'talk')}
					ariaLabel="Default delivery destination"
					fullWidth
					onValueChange={(v) => {
						if (profile) profile.default_destination = v || 'talk';
					}}
				/>
			</SettingsField>
			<SettingsField
				label="Send alerts to"
				hint="Optional. Route alerts (heartbeat failures, security and policy notices) to a louder or separate channel, e.g. ntfy for push. 'talk' uses your alerts channel; leave on (default) to use the default destination."
			>
				<Select
					value={(profile.routing || {})['alert'] || ''}
					options={routeOptions((profile.routing || {})['alert'] || '', {
						talkLabel: 'talk (alerts channel)'
					})}
					ariaLabel="Alert delivery destination"
					fullWidth
					onValueChange={(v) => setRoute('alert', v)}
				/>
			</SettingsField>
			<SettingsField
				label="Send execution log to"
				hint="Optional. The verbose per-task execution log — every tool call plus a final summary. 'talk' uses your logs channel; email and ntfy get a single final summary. (off) disables it."
			>
				<Select
					value={logRouteValue()}
					options={routeOptions(logRouteValue(), {
						emptyValue: 'none',
						emptyLabel: '(off)',
						talkLabel: 'talk (logs channel)'
					})}
					ariaLabel="Execution log destination"
					fullWidth
					onValueChange={(v) => setRoute('log', v)}
				/>
			</SettingsField>
			{#if profileError}
				<div class="banner error">{profileError}</div>
			{/if}
		</SettingsCard>
	{/if}

	<SettingsCard title="Resources ({resources.length})">
		<p class="hint">
			Calendars, folders, modules, and integrations available to your
			account. Operator-managed entries (from <code>config.toml</code>) are
			read-only here.
		</p>

			{#if resources.length === 0}
				<p class="empty">No resources configured yet.</p>
			{:else}
				<div class="table-scroll">
					<table class="grid">
						<thead>
							<tr>
								<th class="col-type">Type</th>
								<th class="col-name">Name</th>
								<th class="col-path">Path</th>
								<th class="col-perms">Perms</th>
								<th class="col-source">Source</th>
								<th class="actions"></th>
							</tr>
						</thead>
						<tbody>
							{#each resources as r (`${r.managed}-${r.id ?? r.path}-${r.type}`)}
								<tr>
									<td class="col-type">{r.type}</td>
									<td class="col-name">{r.name || '—'}</td>
									<td class="col-path"><code>{r.path || '—'}</code></td>
									<td class="col-perms">{r.permissions}</td>
									<td class="col-source muted">
										{r.managed === 'config' ? 'config.toml' : 'user'}
									</td>
									<td class="actions">
										{#if r.managed === 'db' && r.id !== undefined}
											<button
												class="icon-btn danger"
												title="Remove"
												type="button"
												onclick={() => askRemoveResource(r)}>×</button
											>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			<form class="add-resource" onsubmit={submitResource}>
				<h3>Add resource</h3>
				<div class="add-grid">
					<SettingsField label="Type">
						<Select
							value={newRes.type}
							options={resourceTypeOptions}
							onValueChange={(v) => (newRes.type = v)}
							ariaLabel="Type"
							fullWidth
						/>
					</SettingsField>
					<SettingsField label="Path">
						<input
							type="text"
							placeholder="(if applicable)"
							bind:value={newRes.path}
						/>
					</SettingsField>
					<SettingsField label="Display name">
						<input type="text" placeholder="(optional)" bind:value={newRes.name} />
					</SettingsField>
					<SettingsField label="Permissions">
						<Select
							value={newRes.permissions}
							options={permissionOptions}
							onValueChange={(v) => (newRes.permissions = v)}
							ariaLabel="Permissions"
							fullWidth
						/>
					</SettingsField>
					<SettingsField label="Extras (JSON)" wide>
						<textarea
							rows="2"
							placeholder={'e.g. {"ingest_token": "…", "default_radius": 75}'}
							bind:value={newRes.extrasJson}
						></textarea>
					</SettingsField>
				</div>
				<div class="add-actions">
					<Button
						variant="primary"
						size="sm"
						type="submit"
						disabled={resourceSaving}
					>
						{resourceSaving ? 'Adding…' : 'Add resource'}
					</Button>
				</div>
				{#if resourceError}
					<div class="banner error">{resourceError}</div>
				{/if}
			</form>
		</SettingsCard>

		<SettingsCard title="Briefings">
			<p class="hint">
				Cron-scheduled summaries with their own schedule, delivery, and
				content-block editor. Manage them on the
				<a href="{base}/briefings/settings">Briefings settings</a> page.
			</p>
		</SettingsCard>

		{#if activeServices.length > 0 || ncToken}
			<div class="subsection-heading">
				<h2>Connected services</h2>
				<p class="hint">
					Per-service credentials for skills that need them. Values are
					encrypted at rest and never sent back to the browser — secret
					fields are write-only. Module-specific credentials live on
					their own settings pages
					(<a href="{base}/feeds/settings">feeds</a>,
					<a href="{base}/money/settings">money</a>,
					<a href="{base}/location/settings">location</a>).
				</p>
			</div>
		{/if}

		{#if ncToken}
			{@const nc = ncToken}
			<SettingsCard
				title="Nextcloud"
				description="When connected, messages you send from web chat appear in Nextcloud Talk under your own name, and read state syncs between web and Talk."
			>
				{#snippet status()}
					<span class="status-pill status-{nc.connected ? 'configured' : 'missing'}">
						{nc.connected ? 'Connected' : 'Not connected'}
					</span>
				{/snippet}
				{#if nc.connected}
					<div class="oauth-actions">
						<Button
							variant="secondary"
							size="sm"
							onclick={disconnectNextcloud}
							disabled={ncTokenBusy}
						>
							{ncTokenBusy ? 'Disconnecting…' : 'Disconnect'}
						</Button>
					</div>
				{:else}
					<p class="empty">
						Log out and back in to connect — the connection is established at
						login.
					</p>
				{/if}
			</SettingsCard>
		{/if}

		{#each activeServices as svc (svc.service)}
			{#if svc.custom_ui && svc.service === 'garmin'}
				<GarminCard />
			{:else}
				<ServiceCard
					service={svc}
					onChanged={reloadServices}
					onConnect={connectGoogle}
					onDisconnect={disconnectGoogle}
					oauthBusy={oauthBusy}
				/>
			{/if}
		{/each}
</SettingsLayout>

{#if confirmDelete}
	<Modal
		open={true}
		title="Remove resource?"
		onOpenChange={(o) => {
			if (!o) confirmDelete = null;
		}}
	>
		<p>Remove <strong>{confirmDelete.label}</strong>?</p>
		{#snippet footer()}
			<Button variant="ghost" onclick={() => (confirmDelete = null)}>Cancel</Button>
			<Button variant="primary" onclick={performDelete}>Remove</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	/* Shared .settings/.card/.field/.grid/.banner/.icon-btn primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only page-specific
	   layout (resource/briefing add forms, table column widths, module toggles)
	   stays here. */

	.col-type {
		width: 7rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.col-name {
		width: auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.col-path {
		width: auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.col-perms {
		width: 5.5rem;
	}
	.col-source {
		width: 6rem;
	}

	.add-resource {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		padding-top: 0.4rem;
		border-top: 1px solid var(--border-subtle);
	}

	.add-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
		gap: 0.6rem;
	}

	.add-actions {
		display: flex;
		justify-content: flex-end;
	}


	/* Mirrors ServiceCard's Connect/Disconnect row so the Nextcloud card and
	   the OAuth service cards below it line up. */
	.oauth-actions {
		display: flex;
		gap: 0.5rem;
	}

	.module-toggles {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
	}

	.module-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		padding: 0.15rem 0.5rem;
		border-radius: var(--radius-pill);
		background: var(--surface-raised);
		font-size: var(--text-xs);
		color: var(--text-muted);
		cursor: pointer;
	}

	.module-chip input[type='checkbox'] {
		margin: 0;
		width: auto;
	}

	@container settings (max-width: 520px) {
		.col-source,
		.col-perms {
			display: none;
		}
	}
</style>
