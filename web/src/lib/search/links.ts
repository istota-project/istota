import { base } from '$app/paths';
import type { SearchLink } from '$lib/api';

export const ROUTE_PATHS = [
  '/chat/',
  '/briefings/',
  '/feeds/',
  '/health/documents/',
  '/health/labs/panel/',
  '/health/labs/marker/',
  '/health/history/encounter/',
  '/health/history/diagnoses/',
  '/health/immunizations/detail/',
  '/location/',
  '/money/transactions/',
] as const;

export function hrefFor(link: SearchLink | null): string | null {
  if (!link || link.type !== 'route' || !(ROUTE_PATHS as readonly string[]).includes(link.path))
    return null;
  const params = new URLSearchParams(link.params).toString();
  return `${base}${link.path}${params ? `?${params}` : ''}`;
}
