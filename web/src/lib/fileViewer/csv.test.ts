import { describe, expect, it } from 'vitest';
import { csvTable, parseDelimited } from './csv';

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

describe('csvTable', () => {
  it('takes the first record as the header and pads ragged rows', () => {
    expect(csvTable('name,age\nAda\nBo,3,extra\n', ',')).toEqual({
      header: ['name', 'age', ''],
      rows: [
        ['Ada', '', ''],
        ['Bo', '3', 'extra'],
      ],
      totalRows: 2,
      width: 3,
    });
  });

  it('skips blank lines and returns null for an empty file', () => {
    expect(csvTable('a\n\n\nb\n', ',')?.rows).toEqual([['b']]);
    expect(csvTable('', ',')).toBeNull();
    expect(csvTable('\n\n', ',')).toBeNull();
  });

  it('caps the rows it returns but counts them all', () => {
    const text = ['h', ...Array.from({ length: 10 }, (_, i) => String(i))].join('\n');
    const table = csvTable(text, ',', 3)!;
    expect(table.rows).toEqual([['0'], ['1'], ['2']]);
    expect(table.totalRows).toBe(10);
  });
});
