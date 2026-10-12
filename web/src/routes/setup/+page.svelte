<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import { getSetupState, unlockSetup, SetupApiError, type SetupState } from '$lib/api';
  import { Button, NoticeBanner } from '$lib/components/ui';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import SetupFrame from '$lib/components/setup/SetupFrame.svelte';
  import StepUnlock from '$lib/components/setup/StepUnlock.svelte';
  import {
    DEFAULT_BOT_NAME,
    createSetupDraft,
    isSetupStep,
    neighbourStep,
    stepInfo,
    type SetupStepId,
  } from '$lib/components/setup/setupDraft.svelte';

  /**
   * What the page is showing. `steps` is the flow itself; the rest are the
   * screens around it. `finishing` is the server's word for a setup that has
   * written everything and is on its way out.
   */
  type View = 'loading' | 'unreachable' | 'unlock' | 'configured' | 'finishing' | 'steps';

  let view = $state<View>('loading');
  let setup = $state<SetupState | null>(null);
  let unlockError = $state('');
  let unreachable = $state('');
  let step = $state<SetupStepId>('welcome');
  // False until the draft is loaded and the address's step applied, so the
  // step selection below writes nothing over a deep link before then.
  let ready = $state(false);

  const draft = createSetupDraft();

  const botName = $derived(draft.text('bot_name') || setup?.defaults?.bot_name || DEFAULT_BOT_NAME);

  const stepSel = createUrlSelection<{ step: SetupStepId }>({
    key: 'setup',
    params: ['step'],
    encode: (selection) => ({ step: selection.step }),
    decode: (params) => (params.step && isSetupStep(params.step) ? { step: params.step } : null),
    read: () => (ready ? { step } : null),
    apply: (selection) => {
      step = selection.step;
    },
  });
  stepSel.start();

  /**
   * The code from `#code=…`, read and cleared before anything else.
   *
   * A fragment never reaches the server, so the code is in no access log or
   * `Referer`; clearing it keeps it out of the history and of anything the
   * user copies from the address bar.
   */
  function takeFragmentCode(): string {
    const hash = location.hash.replace(/^#/, '');
    const code = new URLSearchParams(hash).get('code') ?? '';
    if (hash) {
      const url = location.pathname + location.search;
      try {
        replaceState(url, page.state);
      } catch {
        // Kit refuses a shallow write before its router has started (dev
        // only); the browser's own is the same write without the bookkeeping.
        history.replaceState(history.state, '', url);
      }
    }
    return code.trim();
  }

  function failed(e: unknown) {
    if (e instanceof SetupApiError && e.status === 401) {
      // The session is gone (an idle hour, or another unlock): the code is
      // what opens it again.
      view = 'unlock';
      return;
    }
    unreachable =
      e instanceof SetupApiError && e.status !== 0
        ? e.message
        : 'If setup stopped, run sudo istota-stack setup --web on the server to start it again.';
    view = 'unreachable';
  }

  async function openSteps() {
    try {
      await draft.load();
    } catch (e) {
      failed(e);
      return;
    }
    step = stepSel.current()?.step ?? 'welcome';
    ready = true;
    view = 'steps';
  }

  async function start(code: string) {
    view = 'loading';
    try {
      setup = await getSetupState();
    } catch (e) {
      failed(e);
      return;
    }
    if (setup.phase === 'configured') {
      view = 'configured';
    } else if (setup.phase === 'finishing') {
      view = 'finishing';
    } else if (setup.phase === 'open') {
      await openSteps();
    } else if (code) {
      try {
        await unlockSetup(code);
      } catch (e) {
        if (e instanceof SetupApiError && e.status === 0) {
          failed(e);
        } else {
          unlockError = e instanceof SetupApiError ? e.message : String(e);
          view = 'unlock';
        }
        return;
      }
      await openSteps();
    } else {
      view = 'unlock';
    }
  }

  onMount(() => {
    void start(takeFragmentCode());
  });

  function go(delta: -1 | 1) {
    const next = neighbourStep(step, delta);
    if (next) stepSel.push({ step: next });
  }

  const title = $derived(stepInfo(step).title);
  const isFirst = $derived(neighbourStep(step, -1) === null);
  const isLast = $derived(neighbourStep(step, 1) === null);
</script>

{#if view === 'loading'}
  <div class="center-msg">Loading…</div>
{:else if view === 'unreachable'}
  <SetupFrame {botName} title="Setup">
    <NoticeBanner variant="danger" title="Can't reach the setup service" collapsed={false}>
      {#snippet children()}<p>{unreachable}</p>{/snippet}
    </NoticeBanner>
    <div class="form-actions">
      <Button variant="secondary" onclick={() => start('')}>Retry</Button>
    </div>
  </SetupFrame>
{:else if view === 'unlock'}
  <SetupFrame {botName} title="Enter your setup code">
    <StepUnlock bind:error={unlockError} onUnlocked={openSteps} />
  </SetupFrame>
{:else if view === 'configured'}
  <SetupFrame {botName} title="Istota is already set up">
    <p>This server is set up and running. Sign in to use it.</p>
    <div class="form-actions">
      <Button variant="primary" href="{base}/login" reload>Sign in</Button>
    </div>
  </SetupFrame>
{:else if view === 'finishing'}
  <SetupFrame {botName} title="Setup has finished">
    <p>Your settings are saved and {botName} is starting. This page can be closed.</p>
  </SetupFrame>
{:else}
  <SetupFrame
    {botName}
    {step}
    {title}
    onBack={isFirst ? undefined : () => go(-1)}
    onNext={isLast ? undefined : () => go(1)}
  >
    <!-- Each screen arrives as its own component in a later stage. -->
    <p class="muted">This screen is not built yet.</p>
  </SetupFrame>
{/if}
