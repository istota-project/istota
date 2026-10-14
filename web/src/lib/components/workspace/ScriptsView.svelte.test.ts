import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';
import type { WorkspaceScriptsIndex } from '$lib/api';
import ScriptsView from './ScriptsView.svelte';

const empty: WorkspaceScriptsIndex = {
  scripts_view_path: '/Users/alice/Helper/scripts',
  files: [],
  broken: [],
  truncated: false,
  sources: { cron: 'missing', heartbeat: 'missing', scripts: 'ok' },
};
const actions = {
  onSelect: vi.fn(),
  onOpen: vi.fn(),
  onRetry: vi.fn(),
  revealAction: () => ({ label: 'Reveal', disabled: true }),
};
afterEach(cleanup);

it('shows an empty index without treating missing sources as errors', () => {
  render(ScriptsView, { ...actions, index: empty });
  expect(screen.getByText('No scripts yet')).toBeTruthy();
  expect(screen.queryByRole('table')).toBeNull();
  expect(screen.queryByRole('note')).toBeNull();
});

it('keeps broken paths visible when no scripts remain and shows partial-index notes', () => {
  render(ScriptsView, {
    ...actions,
    index: {
      ...empty,
      truncated: true,
      sources: { cron: 'error', heartbeat: 'error', scripts: 'refused' },
      broken: [
        {
          path: 'gone.sh',
          referenced_by: [
            { source: 'cron', name: 'backup', field: 'command', kind: 'runs', state: 'suspended' },
          ],
        },
      ],
    },
  });
  expect(screen.getByRole('note')).toHaveTextContent('gone.sh');
  expect(screen.getByRole('note')).toHaveTextContent('suspended');
  expect(screen.getByText('No scripts yet')).toBeTruthy();
  expect(
    screen.getByText('Some files are not shown because the scripts listing reached its limit.'),
  ).toBeTruthy();
  expect(
    screen.getByText('CRON.md could not be read; references from it are not shown.'),
  ).toBeTruthy();
  expect(
    screen.getByText('HEARTBEAT.md could not be read; references from it are not shown.'),
  ).toBeTruthy();
  expect(screen.getByText('The scripts folder could not be read safely.')).toBeTruthy();
});

it('separates mentions from references and expands every reference with its field and kind', () => {
  render(ScriptsView, {
    ...actions,
    selected: 'report.py',
    index: {
      ...empty,
      files: [
        {
          path: 'report.py',
          kind: 'script',
          size: 10,
          modified: '2026-10-01T09:00:00Z',
          description: '<b>Report</b>',
          referenced_by: [
            { source: 'cron', name: 'weekly', field: 'command', kind: 'runs', state: 'not_synced' },
            {
              source: 'heartbeat',
              name: 'check',
              field: 'command',
              kind: 'runs',
              state: 'admin_only',
            },
          ],
          mentioned_in: [
            {
              source: 'cron',
              name: 'summary',
              field: 'prompt',
              kind: 'mentions',
              state: 'disabled',
            },
          ],
        },
      ],
    },
  });
  const table = screen.getByRole('table', { name: 'Scripts' });
  expect(within(table).getByText('<b>Report</b>').querySelector('b')).toBeNull();
  expect(within(table).getByText('mentioned in 1 prompt')).toBeTruthy();
  expect(within(table).queryByText('Not referenced')).toBeNull();
  const details = screen.getByRole('region', { name: 'References for report.py' });
  expect(within(details).getAllByRole('listitem')).toHaveLength(3);
  expect(details).toHaveTextContent('cron: weekly · not_synced · field: command · kind: runs');
  expect(details).toHaveTextContent('heartbeat: check · admin_only · field: command · kind: runs');
  expect(details).toHaveTextContent('cron: summary · disabled · field: prompt · kind: mentions');
});
