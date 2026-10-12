<script lang="ts">
  import { onMount } from 'svelte';
  import {
    cancelConfigRequest,
    getAdminConfig,
    getConfigRequest,
    type AdminConfigView,
    type ConfigRequest,
  } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import {
    NOT_PICKED_UP_MS,
    POLL_MS,
    RESTART_DEADLINE_MS,
    backoffMs,
    restartedAfter,
  } from './progress';

  interface Props {
    requestId: number;
    /** The view from the restarted process, once it has come back. */
    onApplied: (view: AdminConfigView) => void;
    /** Close a finished request's report. */
    onDismiss: () => void;
  }

  let { requestId, onApplied, onDismiss }: Props = $props();

  let request = $state<ConfigRequest | null>(null);
  let phase = $state<'following' | 'restarting' | 'finished' | 'lost'>('following');
  let notPickedUp = $state(false);
  let unreachable = $state(false);
  let cancelling = $state(false);
  let cancelError = $state('');
  let active = true;

  const TERMINAL = new Set(['applied', 'unchanged', 'refused', 'failed', 'cancelled']);

  function sleep(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function follow() {
    const since = Date.now();
    let failures = 0;
    while (active) {
      try {
        request = await getConfigRequest(requestId);
        failures = 0;
        unreachable = false;
      } catch {
        failures += 1;
        unreachable = true;
      }
      if (request && TERMINAL.has(request.status)) break;
      notPickedUp = request?.status === 'pending' && Date.now() - since >= NOT_PICKED_UP_MS;
      await sleep(failures ? backoffMs(failures) : POLL_MS);
    }
    if (!active || !request) return;
    if (request.status === 'applied') await waitForRestart(request.finished_at);
    else phase = 'finished';
  }

  /** After `applied` the host restarts the stack; the result is only the new
   *  state once a process that started after the change answers. */
  async function waitForRestart(finishedAt: string | null) {
    phase = 'restarting';
    const deadline = Date.now() + RESTART_DEADLINE_MS;
    let failures = 0;
    while (active && Date.now() < deadline) {
      try {
        const view = await getAdminConfig();
        failures = 0;
        if (restartedAfter(view.started_at, finishedAt)) {
          phase = 'finished';
          onApplied(view);
          return;
        }
      } catch {
        failures += 1;
      }
      await sleep(failures ? backoffMs(failures) : POLL_MS);
    }
    if (active) phase = 'lost';
  }

  async function cancel() {
    cancelling = true;
    cancelError = '';
    try {
      await cancelConfigRequest(requestId);
      request = request ? { ...request, status: 'cancelled' } : request;
    } catch (e) {
      cancelError = e instanceof Error ? e.message : 'The request could not be cancelled.';
    } finally {
      cancelling = false;
    }
  }

  onMount(() => {
    void follow();
    return () => {
      active = false;
    };
  });
</script>

<section class="card request-progress" aria-live="polite">
  <h2 class="section-title">Change #{requestId}</h2>

  {#if phase === 'lost'}
    <p class="banner danger">
      Istota has not come back three minutes after applying the change. On the host, run
      <code>istota-stack apply</code> with an earlier plan, or copy
      <code>config/config.toml.bak.&lt;timestamp&gt;</code> back as uid 10001 and run
      <code>istota-stack up</code>. The change's history is kept for when the web returns.
    </p>
  {:else if phase === 'restarting'}
    <p><span class="spinner"></span> Applied. Waiting for Istota to restart…</p>
  {:else if !request || request.status === 'pending'}
    <p><span class="spinner"></span> Waiting for the host…</p>
    {#if notPickedUp}
      <p class="banner warn">
        The host has not picked this up after two minutes. The config applier may not be running on
        the host; you can cancel the change while it is waiting.
      </p>
    {/if}
    {#if request}
      <div class="form-actions">
        <Button variant="secondary" onclick={cancel} loading={cancelling} loadingLabel="Cancelling…"
          >Cancel change</Button
        >
      </div>
    {/if}
    {#if cancelError}<p class="form-error">{cancelError}</p>{/if}
  {:else if request.status === 'taken'}
    <p><span class="spinner"></span> Applying…</p>
  {:else if request.status === 'building'}
    <p><span class="spinner"></span> Building the image this change needs…</p>
  {:else if request.status === 'applied'}
    <p class="banner success">Applied. Istota restarted with the new configuration.</p>
  {:else if request.status === 'unchanged'}
    <p class="banner info">The host found nothing to change; nothing was restarted.</p>
  {:else if request.status === 'cancelled'}
    <p class="banner info">Cancelled. Nothing was changed.</p>
  {:else if request.status === 'refused'}
    <p class="banner danger">The host refused the change. Nothing was written.</p>
  {:else if request.status === 'failed'}
    <p class="banner danger">
      The change failed part way. Istota was restarted so that what runs matches what was written.
    </p>
  {/if}

  {#if unreachable && phase === 'following'}
    <p class="caption">Istota is not answering; still trying.</p>
  {/if}

  {#if request?.output_text && (request.status === 'refused' || request.status === 'failed')}
    <pre class="output">{request.output_text}</pre>
  {/if}

  {#if phase === 'finished' || phase === 'lost'}
    <div class="form-actions">
      <Button variant="ghost" onclick={onDismiss}>Dismiss</Button>
    </div>
  {/if}
</section>

<style>
  .request-progress p {
    margin: 0 0 var(--space-2);
  }

  .output {
    margin: 0 0 var(--space-2);
    padding: var(--space-2);
    max-height: 16rem;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-base);
    border-radius: var(--radius-sm);
    white-space: pre-wrap;
  }
</style>
