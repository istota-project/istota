<!--
  An instant as text, inside a `<time>` that carries it machine-readably and
  titles it with the absolute date and time. Every string comes from
  `$lib/dateFormat`; this component only picks which formatter to call.
-->
<script lang="ts">
  import {
    dateTimeAttribute,
    formatAbsolute,
    formatDate,
    formatDateTime,
    formatRelative,
    formatTime,
    type DateFormatOptions,
  } from '$lib/dateFormat';

  interface Props {
    value: string | null | undefined;
    /** `date` (the default), `datetime`, `relative` (`5m ago`) or `time` (`14:22`). */
    format?: 'date' | 'datetime' | 'relative' | 'time';
    /** `Intl` options for `date`, `datetime` and `time`, replacing the default set. */
    options?: DateFormatOptions;
    /** `42s ago` under a minute, for a `relative` liveness readout (`/admin`). */
    seconds?: boolean;
    /** Rendered, with no `<time>`, for a missing value. */
    empty?: string;
    /** `absolute` (the default) titles the element with the full date and time. */
    title?: 'absolute' | false;
    /** Placement and type role only (`caption`, `num`). */
    class?: string;
  }

  let {
    value,
    format = 'date',
    options,
    seconds = false,
    empty = '',
    title = 'absolute',
    class: className = '',
  }: Props = $props();

  const text = $derived.by(() => {
    if (format === 'relative') return formatRelative(value, { seconds });
    if (format === 'datetime') return formatDateTime(value, options);
    if (format === 'time') return formatTime(value, options);
    return formatDate(value, options);
  });
  const datetime = $derived(dateTimeAttribute(value));
  const absolute = $derived(title === 'absolute' && datetime ? formatAbsolute(value) : undefined);
</script>

{#if value}
  <time
    class={className || undefined}
    {datetime}
    title={absolute && absolute !== text ? absolute : undefined}>{text}</time
  >
{:else if empty}
  <span class={className || undefined}>{empty}</span>
{/if}
