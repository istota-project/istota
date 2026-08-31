<script lang="ts">
  import { Avatar, Button } from '$lib/components/ui';

  let {
    onConfirm,
    onReject,
    busy = false,
    botName = 'Istota',
    botAvatar = null,
  }: {
    onConfirm: () => void;
    onReject: () => void;
    busy?: boolean;
    // Who is asking. The card is raised inside a turn whose header names the
    // bot, but a confirmation lands on a continuation row as often as not and
    // that row has no header — so the identity is the card's own.
    botName?: string;
    // The bot icon's content hash off `/me`. `null` is "no icon set", which
    // renders the chip and issues no request; the default is what keeps a
    // caller that knows nothing about avatars rendering what it did before.
    botAvatar?: string | null;
  } = $props();
</script>

<div class="confirm-card">
  <span class="confirm-ask">
    <!-- The wrapper the size is set on, never the card: `--avatar-size`
		     inherits, so putting it on `.confirm-card` would resize anything
		     nested under it later. -->
    <span class="confirm-actor">
      <Avatar kind="bot" version={botAvatar} label={botName} />
    </span>
    <span class="confirm-label">This action needs your confirmation.</span>
  </span>
  <div class="confirm-actions">
    <Button variant="primary" size="sm" disabled={busy} onclick={onConfirm}>Confirm</Button>
    <Button variant="subtle" size="sm" disabled={busy} onclick={onReject}>Cancel</Button>
  </div>
</div>

<style>
  .confirm-card {
    margin-top: var(--space-2);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
  }
  /* Actor and sentence travel together, so the card's own `space-between`
	   pushes the buttons to the far edge and not the face away from the words. */
  .confirm-ask {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .confirm-actor {
    /* Proportionate to the one line of --text-sm beside it, and set here
		   because Avatar deliberately owns no size of its own. */
    --avatar-size: 1.25rem;
    display: flex;
    flex: 0 0 auto;
  }
  .confirm-label {
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .confirm-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
