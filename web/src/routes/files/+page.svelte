<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Button,
    FormModal,
    Field,
    Input,
    FileDropZone,
    HeaderNav,
    EmptyState,
    Badge,
    type KebabItem,
    PageLoading,
    Checkbox,
  } from '$lib/components/ui';
  import {
    getWorkspacePins,
    putWorkspacePin,
    deleteWorkspacePin,
    type WorkspacePin,
    type WorkspacePinInput,
    getWorkspacePlaces,
    getWorkspaceListing,
    getWorkspaceDriveListing,
    getWorkspaceDriveParent,
    type DriveListing,
    createWorkspaceFolder,
    deleteWorkspaceEntry,
    uploadWorkspaceFile,
    type WorkspaceAccess,
    type WorkspacePlace,
    type WorkspaceListing,
    getWorkspaceScripts,
    type WorkspaceScriptsIndex,
    getWorkspaceConfigFiles,
    getWorkspaceConfigFile,
    saveWorkspaceConfigFile,
    type WorkspaceConfigFile,
  } from '$lib/api';
  import TextFileEditor from '$lib/components/TextFileEditor.svelte';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import ScriptIndex from '$lib/components/workspace/ScriptIndex.svelte';
  import { brokenKind } from '$lib/components/workspace/scriptFiles';
  import BotSection from '$lib/components/workspace/BotSection.svelte';
  import ConfigIndex from '$lib/components/workspace/ConfigIndex.svelte';
  import PlaceIndex from '$lib/components/workspace/PlaceIndex.svelte';
  import { configFileDescription } from '$lib/components/workspace/configFiles';
  import {
    INDEX_VIEW_LABELS,
    indexViewForKind,
    parseIndexView,
    type IndexView,
  } from '$lib/components/workspace/indexViews';
  import { getCurrentUser } from '$lib/userContext';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { loadSetting, saveSetting } from '$lib/stores/persisted';
  import PlaceList from '$lib/components/workspace/PlaceList.svelte';
  import ReachPanel from '$lib/components/workspace/ReachPanel.svelte';
  import ReachBadge from '$lib/components/workspace/ReachBadge.svelte';
  import {
    placeForRef,
    sharedAccess,
    taskReachNote,
    type TaskReachScope,
  } from '$lib/components/workspace/reach';
  import { BotOff } from '@lucide/svelte';
  import PinnedList from '$lib/components/workspace/PinnedList.svelte';
  import FileTable from '$lib/components/workspace/FileTable.svelte';

  let pins = $state<WorkspacePin[]>([]);
  let pinsBusy = $state(false);
  let pinsLoading = $state(true);
  let pinError = $state('');
  async function loadPins() {
    pinsLoading = true;
    pinError = '';
    try {
      const result = await getWorkspacePins();
      if (active) pins = result.pins;
    } catch {
      if (active) pinError = 'Could not load pins.';
    } finally {
      pinsLoading = false;
    }
  }
  async function removePin(pin: WorkspacePin) {
    if (pinsBusy) return;
    pinsBusy = true;
    pinError = '';
    try {
      await deleteWorkspacePin(pin.id);
      if (active) pins = pins.filter((item) => item.id !== pin.id);
    } catch (e) {
      pinError = e instanceof Error ? e.message : 'Could not remove this pin.';
    } finally {
      pinsBusy = false;
    }
  }
  async function togglePin(input: WorkspacePinInput) {
    if (pinsBusy || pinsLoading) return;
    const existing = pins.find((pin) => pin.provider === input.provider && pin.ref === input.ref);
    if (existing) return removePin(existing);
    pinsBusy = true;
    pinError = '';
    try {
      const result = await putWorkspacePin(input);
      if (active) pins = [...pins, result.pin];
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Could not pin this file.';
      pinError =
        message === 'pin_limit'
          ? 'You can pin up to 50 items. Remove a pin to add another.'
          : message;
    } finally {
      pinsBusy = false;
    }
  }

  const identity = getCurrentUser();
  const botName = $derived(identity.user?.bot_name || 'Istota');

  let scriptsIndex = $state<WorkspaceScriptsIndex | null>(null);
  let scriptsLoading = $state(true);
  let scriptsError = $state('');
  async function loadScripts() {
    scriptsLoading = true;
    scriptsError = '';
    try {
      const result = await getWorkspaceScripts();
      if (active) scriptsIndex = result;
    } catch {
      if (active) scriptsError = 'Could not load your scripts.';
    } finally {
      scriptsLoading = false;
    }
  }
  let scriptWarnings = $derived({
    ...(scriptsIndex?.broken.some((item) => brokenKind(item) === 'script')
      ? { scripts: 'Jobs refer to missing scripts' }
      : {}),
    ...(scriptsIndex?.broken.some((item) => brokenKind(item) === 'prompt')
      ? { prompts: 'Jobs refer to missing prompt files' }
      : {}),
  });

  let configFiles = $state<WorkspaceConfigFile[]>([]);
  let configLoading = $state(true);
  let configError = $state('');
  let editing = $state<WorkspaceConfigFile | null>(null);
  let editorOpen = $state(false);
  async function loadConfigFiles() {
    configError = '';
    try {
      const result = await getWorkspaceConfigFiles();
      if (active) configFiles = result.files;
    } catch {
      if (active) configError = 'Could not load your configuration files.';
    } finally {
      configLoading = false;
    }
  }
  function retryConfigFiles() {
    configLoading = true;
    void loadConfigFiles();
  }
  function editConfigFile(file: WorkspaceConfigFile) {
    sidebarOpen = false;
    editing = file;
    editorOpen = true;
  }
  function closeEditor() {
    editorOpen = false;
    // Sizes and "not created yet" change with a save.
    void loadConfigFiles();
  }
  async function saveConfigFile(name: string | number, content: string, revision: string) {
    const result = await saveWorkspaceConfigFile(String(name), content, revision);
    return {
      revision: result.revision,
      note:
        result.applies === 'next_sync'
          ? 'Saved. The jobs update at the next CRON.md sync.'
          : undefined,
    };
  }
  // A view shows an index in the main area; place and path are then empty.
  type Selection = { place: string; path: string; view?: IndexView };
  let places = $state<WorkspacePlace[]>([]);
  let notes = $state<string[]>([]);
  let loading = $state(true);
  let error = $state('');
  let sidebarOpen = $state(false);
  let selected = $state<Selection | null>(null);
  let place = $derived(places.find((item) => item.id === selected?.place));
  let view = $derived(selected?.view ?? null);
  let botViews = $derived(
    (['rooms', 'groups', 'config', 'skills', 'scripts', 'prompts'] as IndexView[]).filter(
      (item) => {
        if (item === 'rooms') return places.some((p) => p.kind === 'room');
        if (item === 'groups') return places.some((p) => p.kind === 'group');
        if (item === 'skills') return configFiles.some((file) => file.kind === 'skill');
        if (item === 'prompts') return !!scriptsIndex?.files.some((file) => file.kind === 'prompt');
        return true;
      },
    ),
  );
  // Configuration, skills, scripts and prompts all live in the user's own folder, so they reach as it does.
  let botReach = $derived.by(() => {
    const result: Partial<Record<IndexView, { access: WorkspaceAccess; scope?: TaskReachScope }>> =
      {};
    const homeAccess = places.find((item) => item.kind === 'home')?.reach.private;
    if (homeAccess) {
      for (const item of ['config', 'skills', 'scripts', 'prompts'] as IndexView[]) {
        result[item] = { access: homeAccess };
      }
    }
    const roomAccess = sharedAccess(places.filter((item) => item.kind === 'room'));
    if (roomAccess) result.rooms = { access: roomAccess, scope: 'rooms' };
    const groupAccess = sharedAccess(places.filter((item) => item.kind === 'group'));
    if (groupAccess) result.groups = { access: groupAccess, scope: 'group' };
    return result;
  });
  function indexReachNote(item: IndexView): string | null {
    const entry = botReach[item];
    return entry ? taskReachNote(entry.access, botName, entry.scope) : null;
  }
  let parentView = $derived(indexViewForKind(place?.kind));
  const sidebarKinds: string[] = ['linked', 'drive', 'nextcloud', 'talk'];
  let botCurrent = $derived<IndexView | 'home' | null>(
    view ?? (place?.kind === 'home' ? 'home' : indexViewForKind(place?.kind)),
  );
  let listing = $state<WorkspaceListing | null>(null);
  let driveListing = $state<DriveListing | null>(null);
  let moreLoading = $state(false);
  let moreError = $state('');
  let driveRoot = $derived(selected?.path.split('/')[0] === 'shared' ? 'shared' : 'root');
  let listingLoading = $state(false);
  let listingError = $state('');
  let showHidden = $state(loadSetting<boolean>('workspace.showHidden', false) === true);
  let fileInput = $state<HTMLInputElement>();
  let folderOpen = $state(false);
  let folderName = $state('');
  let folderPath = $state('');
  type Upload = {
    file: File;
    path: string;
    progress: number;
    state: 'waiting' | 'uploading' | 'done' | 'error';
    message: string;
  };
  let uploads = $state<Upload[]>([]);
  let uploading = false;

  function openFolderDialog() {
    folderPath = selected?.path || '';
    folderName = '';
    folderOpen = true;
  }
  async function refreshIfCurrent(path: string) {
    if (active && selected?.place === 'home' && selected.path === path) await loadFolder(selected);
  }
  async function createFolder() {
    await createWorkspaceFolder(folderPath, folderName.trim());
    folderOpen = false;
    await refreshIfCurrent(folderPath);
  }
  function enqueue(files: File[]) {
    if (selected?.place !== 'home') return;
    uploads.push(
      ...files.map((file): Upload => ({
        file,
        path: selected!.path,
        progress: 0,
        state: 'waiting',
        message: '',
      })),
    );
    void runUploads();
  }
  async function runUploads() {
    if (uploading) return;
    uploading = true;
    try {
      for (const row of uploads) {
        if (!active || row.state !== 'waiting') continue;
        row.state = 'uploading';
        try {
          const result = await uploadWorkspaceFile(
            row.path,
            row.file,
            (percent) => (row.progress = percent),
          );
          row.state = 'done';
          row.message = result.name === row.file.name ? 'Uploaded' : `Saved as ${result.name}`;
          await refreshIfCurrent(row.path);
        } catch (e) {
          row.state = 'error';
          row.message = e instanceof Error ? e.message : 'Could not upload this file.';
        }
      }
    } finally {
      uploading = false;
    }
  }
  async function deleteEntries(names: string[]) {
    const path = selected?.path || '';
    const request = generation;
    const deleted: string[] = [];
    const errors: string[] = [];
    for (const name of names) {
      try {
        await deleteWorkspaceEntry(path, name);
        deleted.push(name);
      } catch (e) {
        errors.push(`${name}: ${e instanceof Error ? e.message : 'Could not delete this item.'}`);
      }
    }
    if (active && request === generation && listing) {
      listing = {
        ...listing,
        entries: listing.entries.filter((entry) => !deleted.includes(entry.name)),
      };
    }
    if (active && deleted.length) void loadPins();
    return { deleted, errors };
  }
  let generation = 0;
  let active = true;
  const selection = createUrlSelection<Selection>({
    key: 'workspace',
    params: ['place', 'path', 'view'],
    encode: (value): Record<string, string> =>
      value.view
        ? { view: value.view }
        : { place: value.place, ...(value.path ? { path: value.path } : {}) },
    decode: (params) => {
      const view = parseIndexView(params.view);
      if (view) return { place: '', path: '', view };
      return { place: params.place || places[0]?.id || 'home', path: params.path || '' };
    },
    read: () => selected,
    apply: loadFolder,
  });
  selection.start();

  function href(place: string, path = '') {
    return `${base}/files?${new URLSearchParams({ place, ...(path ? { path } : {}) })}`;
  }
  function viewHref(view: IndexView) {
    return `${base}/files?${new URLSearchParams({ view })}`;
  }
  function plainClick(event: MouseEvent) {
    return !(
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    );
  }
  function navigate(event: MouseEvent, place: string, path = '') {
    if (!plainClick(event)) return;
    event.preventDefault();
    sidebarOpen = false;
    selection.push({ place, path });
  }
  function navigateView(event: MouseEvent, view: IndexView) {
    if (!plainClick(event)) return;
    event.preventDefault();
    sidebarOpen = false;
    selection.push({ place: '', path: '', view });
  }
  function driveFolder(path: string) {
    return path.split('/').filter(Boolean).at(-1)?.split(':')[0] || 'root';
  }
  function revealAction(
    ref: string | null | undefined,
    provider: 'workspace' | 'drive' = 'workspace',
    available = true,
  ): KebabItem {
    const unavailable = { label: 'Reveal', disabled: true };
    if (!ref || !available) return unavailable;
    if (provider === 'drive') {
      const drive = places.find((place) => place.kind === 'drive' && place.browsable);
      if (!drive) return unavailable;
      return { label: 'Reveal', onSelect: () => void revealDrive(ref, drive.id) };
    }
    const place = placeForRef(places, ref);
    if (!place) return unavailable;
    const relative = ref.slice(place.view_root!.length).replace(/^\//, '');
    const path = relative.includes('/') ? relative.slice(0, relative.lastIndexOf('/')) : '';
    return {
      label: 'Reveal',
      href: href(place.id, path),
      onClick: (event) => navigate(event, place.id, path),
    };
  }
  async function revealDrive(ref: string, place: string) {
    pinError = '';
    try {
      const result = await getWorkspaceDriveParent(ref);
      if (!active) return;
      sidebarOpen = false;
      selection.push({ place, path: result.folder });
    } catch (error) {
      if (active) pinError = error instanceof Error ? error.message : 'Could not reveal this file.';
    }
  }
  function breadcrumbLabel(part: string) {
    if (place?.kind !== 'drive') return part;
    if (part === 'shared') return 'Shared with me';
    const colon = part.indexOf(':');
    if (colon < 0) return 'Folder';
    try {
      return decodeURIComponent(part.slice(colon + 1));
    } catch {
      return 'Folder';
    }
  }
  function needsConnection(message: string) {
    return message === 'drive_not_granted' || message.includes('Reconnect in Settings.');
  }
  async function loadMore() {
    if (moreLoading || !driveListing?.next_page_token || !selected) return;
    const request = generation;
    const folder = driveFolder(selected.path);
    moreLoading = true;
    moreError = '';
    try {
      const result = await getWorkspaceDriveListing(folder, driveListing.next_page_token);
      if (request !== generation) return;
      const seen = new Set(driveListing.items.map((item) => item.id));
      driveListing = {
        ...result,
        items: [...driveListing.items, ...result.items.filter((item) => !seen.has(item.id))],
      };
    } catch (e) {
      if (request === generation)
        moreError = e instanceof Error ? e.message : 'Could not load more files.';
    } finally {
      if (request === generation) moreLoading = false;
    }
  }
  async function loadFolder(value: Selection) {
    const request = ++generation;
    selected = value;
    listing = null;
    driveListing = null;
    moreLoading = false;
    moreError = '';
    listingError = '';
    listingLoading = false;
    if (value.view) return;
    if (!places.find((item) => item.id === value.place)?.browsable) return;
    listingLoading = true;
    try {
      if (value.place === 'drive') {
        const result = await getWorkspaceDriveListing(driveFolder(value.path));
        if (request !== generation) return;
        driveListing = result;
      } else {
        const result = await getWorkspaceListing(value.place, value.path);
        if (request !== generation) return;
        listing = result;
        selected = { place: result.place, path: result.path };
      }
    } catch (e) {
      if (request === generation)
        listingError =
          e instanceof Error && !(e instanceof TypeError)
            ? e.message
            : 'Could not load this folder.';
    } finally {
      if (request === generation) listingLoading = false;
    }
  }
  async function load() {
    loading = true;
    error = '';
    try {
      const result = await getWorkspacePlaces();
      if (!active) return;
      places = result.places;
      notes = result.notes;
      if (places.length) void loadFolder(selection.current()!);
    } catch {
      error = 'Could not load your files.';
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    void load();
    void loadPins();
    void loadConfigFiles();
    void loadScripts();
    return () => {
      active = false;
      generation++;
    };
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Files"
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open files"
    >
      {#snippet leading()}<SidebarToggle
          open={sidebarOpen}
          label="Files"
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />{/snippet}
    </ShellHeader>
  {/snippet}
  {#snippet sidebar()}
    {#if places.length || pins.length || configFiles.length || configError}
      <Sidebar title="Files" open={sidebarOpen} onClose={() => (sidebarOpen = false)}>
        <PinnedList
          {pins}
          {places}
          {botName}
          busy={pinsBusy}
          onRemove={removePin}
          {configFiles}
          onEdit={editConfigFile}
          {revealAction}
          {href}
          onNavigate={navigate}
        />
        <BotSection
          label={botName}
          home={places.find((place) => place.kind === 'home')}
          views={botViews}
          warnings={scriptWarnings}
          current={botCurrent}
          viewOpen={view !== null}
          {href}
          {viewHref}
          onNavigate={navigate}
          onNavigateView={navigateView}
        />
        <PlaceList
          places={places.filter((place) => sidebarKinds.includes(place.kind))}
          {botName}
          selected={selected?.place || ''}
          {href}
          onNavigate={navigate}
        />
      </Sidebar>
    {/if}
  {/snippet}
  {#if pinError}
    <div class="pin-error" role="alert">
      <span class="form-error">{pinError}</span>{#if pinError === 'Could not load pins.'}<Button
          size="sm"
          variant="secondary"
          onclick={loadPins}>Retry pins</Button
        >{/if}
    </div>
  {/if}
  {#if loading}
    <PageLoading label="Loading files…" />
  {:else if error}
    <div class="center-msg error">
      <p>{error}</p>
      <Button variant="secondary" onclick={load}>Retry</Button>
    </div>
  {:else if !places.length}
    <EmptyState>No files or connections are available.</EmptyState>
  {:else if view}
    <div class="folder-toolbar">
      <nav aria-label="Folder path">
        <ol>
          <li>
            <a
              href={viewHref(view)}
              onclick={(event) => navigateView(event, view!)}
              aria-current="page">{INDEX_VIEW_LABELS[view]}</a
            >
          </li>
        </ol>
      </nav>
    </div>
    <!-- Disabled, but it still takes a drop, so a file let go here does not navigate the tab away. -->
    <FileDropZone overlay disabled onFiles={enqueue}>
      {#if view === 'rooms'}
        <PlaceIndex
          label="Rooms"
          caption="Each room's folder."
          reachNote={indexReachNote('rooms')}
          empty="You have no room folders."
          places={places.filter((item) => item.kind === 'room')}
          {href}
          onNavigate={navigate}
        />
      {:else if view === 'groups'}
        <PlaceIndex
          label="Groups"
          caption="Folders shared with the groups you belong to."
          reachNote={indexReachNote('groups')}
          empty="You have no group folders."
          places={places.filter((item) => item.kind === 'group')}
          {href}
          onNavigate={navigate}
        />
      {:else if view === 'scripts' || view === 'prompts'}
        <ScriptIndex
          label={INDEX_VIEW_LABELS[view]}
          kind={view === 'scripts' ? 'script' : 'prompt'}
          caption={view === 'scripts'
            ? `Programs in your scripts folder. ${botName} runs them from scheduled jobs and checks.`
            : 'Prompts in your scripts folder that scheduled jobs read as their instructions.'}
          empty={view === 'scripts' ? 'No scripts yet.' : 'No prompt files yet.'}
          reachNote={indexReachNote(view)}
          index={scriptsIndex}
          loading={scriptsLoading}
          error={scriptsError}
          onOpen={(path) => viewer.openFile(path)}
          onRetry={loadScripts}
          {revealAction}
        />
      {:else if view === 'config' || view === 'skills'}
        <ConfigIndex
          label={INDEX_VIEW_LABELS[view]}
          caption={view === 'config'
            ? `${botName} reads these as instructions.`
            : "Added to a skill's instructions whenever it loads."}
          empty={view === 'config'
            ? 'No configuration files are available.'
            : 'No skills take instructions from you.'}
          reachNote={indexReachNote(view)}
          files={configFiles.filter(
            (file) => file.kind === (view === 'config' ? 'config' : 'skill'),
          )}
          loading={configLoading}
          error={configError}
          onEdit={editConfigFile}
          onRetry={retryConfigFiles}
          {revealAction}
        />
      {/if}
    </FileDropZone>
  {:else if !place}
    <EmptyState>This place is no longer available.</EmptyState>
  {:else}
    <div class="folder-toolbar">
      <nav aria-label="Folder path">
        <ol>
          {#if parentView}
            <li>
              <a href={viewHref(parentView)} onclick={(event) => navigateView(event, parentView!)}
                >{INDEX_VIEW_LABELS[parentView]}</a
              ><span class="muted" aria-hidden="true">/</span>
            </li>
          {/if}
          <li>
            <a
              href={href(place.id)}
              onclick={(event) => navigate(event, place.id)}
              aria-current={!selected?.path ? 'page' : undefined}>{place.label}</a
            >
          </li>
          {#each (selected?.path || '').split('/').filter(Boolean) as part, i}
            {@const parts = selected!.path.split('/').filter(Boolean)}
            <li>
              <span class="muted" aria-hidden="true">/</span><a
                href={href(place.id, parts.slice(0, i + 1).join('/'))}
                onclick={(event) => navigate(event, place!.id, parts.slice(0, i + 1).join('/'))}
                aria-current={i === parts.length - 1 ? 'page' : undefined}
                >{breadcrumbLabel(part)}</a
              >
            </li>
          {/each}
        </ol>
      </nav>
      <div class="folder-actions hstack wrap">
        {#if place.kind === 'nextcloud'}
          <Badge size="sm"
            ><span class="only-you"><BotOff size={12} aria-hidden="true" />Only you</span></Badge
          >
        {:else}
          <ReachBadge access={place.reach.private} />
        {/if}
        {#if place.kind === 'group'}
          <HeaderNav
            ariaLabel="Group"
            items={[
              {
                label: 'Group overview',
                href: `${base}/groups?${new URLSearchParams({ group: place.id.slice('group:'.length) })}`,
              },
            ]}
          />
        {/if}
        {#if place.kind === 'drive'}
          <HeaderNav
            ariaLabel="Drive location"
            items={[
              { label: 'My Drive', href: href('drive'), active: driveRoot === 'root' },
              {
                label: 'Shared with me',
                href: href('drive', 'shared'),
                active: driveRoot === 'shared',
              },
            ]}
          />
        {/if}
        {#if place.id === 'home' && listing && !listingLoading && !listingError}
          <Button variant="secondary" size="sm" onclick={openFolderDialog}>New folder</Button>
          <Button size="sm" onclick={() => fileInput?.click()}>Upload</Button>
          <input
            bind:this={fileInput}
            type="file"
            multiple
            hidden
            aria-label="Upload files"
            onchange={(event) => {
              enqueue(Array.from(event.currentTarget.files || []));
              event.currentTarget.value = '';
            }}
          />
        {/if}
        {#if place.browsable && place.kind !== 'drive'}
          <Checkbox
            class="hidden-toggle"
            label="Show hidden"
            tone="muted"
            bind:checked={showHidden}
            onchange={() => saveSetting('workspace.showHidden', showHidden)}
          />
        {/if}
      </div>
    </div>
    <ReachPanel {place} {notes} {botName} />
    {#if uploads.length}
      <section class="uploads" aria-label="Uploads">
        <div class="upload-heading hstack wrap">
          <span class="micro-label">Uploads</span>
          {#if !uploads.some((row) => row.state === 'waiting' || row.state === 'uploading')}
            <Button size="sm" variant="secondary" onclick={() => (uploads = [])}>Dismiss</Button>
          {/if}
        </div>
        <ul class="list-reset">
          {#each uploads as row}
            <li class="upload-row">
              <span class="upload-name"
                >{row.file.name}<span class="caption"
                  >My files{row.path ? ` / ${row.path}` : ''}</span
                ></span
              >
              <span
                class:form-error={row.state === 'error'}
                class="upload-status"
                aria-live="polite"
              >
                {row.state === 'waiting'
                  ? 'Waiting'
                  : row.state === 'uploading'
                    ? row.progress === 100
                      ? 'Saving…'
                      : `${row.progress}%`
                    : row.message}
              </span>
              {#if row.state === 'uploading'}<progress
                  max="100"
                  value={row.progress}
                  aria-label={`Uploading ${row.file.name}`}
                ></progress>{/if}
            </li>
          {/each}
        </ul>
      </section>
    {/if}
    <FileDropZone
      overlay
      disabled={place.id !== 'home' || !listing || listingLoading || !!listingError}
      onFiles={enqueue}
    >
      {#if !place.browsable}
        <div class="center-msg">
          {place.kind === 'talk'
            ? 'Talk attachments are available to tasks, but cannot be browsed here.'
            : 'This connection is reached through a skill during a task.'}
        </div>
      {:else if listingLoading}
        <PageLoading label="Loading folder…" />
      {:else if listingError}
        <div class="center-msg error">
          {#if (place.kind === 'drive' || place.kind === 'nextcloud') && needsConnection(listingError)}
            <p>{listingError === 'drive_not_granted' ? 'Drive access is off' : listingError}</p>
            <Button variant="secondary" href={`${base}/settings/connections`}>Connections</Button>
          {:else}
            <p>{listingError}</p>
            <Button variant="secondary" onclick={() => loadFolder(selected!)}>Retry</Button>
          {/if}
        </div>
      {:else if driveListing}
        <FileTable
          {place}
          {pins}
          pinsBusy={pinsBusy || pinsLoading}
          onTogglePin={togglePin}
          driveItems={driveListing.items}
          drivePath={selected?.path || ''}
          showHidden={true}
          {href}
          onNavigate={navigate}
          onRefresh={() => loadFolder(selected!)}
        />
        {#if driveListing.next_page_token}
          <div class="drive-pagination">
            {#if moreError}<p class="form-error" role="alert">
                {moreError === 'drive_not_granted' ? 'Drive access is off' : moreError}
              </p>{/if}
            {#if needsConnection(moreError)}
              <Button variant="secondary" href={`${base}/settings/connections`}>Connections</Button>
            {:else}
              <Button
                variant="secondary"
                size="sm"
                loading={moreLoading}
                loadingLabel="Loading…"
                onclick={loadMore}>Load more</Button
              >
            {/if}
          </div>
        {/if}
      {:else if listing}
        <FileTable
          {listing}
          onDelete={place.id === 'home' ? deleteEntries : undefined}
          onRefresh={() => loadFolder(selected!)}
          {place}
          {showHidden}
          {href}
          {pins}
          pinsBusy={pinsBusy || pinsLoading}
          onTogglePin={togglePin}
          onNavigate={navigate}
        />
      {/if}
    </FileDropZone>
  {/if}
</AppShell>

{#if editing}
  <TextFileEditor
    bind:open={editorOpen}
    fileKey={editing.name}
    title={editing.name}
    description={configFileDescription(editing)}
    ariaLabel={`${editing.name} (markdown)`}
    placeholder="This file is empty."
    loadFailed="Couldn't load this file."
    load={(name) => getWorkspaceConfigFile(String(name))}
    save={saveConfigFile}
    onClose={closeEditor}
  >
    {#snippet caption()}
      Markdown. Saved in your config folder.
    {/snippet}
  </TextFileEditor>
{/if}

<FormModal
  bind:open={folderOpen}
  title="New folder"
  onOpenAutoFocus={(event) => {
    event.preventDefault();
    document.getElementById('workspace-folder-name')?.focus();
  }}
  description={`Create in My files${folderPath ? ` / ${folderPath}` : ''}`}
  width="420px"
  submitLabel="Create folder"
  busyLabel="Creating…"
  canSubmit={!!folderName.trim()}
  onSubmit={createFolder}
  onCancel={() => (folderOpen = false)}
>
  <Field label="Folder name"><Input id="workspace-folder-name" bind:value={folderName} /></Field>
</FormModal>

<style>
  .only-you {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
  .pin-error {
    padding: var(--space-2) var(--space-3);
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .drive-pagination {
    padding: var(--space-3);
    text-align: center;
  }
  .folder-actions {
    min-height: var(--control-height-sm);
  }
  .uploads {
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    max-height: 30vh;
    overflow-y: auto;
    flex-shrink: 0;
  }
  .upload-heading {
    justify-content: space-between;
  }
  .upload-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
    gap: var(--space-2);
    padding: var(--space-2) 0;
    font-size: var(--text-sm);
  }
  .upload-name {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .upload-status {
    text-align: right;
  }
  progress {
    grid-column: 1 / -1;
    width: 100%;
    height: var(--space-1);
    accent-color: var(--accent);
  }
  .folder-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
    padding: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  nav {
    min-width: 0;
  }
  ol {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  a {
    color: var(--text-muted);
    text-decoration: none;
  }
  a[aria-current='page'] {
    color: var(--text-primary);
    font-weight: var(--weight-medium);
  }
  a:hover {
    text-decoration: underline;
  }
  .folder-toolbar :global(.hidden-toggle) {
    white-space: nowrap;
  }
  .folder-toolbar :global(.ui-checkbox-input) {
    margin: 0;
  }
  @media (pointer: coarse) {
    li a {
      min-height: var(--touch-min);
      display: inline-flex;
      align-items: center;
    }
    .folder-toolbar :global(.hidden-toggle) {
      min-height: var(--touch-min);
    }
  }
</style>
