<script lang="ts">
  import { Button } from '$lib/components/ui';
  import { settingsSave } from '$lib/stores/settingsSave.svelte';
</script>

<!--
  The app bar's slot for a settings page's single save. Renders nothing until a
  page registers one via `useSettingsSave`, so it can sit unconditionally in a
  module layout's `tools` snippet.

  Place it *before* the section's settings cog, not after: the cog is a fixed
  navigation affordance and belongs on the bar's right edge, where it stays put
  whether or not the open page offers a save. `/settings` has no cog, so there
  this is the only thing in `tools`.
-->
{#if $settingsSave}
  {@const save = $settingsSave}
  <span class="header-save">
    <!--
      Always in the layout, hidden rather than removed when there is nothing to
      save. A badge that appears as you type would otherwise widen the group,
      and since `.header-tools` is right-aligned everything to its left — the
      section's cog — would jump sideways mid-edit. `visibility: hidden` also
      takes it out of the accessibility tree, so it is not announced when it
      does not apply.
    -->
    <span class="dirty-badge" class:reserved={!save.dirty}>Unsaved changes</span>
    <Button variant="primary" size="sm" onclick={save.save} disabled={!save.dirty || save.saving}>
      {save.saving ? 'Saving…' : (save.label ?? 'Save changes')}
    </Button>
  </span>
{/if}

<style>
  .header-save {
    display: inline-flex;
    align-items: center;
    gap: 0.6rem;
  }

  /* Same treatment as `.settings .dirty-badge` (settings.css), restated
	   because this one renders in the app bar, outside any `.settings` wrapper. */
  .dirty-badge {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    white-space: nowrap;
  }

  .dirty-badge.reserved {
    visibility: hidden;
  }

  /* Below the mobile breakpoint the bar has no width to hold a reserved slot,
	   and the button's own enabled state already says whether there is anything
	   to save. Dropped from the layout entirely rather than merely hidden. */
  @media (max-width: 768px) {
    .dirty-badge {
      display: none;
    }
  }
</style>
