<script lang="ts">
  import { onMount } from 'svelte';
  import { getAdminDoctor, type DoctorCheck, type DoctorReport } from '$lib/api';
  import { waitForDoctorRun } from '$lib/doctorRun';
  import { notifyError } from '$lib/stores/notices';
  import {
    Alert,
    Badge,
    Button,
    Card,
    Disclosure,
    LoadState,
    StatusDot,
    Timestamp,
  } from '$lib/components/ui';

  let report = $state<DoctorReport | null>(null);
  let loading = $state(true);
  let running = $state(false);
  let deepRunning = $state(false);
  let error = $state('');
  let notice = $state('');

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

  async function load() {
    try {
      report = await getAdminDoctor();
      error = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load checks';
    } finally {
      loading = false;
    }
  }

  async function rerun(deep: boolean) {
    if (deep) deepRunning = true;
    else running = true;
    notice = '';
    try {
      const result = await waitForDoctorRun(deep, (next) => {
        report = next;
      });
      if (result.answered) {
        if (result.deep) {
          notice = 'Deep checks included: the daemon spawned a sandbox namespace and inspected it.';
        }
      } else {
        notice = 'The daemon has not answered the request yet. Check that it is running.';
      }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to run checks');
    } finally {
      running = false;
      deepRunning = false;
    }
  }

  onMount(() => load());
</script>

<div class="settings health-page card-pane">
  <!-- The whole-pane error is for the *initial* load only. A failed re-run is
       a notice: the exclusive branch would otherwise unmount the report and
       both buttons, so a transient 500 on the deep button left no way to retry
       short of a page reload, and took the findings the operator was reading
       with it. -->
  <LoadState
    {loading}
    error={report ? null : error}
    hasData={!!report}
    loadingLabel="Running checks…"
  >
    {#if report}
      {@const status = report.status}
      {#snippet headline()}
        <span class="hstack headline">
          <StatusDot tone={status === 'fail' ? 'danger' : status === 'warn' ? 'warn' : 'success'} />
          {HEADLINE[status]}
        </span>
      {/snippet}
      <Disclosure variant="card" summary={headline}>
        <p>
          Each check names one thing the code assumes about the machine it is running on — a binary
          at a path, a writable directory, a mount that is really mounted. The daemon runs them,
          since it is the process that runs tasks: once when it starts, on an interval after that,
          and when you ask here. A failure is also sent to the admin channel when it first appears.
        </p>
        <p>
          A <strong>skip</strong> is not a problem: it means the check does not apply to this
          deployment, and the reason is on the row. Only <strong>fail</strong> means something is broken.
        </p>
        <p>
          <strong>Deep checks</strong> spawn a real sandbox namespace and inspect it from the inside,
          which is slower. Their last result stays on this page until the next deep run.
        </p>
      </Disclosure>

      {#if notice}
        <Alert appearance="inline" variant="info" role="status">{notice}</Alert>
      {/if}

      <p class="caption run-times">
        Last run by the daemon: <Timestamp
          value={report.checked_at}
          format="datetime"
          empty="never"
        />{#if report.deep_checked_at}
          · deep checks: <Timestamp
            value={report.deep_checked_at}
            format="datetime"
          />{/if}{#if report.pending}
          · a {report.pending.deep ? 'deep ' : ''}run is waiting for the daemon{/if}
      </p>

      <div class="health-toolbar toolbar control-row">
        <div class="counts">
          <Badge variant="success">{report.summary.ok} ok</Badge>
          {#if report.summary.warn}<Badge variant="warn">{report.summary.warn} warn</Badge>{/if}
          {#if report.summary.fail}<Badge variant="danger">{report.summary.fail} fail</Badge>{/if}
          {#if report.summary.skip}<Badge variant="neutral">{report.summary.skip} skip</Badge>{/if}
        </div>
        <div class="actions">
          <Button
            variant="secondary"
            onclick={() => rerun(false)}
            loading={running}
            disabled={deepRunning}
            loadingLabel="Running…">Re-run</Button
          >
          <Button
            variant="secondary"
            onclick={() => rerun(true)}
            loading={deepRunning}
            disabled={running}
            loadingLabel="Running deep checks…"
            title="The daemon spawns a sandbox namespace and inspects it. Slower."
            >Run deep checks</Button
          >
        </div>
      </div>

      {#each groups as group (group.name)}
        <Card title={group.name}>
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
        </Card>
      {/each}
    {/if}
  </LoadState>
</div>

<style>
  .run-times {
    margin: 0;
  }

  .health-toolbar {
    justify-content: space-between;
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
  /* The overall status leads the page, so it reads at full strength rather
     than in the disclosure's muted summary type. */
  .headline {
    color: var(--text-primary);
  }

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
     `--text-base` — so the detail line sat larger than the notice body then
     directly above it. That is verbatim the defect web/AGENTS.md records under
     the Alert typography rule. `lint:design` cannot catch it: the ramp
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
