/**
 * Presentation helpers for health documents.
 *
 * Kept out of the components so the copy that has to be exactly right —
 * how many *other* records a delete would strip a document from — is unit
 * testable without mounting anything.
 */

import type { HealthDocument } from '$lib/api';

/** Human file size. Binary units, one decimal above KB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 || unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

/**
 * The warning under a delete confirmation.
 *
 * `otherLinks` counts links to records *other* than the one the user is
 * deleting from — a document attached only here has no extra consequence,
 * so it gets no sentence at all rather than a "0 other records" one.
 */
export function otherRecordsWarning(otherLinks: number): string {
  if (otherLinks <= 0) return '';
  if (otherLinks === 1) {
    return 'This document is also attached to 1 other record. Deleting removes it everywhere.';
  }
  return `This document is also attached to ${otherLinks} other records. Deleting removes it everywhere.`;
}

/** Short label for the MIME chip — `application/pdf` reads as noise. */
export function mimeLabel(mime: string): string {
  if (mime === 'application/pdf') return 'PDF';
  if (mime === 'text/plain') return 'Text';
  if (mime.startsWith('image/')) return mime.slice('image/'.length).toUpperCase();
  return mime;
}

/** Display name, preferring what the user actually uploaded. */
export function documentName(doc: HealthDocument): string {
  return doc.original_filename || doc.filename;
}
