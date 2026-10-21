import type { KebabItem } from '@minot/ui';
import { workExportUrl } from './api';

/**
 * The Download menu the Work and Invoices toolbars share. `year` narrows the
 * export to entries dated that year, and the labels say so. `download` is
 * empty on purpose: the server's Content-Disposition names the file, so there
 * is one naming rule (`work_export.filename`), not two.
 */
export function workExportItems(year?: string): KebabItem[] {
  const scope = year ? `${year} ` : '';
  return [
    { label: `Download ${scope}as CSV`, href: workExportUrl('csv', year), download: '' },
    {
      label: `Download ${scope}as JSON (full record)`,
      href: workExportUrl('json', year),
      download: '',
    },
  ];
}
