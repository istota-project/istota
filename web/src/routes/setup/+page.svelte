<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getSetupState,
    unlockSetup,
    finishSetup,
    validateSetupStep,
    SetupApiError,
    type SetupFieldError,
    type SetupFinished,
    type SetupState,
  } from '$lib/api';
  import { Button, NoticeBanner } from '$lib/components/ui';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import SetupFrame from '$lib/components/setup/SetupFrame.svelte';
  import StepUnlock from '$lib/components/setup/StepUnlock.svelte';
  import StepWelcome from '$lib/components/setup/StepWelcome.svelte';
  import StepAccount from '$lib/components/setup/StepAccount.svelte';
  import StepModel from '$lib/components/setup/StepModel.svelte';
  import StepAddress from '$lib/components/setup/StepAddress.svelte';
  import StepNextcloud from '$lib/components/setup/StepNextcloud.svelte';
  import StepConnections from '$lib/components/setup/StepConnections.svelte';
  import StepFeatures from '$lib/components/setup/StepFeatures.svelte';
  import StepReview from '$lib/components/setup/StepReview.svelte';
  import StepFinishing from '$lib/components/setup/StepFinishing.svelte';
  import {
    BROWSER_PROFILE,
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
   * written everything and is on its way out, found on a reload; `finished`
   * is this page's own Finish having been answered, which carries where to
   * go next.
   */
  type View =
    'loading' | 'unreachable' | 'unlock' | 'configured' | 'finishing' | 'finished' | 'steps';

  let view = $state<View>('loading');
  let setup = $state<SetupState | null>(null);
  let unlockError = $state('');
  let unreachable = $state('');
  let step = $state<SetupStepId>('welcome');
  let finished = $state<SetupFinished | null>(null);
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
      errors = {};
      formError = '';
      offerSkip = false;
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

  /** What a screen may hand the page before its answers are saved and checked. */
  interface StepHooks {
    /** Client-side refusals, by field, that keep the screen from saving. */
    precheck?: () => Record<string, string> | null;
    /** After the server has accepted the screen: false to stay (a failed test). */
    beforeNext?: () => Promise<boolean>;
  }

  let stepRef = $state<StepHooks | undefined>();
  let errors = $state<Record<string, string>>({});
  let formError = $state('');
  let busy = $state(false);
  let offerSkip = $state(false);

  function go(delta: -1 | 1) {
    const next = neighbourStep(step, delta);
    if (!next) return;
    errors = {};
    formError = '';
    offerSkip = false;
    stepSel.push({ step: next });
  }

  function byField(fields: SetupFieldError[]): Record<string, string> {
    const out: Record<string, string> = {};
    for (const f of fields) if (!(f.field in out)) out[f.field] = f.message;
    return out;
  }

  /**
   * Next: save the screen's answers, have the server check them, then run
   * whatever the screen still owes (a connection test) before moving on.
   * `skipChecks` is "Continue without testing", which still saves and
   * validates.
   */
  async function advance(skipChecks = false) {
    if (busy) return;
    const refused = stepRef?.precheck?.();
    if (refused) {
      errors = refused;
      return;
    }
    busy = true;
    formError = '';
    try {
      await draft.save();
      const result = await validateSetupStep(step);
      errors = byField(result.fields);
      formError = errors[''] ?? '';
      if (!result.ok) return;
      if (!skipChecks && stepRef?.beforeNext && !(await stepRef.beforeNext())) return;
      go(1);
    } catch (e) {
      if (e instanceof SetupApiError && e.status === 401) {
        view = 'unlock';
        return;
      }
      if (e instanceof SetupApiError) errors = byField(e.fields);
      formError = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  /**
   * Finish: save anything typed since the last Next, then have the server
   * check and write the lot. A refusal names the earliest screen with an
   * error, and the page goes there with the errors under their fields.
   */
  async function finish() {
    if (busy) return;
    busy = true;
    errors = {};
    formError = '';
    try {
      await draft.save();
      finished = await finishSetup();
      view = 'finished';
    } catch (e) {
      if (e instanceof SetupApiError && e.status === 401) {
        view = 'unlock';
        return;
      }
      if (e instanceof SetupApiError && e.status === 409) {
        // An earlier Finish already wrote everything.
        view = 'finishing';
        return;
      }
      if (e instanceof SetupApiError && e.status === 400 && e.step && isSetupStep(e.step)) {
        const target = e.step;
        if (target !== step) stepSel.push({ step: target });
        errors = byField(e.fields.filter((f) => !f.step || f.step === target));
        formError = errors[''] ?? e.message;
        return;
      }
      formError = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  const title = $derived(stepInfo(step).title);
  const isFirst = $derived(neighbourStep(step, -1) === null);
  const isLast = $derived(neighbourStep(step, 1) === null);
  // The screens whose Next runs a connection test that has not passed yet.
  const testsOnNext = $derived(step === 'model' || step === 'nextcloud' || step === 'connections');
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
{:else if view === 'finished' && finished}
  <SetupFrame {botName} title="Finishing setup">
    <StepFinishing
      {botName}
      loginUrl={finished.login_url}
      email={finished.email}
      browser={draft.list('profiles').includes(BROWSER_PROFILE)}
      subscription={draft.text('brain_kind') === 'claude_code'}
      internet={(draft.text('ingress') || 'local') !== 'local'}
      nextcloud={draft.text('storage') === 'nextcloud'}
    />
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
    onNext={isLast ? () => finish() : () => advance()}
    nextLabel={isLast ? 'Finish setup' : 'Next'}
    nextLoading={busy}
    nextLoadingLabel={isLast ? 'Finishing…' : testsOnNext ? 'Testing…' : 'Saving…'}
  >
    {#snippet actions()}
      {#if testsOnNext && offerSkip}
        <Button variant="secondary" disabled={busy} onclick={() => advance(true)}
          >Continue without testing</Button
        >
      {/if}
    {/snippet}
    {#if formError}
      <NoticeBanner variant="danger" title={formError} />
    {/if}
    {#if step === 'welcome'}
      <StepWelcome
        bind:this={stepRef}
        {draft}
        {errors}
        defaultBotName={setup?.defaults?.bot_name}
      />
    {:else if step === 'account'}
      <StepAccount bind:this={stepRef} {draft} {errors} />
    {:else if step === 'model'}
      <StepModel
        bind:this={stepRef}
        {draft}
        {errors}
        models={setup?.defaults?.models}
        bind:offerSkip
      />
    {:else if step === 'address'}
      <StepAddress bind:this={stepRef} {draft} {errors} host={setup?.host} />
    {:else if step === 'nextcloud'}
      <StepNextcloud bind:this={stepRef} {draft} {errors} {botName} bind:offerSkip />
    {:else if step === 'connections'}
      <StepConnections bind:this={stepRef} {draft} {errors} {botName} bind:offerSkip />
    {:else if step === 'features'}
      <StepFeatures
        {draft}
        {errors}
        {botName}
        modules={setup?.modules_available}
        memoryMib={setup?.memory_total_mib}
      />
    {:else}
      <StepReview
        {draft}
        {botName}
        models={setup?.defaults?.models}
        modules={setup?.modules_available}
        onEdit={(target) => stepSel.push({ step: target })}
      />
    {/if}
  </SetupFrame>
{/if}
