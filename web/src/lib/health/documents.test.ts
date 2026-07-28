import { describe, expect, it } from 'vitest';
import { documentName, formatBytes, mimeLabel, otherRecordsWarning } from './documents';

describe('formatBytes', () => {
  it('renders bytes below 1 KB verbatim', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('steps up through binary units', () => {
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(184320)).toBe('180 KB');
    expect(formatBytes(1024 * 1024)).toBe('1 MB');
    expect(formatBytes(25 * 1024 * 1024)).toBe('25 MB');
    expect(formatBytes(1024 ** 3)).toBe('1 GB');
  });

  it('keeps one decimal only below 10 of a unit', () => {
    expect(formatBytes(1024 * 1024 * 1.5)).toBe('1.5 MB');
    expect(formatBytes(1024 * 1024 * 12.34)).toBe('12 MB');
  });

  it('does not pretend to know a nonsensical size', () => {
    expect(formatBytes(-1)).toBe('—');
    expect(formatBytes(Number.NaN)).toBe('—');
  });
});

describe('otherRecordsWarning', () => {
  it('says nothing when the document is attached only here', () => {
    expect(otherRecordsWarning(0)).toBe('');
    expect(otherRecordsWarning(-1)).toBe('');
  });

  it('is singular for one other record', () => {
    expect(otherRecordsWarning(1)).toBe(
      'This document is also attached to 1 other record. Deleting removes it everywhere.',
    );
  });

  it('is plural for several', () => {
    expect(otherRecordsWarning(2)).toBe(
      'This document is also attached to 2 other records. Deleting removes it everywhere.',
    );
  });
});

describe('mimeLabel', () => {
  it('shortens the types a user actually sees', () => {
    expect(mimeLabel('application/pdf')).toBe('PDF');
    expect(mimeLabel('image/jpeg')).toBe('JPEG');
    expect(mimeLabel('text/plain')).toBe('Text');
  });

  it('falls through unchanged for anything else', () => {
    expect(mimeLabel('application/zip')).toBe('application/zip');
  });
});

describe('documentName', () => {
  const base = {
    id: 1,
    filename: 'after-visit-summary.pdf',
    mime: 'application/pdf',
    byte_size: 10,
    source: 'import' as const,
    notes: null,
    created_at: '',
    url: '',
  };

  it('prefers what the user uploaded over the sanitized name', () => {
    expect(documentName({ ...base, original_filename: 'After Visit Summary.pdf' })).toBe(
      'After Visit Summary.pdf',
    );
  });

  it('falls back to the stored name', () => {
    expect(documentName({ ...base, original_filename: null })).toBe('after-visit-summary.pdf');
  });
});
