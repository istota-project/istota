import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import OptionCards from './OptionCards.svelte';

afterEach(cleanup);

const options = [
  {
    value: 'anthropic',
    title: 'Anthropic API key',
    description: 'Pay as you go.',
    badge: 'Recommended',
  },
  { value: 'openrouter', title: 'OpenRouter API key', description: 'Many models, one key.' },
  { value: 'subscription', title: 'Claude subscription', description: 'Takes longer to finish.' },
];

function radio(name: string) {
  return screen.getByRole('radio', { name: new RegExp(name) });
}

describe('OptionCards', () => {
  it('is a labelled radio group exposing the checked card as aria-checked', () => {
    render(OptionCards, { options, value: 'openrouter', label: 'AI service' });
    expect(screen.getByRole('radiogroup', { name: 'AI service' })).toBeInTheDocument();
    expect(radio('OpenRouter')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Anthropic')).toHaveAttribute('aria-checked', 'false');
    expect(radio('Claude')).toHaveAttribute('aria-checked', 'false');
  });

  it('shows each card’s description and badge', () => {
    render(OptionCards, { options, value: 'anthropic', label: 'AI service' });
    expect(screen.getByText('Pay as you go.')).toBeInTheDocument();
    expect(screen.getByText('Recommended')).toBeInTheDocument();
  });

  it('keeps only the checked card in the tab order', () => {
    render(OptionCards, { options, value: 'openrouter', label: 'AI service' });
    expect(radio('OpenRouter')).toHaveAttribute('tabindex', '0');
    expect(radio('Anthropic')).toHaveAttribute('tabindex', '-1');
  });

  it('selects a card on click and reports the change', async () => {
    const onchange = vi.fn();
    render(OptionCards, { options, value: 'anthropic', label: 'AI service', onchange });
    await fireEvent.click(radio('Claude'));
    expect(radio('Claude')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Anthropic')).toHaveAttribute('aria-checked', 'false');
    expect(onchange).toHaveBeenCalledWith('subscription');
  });

  it('moves the selection and the focus with the arrow keys, wrapping at the ends', async () => {
    const onchange = vi.fn();
    render(OptionCards, { options, value: 'anthropic', label: 'AI service', onchange });
    await fireEvent.keyDown(radio('Anthropic'), { key: 'ArrowDown' });
    expect(radio('OpenRouter')).toHaveAttribute('aria-checked', 'true');
    expect(document.activeElement).toBe(radio('OpenRouter'));
    await fireEvent.keyDown(radio('OpenRouter'), { key: 'ArrowRight' });
    expect(radio('Claude')).toHaveAttribute('aria-checked', 'true');
    await fireEvent.keyDown(radio('Claude'), { key: 'ArrowDown' });
    expect(radio('Anthropic')).toHaveAttribute('aria-checked', 'true');
    await fireEvent.keyDown(radio('Anthropic'), { key: 'ArrowUp' });
    expect(radio('Claude')).toHaveAttribute('aria-checked', 'true');
    expect(onchange).toHaveBeenLastCalledWith('subscription');
  });

  it('skips a disabled card with the arrows and refuses a click on it', async () => {
    const withDisabled = options.map((o) =>
      o.value === 'openrouter' ? { ...o, disabled: true } : o,
    );
    const onchange = vi.fn();
    render(OptionCards, {
      options: withDisabled,
      value: 'anthropic',
      label: 'AI service',
      onchange,
    });
    await fireEvent.keyDown(radio('Anthropic'), { key: 'ArrowDown' });
    expect(radio('Claude')).toHaveAttribute('aria-checked', 'true');
    expect(radio('OpenRouter')).toHaveAttribute('aria-checked', 'false');
    await fireEvent.click(radio('OpenRouter'));
    expect(radio('OpenRouter')).toHaveAttribute('aria-checked', 'false');
    expect(onchange).not.toHaveBeenCalledWith('openrouter');
  });

  it('puts the first enabled card in the tab order when nothing is checked', () => {
    const withDisabled = options.map((o) =>
      o.value === 'anthropic' ? { ...o, disabled: true } : o,
    );
    render(OptionCards, { options: withDisabled, value: null, label: 'AI service' });
    expect(radio('OpenRouter')).toHaveAttribute('tabindex', '0');
    expect(radio('Anthropic')).toHaveAttribute('tabindex', '-1');
  });
});
