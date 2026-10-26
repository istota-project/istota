/** The rows the viewer draws as a table; the rest stay in the source view. */
export const CSV_MAX_ROWS = 2_000;

export type CsvTable = { header: string[]; rows: string[][]; totalRows: number; width: number };

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

/** The first record is the header; every row is padded to the widest one. */
export function csvTable(text: string, delimiter: string, maxRows = CSV_MAX_ROWS): CsvTable | null {
  const records = parseDelimited(text, delimiter).filter(
    (record) => !(record.length === 1 && record[0] === ''),
  );
  if (records.length === 0) return null;
  const width = Math.max(...records.map((record) => record.length));
  const pad = (record: string[]) =>
    record.length < width ? [...record, ...Array(width - record.length).fill('')] : record;
  const body = records.slice(1);
  return {
    header: pad(records[0]),
    rows: body.slice(0, maxRows).map(pad),
    totalRows: body.length,
    width,
  };
}
