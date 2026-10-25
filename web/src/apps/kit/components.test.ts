import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const KIT = join(process.cwd(), 'src', 'apps', 'kit');

type Dialogs = {
  alert(message: string, options?: Record<string, unknown>): Promise<void>;
  confirm(message: string, options?: Record<string, unknown>): Promise<boolean>;
  prompt(message: string, options?: Record<string, unknown>): Promise<string | null>;
};
type ChartElement = HTMLElement & { data: unknown };
type DialogElement = HTMLElement & { show(): void; close(value?: unknown): void };

// One media query list the components subscribe to, flipped by the tests.
const listeners = new Set<(event: { matches: boolean }) => void>();
const phone = {
  matches: false,
  media: '(max-width: 640px)',
  addEventListener: (_type: string, fn: (event: { matches: boolean }) => void) => listeners.add(fn),
  removeEventListener: (_type: string, fn: (event: { matches: boolean }) => void) =>
    listeners.delete(fn),
};

function setPhone(matches: boolean): void {
  phone.matches = matches;
  for (const fn of listeners) fn({ matches });
}

let dialogs: Dialogs;

beforeAll(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => (query === phone.media ? phone : { ...phone, matches: false })),
  );
  (globalThis as unknown as { istota: Record<string, unknown> }).istota = {};
  new Function(readFileSync(join(KIT, 'components.js'), 'utf8'))();
  dialogs = (globalThis as unknown as { istota: { dialog: Dialogs } }).istota.dialog;
});

afterEach(() => {
  document.body.replaceChildren();
  setPhone(false);
});

function mount(html: string): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = html;
  document.body.append(host);
  return host;
}

describe('the phone breakpoint', () => {
  it.each(['istota-list', 'istota-table', 'istota-form', 'istota-chart'])(
    '%s collapses below 641px and widens again',
    (tag) => {
      const host = mount(`<${tag}></${tag}>`);
      const element = host.querySelector(tag)!;
      expect(element.hasAttribute('data-compact')).toBe(false);
      setPhone(true);
      expect(element.hasAttribute('data-compact')).toBe(true);
      setPhone(false);
      expect(element.hasAttribute('data-compact')).toBe(false);
    },
  );

  it('an element added on a phone starts collapsed', () => {
    setPhone(true);
    const host = mount('<istota-table></istota-table>');
    expect(host.querySelector('istota-table')!.hasAttribute('data-compact')).toBe(true);
  });

  it('the stylesheet turns a collapsed table into cards and stacks a form', () => {
    const css = readFileSync(join(KIT, 'istota.css'), 'utf8');
    expect(css).toMatch(/istota-table\[data-compact\] thead/);
    expect(css).toMatch(/istota-table\[data-compact\] td::before[\s\S]*attr\(data-label\)/);
    expect(css).toMatch(/istota-form\[data-compact\] label/);
  });
});

describe('istota-button', () => {
  it('renders a real button around its label, with the variant', () => {
    const host = mount('<istota-button variant="primary">Save</istota-button>');
    const button = host.querySelector('istota-button > button')!;
    expect(button.textContent).toBe('Save');
    expect(button.getAttribute('type')).toBe('button');
    expect(button.classList.contains('istota-btn--primary')).toBe(true);
  });

  it('passes disabled through and keeps one button when moved', () => {
    const host = mount('<istota-button disabled>Go</istota-button>');
    const element = host.querySelector('istota-button')!;
    expect((element.querySelector('button') as HTMLButtonElement).disabled).toBe(true);
    element.removeAttribute('disabled');
    expect((element.querySelector('button') as HTMLButtonElement).disabled).toBe(false);
    document.body.append(element);
    expect(element.querySelectorAll('button')).toHaveLength(1);
  });

  it('falls back to secondary for an unknown variant', () => {
    const host = mount('<istota-button variant="loud">x</istota-button>');
    expect(host.querySelector('button')!.className).toBe('istota-btn istota-btn--secondary');
  });
});

describe('istota-list', () => {
  it('marks itself and its rows as a list', () => {
    const host = mount('<istota-list><div>a</div><div>b</div></istota-list>');
    const list = host.querySelector('istota-list')!;
    expect(list.getAttribute('role')).toBe('list');
    expect([...list.children].map((c) => c.getAttribute('role'))).toEqual(['listitem', 'listitem']);
  });
});

describe('istota-table', () => {
  it('labels every cell with its column header for the card layout', async () => {
    const host = mount(`
      <istota-table><table>
        <thead><tr><th>Name</th><th>Rent</th></tr></thead>
        <tbody><tr><td>A</td><td>900</td></tr></tbody>
      </table></istota-table>`);
    const cells = [...host.querySelectorAll('td')];
    expect(cells.map((c) => c.dataset.label)).toEqual(['Name', 'Rent']);
    host
      .querySelector('tbody')!
      .insertAdjacentHTML('beforeend', '<tr><td>B</td><td>1000</td></tr>');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...host.querySelectorAll('td')].map((c) => c.dataset.label)).toEqual([
      'Name',
      'Rent',
      'Name',
      'Rent',
    ]);
  });
});

describe('istota-form', () => {
  it('hands the values over as an event instead of submitting', () => {
    const host = mount(`
      <istota-form><form>
        <label><span>Text</span><input name="text" value="rain"></label>
        <label><span>Tags</span><input name="tag" value="a"></label>
        <input name="tag" value="b">
      </form></istota-form>`);
    const form = host.querySelector('form')!;
    const seen: unknown[] = [];
    host
      .querySelector('istota-form')!
      .addEventListener('istota-submit', (event) => seen.push((event as CustomEvent).detail));
    const submit = new Event('submit', { bubbles: true, cancelable: true });
    form.dispatchEvent(submit);
    expect(submit.defaultPrevented).toBe(true);
    expect(seen).toEqual([{ text: 'rain', tag: ['a', 'b'] }]);
  });

  // Chromium and Firefox fire no submit event in a frame sandboxed without
  // allow-forms, which an app's frame always is. A capturing listener that
  // stops every submit stands in for that here.
  describe('in a frame that fires no submit event', () => {
    const swallow = (event: Event) => event.stopImmediatePropagation();
    beforeAll(() => window.addEventListener('submit', swallow, true));
    afterAll(() => window.removeEventListener('submit', swallow, true));

    function form(extra = '') {
      const host = mount(`
        <istota-form><form>
          <label><span>Text</span><input name="text" value="rain"></label>
          <label><span>Notes</span><textarea name="notes">wet</textarea></label>
          ${extra}
          <istota-button type="submit">Add</istota-button>
        </form></istota-form>`);
      const seen: unknown[] = [];
      host
        .querySelector('istota-form')!
        .addEventListener('istota-submit', (event) => seen.push((event as CustomEvent).detail));
      return { host, seen };
    }

    it('hands the values over from a click on the submit button', () => {
      const { host, seen } = form();
      host.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
      expect(seen).toEqual([{ text: 'rain', notes: 'wet' }]);
    });

    it('hands the values over from Enter in a text field, but not in a textarea', () => {
      const { host, seen } = form();
      const enter = () =>
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      const inTextarea = enter();
      host.querySelector('textarea')!.dispatchEvent(inTextarea);
      expect(seen).toEqual([]);
      expect(inTextarea.defaultPrevented).toBe(false);
      const inInput = enter();
      host.querySelector('input')!.dispatchEvent(inInput);
      expect(inInput.defaultPrevented).toBe(true);
      expect(seen).toEqual([{ text: 'rain', notes: 'wet' }]);
    });

    it('does not hand over a form that fails its own validation', () => {
      const { host, seen } = form('<label><span>Due</span><input name="due" required></label>');
      host.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
      expect(seen).toEqual([]);
    });

    it('ignores a plain button', () => {
      const { host, seen } = form('<istota-button>Other</istota-button>');
      host.querySelector<HTMLButtonElement>('button[type="button"]')!.click();
      expect(seen).toEqual([]);
    });
  });

  it('emits once where the native submit event does fire', () => {
    const host = mount(`
      <istota-form><form>
        <input name="text" value="rain">
        <button type="submit">Add</button>
      </form></istota-form>`);
    const seen: unknown[] = [];
    host
      .querySelector('istota-form')!
      .addEventListener('istota-submit', (event) => seen.push((event as CustomEvent).detail));
    host.querySelector('button')!.click();
    host
      .querySelector('input')!
      .dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
      );
    expect(seen).toEqual([{ text: 'rain' }, { text: 'rain' }]);
  });
});

describe('istota-dialog', () => {
  it('opens as a modal dialog, closes on Escape and restores focus', () => {
    const host = mount(`
      <button id="before">x</button>
      <istota-dialog label="Move card"><p>Where to?</p><button id="inside">Here</button></istota-dialog>`);
    const before = host.querySelector<HTMLButtonElement>('#before')!;
    before.focus();
    const dialog = host.querySelector('istota-dialog') as DialogElement;
    const closed: unknown[] = [];
    dialog.addEventListener('close', (event) => closed.push((event as CustomEvent).detail));
    dialog.show();
    const panel = dialog.querySelector('[role="dialog"]')!;
    expect(panel.getAttribute('aria-modal')).toBe('true');
    expect(panel.getAttribute('aria-label')).toBe('Move card');
    expect(dialog.hasAttribute('open')).toBe(true);
    expect(document.activeElement?.id).toBe('inside');
    panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(dialog.hasAttribute('open')).toBe(false);
    expect(closed).toEqual([null]);
    expect(document.activeElement).toBe(before);
  });

  it('confirm resolves true on the confirm button and removes itself', async () => {
    const answer = dialogs.confirm('Delete <b>this</b>?', { confirmLabel: 'Delete', danger: true });
    const dialog = document.querySelector('istota-dialog')!;
    expect(dialog.textContent).toContain('Delete <b>this</b>?');
    expect(dialog.querySelector('b')).toBeNull();
    const buttons = [...dialog.querySelectorAll('button')];
    const confirm = buttons.find((b) => b.textContent === 'Delete')!;
    expect(confirm.classList.contains('istota-btn--danger')).toBe(true);
    confirm.click();
    await expect(answer).resolves.toBe(true);
    expect(document.querySelector('istota-dialog')).toBeNull();
  });

  it('confirm resolves false on cancel', async () => {
    const answer = dialogs.confirm('Sure?');
    const cancel = [...document.querySelectorAll('istota-dialog button')].find(
      (b) => b.textContent === 'Cancel',
    ) as HTMLButtonElement;
    cancel.click();
    await expect(answer).resolves.toBe(false);
  });

  it('prompt resolves the typed text, or null when dismissed', async () => {
    const typed = dialogs.prompt('Name?', { value: 'Board' });
    const input = document.querySelector<HTMLInputElement>('istota-dialog input')!;
    expect(input.value).toBe('Board');
    input.value = 'Ledger';
    [...document.querySelectorAll<HTMLButtonElement>('istota-dialog button')]
      .find((b) => b.textContent === 'OK')!
      .click();
    await expect(typed).resolves.toBe('Ledger');

    const dismissed = dialogs.prompt('Name?');
    document
      .querySelector('istota-dialog [role="dialog"]')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await expect(dismissed).resolves.toBeNull();
  });

  it('alert resolves once acknowledged', async () => {
    const done = dialogs.alert('Saved.');
    document.querySelector<HTMLButtonElement>('istota-dialog button')!.click();
    await expect(done).resolves.toBeUndefined();
  });
});

describe('istota-chart', () => {
  const series = [
    {
      name: 'Rent',
      points: [
        { x: 1, y: 900 },
        { x: 2, y: 950 },
        { x: 3, y: 1000 },
      ],
    },
    {
      name: 'Commute',
      points: [
        { x: 1, y: 30 },
        { x: 2, y: 45 },
        { x: 3, y: 20 },
      ],
    },
  ];

  function chart(type: string, data: unknown): ChartElement {
    const host = mount(`<istota-chart type="${type}" label="Costs"></istota-chart>`);
    const element = host.querySelector('istota-chart') as ChartElement;
    element.data = data;
    return element;
  }

  it('draws a line per series with a marker per point, and a legend for two', () => {
    const element = chart('line', { series });
    const svg = element.querySelector('svg')!;
    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.getAttribute('aria-label')).toBe('Costs');
    expect(svg.querySelectorAll('path.istota-chart-line')).toHaveLength(2);
    expect(svg.querySelectorAll('circle.istota-chart-dot')).toHaveLength(6);
    expect(element.querySelectorAll('.istota-chart-legend li')).toHaveLength(2);
    expect(element.querySelector('.istota-chart-legend')!.textContent).toContain('Commute');
  });

  it('draws one bar per category and series, with no legend for one series', () => {
    const element = chart('bar', {
      series: [
        {
          name: 'Guesses',
          points: [
            { x: 'Jan', y: 3 },
            { x: 'Feb', y: 0 },
          ],
        },
      ],
    });
    expect(element.querySelectorAll('svg .istota-chart-bar')).toHaveLength(2);
    expect(element.querySelector('.istota-chart-legend')).toBeNull();
    expect(element.querySelector('svg')!.textContent).toContain('Feb');
  });

  it('draws a dot per point for a scatter, with no line', () => {
    const element = chart('scatter', { series: [series[0]] });
    expect(element.querySelectorAll('svg circle.istota-chart-dot')).toHaveLength(3);
    expect(element.querySelector('svg path.istota-chart-line')).toBeNull();
  });

  it('names each mark for the hover tooltip', () => {
    const element = chart('scatter', { series: [series[0]] });
    expect(element.querySelector('svg circle.istota-chart-dot title')!.textContent).toBe(
      'Rent: 1, 900',
    );
  });

  it('colours series by position through the kit tokens, never text', () => {
    const element = chart('line', { series });
    const lines = element.querySelectorAll('path.istota-chart-line');
    expect(lines[0].classList.contains('istota-series-0')).toBe(true);
    expect(lines[1].classList.contains('istota-series-1')).toBe(true);
  });

  it('draws nothing but an empty frame for no data, and survives bad data', () => {
    expect(chart('line', null).querySelector('svg')).not.toBeNull();
    expect(() =>
      chart('line', { series: [{ name: 'x', points: [{ x: 'a', y: NaN }] }] }),
    ).not.toThrow();
  });

  it('puts fewer ticks on the axis on a phone', () => {
    const element = chart('line', { series });
    const wide = element.querySelectorAll('.istota-chart-tick').length;
    setPhone(true);
    const narrow = element.querySelectorAll('.istota-chart-tick').length;
    expect(narrow).toBeLessThan(wide);
  });
});
