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
		getBriefings,
		upsertBriefing,
		deleteBriefing,
		type ServiceCard as ServiceCardData,
		type UserProfile,
		type UserResourceRow,
		type ResourceTypeSchema,
		type UserBriefingRow,
		type BriefingRoomOption,
	} from '$lib/api';
	import { Button, Modal } from '$lib/components/ui';
	import { ServiceCard } from '$lib/components/settings';

	let services: ServiceCardData[] = $state([]);
	let allModules: string[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let info = $state('');
	let oauthBusy = $state(false);

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

	let briefings: UserBriefingRow[] = $state([]);
	let briefingRooms: BriefingRoomOption[] = $state([]);
	let briefingOutputs: string[] = $state(['talk', 'email', 'both']);
	let newBriefing = $state({
		name: '',
		cron: '0 7 * * *',
		conversation_token: '',
		output: 'talk' as 'talk' | 'email' | 'both',
		componentsJson: '{"calendar": true, "todos": true, "email": true}',
		enabled: true,
	});
	let briefingError = $state('');
	let briefingSaving = $state(false);

	type ConfirmKind =
		| { kind: 'resource'; id: number; label: string }
		| { kind: 'briefing'; id: number; label: string };
	let confirmDelete: ConfirmKind | null = $state(null);

	async function refresh() {
		loading = true;
		try {
			const [svcResp, profResp, resResp, briefResp, modResp] = await Promise.all([
				getSettingsServices(),
				getProfile(),
				getResources(),
				getBriefings(),
				getModules(),
			]);
			services = svcResp.services;
			profile = profResp.profile;
			initialProfileJson = profile ? JSON.stringify(profile) : '';
			resourceTypes = resResp.types;
			resources = resResp.resources;
			briefings = briefResp.briefings;
			briefingRooms = briefResp.rooms;
			briefingOutputs = briefResp.outputs?.length
				? briefResp.outputs
				: ['talk', 'email', 'both'];
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
				ntfy_topic: profile.ntfy_topic,
				email_addresses: profile.email_addresses,
				trusted_email_senders: profile.trusted_email_senders,
				disabled_skills: profile.disabled_skills,
				disabled_modules: profile.disabled_modules,
				site_enabled: profile.site_enabled,
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

	function askRemoveBriefing(b: UserBriefingRow) {
		if (b.id === undefined) return;
		confirmDelete = {
			kind: 'briefing',
			id: b.id,
			label: b.name,
		};
	}

	async function submitBriefing(e: SubmitEvent) {
		e.preventDefault();
		briefingError = '';
		const name = newBriefing.name.trim();
		const cron = newBriefing.cron.trim();
		if (!name || !cron) {
			briefingError = 'Name and cron are required.';
			return;
		}
		if ((newBriefing.output === 'talk' || newBriefing.output === 'both') &&
			!newBriefing.conversation_token.trim()) {
			briefingError = `Conversation token is required when output is "${newBriefing.output}".`;
			return;
		}
		let components: Record<string, unknown> | undefined;
		const raw = newBriefing.componentsJson.trim();
		if (raw) {
			try {
				const parsed = JSON.parse(raw);
				if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
					briefingError = 'Components must be a JSON object.';
					return;
				}
				components = parsed as Record<string, unknown>;
			} catch (err) {
				briefingError = `Components JSON parse error: ${(err as Error).message}`;
				return;
			}
		}
		briefingSaving = true;
		try {
			await upsertBriefing({
				name,
				cron,
				conversation_token: newBriefing.conversation_token.trim() || undefined,
				output: newBriefing.output,
				components,
				enabled: newBriefing.enabled,
			});
			newBriefing = {
				name: '',
				cron: '0 7 * * *',
				conversation_token: '',
				output: 'talk',
				componentsJson: '{"calendar": true, "todos": true, "email": true}',
				enabled: true,
			};
			await refresh();
		} catch (e) {
			briefingError = (e as Error).message || 'Save failed';
		} finally {
			briefingSaving = false;
		}
	}

	function componentsSummary(components: Record<string, unknown>): string {
		const parts: string[] = [];
		for (const [key, value] of Object.entries(components)) {
			if (key === '__output__') continue;
			if (value === true) parts.push(key);
			else if (
				typeof value === 'object' &&
				value !== null &&
				!Array.isArray(value) &&
				(value as Record<string, unknown>).enabled === true
			) {
				parts.push(key);
			}
		}
		return parts.join(', ');
	}

	async function performDelete() {
		if (!confirmDelete) return;
		const target = confirmDelete;
		confirmDelete = null;
		try {
			if (target.kind === 'resource') {
				await deleteResource(target.id);
				await refresh();
			} else if (target.kind === 'briefing') {
				await deleteBriefing(target.id);
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

<div class="settings">
	<header class="settings-header">
		<div>
			<h1>Settings</h1>
			<p class="hint">
				Profile, resources, and per-service credentials. Secrets are encrypted
				at rest and never sent back to the browser — secret fields are
				write-only.
			</p>
		</div>
	</header>

	{#if error}
		<div class="banner error">{error}</div>
	{/if}
	{#if info}
		<div class="banner info">{info}</div>
	{/if}

	{#if loading}
		<div class="placeholder">Loading…</div>
	{:else}
		{#if profile}
			{@const saveBtn = {
				dirty: profileDirty,
				saving: profileSaving,
			}}

			<section class="card">
				<header class="section-header">
					<h2>Identity</h2>
					<div class="header-actions">
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
					</div>
				</header>
				<p class="hint">
					How Istota addresses you. User ID: <code>{profile.user_id}</code>
				</p>

				<label class="field">
					<span>Display name</span>
					<input type="text" bind:value={profile.display_name} />
				</label>
				<label class="field">
					<span>Timezone (IANA)</span>
					<input type="text" placeholder="UTC" bind:value={profile.timezone} />
				</label>
				<label class="field">
					<span>Email addresses (comma-separated)</span>
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
				</label>
				<label class="field">
					<span>ntfy topic (optional)</span>
					<input type="text" bind:value={profile.ntfy_topic} />
				</label>
			</section>

			<section class="card">
				<header class="section-header">
					<h2>Preferences</h2>
					<div class="header-actions">
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
					</div>
				</header>
				<p class="hint">
					How Istota behaves for your account.
				</p>

				<label class="field">
					<span>Trusted email senders (fnmatch patterns, comma-separated)</span>
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
				</label>
				<label class="field">
					<span>Disabled skills (comma-separated)</span>
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
				</label>
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
				<label class="field checkbox">
					<input type="checkbox" bind:checked={profile.site_enabled} />
					<span>Static website hosting at /~user/</span>
				</label>

				{#if profileError}
					<div class="banner error">{profileError}</div>
				{/if}
			</section>
		{/if}

		<section class="card">
			<header class="section-header">
				<h2>Resources ({resources.length})</h2>
			</header>
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
					<label class="field">
						<span>Type</span>
						<select bind:value={newRes.type}>
							<option value="">Type…</option>
							{#each resourceTypes as rt (rt.type)}
								<option value={rt.type}>{rt.label}</option>
							{/each}
						</select>
					</label>
					<label class="field">
						<span>Path</span>
						<input
							type="text"
							placeholder="(if applicable)"
							bind:value={newRes.path}
						/>
					</label>
					<label class="field">
						<span>Display name</span>
						<input type="text" placeholder="(optional)" bind:value={newRes.name} />
					</label>
					<label class="field">
						<span>Permissions</span>
						<select bind:value={newRes.permissions}>
							<option value="read">read</option>
							<option value="readwrite">readwrite</option>
						</select>
					</label>
					<label class="field field-wide">
						<span>Extras (JSON)</span>
						<textarea
							rows="2"
							placeholder={'e.g. {"ingest_token": "…", "default_radius": 75}'}
							bind:value={newRes.extrasJson}
						></textarea>
					</label>
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
		</section>

		<section class="card">
			<header class="section-header">
				<h2>Briefings ({briefings.length})</h2>
			</header>
			<p class="hint">
				Cron-scheduled summaries posted to a Talk room or sent by email.
				Operator-managed entries (from <code>config.toml</code>) are
				read-only here.
			</p>

			{#if briefings.length === 0}
				<p class="empty">No briefings configured yet.</p>
			{:else}
				<div class="table-scroll">
					<table class="grid">
						<thead>
							<tr>
								<th class="col-name">Name</th>
								<th>Cron</th>
								<th>Output</th>
								<th>Components</th>
								<th>Token</th>
								<th class="col-source">Source</th>
								<th class="actions"></th>
							</tr>
						</thead>
						<tbody>
							{#each briefings as b (`${b.managed}-${b.id ?? b.name}`)}
								<tr>
									<td class="col-name">
										{b.name}
										{#if !b.enabled}<span class="muted"> (disabled)</span>{/if}
									</td>
									<td><code>{b.cron}</code></td>
									<td>{b.output}</td>
									<td class="muted">{componentsSummary(b.components) || '—'}</td>
									<td class="muted"><code>{b.conversation_token || '—'}</code></td>
									<td class="col-source muted">
										{b.managed === 'config' ? 'config.toml' : 'user'}
									</td>
									<td class="actions">
										{#if b.managed === 'db' && b.id !== undefined}
											<button
												class="icon-btn danger"
												title="Remove"
												type="button"
												onclick={() => askRemoveBriefing(b)}>×</button
											>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			<form class="add-resource" onsubmit={submitBriefing}>
				<h3>Add briefing</h3>
				<div class="add-grid">
					<label class="field">
						<span>Name</span>
						<input
							type="text"
							placeholder="morning"
							bind:value={newBriefing.name}
						/>
					</label>
					<label class="field">
						<span>Cron (user TZ)</span>
						<input
							type="text"
							placeholder="0 7 * * 1-5"
							bind:value={newBriefing.cron}
						/>
					</label>
					<label class="field">
						<span>Output</span>
						<select bind:value={newBriefing.output}>
							{#each briefingOutputs as opt (opt)}
								<option value={opt}>{opt}</option>
							{/each}
						</select>
					</label>
					<label class="field">
						<span>Conversation token</span>
						{#if briefingRooms.length > 0}
							<select bind:value={newBriefing.conversation_token}>
								<option value="">(paste token below)</option>
								{#each briefingRooms as room (room.token)}
									<option value={room.token}>{room.name} — {room.token}</option>
								{/each}
							</select>
						{:else}
							<input
								type="text"
								placeholder="Talk room token"
								bind:value={newBriefing.conversation_token}
							/>
						{/if}
					</label>
					{#if briefingRooms.length > 0}
						<label class="field">
							<span>Or paste a token</span>
							<input
								type="text"
								placeholder="(optional override)"
								bind:value={newBriefing.conversation_token}
							/>
						</label>
					{/if}
					<label class="field field-wide">
						<span>Components (JSON)</span>
						<textarea
							rows="2"
							placeholder={'e.g. {"calendar": true, "email": true, "markets": true}'}
							bind:value={newBriefing.componentsJson}
						></textarea>
					</label>
					<label class="field">
						<span>Enabled</span>
						<input type="checkbox" bind:checked={newBriefing.enabled} />
					</label>
				</div>
				<div class="add-actions">
					<Button
						variant="primary"
						size="sm"
						type="submit"
						disabled={briefingSaving}
					>
						{briefingSaving ? 'Saving…' : 'Add briefing'}
					</Button>
				</div>
				{#if briefingError}
					<div class="banner error">{briefingError}</div>
				{/if}
			</form>
		</section>

		{#if activeServices.length > 0}
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

		{#each activeServices as svc (svc.service)}
			<ServiceCard
				service={svc}
				onChanged={reloadServices}
				onConnect={connectGoogle}
				onDisconnect={disconnectGoogle}
				oauthBusy={oauthBusy}
			/>
		{/each}
	{/if}
</div>

{#if confirmDelete}
	<Modal
		open={true}
		title={confirmDelete.kind === 'resource' ? 'Remove resource?' : 'Remove briefing?'}
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
	.settings {
		width: 100%;
		max-width: 980px;
		margin: 0 auto;
		padding: 1.5rem 1rem 4rem;
		display: flex;
		flex-direction: column;
		gap: 1rem;
		box-sizing: border-box;
		container-type: inline-size;
		container-name: settings;
	}

	.settings-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.settings-header h1 {
		margin: 0;
		font-size: var(--text-lg, 1.05rem);
		color: var(--text-primary);
	}

	.hint {
		margin: 0.25rem 0 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
		max-width: 60ch;
	}

	.hint code,
	code {
		background: var(--surface-raised);
		padding: 0 0.3rem;
		border-radius: 0.2rem;
		font-size: 0.8em;
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}

	.dirty-badge {
		font-size: var(--text-xs);
		color: #d6a000;
	}

	.banner {
		padding: 0.4rem 0.75rem;
		border-radius: var(--radius-card);
		font-size: var(--text-sm);
	}
	.banner.error {
		background: rgba(204, 102, 102, 0.15);
		color: #e88;
	}
	.banner.info {
		background: rgba(110, 184, 132, 0.15);
		color: #8d8;
	}

	.placeholder {
		color: var(--text-dim);
		padding: 2rem 0;
		text-align: center;
	}

	.card {
		background: var(--surface-card);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-card);
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.card h2 {
		margin: 0;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.card h3 {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--text-muted);
		font-weight: 600;
	}

	.section-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
	}

	.subsection-heading {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		margin: 0.5rem 0 -0.25rem;
	}

	.subsection-heading h2 {
		margin: 0;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.subsection-heading .hint {
		margin: 0;
	}

	.empty {
		font-size: var(--text-sm);
		color: var(--text-dim);
		margin: 0;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--text-sm);
	}

	.field > span {
		color: var(--text-muted);
	}

	.field input:not([type='checkbox']),
	.field select,
	.field textarea {
		background: var(--surface-base);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		border-radius: 0.3rem;
		padding: 0.3rem 0.5rem;
		font: inherit;
		font-size: var(--text-sm);
		width: 100%;
		max-width: 24rem;
		min-width: 0;
		box-sizing: border-box;
	}

	.field textarea {
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
		resize: vertical;
	}

	.field-wide textarea {
		max-width: 36rem;
	}

	.field input:focus,
	.field select:focus,
	.field textarea:focus {
		outline: 1px solid var(--accent, #6c8ebf);
	}

	.field.checkbox {
		flex-direction: row;
		align-items: center;
		gap: 0.4rem;
		color: var(--text-primary);
	}
	.field.checkbox > span {
		color: var(--text-primary);
	}
	.field.checkbox input[type='checkbox'] {
		width: auto;
	}

	.table-scroll {
		width: 100%;
		overflow-x: auto;
		-webkit-overflow-scrolling: touch;
	}

	.grid {
		width: 100%;
		table-layout: fixed;
		border-collapse: collapse;
		font-size: var(--text-sm);
	}

	.grid th,
	.grid td {
		text-align: left;
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--border-subtle);
		vertical-align: middle;
	}

	.grid th {
		color: var(--text-dim);
		font-weight: 500;
		font-size: var(--text-xs);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

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

	.grid td.actions,
	.grid th.actions {
		text-align: right;
		width: 3rem;
		white-space: nowrap;
	}

	.muted {
		color: var(--text-dim);
	}

	.icon-btn {
		background: transparent;
		border: none;
		color: var(--text-dim);
		cursor: pointer;
		padding: 0.1rem 0.35rem;
		border-radius: 0.2rem;
		font: inherit;
		font-size: var(--text-base);
		line-height: 1;
	}

	.icon-btn:hover:not(:disabled) {
		color: var(--text-primary);
		background: var(--surface-raised);
	}

	.icon-btn.danger:hover:not(:disabled) {
		color: #e88;
	}

	.icon-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
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
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.6rem;
	}

	.add-actions {
		display: flex;
		justify-content: flex-end;
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

	@media (max-width: 768px) {
		.settings {
			padding: 1rem 0.75rem 3rem;
		}
		.settings-header {
			flex-direction: column;
			align-items: stretch;
		}
		.card {
			padding: 0.75rem;
		}
	}

	@media (max-width: 640px) {
		.settings {
			padding: 0.75rem 0.5rem 3rem;
		}
		.card {
			padding: 0.6rem;
		}
	}
</style>
