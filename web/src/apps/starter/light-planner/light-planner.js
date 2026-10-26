// The Light planner's page: reads the user's places through
// `skill:location.places` and two weeks of their calendar through
// `calendar:read`, then draws today's sun and moon for one place and the
// golden hours to come that fall in free time. Each read may be refused
// ("Not now" on the first-open screen); the page says what it is missing
// and draws what it can.
(() => {
  const istota = /** @type {any} */ (globalThis).istota;
  const Model = /** @type {any} */ (globalThis).PlannerModel;
  const E = /** @type {any} */ (globalThis).Ephemeris;
  const DAYS = 14;
  const COMPASS = [
    'north',
    'northeast',
    'east',
    'southeast',
    'south',
    'southwest',
    'west',
    'northwest',
  ];

  /** @param {string} id */
  const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  const statusEl = byId('status');
  const emptyEl = byId('empty');
  const todaySection = byId('today-section');
  const freeSection = byId('free-section');
  const pickerEl = byId('place-picker');
  const selectEl = /** @type {HTMLSelectElement} */ (byId('place'));
  const nowEl = byId('now');
  const timesEl = byId('times');
  const chartEl = /** @type {HTMLElement & {data: unknown}} */ (byId('chart'));
  const calendarNoteEl = byId('calendar-note');
  const freeEmptyEl = byId('free-empty');
  const freeEl = byId('free');

  /** @type {PlannerPlace[]} */
  let places = [];
  /** @type {PlannerSpan[]} */
  let busy = [];
  let calendarNote = '';
  let selected = '';

  /** @param {unknown} error */
  function codeOf(error) {
    return /** @type {{code?: string}} */ (error || {}).code || 'internal';
  }

  /** @param {unknown} error */
  function messageOf(error) {
    return /** @type {{message?: string}} */ (error || {}).message || 'no answer';
  }

  /** @param {number | null} ms */
  function clock(ms) {
    if (ms === null) return '–';
    return new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }

  /** @param {{from: number, to: number}} span */
  function range(span) {
    return `${clock(span.from)}–${clock(span.to)}`;
  }

  /** @param {number} ms */
  function dayLabel(ms) {
    return new Date(ms).toLocaleDateString(undefined, {
      weekday: 'short',
      day: 'numeric',
      month: 'short',
    });
  }

  /**
   * @param {string} body
   * @param {{altitude: number, azimuth: number}} position
   */
  function where(body, position) {
    const degrees = Math.round(Math.abs(position.altitude));
    if (position.altitude < 0) return `the ${body} is ${degrees}° below the horizon`;
    const direction = COMPASS[Math.round(position.azimuth / 45) % 8];
    return `the ${body} is ${degrees}° up in the ${direction}`;
  }

  /**
   * @param {string} tag
   * @param {string} [className]
   * @param {string} [text]
   */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function readPlaces() {
    try {
      places = Model.parsePlaces(await istota.skill('location.places'));
      return '';
    } catch (error) {
      places = [];
      const code = codeOf(error);
      if (code === 'not_granted') return 'not_granted';
      if (code === 'not_found') return 'not_found';
      return `Could not read your places: ${messageOf(error)}.`;
    }
  }

  /** @param {PlannerDay[]} days */
  async function readCalendar(days) {
    busy = [];
    try {
      const answer = await istota.calendar.list({
        from: days[0].key,
        to: Model.localDays(days[days.length - 1].to, 1)[0].key,
      });
      busy = Model.busyIntervals(answer);
      calendarNote =
        answer && answer.truncated ? 'Only the first events of your calendar were read.' : '';
    } catch (error) {
      const code = codeOf(error);
      const rest = 'so every golden hour is listed, busy or not.';
      if (code === 'not_granted') {
        calendarNote = `Light planner has not been allowed to read your calendar, ${rest}`;
      } else if (code === 'not_found') {
        calendarNote = `No calendar is connected, ${rest}`;
      } else {
        calendarNote = `Could not read your calendar (${messageOf(error)}), ${rest}`;
      }
    }
  }

  async function load() {
    statusEl.textContent = '';
    const days = Model.localDays(Date.now(), DAYS);
    const [placesProblem] = await Promise.all([readPlaces(), readCalendar(days)]);
    if (placesProblem && placesProblem !== 'not_granted' && placesProblem !== 'not_found') {
      statusEl.textContent = placesProblem;
    }
    showEmpty(placesProblem);
    if (!places.some((p) => p.id === selected)) selected = places.length ? places[0].id : '';
    render(days);
  }

  /** @param {string} problem */
  function showEmpty(problem) {
    emptyEl.hidden = places.length > 0;
    if (places.length) return;
    const [title, detail] =
      problem === 'not_granted'
        ? [
            'Light planner cannot see your places.',
            'Allow it to read your places from Permissions in this app’s menu.',
          ]
        : problem === 'not_found'
          ? ['Location is off.', 'Turn Location on in Settings and save the places you go.']
          : [
              'No places yet.',
              'Save the places you go in Location, and they appear here with their sunrise, golden hour and moon.',
            ];
    byId('empty-title').textContent = title;
    byId('empty-detail').textContent = detail;
  }

  /** @param {PlannerDay[]} days */
  function render(days) {
    const place = places.find((p) => p.id === selected);
    todaySection.hidden = !place;
    freeSection.hidden = !place;
    if (!place) return;

    selectEl.replaceChildren(
      ...places.map((p) => {
        const option = /** @type {HTMLOptionElement} */ (el('option', '', p.name));
        option.value = p.id;
        option.selected = p.id === selected;
        return option;
      }),
    );
    pickerEl.hidden = places.length < 2;
    byId('today-heading').textContent = `Today at ${place.name}`;
    renderToday(days[0], place);
    renderFree(days);
  }

  /**
   * @param {PlannerDay} day
   * @param {PlannerPlace} place
   */
  function renderToday(day, place) {
    const now = Date.now();
    nowEl.textContent = `Now ${where('sun', E.sun(now, place.lat, place.lon))}, and ${where(
      'moon',
      E.moon(now, place.lat, place.lon),
    )}.`;
    const details = Model.dayDetails(day, place);
    /** @param {'golden' | 'blue'} kind @param {'morning' | 'evening'} part */
    const windows = (kind, part) => {
      const found = details.windows.filter(
        (/** @type {PlannerWindow} */ w) => w.kind === kind && w.part === part,
      );
      return found.length ? found.map(range).join(', ') : 'none today';
    };
    const lit = Math.round(details.illumination * 100);
    /** @type {Array<[string, string]>} */
    const rows = [
      ['Morning blue hour', windows('blue', 'morning')],
      ['Morning golden hour', windows('golden', 'morning')],
      ['Sunrise', details.sunrise === null ? 'none today' : clock(details.sunrise)],
      ['Solar noon', clock(details.noon)],
      ['Sunset', details.sunset === null ? 'none today' : clock(details.sunset)],
      ['Evening golden hour', windows('golden', 'evening')],
      ['Evening blue hour', windows('blue', 'evening')],
      ['Moonrise', details.moonrise === null ? 'none today' : clock(details.moonrise)],
      ['Moonset', details.moonset === null ? 'none today' : clock(details.moonset)],
      ['Moon', `${lit}% lit, ${details.waxing ? 'waxing' : 'waning'}`],
    ];
    timesEl.replaceChildren(
      ...rows.map(([what, when]) => {
        const tr = el('tr');
        tr.append(el('td', '', what), el('td', '', when));
        return tr;
      }),
    );
    const series = Model.altitudeSeries(day, place, 20);
    /** @param {number | string} hours */
    const hourLabel = (hours) => clock(day.from + Number(hours) * 3_600_000);
    chartEl.data = {
      series: [
        { name: 'Sun', points: series.sun },
        { name: 'Moon', points: series.moon },
      ],
      xFormat: hourLabel,
      yFormat: (/** @type {number} */ y) => `${Math.round(y)}°`,
    };
  }

  /** @param {PlannerDay[]} days */
  function renderFree(days) {
    calendarNoteEl.textContent = calendarNote;
    calendarNoteEl.hidden = !calendarNote;
    const found = Model.freeGoldenHours(days, places, busy, Date.now());
    freeEmptyEl.hidden = found.length > 0;
    freeEmptyEl.textContent = `No free golden hour in the next ${DAYS} days.`;
    freeEl.hidden = found.length === 0;
    freeEl.replaceChildren(
      ...found.map((/** @type {PlannerOpportunity} */ o) => {
        const row = el('div', 'planner-row');
        const main = el('div', 'planner-row-main');
        main.append(
          el('span', 'planner-row-text', `${dayLabel(o.from)}, ${o.part}`),
          el('span', 'planner-row-meta', `${o.place.name} · golden hour ${range(o)}`),
        );
        row.append(main);
        const whole = o.free.length === 1 && o.free[0].from === o.from && o.free[0].to === o.to;
        if (!whole)
          row.append(el('span', 'planner-row-free', `free ${o.free.map(range).join(', ')}`));
        return row;
      }),
    );
  }

  selectEl.addEventListener('change', () => {
    selected = selectEl.value;
    render(Model.localDays(Date.now(), DAYS));
  });
  byId('refresh').addEventListener('click', load);
  // Back after a while: the day may have turned, and the calendar changed.
  let visibility = istota.env.visibility;
  istota.on('env', (/** @type {{visibility: string}} */ env) => {
    const returned = visibility === 'hidden' && env.visibility === 'visible';
    visibility = env.visibility;
    if (returned) load();
  });

  load();
})();
