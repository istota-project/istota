import { describe, expect, it } from 'vitest';
import { cellParts, csvRecords, csvRows, parseDelimited } from './csv';

describe('parseDelimited', () => {
  it('splits records on LF and CRLF', () => {
    expect(parseDelimited('a,b\r\nc,d\ne,f', ',')).toEqual([
      ['a', 'b'],
      ['c', 'd'],
      ['e', 'f'],
    ]);
  });

  it('keeps delimiters, newlines and doubled quotes inside a quoted field', () => {
    expect(parseDelimited('"a,1","line\nbreak","say ""hi"""\n', ',')).toEqual([
      ['a,1', 'line\nbreak', 'say "hi"'],
    ]);
  });

  it('keeps empty fields, including a trailing one', () => {
    expect(parseDelimited('a,,c,\n', ',')).toEqual([['a', '', 'c', '']]);
  });

  it('keeps a quote inside an unquoted field as text', () => {
    expect(parseDelimited('5" disk,x', ',')).toEqual([['5" disk', 'x']]);
  });

  it('runs an unclosed quote to the end of the input', () => {
    expect(parseDelimited('a,"open\nstill', ',')).toEqual([['a', 'open\nstill']]);
  });

  it('splits on tabs for TSV', () => {
    expect(parseDelimited('a\tb,c\n', '\t')).toEqual([['a', 'b,c']]);
  });
});

describe('csvRecords and csvRows', () => {
  it('takes the first record as the header and pads ragged rows', () => {
    const records = csvRecords('name,age\nAda\nBo,3,extra\n', ',')!;
    expect(records.header).toEqual(['name', 'age', '']);
    expect(records.width).toBe(3);
    expect(records.body.length).toBe(2);
    expect(csvRows(records, null)).toEqual([
      ['Ada', '', ''],
      ['Bo', '3', 'extra'],
    ]);
  });

  it('skips blank lines and returns null for an empty file', () => {
    expect(csvRows(csvRecords('a\n\n\nb\n', ',')!, null)).toEqual([['b']]);
    expect(csvRecords('', ',')).toBeNull();
    expect(csvRecords('\n\n', ',')).toBeNull();
  });

  it('caps the rows it returns but keeps them all', () => {
    const text = ['h', ...Array.from({ length: 10 }, (_, i) => String(i))].join('\n');
    const records = csvRecords(text, ',')!;
    expect(csvRows(records, null, 3)).toEqual([['0'], ['1'], ['2']]);
    expect(records.body.length).toBe(10);
  });

  it('sorts a numeric column as numbers, signs and decimals included', () => {
    const records = csvRecords('n\n10\n-2\n1.5\n1.25\n9\n', ',')!;
    const up = csvRows(records, { column: 0, descending: false }).map((r) => r[0]);
    expect(up).toEqual(['-2', '1.25', '1.5', '9', '10']);
    const down = csvRows(records, { column: 0, descending: true }).map((r) => r[0]);
    expect(down).toEqual(['10', '9', '1.5', '1.25', '-2']);
  });

  it('sorts a text column as text, case aside', () => {
    const records = csvRecords('k,name\n1,bo\n2,Ada\n3,cy\n', ',')!;
    expect(csvRows(records, { column: 1, descending: false }).map((r) => r[1])).toEqual([
      'Ada',
      'bo',
      'cy',
    ]);
  });

  it('puts empty cells last in both directions, numeric or text', () => {
    const numeric = csvRecords('n,k\n2,a\n,b\n1,c\n', ',')!;
    expect(csvRows(numeric, { column: 0, descending: false }).map((r) => r[0])).toEqual([
      '1',
      '2',
      '',
    ]);
    expect(csvRows(numeric, { column: 0, descending: true }).map((r) => r[0])).toEqual([
      '2',
      '1',
      '',
    ]);
    const words = csvRecords('k,s\n1,bo\n2,\n3,Ada\n', ',')!;
    expect(csvRows(words, { column: 1, descending: false }).map((r) => r[1])).toEqual([
      'Ada',
      'bo',
      '',
    ]);
    expect(csvRows(words, { column: 1, descending: true }).map((r) => r[1])).toEqual([
      'bo',
      'Ada',
      '',
    ]);
    // A ragged row padded to the header's width is empty in the missing column.
    const text = csvRecords('a,b\nx\ny,2\nz,1\n', ',')!;
    expect(csvRows(text, { column: 1, descending: true }).map((r) => r[0])).toEqual([
      'y',
      'z',
      'x',
    ]);
  });

  it('sorts every row before cutting to the rows drawn', () => {
    const values = Array.from({ length: 10 }, (_, i) => String(i));
    const records = csvRecords(['n', ...values].join('\n'), ',')!;
    expect(csvRows(records, { column: 0, descending: true }, 3)).toEqual([['9'], ['8'], ['7']]);
  });
});

describe('cellParts', () => {
  it('links an http or https URL and keeps the text around it', () => {
    expect(cellParts('see https://example.com/a?b=1 for more')).toEqual([
      { text: 'see ' },
      { text: 'https://example.com/a?b=1', href: 'https://example.com/a?b=1' },
      { text: ' for more' },
    ]);
    expect(cellParts('http://example.com')).toEqual([
      { text: 'http://example.com', href: 'http://example.com' },
    ]);
  });

  it('requires a scheme, so a filename or a bare domain stays text', () => {
    expect(cellParts('report.md')).toEqual([{ text: 'report.md' }]);
    expect(cellParts('example.com')).toEqual([{ text: 'example.com' }]);
  });

  it('leaves a protocol-relative address as text, colon or not', () => {
    expect(cellParts('see //evil.example/x at 10:00')).toEqual([
      { text: 'see //evil.example/x at 10:00' },
    ]);
  });

  it('leaves a javascript: value as text', () => {
    expect(cellParts('javascript:alert(1)')).toEqual([{ text: 'javascript:alert(1)' }]);
  });

  it('returns one plain part for an empty cell', () => {
    expect(cellParts('')).toEqual([{ text: '' }]);
  });
});
