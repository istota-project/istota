<script lang="ts">
  import { SortHeader, sortAria } from '@minot/ui';
  import { cellParts, csvRows, type CsvRecords, type CsvSort } from '$lib/fileViewer/csv';
  import ColumnResizeHandle from './ColumnResizeHandle.svelte';

  let { name, records }: { name: string; records: CsvRecords } = $props();

  // Not `createTableSort`: that remembers a column index across files, where
  // the index means nothing, and has no unsorted state. A file opens in file
  // order; the caller remounts this per file and on each return from the
  // source view, which resets both.
  let sort = $state<CsvSort | null>(null);
  let widths = $state<number[] | null>(null);
  let drawn = $state<number[]>([]);
  let table: HTMLTableElement | undefined = $state();

  let rows = $derived(csvRows(records, sort));
  let labels = $derived(records.header.map((cell, i) => cell || `Column ${i + 1}`));

  function directionOf(column: number) {
    if (sort?.column !== column) return null;
    return sort.descending ? 'descending' : 'ascending';
  }

  function toggle(column: number) {
    sort =
      sort?.column === column
        ? { column, descending: !sort.descending }
        : { column, descending: false };
  }

  function resize(column: number, width: number) {
    // The first resize fixes every column at its drawn width, so nothing else moves.
    const next = widths ? [...widths] : labels.map((_, i) => drawn[i] ?? 0);
    next[column] = Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width)));
    widths = next;
  }

  /** What a cell's content needs, not the box it is in: a cell is at least as
   *  wide as its column, so its `scrollWidth` could only ever widen a column. */
  function contentWidth(cell: HTMLTableCellElement): number {
    let width: number;
    if (cell.tagName === 'TH') {
      // The sort button only; the resize handle sits at the cell's right edge.
      width = cell.firstElementChild?.getBoundingClientRect().width ?? 0;
    } else {
      const range = document.createRange();
      range.selectNodeContents(cell);
      width = range.getBoundingClientRect?.().width ?? 0;
    }
    const style = getComputedStyle(cell);
    return width + (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
  }

  function fit(column: number) {
    let widest = 0;
    for (const row of table?.rows ?? []) {
      const cell = row.cells[column];
      if (cell) widest = Math.max(widest, contentWidth(cell));
    }
    resize(column, Math.ceil(widest));
  }

  const MIN_WIDTH = 48;
  const MAX_WIDTH = 1600;
</script>

{#if rows.length < records.body.length}<p class="caption table-note">
    Showing the first {rows.length.toLocaleString()} of {records.body.length.toLocaleString()}
    rows. The source view has the rest.
  </p>{/if}
<div class="csv-scroll">
  <table
    bind:this={table}
    class="table csv-table prose"
    class:is-sized={widths !== null}
    style:width={widths ? `${widths.reduce((sum, w) => sum + w, 0)}px` : null}
    aria-label={name}
  >
    {#if widths}
      <colgroup>
        {#each widths as width, i (i)}<col style:width={`${width}px`} />{/each}
      </colgroup>
    {/if}
    <thead>
      <tr>
        {#each labels as label, i (i)}
          <th
            scope="col"
            title={records.header[i]}
            aria-sort={sortAria(directionOf(i))}
            bind:clientWidth={drawn[i]}
          >
            <SortHeader {label} direction={directionOf(i)} onclick={() => toggle(i)} />
            <ColumnResizeHandle
              {label}
              width={widths?.[i] ?? drawn[i] ?? 0}
              min={MIN_WIDTH}
              max={MAX_WIDTH}
              onresize={(width) => resize(i, width)}
              onfit={() => fit(i)}
            />
          </th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each rows as row, r (r)}
        <tr>
          {#each row as cell, i (i)}<td title={cell}
              >{#each cellParts(cell) as part, p (p)}{#if part.href}<a
                    href={part.href}
                    target="_blank"
                    rel="noopener noreferrer">{part.text}</a
                  >{:else}{part.text}{/if}{/each}</td
            >{/each}
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  /* One scroller for both axes, so the header can stick while a wide table
     scrolls sideways. */
  .csv-scroll {
    overflow: auto;
    -webkit-overflow-scrolling: touch;
  }
  :global(.fill) .csv-scroll {
    flex: 1;
    min-height: 0;
  }
  .table-note {
    margin: 0;
    padding: var(--space-2) var(--space-3);
  }
  .csv-table {
    width: max-content;
    min-width: 100%;
  }
  /* Once a column has been resized every width is explicit, so a column can
     be narrower than its content and the table no larger than its columns. */
  .csv-table.is-sized {
    table-layout: fixed;
    min-width: 0;
  }
  .csv-table th,
  .csv-table td {
    max-width: 24rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    overflow-wrap: normal;
  }
  .csv-table.is-sized th,
  .csv-table.is-sized td {
    max-width: none;
  }
  .csv-table thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--surface-card);
  }
  .csv-table th:first-child,
  .csv-table td:first-child {
    padding-inline-start: var(--space-3);
  }
</style>
