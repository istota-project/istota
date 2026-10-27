<script lang="ts">
  // Static on purpose: x11vnc sends its banner as soon as the relay connects,
  // and any await between the relay's `ok` and `new RFB` lets that banner reach
  // this component's handler instead of noVNC's, where it is lost.
  import RFB from '@novnc/novnc';
  import { onDestroy } from 'svelte';
  import {
    adminBrowserConsoleUrl,
    getAdminBrowserConsoleTicket,
    GroupApiError,
    type StepUpProof,
  } from '$lib/api';
  import { isStepUpRefusal, StepUpDialog } from '$lib/components/settings';
  import {
    Alert,
    BackLink,
    Button,
    Checkbox,
    EmptyState,
    Field,
    FormModal,
    Input,
    StatusDot,
  } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { CLOSE_NOT_RUNNING, describeClose, type ConsoleClose } from './consoleClose';

  /**
   * One user's live browser, driven over the admin console relay. Each open
   * asks for a step-up, trades the proof for a single-use ticket, sends the
   * ticket as the websocket's first frame and hands the socket to noVNC once
   * the relay answers `ok`. The closed state comes from the socket's close
   * code, which noVNC's own `disconnect` event does not carry.
   */
  let {
    user,
    backHref,
    onExit,
  }: {
    user: string;
    /** The list this console was opened from. */
    backHref: string;
    /** Leave the console: a cancelled step-up, or Disconnect. */
    onExit: () => void;
  } = $props();

  type Phase = 'step-up' | 'connecting' | 'connected' | 'closed' | 'refused';
  let phase = $state<Phase>('step-up');
  let closed = $state<(ConsoleClose & { code: number }) | null>(null);
  let refusal = $state('');
  let screen = $state<HTMLDivElement | undefined>();
  let viewOnly = $state(false);
  let credentialsOpen = $state(false);
  let password = $state('');
  let credentialError = $state('');

  let rfb: RFB | null = null;
  let socket: WebSocket | null = null;
  let afterStepUp: (() => void) | null = null;
  let active = true;

  let status = $derived(
    phase === 'connected'
      ? { tone: 'success' as const, label: 'Connected' }
      : phase === 'step-up' || phase === 'connecting'
        ? { tone: 'info' as const, label: 'Connecting' }
        : { tone: 'neutral' as const, label: 'Closed' },
  );
  let live = $derived(phase === 'connecting' || phase === 'connected');

  // The step-up dialog is still mounted while this runs; what it decides is
  // applied once the dialog completes.
  async function redeem(proof: StepUpProof) {
    try {
      const { ticket } = await getAdminBrowserConsoleTicket(user, proof);
      afterStepUp = () => connect(ticket);
    } catch (e) {
      if (isStepUpRefusal(e)) throw e;
      afterStepUp = () => refuse(e);
    }
  }

  function stepUpComplete() {
    const next = afterStepUp;
    afterStepUp = null;
    if (active) next?.();
  }

  function refuse(e: unknown) {
    const code = e instanceof GroupApiError ? e.code : '';
    if (code === 'admin_identity_required' || code === 'browser_disabled') {
      refusal = (e as Error).message;
      phase = 'refused';
      return;
    }
    if (code === 'console_limit') {
      finish(4429, false);
      return;
    }
    notifyError(e instanceof Error && e.message ? e.message : 'The console could not start.');
    closed = { code: 0, message: 'The console could not start.', retry: true };
    phase = 'closed';
  }

  function connect(ticket: string) {
    phase = 'connecting';
    let opened = false;
    const ws = new WebSocket(adminBrowserConsoleUrl());
    socket = ws;
    // A listener, not `onclose`: noVNC's `Websock.attach` overwrites the
    // property, and the close code is what this view is driven by.
    ws.addEventListener('close', (event) => {
      if (socket !== ws) return;
      socket = null;
      rfb = null;
      finish(event.code, opened);
    });
    ws.addEventListener('open', () => ws.send(ticket));
    const onMessage = (event: MessageEvent) => {
      if (event.data !== 'ok') return;
      ws.removeEventListener('message', onMessage);
      opened = true;
      start(ws);
    };
    ws.addEventListener('message', onMessage);
  }

  // Runs inside the `ok` message handler, so noVNC attaches before the next
  // frame (the RFB banner) is dispatched.
  function start(ws: WebSocket) {
    if (!screen) {
      ws.close();
      return;
    }
    const view = new RFB(screen, ws);
    view.scaleViewport = true;
    view.resizeSession = false;
    view.clipViewport = false;
    view.focusOnClick = true;
    view.viewOnly = viewOnly;
    view.addEventListener('connect', () => {
      phase = 'connected';
      credentialsOpen = false;
      credentialError = '';
    });
    view.addEventListener('credentialsrequired', () => {
      password = '';
      credentialsOpen = true;
    });
    view.addEventListener('securityfailure', (event) => {
      const reason = (event as CustomEvent<{ reason?: string }>).detail?.reason;
      credentialError = reason || 'The VNC password was not accepted.';
    });
    rfb = view;
  }

  function finish(code: number, opened: boolean) {
    if (!active) return;
    // A refused password stays in its dialog after the socket closes.
    if (!credentialError) credentialsOpen = false;
    closed = { code, ...describeClose(code, opened) };
    phase = 'closed';
  }

  function sendPassword() {
    if (!rfb) return false;
    credentialError = '';
    rfb.sendCredentials({ password });
    password = '';
    // Kept open until noVNC connects or reports a security failure.
    return false;
  }

  function setViewOnly(next: boolean) {
    if (rfb) rfb.viewOnly = next;
  }

  function teardown() {
    const view = rfb;
    const ws = socket;
    rfb = null;
    socket = null;
    if (view) view.disconnect();
    else ws?.close();
  }

  function disconnect() {
    teardown();
    onExit();
  }

  function reconnect() {
    closed = null;
    refusal = '';
    credentialError = '';
    credentialsOpen = false;
    phase = 'step-up';
  }

  onDestroy(() => {
    active = false;
    teardown();
  });
</script>

<section class="browser-console">
  <div class="toolbar toolbar--bar console-bar">
    <BackLink href={backHref} label="Browsers" />
    <h2 class="title-card truncate console-user">{user}</h2>
    <StatusDot tone={status.tone} label={status.label} showLabel />
    {#if live}
      <span class="hstack console-tools">
        <Checkbox
          tone="muted"
          label="View only"
          bind:checked={viewOnly}
          onchange={setViewOnly}
          disabled={phase !== 'connected'}
        />
        <Button variant="secondary" size="sm" onclick={disconnect}>Disconnect</Button>
      </span>
    {/if}
  </div>
  {#if phase === 'refused'}
    <Alert variant="warn" title="The console cannot open">{refusal}</Alert>
  {:else if phase === 'closed' && closed}
    {#if closed.code === CLOSE_NOT_RUNNING}
      <EmptyState actionLabel="Back to browsers" actionHref={backHref}>{closed.message}</EmptyState>
    {:else if closed.retry}
      <EmptyState actionLabel="Reconnect" onaction={reconnect}>{closed.message}</EmptyState>
    {:else}
      <EmptyState>{closed.message}</EmptyState>
    {/if}
  {:else}
    <div class="console-screen" bind:this={screen}></div>
  {/if}
</section>

{#if phase === 'step-up'}
  <StepUpDialog
    action="admin_browser_console"
    onConfirm={redeem}
    onComplete={stepUpComplete}
    onCancel={onExit}
  />
{/if}

<FormModal
  bind:open={credentialsOpen}
  title="VNC password"
  description="This browser's VNC server asks for a password."
  submitLabel="Send"
  canSubmit={phase !== 'closed' && password.length > 0}
  onSubmit={sendPassword}
  onCancel={() => (credentialsOpen = false)}
>
  <Field label="Password" error={credentialError}>
    <Input type="password" bind:value={password} autocomplete="off" />
  </Field>
</FormModal>

<style>
  .browser-console {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    height: 100%;
  }
  .console-bar {
    flex-shrink: 0;
  }
  .console-user {
    min-width: 0;
  }
  .console-tools {
    margin-left: auto;
  }
  .console-screen {
    flex: 1;
    min-height: 0;
    background: var(--surface-base);
  }
</style>
