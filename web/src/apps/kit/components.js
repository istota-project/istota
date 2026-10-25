// The house kit's web components, in light DOM so `istota.css` and the app's
// own styles reach them. Inlined after the client library, before app code.
//
//   <istota-button variant>   a real <button>, primary | secondary | danger
//   <istota-list>             rows; on a phone a row's trailing parts wrap under it
//   <istota-table>            wraps a <table>; on a phone each row becomes a card
//   <istota-form>             wraps a <form>; label beside control, stacked on a phone;
//                             submit becomes an `istota-submit` event with the values
//   <istota-dialog label>     the in-frame dialog, with istota.dialog.alert/confirm/prompt
//   <istota-chart type label> line, bar or scatter in SVG, from `element.data`
//
// "On a phone" is minot's sm step, (max-width: 640px), measured against the
// frame: the components set `data-compact` and the stylesheet does the rest.
(() => {
  const PHONE = '(max-width: 640px)';
  const phone = typeof window.matchMedia === 'function' ? window.matchMedia(PHONE) : null;
  /** @type {Set<HTMLElement>} */
  const compactable = new Set();

  /** @param {HTMLElement} element */
  function applyCompact(element) {
    element.toggleAttribute('data-compact', Boolean(phone && phone.matches));
    if (element instanceof IstotaChart) element.render();
  }

  if (phone) {
    phone.addEventListener('change', () => {
      for (const element of compactable) applyCompact(element);
    });
  }

  class CompactElement extends HTMLElement {
    connectedCallback() {
      compactable.add(this);
      applyCompact(this);
    }

    disconnectedCallback() {
      compactable.delete(this);
    }
  }

  const VARIANTS = ['primary', 'secondary', 'danger'];

  /** @param {string | null} variant */
  function buttonClass(variant) {
    return `istota-btn istota-btn--${variant && VARIANTS.includes(variant) ? variant : 'secondary'}`;
  }

  class IstotaButton extends HTMLElement {
    static observedAttributes = ['disabled', 'variant', 'type'];

    /** @type {HTMLButtonElement | null} */
    button = null;

    connectedCallback() {
      if (!this.button) {
        const button = document.createElement('button');
        button.append(...this.childNodes);
        this.append(button);
        this.button = button;
      }
      this.sync();
    }

    attributeChangedCallback() {
      this.sync();
    }

    sync() {
      if (!this.button) return;
      this.button.className = buttonClass(this.getAttribute('variant'));
      const type = this.getAttribute('type');
      this.button.type = type === 'submit' || type === 'reset' ? type : 'button';
      this.button.disabled = this.hasAttribute('disabled');
    }
  }

  class IstotaList extends CompactElement {
    /** @type {MutationObserver | null} */
    observer = null;

    connectedCallback() {
      super.connectedCallback();
      this.setAttribute('role', 'list');
      this.markRows();
      this.observer = new MutationObserver(() => this.markRows());
      this.observer.observe(this, { childList: true });
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      this.observer?.disconnect();
    }

    markRows() {
      for (const child of this.children) {
        if (!child.hasAttribute('role')) child.setAttribute('role', 'listitem');
      }
    }
  }

  class IstotaTable extends CompactElement {
    /** @type {MutationObserver | null} */
    observer = null;

    connectedCallback() {
      super.connectedCallback();
      this.label();
      this.observer = new MutationObserver(() => this.label());
      this.observer.observe(this, { childList: true, subtree: true, characterData: true });
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      this.observer?.disconnect();
    }

    // The card layout names each cell from its column header.
    label() {
      const table = this.querySelector('table');
      if (!table) return;
      const headers = [...table.querySelectorAll('thead th')].map((th) =>
        (th.textContent || '').trim(),
      );
      for (const row of table.querySelectorAll('tbody tr')) {
        [...row.children].forEach((cell, index) => {
          const text = headers[index] || '';
          if (cell instanceof HTMLElement && cell.dataset.label !== text) {
            cell.dataset.label = text;
          }
        });
      }
    }
  }

  // Input types where Enter submits the form, as the browser's implicit submission does.
  const ENTER_SUBMITS = [
    'text',
    'search',
    'url',
    'tel',
    'email',
    'password',
    'number',
    'date',
    'time',
    'datetime-local',
    'month',
    'week',
  ];

  // Chromium and Firefox fire no submit event at all in a frame sandboxed
  // without allow-forms, which an app's frame always is; WebKit does. So the
  // form takes the two things that submit, a submit button's click and Enter in
  // a field, itself, and cancels them, which also stops the native submit where
  // it would have fired: one `istota-submit` either way. The submit listener
  // stays for an app that calls `requestSubmit()` in a browser that fires it.
  class IstotaForm extends CompactElement {
    connectedCallback() {
      super.connectedCallback();
      this.addEventListener('submit', this.onSubmit);
      this.addEventListener('click', this.onClick);
      this.addEventListener('keydown', this.onKeyDown);
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      this.removeEventListener('submit', this.onSubmit);
      this.removeEventListener('click', this.onClick);
      this.removeEventListener('keydown', this.onKeyDown);
    }

    /** @param {Element | null | undefined} form */
    ownForm(form) {
      return form instanceof HTMLFormElement && this.contains(form) ? form : null;
    }

    /** @param {Event} event */
    onSubmit = (event) => {
      const form = this.ownForm(/** @type {Element} */ (event.target));
      if (!form) return;
      // The CSP's `form-action 'none'` refuses a real submission anyway.
      event.preventDefault();
      this.emit(form);
    };

    /** @param {Event} event */
    onClick = (event) => {
      const target = /** @type {Element} */ (event.target);
      const button = target instanceof Element ? target.closest('button, input') : null;
      if (!(button instanceof HTMLButtonElement || button instanceof HTMLInputElement)) return;
      if (button.type !== 'submit' || button.disabled) return;
      const form = this.ownForm(button.form);
      if (!form) return;
      event.preventDefault();
      this.submit(form);
    };

    /** @param {KeyboardEvent} event */
    onKeyDown = (event) => {
      if (event.key !== 'Enter' || event.isComposing || event.defaultPrevented) return;
      const field = event.target;
      if (!(field instanceof HTMLInputElement) || !ENTER_SUBMITS.includes(field.type)) return;
      const form = this.ownForm(field.form);
      if (!form) return;
      event.preventDefault();
      this.submit(form);
    };

    // The browser checks a form's constraints before it submits; so does this.
    /** @param {HTMLFormElement} form */
    submit(form) {
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }
      this.emit(form);
    }

    /** @param {HTMLFormElement} form */
    emit(form) {
      /** @type {Record<string, FormDataEntryValue | FormDataEntryValue[]>} */
      const values = {};
      for (const [name, value] of new FormData(form)) {
        const seen = values[name];
        if (seen === undefined) values[name] = value;
        else if (Array.isArray(seen)) seen.push(value);
        else values[name] = [seen, value];
      }
      this.dispatchEvent(new CustomEvent('istota-submit', { detail: values, bubbles: true }));
    }
  }

  const FOCUSABLE =
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  class IstotaDialog extends HTMLElement {
    static observedAttributes = ['label'];

    /** @type {HTMLDivElement | null} */
    panel = null;
    /** @type {Element | null} */
    returnFocus = null;

    connectedCallback() {
      if (this.panel) return;
      const backdrop = document.createElement('div');
      backdrop.className = 'istota-dialog-backdrop';
      backdrop.addEventListener('click', () => this.close(null));
      const panel = document.createElement('div');
      panel.className = 'istota-dialog-panel';
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
      panel.tabIndex = -1;
      panel.append(...this.childNodes);
      panel.addEventListener('keydown', (event) => this.onKey(event));
      this.append(backdrop, panel);
      this.panel = panel;
      this.syncLabel();
    }

    attributeChangedCallback() {
      this.syncLabel();
    }

    syncLabel() {
      const label = this.getAttribute('label');
      if (!this.panel) return;
      if (label) this.panel.setAttribute('aria-label', label);
      else this.panel.removeAttribute('aria-label');
    }

    show() {
      if (!this.panel) this.connectedCallback();
      this.returnFocus = document.activeElement;
      this.setAttribute('open', '');
      const panel = /** @type {HTMLDivElement} */ (this.panel);
      const first = panel.querySelector(FOCUSABLE);
      if (first instanceof HTMLElement) first.focus();
      else panel.focus();
    }

    /** @param {unknown} [value] */
    close(value = null) {
      if (!this.hasAttribute('open')) return;
      this.removeAttribute('open');
      if (this.returnFocus instanceof HTMLElement) this.returnFocus.focus();
      this.returnFocus = null;
      this.dispatchEvent(new CustomEvent('close', { detail: value }));
    }

    /** @param {KeyboardEvent} event */
    onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        this.close(null);
        return;
      }
      if (event.key !== 'Tab' || !this.panel) return;
      const items = [...this.panel.querySelectorAll(FOCUSABLE)].filter(
        (item) => item instanceof HTMLElement,
      );
      if (items.length === 0) return;
      const first = /** @type {HTMLElement} */ (items[0]);
      const last = /** @type {HTMLElement} */ (items[items.length - 1]);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  /**
   * @param {string} text
   * @param {string} variant
   * @param {() => void} onClick
   */
  function dialogButton(text, variant, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = buttonClass(variant);
    button.textContent = text;
    button.addEventListener('click', onClick);
    return button;
  }

  /**
   * One dialog built from text, never markup, removed once answered.
   * @param {{title?: string, message: string, input?: string | null,
   *   buttons: Array<{text: string, variant: string, value: unknown}>}} spec
   * @returns {Promise<unknown>}
   */
  function openDialog(spec) {
    return new Promise((resolve) => {
      const dialog = /** @type {IstotaDialog} */ (document.createElement('istota-dialog'));
      dialog.setAttribute('label', spec.title || spec.message);
      if (spec.title) {
        const heading = document.createElement('h2');
        heading.className = 'istota-dialog-title';
        heading.textContent = spec.title;
        dialog.append(heading);
      }
      const message = document.createElement('p');
      message.className = 'istota-dialog-message';
      message.textContent = spec.message;
      dialog.append(message);
      /** @type {HTMLInputElement | null} */
      let input = null;
      if (spec.input !== undefined && spec.input !== null) {
        input = document.createElement('input');
        input.type = 'text';
        input.className = 'istota-input';
        input.value = spec.input;
        dialog.append(input);
      }
      const actions = document.createElement('div');
      actions.className = 'istota-dialog-actions';
      for (const button of spec.buttons) {
        actions.append(
          dialogButton(button.text, button.variant, () => {
            dialog.close(button.value === INPUT ? (input ? input.value : '') : button.value);
          }),
        );
      }
      dialog.append(actions);
      dialog.addEventListener('close', (event) => {
        dialog.remove();
        resolve(/** @type {CustomEvent} */ (event).detail);
      });
      document.body.append(dialog);
      dialog.show();
      if (input) {
        input.focus();
        input.select();
      }
    });
  }

  const INPUT = Symbol('input');

  /** @typedef {{title?: string, okLabel?: string, confirmLabel?: string, cancelLabel?: string, danger?: boolean, value?: string}} DialogOptions */

  const dialog = Object.freeze({
    /**
     * @param {string} message
     * @param {DialogOptions} [options]
     */
    alert: (message, options = {}) =>
      openDialog({
        title: options.title,
        message: String(message),
        buttons: [{ text: options.okLabel || 'OK', variant: 'primary', value: undefined }],
      }).then(() => undefined),
    /**
     * @param {string} message
     * @param {DialogOptions} [options]
     */
    confirm: (message, options = {}) =>
      openDialog({
        title: options.title,
        message: String(message),
        buttons: [
          { text: options.cancelLabel || 'Cancel', variant: 'secondary', value: false },
          {
            text: options.confirmLabel || 'OK',
            variant: options.danger ? 'danger' : 'primary',
            value: true,
          },
        ],
      }).then((value) => value === true),
    /**
     * @param {string} message
     * @param {DialogOptions} [options]
     */
    prompt: (message, options = {}) =>
      openDialog({
        title: options.title,
        message: String(message),
        input: options.value === undefined ? '' : String(options.value),
        buttons: [
          { text: options.cancelLabel || 'Cancel', variant: 'secondary', value: null },
          { text: options.okLabel || 'OK', variant: 'primary', value: INPUT },
        ],
      }).then((value) => (typeof value === 'string' ? value : null)),
  });

  const SVG = 'http://www.w3.org/2000/svg';
  const SERIES_COLOURS = 5;
  const CHART_TYPES = ['line', 'bar', 'scatter'];
  const PAD = { top: 12, right: 12, bottom: 28, left: 44 };

  /**
   * @typedef {{x: number | string, y: number}} Point
   * @typedef {{name: string, points: Point[]}} Series
   * @typedef {{series: Series[], xLabel?: string, yLabel?: string, yMin?: number, yMax?: number,
   *   xFormat?: (x: number | string) => string, yFormat?: (y: number) => string}} ChartData
   */

  /**
   * @param {string} name
   * @param {Record<string, string | number>} attributes
   */
  function svg(name, attributes = {}) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    return node;
  }

  /**
   * Round tick values covering [min, max], about `count` of them.
   * @param {number} min
   * @param {number} max
   * @param {number} count
   */
  function niceTicks(min, max, count) {
    if (!(max > min)) return [min];
    const raw = (max - min) / Math.max(1, count - 1);
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || raw;
    const ticks = [];
    for (let value = Math.ceil(min / step) * step; value <= max + step / 1e6; value += step) {
      ticks.push(Number(value.toFixed(10)));
    }
    return ticks;
  }

  /** @param {number} value */
  function compact(value) {
    const size = Math.abs(value);
    if (size >= 1e6) return `${Number((value / 1e6).toFixed(1))}M`;
    if (size >= 1e3) return `${Number((value / 1e3).toFixed(1))}K`;
    return String(Number(value.toFixed(2)));
  }

  /** @param {unknown} data @returns {ChartData} */
  function chartData(data) {
    const value = /** @type {Partial<ChartData> | null} */ (
      data && typeof data === 'object' ? data : null
    );
    const series = Array.isArray(value?.series) ? value.series : [];
    return {
      ...value,
      series: series
        .filter((s) => s && Array.isArray(s.points))
        .map((s) => ({
          name: String(s.name ?? ''),
          points: s.points.filter(
            (p) => p && Number.isFinite(p.y) && (typeof p.x === 'string' || Number.isFinite(p.x)),
          ),
        })),
    };
  }

  class IstotaChart extends CompactElement {
    static observedAttributes = ['type', 'label', 'height'];

    /** @type {unknown} */
    chartValue = null;
    /** @type {ResizeObserver | null} */
    resize = null;

    get data() {
      return this.chartValue;
    }

    set data(value) {
      this.chartValue = value;
      this.render();
    }

    connectedCallback() {
      super.connectedCallback();
      if (typeof ResizeObserver === 'function' && !this.resize) {
        this.resize = new ResizeObserver(() => this.render());
        this.resize.observe(this);
      }
      this.render();
    }

    disconnectedCallback() {
      super.disconnectedCallback();
      this.resize?.disconnect();
      this.resize = null;
    }

    attributeChangedCallback() {
      this.render();
    }

    render() {
      if (!this.isConnected) return;
      const data = chartData(this.chartValue);
      const typeAttr = this.getAttribute('type');
      const type = typeAttr && CHART_TYPES.includes(typeAttr) ? typeAttr : 'line';
      const narrow = this.hasAttribute('data-compact');
      const width = Math.max(200, Math.round(this.clientWidth || 600));
      const height = Math.max(120, Number(this.getAttribute('height')) || 240);
      const plot = {
        left: PAD.left,
        top: PAD.top,
        width: width - PAD.left - PAD.right,
        height: height - PAD.top - PAD.bottom,
      };

      const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' });
      root.setAttribute('class', 'istota-chart-svg');
      root.setAttribute('aria-label', this.getAttribute('label') || 'Chart');

      const points = data.series.flatMap((s) => s.points);
      const ys = points.map((p) => p.y);
      const yMin = Math.min(data.yMin ?? Infinity, type === 'bar' ? 0 : Infinity, ...ys);
      const yMax = Math.max(data.yMax ?? -Infinity, ...ys);
      const lo = Number.isFinite(yMin) ? yMin : 0;
      const hi = Number.isFinite(yMax) && yMax > lo ? yMax : lo + 1;
      const tickCount = narrow ? 3 : 5;
      const yTicks = niceTicks(lo, hi, tickCount);
      const top = Math.max(hi, yTicks[yTicks.length - 1]);
      const bottom = Math.min(lo, yTicks[0]);
      /** @param {number} y */
      const yAt = (y) =>
        plot.top + plot.height - ((y - bottom) / (top - bottom || 1)) * plot.height;
      const yFormat = typeof data.yFormat === 'function' ? data.yFormat : compact;

      for (const tick of yTicks) {
        const y = yAt(tick);
        root.append(
          svg('line', {
            class: 'istota-chart-grid',
            x1: plot.left,
            x2: plot.left + plot.width,
            y1: y,
            y2: y,
          }),
        );
        const label = svg('text', {
          class: 'istota-chart-tick',
          x: plot.left - 6,
          y: y + 4,
          'text-anchor': 'end',
        });
        label.textContent = yFormat(tick);
        root.append(label);
      }

      const categorical = type === 'bar' || points.some((p) => typeof p.x === 'string');
      /** @type {Array<number | string>} */
      const categories = [];
      if (categorical) {
        for (const p of points) if (!categories.includes(p.x)) categories.push(p.x);
      }
      const xs = categorical ? [] : points.map((p) => Number(p.x));
      const xMin = xs.length ? Math.min(...xs) : 0;
      const xMax = xs.length ? Math.max(...xs) : 1;
      const band = plot.width / Math.max(1, categories.length);
      /** @param {number | string} x */
      const xAt = (x) =>
        categorical
          ? plot.left + band * (categories.indexOf(x) + 0.5)
          : plot.left + ((Number(x) - xMin) / (xMax - xMin || 1)) * plot.width;
      const xFormat =
        typeof data.xFormat === 'function'
          ? data.xFormat
          : /** @param {number | string} x */ (x) => (typeof x === 'number' ? compact(x) : x);

      const xTicks = categorical
        ? categories.filter(
            (_c, i) => i % Math.max(1, Math.ceil(categories.length / (narrow ? 4 : 8))) === 0,
          )
        : niceTicks(xMin, xMax, tickCount);
      for (const tick of xTicks) {
        const label = svg('text', {
          class: 'istota-chart-tick',
          x: xAt(tick),
          y: plot.top + plot.height + 18,
          'text-anchor': 'middle',
        });
        label.textContent = String(xFormat(tick));
        root.append(label);
      }
      root.append(
        svg('line', {
          class: 'istota-chart-axis',
          x1: plot.left,
          x2: plot.left + plot.width,
          y1: yAt(Math.max(bottom, Math.min(0, top))),
          y2: yAt(Math.max(bottom, Math.min(0, top))),
        }),
      );

      data.series.forEach((series, index) => {
        const colour = `istota-series-${index % SERIES_COLOURS}`;
        /** @param {Point} p */
        const name = (p) => `${series.name}: ${xFormat(p.x)}, ${yFormat(p.y)}`;
        if (type === 'bar') {
          const count = data.series.length;
          const thickness = Math.min(24, (band * 0.7 - 2 * (count - 1)) / count);
          for (const p of series.points) {
            const x =
              xAt(p.x) - (count * thickness + 2 * (count - 1)) / 2 + index * (thickness + 2);
            const base = yAt(Math.max(bottom, Math.min(0, top)));
            const end = yAt(p.y);
            const bar = svg('path', {
              class: `istota-chart-bar ${colour}`,
              d: barPath(x, base, end, thickness),
            });
            const title = svg('title');
            title.textContent = name(p);
            bar.append(title);
            root.append(bar);
          }
          return;
        }
        if (type === 'line' && series.points.length > 1) {
          const sorted = [...series.points].sort((a, b) => Number(a.x) - Number(b.x));
          const d = sorted
            .map((p, i) => `${i === 0 ? 'M' : 'L'}${xAt(p.x).toFixed(1)},${yAt(p.y).toFixed(1)}`)
            .join(' ');
          root.append(svg('path', { class: `istota-chart-line ${colour}`, d }));
        }
        for (const p of series.points) {
          const dot = svg('circle', {
            class: `istota-chart-dot ${colour}`,
            cx: xAt(p.x).toFixed(1),
            cy: yAt(p.y).toFixed(1),
            r: 4,
          });
          const title = svg('title');
          title.textContent = name(p);
          dot.append(title);
          root.append(dot);
        }
      });

      const nodes = /** @type {Node[]} */ ([root]);
      if (data.series.length > 1) {
        const legend = document.createElement('ul');
        legend.className = 'istota-chart-legend';
        data.series.forEach((series, index) => {
          const item = document.createElement('li');
          const swatch = document.createElement('span');
          swatch.className = `istota-chart-swatch istota-series-${index % SERIES_COLOURS}`;
          item.append(swatch, document.createTextNode(series.name));
          legend.append(item);
        });
        nodes.push(legend);
      }
      this.replaceChildren(...nodes);
    }
  }

  /**
   * A bar from the baseline, its data end rounded and its base square.
   * @param {number} x
   * @param {number} base
   * @param {number} end
   * @param {number} width
   */
  function barPath(x, base, end, width) {
    const length = Math.abs(base - end);
    const radius = Math.min(4, width / 2, length);
    const up = end < base;
    const tip = up ? end + radius : end - radius;
    const sweep = up ? 1 : 0;
    const out = up ? -radius : radius;
    return [
      `M${x},${base}`,
      `L${x},${tip}`,
      `A${radius},${radius} 0 0 ${sweep} ${x + radius},${tip + out}`,
      `L${x + width - radius},${tip + out}`,
      `A${radius},${radius} 0 0 ${sweep} ${x + width},${tip}`,
      `L${x + width},${base}`,
      'Z',
    ].join(' ');
  }

  /** @type {Array<[string, CustomElementConstructor]>} */
  const ELEMENTS = [
    ['istota-button', IstotaButton],
    ['istota-list', IstotaList],
    ['istota-table', IstotaTable],
    ['istota-form', IstotaForm],
    ['istota-dialog', IstotaDialog],
    ['istota-chart', IstotaChart],
  ];
  for (const [name, element] of ELEMENTS) {
    if (!customElements.get(name)) customElements.define(name, element);
  }

  /** @type {any} */
  const scope = globalThis;
  // Added to the client library's object rather than copied, so `istota.env`
  // stays its live getter.
  if (scope.istota && typeof scope.istota === 'object') scope.istota.dialog = dialog;
  else scope.istota = { dialog };
})();
