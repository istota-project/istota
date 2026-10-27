import { sortRows } from '@minot/ui/ui/tableSort.svelte';
import { findLinks } from '$lib/markdown';

/** The rows the viewer draws as a table; the rest stay in the source view. */
export const CSV_MAX_ROWS = 2_000;

export type CsvRecords = { header: string[]; body: string[][]; width: number };
export type CsvSort = { column: number; descending: boolean };
export type CellPart = { text: string; href?: string };

/**
 * Parse delimited text the RFC 4180 way: quoted fields may hold the delimiter,
 * newlines and doubled quotes; CRLF and LF both end a record. Lenient rather
 * than strict, since a viewer should show a malformed file, not refuse it: a
 * quote inside an unquoted field is kept as text, and an unclosed quote runs
 * to the end of the input.
 */
export function parseDelimited(text: string, delimiter: string): string[][] {
  const records: string[][] = [];
  let record: string[] = [];
  let field = '';
  let quoted = false;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
      } else {
        field += ch;
      }
      i += 1;
      continue;
    }
    if (ch === '"' && field === '') {
      quoted = true;
    } else if (ch === delimiter) {
      record.push(field);
      field = '';
    } else if (ch === '\n' || ch === '\r') {
      record.push(field);
      records.push(record);
      record = [];
      field = '';
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
    } else {
      field += ch;
    }
    i += 1;
  }
  if (field !== '' || record.length > 0 || quoted) {
    record.push(field);
    records.push(record);
  }
  return records;
}

/** The first record is the header, padded to the widest record; the body is every other record, unpadded. */
export function csvRecords(text: string, delimiter: string): CsvRecords | null {
  const records = parseDelimited(text, delimiter).filter(
    (record) => !(record.length === 1 && record[0] === ''),
  );
  if (records.length === 0) return null;
  const width = Math.max(...records.map((record) => record.length));
  return { header: pad(records[0], width), body: records.slice(1), width };
}

/**
 * The rows to draw: the whole body sorted, then cut to `maxRows` and padded.
 * Sorting before the cut is what makes a sorted view of a long file show the
 * top of the whole file rather than of its first `maxRows` rows.
 */
export function csvRows(
  records: CsvRecords,
  sort: CsvSort | null,
  maxRows = CSV_MAX_ROWS,
): string[][] {
  const rows = sort ? sortBody(records.body, sort) : records.body;
  return rows.slice(0, maxRows).map((row) => pad(row, records.width));
}

function pad(record: string[], width: number): string[] {
  return record.length < width ? [...record, ...Array(width - record.length).fill('')] : record;
}

const NUMBER = /^[-+]?(\d+(\.\d*)?|\.\d+)(e[-+]?\d+)?$/i;

/** A column whose every non-empty cell is a number sorts by value; any other by text. */
function sortBody(body: string[][], sort: CsvSort): string[][] {
  const cell = (row: string[]) => (row[sort.column] ?? '').trim();
  const numeric = body.every((row) => cell(row) === '' || NUMBER.test(cell(row)));
  if (!numeric) return sortRows(body, sort.descending, (row) => [cell(row)]);
  const keyed = body.map((row) => ({ row, value: cell(row) === '' ? null : Number(cell(row)) }));
  keyed.sort((a, b) => {
    if (a.value === null || b.value === null)
      return (a.value === null ? 1 : 0) - (b.value === null ? 1 : 0);
    return sort.descending ? b.value - a.value : a.value - b.value;
  });
  return keyed.map((k) => k.row);
}

/** A cell's text split around the links in it, by the rule rendered documents autolink by. */
export function cellParts(text: string): CellPart[] {
  const links = text.includes(':') ? findLinks(text) : [];
  if (links.length === 0) return [{ text }];
  const parts: CellPart[] = [];
  let at = 0;
  for (const link of links) {
    if (link.start > at) parts.push({ text: text.slice(at, link.start) });
    parts.push({ text: text.slice(link.start, link.end), href: link.url });
    at = link.end;
  }
  if (at < text.length) parts.push({ text: text.slice(at) });
  return parts;
}
