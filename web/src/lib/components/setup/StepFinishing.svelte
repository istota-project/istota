<script lang="ts">
  import { Check } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { Icon, Alert, Spinner } from '@minot/ui';
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
    /** A re-run: the stack was running before and is starting again. */
    rerun?: boolean;
  }

  let {
    botName,
    loginUrl,
    email,
    browser,
    subscription,
    internet,
    nextcloudMount,
    rerun = false,
  }: Props = $props();

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

<div class="vstack">
  <p>Your settings are saved and {botName} is {rerun ? 'restarting' : 'starting'}.</p>
  <ol class="vstack list-reset" style:--vstack-gap="var(--space-2)" aria-live="polite">
    <li class="hstack done"><span class="mark"><Icon icon={Check} /></span>Settings saved</li>
    <li class="hstack" class:done={ready} class:busy={!ready}>
      <span class="mark"
        >{#if ready}<Icon icon={Check} />{:else}<Spinner size="sm" />{/if}</span
      >{rerun ? 'Restarting' : 'Starting'}
      {botName}
    </li>
    <li class="hstack" class:done={ready} class:todo={!ready}>
      <span class="mark"
        >{#if ready}<Icon icon={Check} />{/if}</span
      >Ready
    </li>
  </ol>
  {#if rerun}
    <p class="muted">
      This usually takes a minute or two, longer when {botName} has something new to download. You sign
      in as before once it is back.
    </p>
  {:else if slowParts.length}
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
    <Alert variant="warn" title="This is taking longer than expected" collapsed={false}>
      {#snippet children()}
        <p>
          On the server, run <code>sudo istota-stack status</code> to see what {botName} is doing.
          {#if internet}<code>journalctl -u istota-certbot</code> shows the certificate request.{/if}
          {#if nextcloudMount}<code>journalctl -u mount-nextcloud</code> shows the connection to your
            Nextcloud.{/if}
          This page keeps checking.
        </p>
      {/snippet}
    </Alert>
  {/if}
</div>

<style>
  p {
    margin: 0;
  }

  .mark {
    flex: none;
    display: inline-flex;
    justify-content: center;
    width: 1em;
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
