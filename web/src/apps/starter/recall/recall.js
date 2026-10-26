// Recall's page: one card at a time from today's queue, its answer, a grade
// that sm2.js turns into the next review, and on a miss the paragraph of the
// note the card came from, read through `istota.files.readShared`. Review
// state is written to `app.recall` as each answer is given.
(() => {
  const istota = /** @type {any} */ (globalThis).istota;
  const SM2 = /** @type {any} */ (globalThis).SM2;
  const Model = /** @type {any} */ (globalThis).RecallModel;
  const PAGE = 1000;
  // The folder the manifest asks for. The user may grant another folder in its
  // place; the bridge reads whichever was granted under this capability's name.
  const FOLDER = 'notes';
  const GRADES = [
    { quality: 1, label: 'Again', variant: 'danger' },
    { quality: 3, label: 'Hard', variant: 'secondary' },
    { quality: 4, label: 'Good', variant: 'primary' },
    { quality: 5, label: 'Easy', variant: 'secondary' },
  ];

  /** @param {string} id */
  const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  const statusEl = byId('status');
  const emptyEl = byId('empty');
  const doneEl = byId('done');
  const progressEl = byId('progress');
  const questionEl = byId('question');
  const answerEl = byId('answer');
  const gradesEl = byId('grades');
  const sourcePathEl = byId('source-path');
  const sourceTextEl = byId('source-text');
  const sourceNoteEl = byId('source-note');
  const cardsEl = byId('cards');

  /** @type {RecallCard[]} */
  let cards = [];
  /** @type {Record<string, RecallReview>} */
  let reviews = {};
  /** @type {RecallCard[]} today's cards still to answer, in order */
  let session = [];
  /** @type {'question' | 'answer' | 'source'} */
  let phase = 'question';
  let loaded = false;
  let dialogOpen = false;
  // Bumped whenever the card on screen changes, so a slow read for the last
  // card cannot draw its paragraph under the next one.
  let sourceToken = 0;

  function now() {
    return new Date().toISOString();
  }

  /** @param {number} n */
  function pad(n) {
    return String(n).padStart(2, '0');
  }

  /** The user's own calendar day, which is what "due today" means to them. */
  function today() {
    const date = new Date();
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  /** @param {string} message */
  function say(message) {
    statusEl.textContent = message;
  }

  /** @param {unknown} error */
  function messageOf(error) {
    const err = /** @type {{message?: string}} */ (error || {});
    return err.message || 'no answer';
  }

  /** @param {unknown} error */
  function failed(error) {
    say(`Could not save: ${messageOf(error)}. Refresh to see what is stored.`);
  }

  /**
   * @param {string} key
   * @param {unknown} value
   */
  async function write(key, value) {
    try {
      await istota.kv.set(key, value);
    } catch (error) {
      failed(error);
    }
  }

  /** @param {string} key */
  async function remove(key) {
    try {
      await istota.kv.delete(key);
    } catch (error) {
      failed(error);
    }
  }

  async function load() {
    say('');
    try {
      const items = [];
      for (let offset = 0; ;) {
        const page = await istota.kv.list({ offset, limit: PAGE });
        items.push(...page.items);
        offset += page.items.length;
        if (!page.items.length || offset >= page.total) break;
      }
      const stored = Model.fromStorage(items);
      cards = stored.cards;
      reviews = stored.reviews;
      session = Model.queue(cards, reviews, today());
      phase = 'question';
      sourceToken += 1;
      loaded = true;
    } catch (error) {
      say(`Could not load your cards: ${messageOf(error)}.`);
    }
    render();
  }

  // -- drawing ---------------------------------------------------------------

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

  /**
   * @param {string} text
   * @param {string} variant
   * @param {() => void} onClick
   */
  function kitButton(text, variant, onClick) {
    const button = el('istota-button', '', text);
    button.setAttribute('variant', variant);
    button.addEventListener('click', onClick);
    return button;
  }

  /** @param {string | null} day */
  function dayLabel(day) {
    if (!day) return 'no date';
    const [year, month, date] = day.split('-').map(Number);
    return new Date(year, month - 1, date).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  }

  /** @param {number} days */
  function daysLabel(days) {
    return days === 1 ? '1 day' : `${days} days`;
  }

  /** @param {RecallCard} card */
  function reviewOf(card) {
    return reviews[card.id] || SM2.newReview();
  }

  function renderReview() {
    const card = session[0];
    byId('review-section').hidden = !card;
    doneEl.hidden = !loaded || !cards.length || Boolean(card);
    if (!card) {
      const next = Model.nextDue(cards, reviews, today());
      doneEl.textContent = next
        ? `Nothing left to review today. The next card is due ${dayLabel(next)}.`
        : 'Nothing left to review today.';
      return;
    }
    const left = session.length;
    progressEl.textContent = `${left} card${left === 1 ? '' : 's'} to review today.`;
    questionEl.textContent = card.q;
    answerEl.textContent = card.a || 'This card has no answer written.';
    answerEl.hidden = phase === 'question';
    byId('reveal-row').hidden = phase !== 'question';
    byId('grade-row').hidden = phase !== 'answer';
    byId('source-block').hidden = phase !== 'source';
    if (phase === 'answer') {
      const review = reviewOf(card);
      gradesEl.replaceChildren(
        ...GRADES.map((g) =>
          kitButton(
            `${g.label} · ${daysLabel(SM2.nextInterval(review, g.quality))}`,
            g.variant,
            () => answer(g.quality),
          ),
        ),
      );
    }
  }

  /** @param {RecallCard} card */
  function cardMeta(card) {
    const review = reviews[card.id];
    if (!review || !review.due) return 'New';
    const lapses = review.lapses ? ` · missed ${review.lapses}×` : '';
    return `Due ${dayLabel(review.due)}${lapses}`;
  }

  function renderCards() {
    byId('cards-section').hidden = cards.length === 0;
    cardsEl.replaceChildren(
      ...cards.map((card) => {
        const row = el('div', 'recall-row');
        row.dataset.id = card.id;
        const main = el('div', 'recall-list-main');
        main.append(
          el('span', 'recall-list-text', card.q),
          el('span', 'recall-list-meta', cardMeta(card)),
        );
        const menu = /** @type {HTMLButtonElement} */ (el('button', 'recall-icon-button', '⋯'));
        menu.type = 'button';
        menu.setAttribute('aria-label', `More for ${card.q}`);
        menu.title = 'More';
        menu.addEventListener('click', () => cardMenu(card.id));
        row.append(main, menu);
        return row;
      }),
    );
  }

  function render() {
    emptyEl.hidden = !loaded || cards.length > 0;
    renderReview();
    renderCards();
  }

  // -- reviewing -------------------------------------------------------------

  function reveal() {
    if (!session.length || phase !== 'question') return;
    phase = 'answer';
    render();
  }

  /** @param {number} quality */
  function answer(quality) {
    const card = session[0];
    if (!card || phase !== 'answer') return;
    const review = SM2.grade(reviewOf(card), quality, today());
    reviews = { ...reviews, [card.id]: review };
    write(Model.reviewKey(card.id), review);
    if (SM2.isMiss(quality)) {
      phase = 'source';
      render();
      showSource(card);
    } else {
      advance();
    }
  }

  function advance() {
    session = session.slice(1);
    phase = 'question';
    sourceToken += 1;
    render();
  }

  /**
   * @param {string} path
   * @param {string} [note]
   * @param {string | null} [passage]
   */
  function drawSource(path, note = '', passage = null) {
    sourcePathEl.textContent = path;
    sourceTextEl.hidden = passage === null;
    sourceTextEl.textContent = passage || '';
    sourceNoteEl.hidden = !note;
    sourceNoteEl.textContent = note;
  }

  /**
   * The card's paragraph, read from the folder Recall was granted.
   * @param {RecallCard} card
   */
  async function showSource(card) {
    const token = (sourceToken += 1);
    const candidates = Model.sourceCandidates(card.source_path);
    if (!card.source_path) {
      drawSource('', 'This card does not say which note it came from.');
      return;
    }
    if (!candidates.length) {
      drawSource(card.source_path, 'That is not a path Recall can read.');
      return;
    }
    drawSource(card.source_path, 'Reading the note…');
    for (const path of candidates) {
      let text;
      try {
        text = await istota.files.readShared(FOLDER, path);
      } catch (error) {
        if (token !== sourceToken) return;
        const code = /** @type {{code?: string}} */ (error || {}).code;
        if (code === 'not_found') continue;
        if (code === 'not_granted') {
          drawSource(
            card.source_path,
            'Recall has not been given a folder of notes to read. Allow it from Permissions in the app’s menu, or on its next open.',
          );
        } else {
          drawSource(card.source_path, `Could not read the note: ${messageOf(error)}.`);
        }
        return;
      }
      if (token !== sourceToken) return;
      const passage = Model.findPassage(String(text), card.source_anchor);
      if (passage !== null) drawSource(card.source_path, '', passage);
      else if (!card.source_anchor) {
        drawSource(
          card.source_path,
          'The card does not say which paragraph; open the note to read it.',
        );
      } else {
        drawSource(card.source_path, 'That paragraph is no longer in the note.');
      }
      return;
    }
    if (token === sourceToken) {
      drawSource(card.source_path, 'That note is not in the folder Recall may read.');
    }
  }

  // -- the menu and the editor -----------------------------------------------

  /**
   * An istota-dialog whose content `build` fills; `build` gets `close`.
   * @param {string} label
   * @param {(close: () => void) => Node[]} build
   */
  function openPanel(label, build) {
    const dialog = /** @type {any} */ (el('istota-dialog'));
    dialog.setAttribute('label', label);
    const close = () => dialog.close(null);
    dialog.append(el('h2', 'istota-dialog-title', label), ...build(close));
    dialog.addEventListener('close', () => {
      dialog.remove();
      dialogOpen = false;
    });
    dialogOpen = true;
    document.body.append(dialog);
    dialog.show();
  }

  /** @param {string} id */
  function cardMenu(id) {
    const card = cards.find((c) => c.id === id);
    if (!card) return;
    openPanel(card.q, (close) => {
      /** @param {() => void} action */
      const then = (action) => () => {
        close();
        action();
      };
      const row = el('div', 'recall-row');
      if (reviews[id])
        row.append(
          kitButton(
            'Start over',
            'secondary',
            then(() => resetCard(id)),
          ),
        );
      row.append(
        kitButton(
          'Delete',
          'danger',
          then(() => deleteCard(id)),
        ),
      );
      return [el('p', 'recall-note', cardMeta(card)), row, kitButton('Close', 'primary', close)];
    });
  }

  /** @param {string} id */
  function resetCard(id) {
    const { [id]: _gone, ...rest } = reviews;
    reviews = rest;
    remove(Model.reviewKey(id));
    render();
  }

  /** @param {string} id */
  async function deleteCard(id) {
    const card = cards.find((c) => c.id === id);
    if (!card) return;
    const sure = await istota.dialog.confirm(`Delete the card “${card.q}”?`, {
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!sure) return;
    const wasCurrent = session[0] && session[0].id === id;
    cards = cards.filter((c) => c.id !== id);
    session = session.filter((c) => c.id !== id);
    const { [id]: _gone, ...rest } = reviews;
    reviews = rest;
    if (wasCurrent) {
      phase = 'question';
      sourceToken += 1;
    }
    render();
    await remove(Model.cardKey(id));
    await remove(Model.reviewKey(id));
  }

  /**
   * The add form. It submits through istota-form's `istota-submit`, which the
   * kit raises from the button and from Enter in the question field.
   */
  function addCard() {
    openPanel('Add card', (close) => {
      const wrapper = el('istota-form');
      const form = /** @type {HTMLFormElement} */ (el('form'));
      /**
       * @param {string} label
       * @param {HTMLElement} control
       */
      const field = (label, control) => {
        const row = el('label');
        row.append(el('span', '', label), control);
        return row;
      };
      const q = /** @type {HTMLInputElement} */ (el('input'));
      q.name = 'q';
      q.required = true;
      q.placeholder = 'What should you remember?';
      const a = /** @type {HTMLTextAreaElement} */ (el('textarea'));
      a.name = 'a';
      a.rows = 3;
      wrapper.addEventListener('istota-submit', (event) => {
        const values = /** @type {CustomEvent} */ (event).detail;
        const card = Model.addCard(
          { q: String(values.q || ''), a: String(values.a || '') },
          now(),
          Model.newId(),
        );
        if (!card.q) return;
        close();
        cards = [...cards, card];
        session = [...session, card];
        render();
        write(Model.cardKey(card.id), card);
      });
      const actions = el('div', 'istota-dialog-actions');
      const save = el('istota-button', '', 'Add');
      save.setAttribute('variant', 'primary');
      save.setAttribute('type', 'submit');
      actions.append(kitButton('Cancel', 'secondary', close), save);
      form.append(field('Question', q), field('Answer', a), actions);
      wrapper.append(form);
      return [wrapper];
    });
  }

  // -- wiring ----------------------------------------------------------------

  byId('reveal').addEventListener('click', reveal);
  byId('next').addEventListener('click', advance);
  byId('add').addEventListener('click', addCard);
  byId('refresh').addEventListener('click', load);
  // The agent may have made cards, or a day turned, while the page was hidden.
  // A card half answered is left as it is.
  let visibility = istota.env.visibility;
  istota.on('env', (/** @type {{visibility: string}} */ env) => {
    const returned = visibility === 'hidden' && env.visibility === 'visible';
    visibility = env.visibility;
    if (returned && !dialogOpen && phase === 'question') load();
  });

  load();
})();
