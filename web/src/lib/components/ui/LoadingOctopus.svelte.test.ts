import { render } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import LoadingOctopus from './LoadingOctopus.svelte';

describe('LoadingOctopus', () => {
  it('draws the sigil with the label for a screen reader only', () => {
    const { container } = render(LoadingOctopus, { label: 'Loading messages…' });
    const mark = container.querySelector<HTMLElement>('.loading-octopus-mark');
    expect(mark?.style.getPropertyValue('--sigil-url')).toMatch(/octopus-sigil\.webp/);
    const label = container.querySelector('.loading-octopus .visually-hidden');
    expect(label?.textContent).toBe('Loading messages…');
  });
});
