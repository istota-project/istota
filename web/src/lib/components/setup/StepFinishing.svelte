<script lang="ts">
  import { onMount } from 'svelte';
  import { NoticeBanner } from '$lib/components/ui';
  import {
    LONG_WAIT_MS,
    POLL_INTERVAL_MS,
    browserNavigation,
    loginTarget,
    stackAnswers,
  } from './finishing';

  interface Props {
    botName: string;
    /** The final address's login page, from `/finish`. */
    loginUrl: string;
    email: string;
    /** What makes the first start slow, for the copy. */
    browser: boolean;
    subscription: boolean;
    /** Reached at its own web address, so a certificate has to be issued first. */
    internet: boolean;
    /** The workspace is the VM's rclone mount of the bot's Nextcloud files. */
    nextcloudMount: boolean;
  }

  let { botName, loginUrl, email, browser, subscription, internet, nextcloudMount }: Props =
    $props();

  let ready = $state(false);
  let longWait = $state(false);

  const target = $derived(loginTarget(loginUrl, email));
  const host = $derived.by(() => {
    try {
      return new URL(loginUrl).host;
    } catch {
      return loginUrl;
    }
  });
  const slowParts = $derived(
    [browser ? 'downloads the web browser' : '', subscription ? 'installs Claude Code' : ''].filter(
      Boolean,
    ),
  );

  onMount(() => {
    let stopped = false;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    const waitTimer = setTimeout(() => (longWait = true), LONG_WAIT_MS);

    async function poll() {
      if (stopped) return;
      if (await stackAnswers(loginUrl)) {
        if (stopped) return;
        ready = true;
        clearTimeout(waitTimer);
        browserNavigation.assign(target);
        return;
      }
      if (!stopped) pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
    }
    // The first poll waits a beat: the setup server is still on its way out.
    pollTimer = setTimeout(poll, POLL_INTERVAL_MS);

    return () => {
      stopped = true;
      clearTimeout(pollTimer);
      clearTimeout(waitTimer);
    };
  });
</script>

<div class="step">
  <p>Your settings are saved and {botName} is starting.</p>
  <ol class="progress" aria-live="polite">
    <li class="done"><span class="mark" aria-hidden="true">✓</span>Settings saved</li>
    <li class={ready ? 'done' : 'busy'}>
      <span class="mark" aria-hidden="true"
        >{#if ready}✓{:else}<span class="spinner"></span>{/if}</span
      >Starting {botName}
    </li>
    <li class={ready ? 'done' : 'todo'}>
      <span class="mark" aria-hidden="true">{ready ? '✓' : ''}</span>Ready
    </li>
  </ol>
  {#if slowParts.length}
    <p class="muted">
      This usually takes a few minutes the first time, while {botName}
      {slowParts.join(' and ')}.
    </p>
  {:else}
    <p class="muted">This usually takes under a minute.</p>
  {/if}
  {#if internet}
    <p class="muted">
      {botName} is asking for a security certificate for {host}, which can take a little while after
      it starts. If this page doesn't move on, try
      <a href={target} target="_blank" rel="noopener noreferrer">{host}</a> yourself.
    </p>
  {/if}
  {#if longWait && !ready}
    <NoticeBanner variant="warn" title="This is taking longer than expected" collapsed={false}>
      {#snippet children()}
        <p>
          On the server, run <code>sudo istota-stack status</code> to see what {botName} is doing.
          {#if internet}<code>journalctl -u istota-certbot</code> shows the certificate request.{/if}
          {#if nextcloudMount}<code>journalctl -u mount-nextcloud</code> shows the connection to your
            Nextcloud.{/if}
          This page keeps checking.
        </p>
      {/snippet}
    </NoticeBanner>
  {/if}
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  p {
    margin: 0;
  }

  .progress {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .progress li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .mark {
    flex: none;
    width: 1em;
    text-align: center;
  }

  .done {
    color: var(--text-primary);
  }

  .done .mark {
    color: var(--status-success-fg);
  }

  .todo {
    color: var(--text-muted);
  }
</style>
