<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getBriefingTargets,
    getGroupBriefings,
    getGroupBriefingPreferences,
    getGroupDeliveryPreferences,
    saveGroupBriefingPreferences,
    saveGroupDeliveryPreferences,
    type BriefingDeliveryPreferences,
    type BriefingTarget,
    type GroupBriefingDefinition,
  } from '$lib/api';
  import { EmptyState } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { notifyError } from '$lib/stores/notices';
  import { channelLabel } from './settings/briefingSettings';

  // A member's own choices about group briefings: one row per group for every
  // briefing in it, including later ones, then one per briefing. A mute at
  // either level wins, so a briefing row is locked while its group row is on.
  type Row = {
    definition: GroupBriefingDefinition;
    prefs: BriefingDeliveryPreferences;
  };
  type Entry = { target: BriefingTarget; group: BriefingDeliveryPreferences; rows: Row[] };
  let entries = $state<Entry[]>([]);
  let loaded = $state(false);
  let saved = $state('');
  let saving = $state(false);
  let failed = $state(false);

  const snapshot = () => JSON.stringify(entries.map((e) => [e.group, e.rows.map((r) => r.prefs)]));
  const pick = (p: BriefingDeliveryPreferences) => ({
    outbound_muted: p.outbound_muted,
    in_app_muted: p.in_app_muted,
  });

  onMount(async () => {
    try {
      const { targets } = await getBriefingTargets();
      const groups = targets.filter((t) => t.scope === 'group' && t.capabilities.receive_briefings);
      entries = await Promise.all(
        groups.map(async (target) => {
          const id = target.group_id!;
          const [group, list] = await Promise.all([
            getGroupDeliveryPreferences(id),
            getGroupBriefings(id),
          ]);
          const rows = await Promise.all(
            list.items.map(async (definition) => ({
              definition,
              prefs: pick(await getGroupBriefingPreferences(id, definition.name)),
            })),
          );
          return { target, group: pick(group), rows };
        }),
      );
      saved = snapshot();
    } catch {
      failed = true;
    } finally {
      loaded = true;
    }
  });

  async function save() {
    const before = JSON.parse(saved) as [
      BriefingDeliveryPreferences,
      BriefingDeliveryPreferences[],
    ][];
    saving = true;
    try {
      for (const [i, entry] of entries.entries()) {
        const id = entry.target.group_id!;
        if (JSON.stringify(entry.group) !== JSON.stringify(before[i]?.[0]))
          await saveGroupDeliveryPreferences(id, entry.group);
        for (const [j, row] of entry.rows.entries())
          if (JSON.stringify(row.prefs) !== JSON.stringify(before[i]?.[1][j]))
            await saveGroupBriefingPreferences(id, row.definition.name, row.prefs);
      }
      saved = snapshot();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not save your delivery settings.');
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() => (saved ? { dirty: snapshot() !== saved, saving, save } : null));
</script>

<SettingsCard
  title="Group briefings"
  description="Mute the copies sent to you. You stay in the group, and every edition remains readable in Briefings."
>
  {#if failed}
    <p class="caption">Could not load your group delivery settings.</p>
  {:else if loaded && !entries.length}
    <EmptyState size="sm">You are not in any group with briefings.</EmptyState>
  {:else if entries.length}
    <div class="table-scroll">
      <table class="table">
        <thead>
          <tr>
            <th>Briefing</th>
            <th class="toggle">Mute delivery</th>
            <th class="toggle">Mute notifications</th>
          </tr>
        </thead>
        {#each entries as entry (entry.target.group_id)}
          <tbody>
            <tr class="group-row">
              <th scope="row">
                <span>All {entry.target.label} briefings</span>
              </th>
              <td class="toggle">
                <input
                  type="checkbox"
                  bind:checked={entry.group.outbound_muted}
                  aria-label={`Mute delivery of all ${entry.target.label} briefings`}
                />
              </td>
              <td class="toggle">
                <input
                  type="checkbox"
                  bind:checked={entry.group.in_app_muted}
                  aria-label={`Mute notifications for all ${entry.target.label} briefings`}
                />
              </td>
            </tr>
            {#each entry.rows as row (row.definition.id)}
              <tr>
                <td>
                  <span class="name">
                    <span>{row.definition.title || row.definition.name}</span>
                    <span class="caption">{channelLabel(row.definition.channel)}</span>
                  </span>
                </td>
                <td class="toggle">
                  <input
                    type="checkbox"
                    checked={entry.group.outbound_muted || row.prefs.outbound_muted}
                    disabled={entry.group.outbound_muted}
                    onchange={(e) => (row.prefs.outbound_muted = e.currentTarget.checked)}
                    aria-label={`Mute delivery of ${row.definition.title || row.definition.name}`}
                  />
                </td>
                <td class="toggle">
                  <input
                    type="checkbox"
                    checked={entry.group.in_app_muted || row.prefs.in_app_muted}
                    disabled={entry.group.in_app_muted}
                    onchange={(e) => (row.prefs.in_app_muted = e.currentTarget.checked)}
                    aria-label={`Mute notifications for ${row.definition.title || row.definition.name}`}
                  />
                </td>
              </tr>
            {/each}
          </tbody>
        {/each}
      </table>
    </div>
  {/if}
</SettingsCard>

<style>
  .toggle {
    width: 9rem;
    text-align: center;
  }
  .group-row th {
    font-weight: var(--weight-semibold);
  }
  .name {
    display: flex;
    flex-direction: column;
  }
</style>
