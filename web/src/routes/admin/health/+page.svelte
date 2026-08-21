<script lang="ts">
  import { onMount } from 'svelte';
  import { getAdminDoctor, type DoctorCheck, type DoctorReport } from '$lib/api';
  import { Badge, Button, NoticeBanner } from '$lib/components/ui';

  let report = $state<DoctorReport | null>(null);
  let loading = $state(true);
  let deepRunning = $state(false);
  let error = $state('');
  let notice = $state('');
  let aboutCollapsed = $state(true);

  // Grouped by the first dotted segment, in the order the registry produced
  // them — the same grouping `istota doctor` prints on a terminal, so an
  // operator reading one and then the other is not re-learning the layout.
  //
  // A Map rather than "compare against the last group": the registry's order
  // happens to be prefix-contiguous today, but nothing asserts it, and the
  // comparison version turns a future `runtime, web, runtime` into two groups
  // with the same key. The terminal renderer degrades to a repeated heading
  // there; a keyed `{#each}` throws, so the pane would go blank instead.
  let groups = $derived.by(() => {
    if (!report) return [];
    const byPrefix = new Map<string, DoctorCheck[]>();
    for (const check of report.checks) {
      const key = check.name.split('.')[0];
      const bucket = byPrefix.get(key);
      if (bucket) bucket.push(check);
      else byPrefix.set(key, [check]);
    }
    // Map preserves insertion order, so first-seen order is registry order.
    return [...byPrefix].map(([name, checks]) => ({ name, checks }));
  });

  const BADGE = {
    ok: 'success',
    warn: 'warn',
    fail: 'danger',
    skip: 'neutral',
  } as const;

  const HEADLINE = {
    ok: 'Everything this deployment depends on is present.',
    warn: 'Running, with something worth looking at.',
    fail: 'Something this deployment depends on is missing or broken.',
  } as const;

  async function load(deep = false) {
    if (deep) deepRunning = true;
    else loading = true;
    notice = '';
    try {
      report = await getAdminDoctor(deep);
      error = '';
      if (deep) notice = 'Deep checks included — a sandbox namespace was spawned and inspected.';
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to run checks';
      // A 409 is not a failure of the deployment, it is two admins pressing the
      // same button. Say which it was rather than colouring the page red.
      // Matched on the status alone: `apiFetch` throws `API error: <status>`
      // and discards the body, so the endpoint's "a deep run is in flight"
      // detail is for API clients and never reaches this page.
      if (message.includes('409')) {
        notice = 'A deep run is already in flight. Try again once it finishes.';
      } else {
        error = message;
      }
    } finally {
      loading = false;
      deepRunning = false;
    }
  }

  onMount(() => load(false));
</script>

<div class="settings health-page">
  <!-- The whole-pane error is for the *initial* load only. Once a report
       exists, a failed re-run reports inline above the toolbar: the exclusive
       branch would otherwise unmount the report and both buttons, so a
       transient 500 on the deep button left no way to retry short of a page
       reload, and took the findings the operator was reading with it. -->
  {#if loading && !report}
    <div class="center-msg">Running checks…</div>
  {:else if error && !report}
    <div class="banner error">{error}</div>
  {:else if report}
    <NoticeBanner
      title={HEADLINE[report.status]}
      variant={report.status === 'fail' ? 'danger' : report.status === 'warn' ? 'warn' : 'info'}
      bind:collapsed={aboutCollapsed}
    >
      <p>
        Each check names one thing the code assumes about the machine it is running on — a binary at
        a path, a writable directory, a mount that is really mounted. They run here on demand, once
        when the daemon starts, and on an interval after that; a failure is also sent to the admin
        channel when it first appears.
      </p>
      <p>
        A <strong>skip</strong> is not a problem: it means the check does not apply to this
        deployment, and the reason is on the row. Only <strong>fail</strong> means something is broken.
      </p>
      <p>
        <strong>Deep checks</strong> spawn a real sandbox namespace and inspect it from the inside, which
        is slower and runs one at a time.
      </p>
    </NoticeBanner>

    {#if error}
      <div class="banner error">{error}</div>
    {/if}

    {#if notice}
      <div class="banner info">{notice}</div>
    {/if}

    <div class="health-toolbar control-row">
      <div class="counts">
        <Badge variant="success">{report.summary.ok} ok</Badge>
        {#if report.summary.warn}<Badge variant="warn">{report.summary.warn} warn</Badge>{/if}
        {#if report.summary.fail}<Badge variant="danger">{report.summary.fail} fail</Badge>{/if}
        {#if report.summary.skip}<Badge variant="neutral">{report.summary.skip} skip</Badge>{/if}
      </div>
      <div class="actions">
        <Button variant="secondary" onclick={() => load(false)} {loading} loadingLabel="Running…"
          >Re-run</Button
        >
        <Button
          variant="secondary"
          onclick={() => load(true)}
          loading={deepRunning}
          loadingLabel="Running deep checks…"
          title="Spawns a sandbox namespace and inspects it. Slower; one at a time."
          >Run deep checks</Button
        >
      </div>
    </div>

    {#each groups as group (group.name)}
      <section class="card">
        <h2 class="section-title">{group.name}</h2>
        <ul class="check-list">
          {#each group.checks as check (check.name)}
            <li class="check" class:muted={check.status === 'skip'}>
              <div class="check-head">
                <Badge variant={BADGE[check.status]}>{check.status}</Badge>
                <code class="check-name">{check.name}</code>
              </div>
              <p class="check-detail">{check.detail}</p>
              {#if check.remedy}
                <p class="check-remedy caption">{check.remedy}</p>
              {/if}
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {/if}
</div>

<style>
  .health-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .counts,
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }

  .check-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  /* A skip is the most common row on a healthy install and the least
     interesting, so it recedes rather than competing with the two that matter. */
  .check.muted {
    opacity: 0.62;
  }

  .check-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  /* Sizes come from the `--text-*` roster, not from hand-picked rem values. The
     first draft used 0.82 / 0.9 / 0.86rem, and 0.9rem renders a step *above*
     `--text-base` — so the detail line sat larger than the NoticeBanner body
     directly above it. That is verbatim the defect web/AGENTS.md records under
     the NoticeBanner typography rule. `lint:design` cannot catch it: the ramp
     it enforces is spacing, and a type size has its own tokens. */
  .check-name {
    font-size: var(--text-sm);
    color: var(--text-secondary);
    word-break: break-word;
  }

  .check-detail {
    margin: var(--space-1) 0 0;
    font-size: var(--text-sm);
  }

  /* The remedy is the only actionable line on a failing row, so it is set apart
     rather than run together with the observation above it. Colour and size
     come from `.caption` on the element; this rule adds only the separation. */
  .check-remedy {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-3);
    border-left: 2px solid var(--border-default);
  }
</style>
