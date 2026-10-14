import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';
import type { WorkspaceScriptsIndex } from '$lib/api';
import ScriptIndex from './ScriptIndex.svelte';

const empty: WorkspaceScriptsIndex = {
  scripts_view_path: '/Users/alice/Helper/scripts',
  files: [],
  broken: [],
  truncated: false,
  sources: { cron: 'missing', heartbeat: 'missing', scripts: 'ok' },
};
const props = {
  label: 'Scripts',
  caption: 'Programs in your scripts folder.',
  empty: 'No scripts yet.',
  kind: 'script' as const,
  loading: false,
  error: '',
  onOpen: vi.fn(),
  onRetry: vi.fn(),
  revealAction: () => ({ label: 'Reveal', disabled: true }),
};
afterEach(cleanup);

it('shows an empty index without treating missing sources as errors', () => {
  render(ScriptIndex, { ...props, index: empty });
  expect(screen.getByText('No scripts yet.')).toBeTruthy();
  expect(screen.queryByRole('note')).toBeNull();
  expect(screen.queryByRole('status')).toBeNull();
});

it('sorts broken paths by kind and shows partial-index notes', () => {
  const index: WorkspaceScriptsIndex = {
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
      {
        path: 'gone.md',
        referenced_by: [
          {
            source: 'cron',
            name: 'review',
            field: 'prompt_file',
            kind: 'prompt_file',
            state: 'enabled',
          },
        ],
      },
    ],
  };
  render(ScriptIndex, { ...props, index });
  const note = screen.getByRole('note');
  expect(note).toHaveTextContent('gone.sh: CRON.md: backup (suspended)');
  expect(note).not.toHaveTextContent('gone.md');
  expect(screen.getAllByRole('status').map((node) => node.textContent)).toEqual([
    'Some files are not shown because the listing reached its limit.',
    'CRON.md could not be read; references from it are not shown.',
    'HEARTBEAT.md could not be read; references from it are not shown.',
    'The scripts folder could not be read safely.',
  ]);
  cleanup();
  render(ScriptIndex, { ...props, kind: 'prompt', index });
  expect(screen.getByRole('note')).toHaveTextContent('gone.md: CRON.md: review');
  expect(screen.getByRole('note')).not.toHaveTextContent('gone.sh');
});

it('summarises references and mentions on the row, as text', () => {
  render(ScriptIndex, {
    ...props,
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
              state: 'enabled',
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
  const list = screen.getByRole('list');
  expect(within(list).getByText('<b>Report</b>').querySelector('b')).toBeNull();
  expect(
    within(list).getByText(
      'Used by CRON.md: weekly (not synced yet), HEARTBEAT.md: check · mentioned in 1 prompt',
    ),
  ).toBeTruthy();
});
