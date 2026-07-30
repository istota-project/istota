# Web UI design language

Rules for building and changing the SvelteKit frontend in `web/`. Read this before adding a page, a component, or a color.

Root `AGENTS.md` ("Web UI" section) carries the _rationale_ — why `HintPopover` is a Popover and not a Tooltip, why composer wrap detection measures at the single-row width, why the money table shell exists. This file carries the _rules and the inventory_. When the two disagree, the source files win and both docs are stale.

## Before you write anything

1. `cat src/lib/components/ui/index.ts` — the full primitive inventory. If a primitive fits, use it; do not hand-roll a second one.
2. `grep -n '^\s*--' src/app.css` — the token roster. Every color you write must be an existing token, or a new token pair added to **both** theme blocks in `app.css`.
3. Find the nearest sibling page and read it. A new money page copies `routes/money/transactions`; a new module settings page copies `routes/feeds/settings`. Match its structure before inventing one.
4. `npm run lint:design` before you commit. It fails on new hardcoded colors and new per-page theme overrides.
5. `npm run format` — prettier, 2-space, single quotes, 100 cols. Never tabs.

## Component inventory

Import everything from the barrel: `import { Button, Modal } from '$lib/components/ui';`

### Shell and navigation

| Component       | Use for                                                          | Notes                                                                                                                                                                                |
| --------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `AppShell`      | The page frame every module layout is built on                   | Slots: `header`, `sidebar`, `children`, `extras`. `getShellScrollRoot()` is the scroll container — use it instead of `window` for scroll listeners                                   |
| `ShellHeader`   | The app bar inside `AppShell`                                    | Slots: `leading`, `nav`, `tools`. `SidebarToggle` belongs in `leading`, before the title. `onTitleClick` + `titleActionLabel` make the title a second hit target for the same toggle |
| `Sidebar`       | List/detail sidebars (archive lists, room lists, category trees) | Below 768px it becomes an overlay drawer; pair with `SidebarToggle`                                                                                                                  |
| `SidebarToggle` | The drawer opener                                                | Layout box is `1.5rem` tall to match the title's line box, with an out-of-flow `::before` touch target. Do not give it its own height                                                |
| `HeaderNav`     | Horizontal section nav in `ShellHeader`'s `nav` slot             | Takes `NavItem[]`                                                                                                                                                                    |
| `NavLink`       | A single nav anchor with an active state                         |                                                                                                                                                                                      |
| `CategoryGroup` | Collapsible grouped list sections                                | `onSelect` turns the label into a filter button while the caret still collapses                                                                                                      |

### Controls

| Component           | Use for                                  | Notes                                                                                                                                                    |
| ------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Button`            | Every button                             | Variants: `primary`, `secondary`, `ghost`, `pill`, `subtle`, `danger`, `danger-icon`. Sizes `sm`/`md`. Never style a bare `<button>` as a primary action |
| `Select`            | Every dropdown                           | bits-ui backed. `fullWidth` for settings forms so it matches text inputs                                                                                 |
| `AutocompleteInput` | Text input with a suggestion pool        | `monospace` for paths/tokens/ids, `onCommit` (blur) is the validation hook                                                                               |
| `Chip`              | Toggleable filter chips and small labels | `checked` for on/off state                                                                                                                               |
| `KebabMenu`         | Per-row and per-card actions             | Takes `KebabItem[]`. One kebab per row — do not line up bare icon buttons instead                                                                        |

### Overlays and feedback

| Component       | Use for                                                             | Notes                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Modal`         | Any dialog                                                          | bits-ui `Dialog`. Never hand-roll a `.modal-backdrop`                                                                                                                         |
| `ConfirmDialog` | Every destructive, irreversible or session-ending action            | Imperative `title` with no "?", full "Are you sure…" `message`. `confirmVariant="danger"`. `challenge` adds a type-the-name gate for hard deletes. **Never `window.confirm`** |
| `NoticeBanner`  | A single-line collapsible notice with a variant-colored left border | Variants follow the status scale                                                                                                                                              |
| `HintPopover`   | Optional guidance behind a "?"                                      | See the `SettingsField` rule below before reaching for it directly                                                                                                            |

### Settings pages

Every module settings page is `SettingsLayout` → `SettingsCard` → `SettingsField`. Do not build a settings page out of raw markup.

| Component        | Use for                                                                                                                                                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SettingsLayout` | The page frame: `title`, `description`, `loading`, `error`, `info`, `headerActions`. `title` is optional — omit it when the `ShellHeader` above already names the page, and the header row collapses |
| `SettingsCard`   | A titled group of fields, with its own `actions` and `status`. `actions` is for actions belonging to _that card_ ("Refresh all now") — not the page's save, see below                                |
| `HeaderSave`     | The app bar's slot for a page's single save. Pair with `useSettingsSave`                                                                                                                             |
| `SettingsField`  | One labelled control. `wide` for full-width, `checkbox` for checkbox rows                                                                                                                            |
| `SecretField`    | A write-only credential input (bullet-masked, `configured` shows it is set without echoing it)                                                                                                       |
| `ServiceCard`    | A connected-service card on `/settings` or a module settings page                                                                                                                                    |

**A settings page has exactly one Save, and it lives in the app bar.** Call `useSettingsSave(() => ({ dirty, saving, save }))` during component init and render `<HeaderSave />` in the `ShellHeader`'s `tools` snippet — in the page itself for `/settings`, in the module `+layout.svelte` for a module settings page, where it renders nothing on every other page of the section. Return `null` from the callback to withdraw the contribution (a page whose module is switched off has nothing to save). Do not put a page-level Save on a `SettingsCard`.

**Contributors aggregate; they do not replace each other.** `/settings` edits the profile _and_ holds a `ServiceCard` per connected service, each writing through a different endpoint — so `ServiceCard` registers itself and has no button of its own, and one header Save covers the lot, asking only the parts actually holding edits. This is why `/money/settings` and `/location/settings` get a working header Save from their `ServiceCard`s alone, with no page-level registration. Before this, `/settings` carried three copies of the same-looking button (Identity, Preferences, Karakeep) and a fourth card would have added a fourth.

Two placement rules the component depends on. `<HeaderSave />` goes **before** the section's settings cog, so the cog keeps the bar's right edge and does not move as the save comes and goes. And the "Unsaved changes" badge is always in the layout, merely `visibility: hidden` when clean — `.header-tools` is right-aligned, so a badge that appeared as you type would shove everything to its left sideways mid-edit.

Still their own buttons, because they are per-record forms rather than the page's state: "save this source" / "save this shared block" on briefings settings, and the entity/service modals on money settings. Clearing a stored secret also stays immediate and separately confirmed — a deletion is not an edit awaiting a Save.

**`SettingsField` supplementary text is three-slotted by whether the user must see it.** `hint` is optional guidance and renders in a `HintPopover` behind a "?" beside the label. `warning` and `error` render inline, because a hover popover is discoverable, not seen. A data-integrity notice ("this client is never invoiced automatically") is a `warning`, not a `hint` — do not put anything the user needs to act on behind hover.

## Color

Every recurring meaning-bearing color is a token in `src/app.css`, defined in **both** the `:root` and `:root[data-theme='light']` blocks. Four roles:

- `--status-{danger,warn,success,info}-{fg,bg}` — severity. `-fg` for a bare status label, `-bg` for a filled chip's fill (pairs with the same `-fg`). Errors, warnings, status chips, badges, danger buttons, delete links.
- `--accent-blue` — the _interactive_ blue: primary buttons, active tabs, focus rings, in-app cross-references. Distinct from `--status-info-fg`, which marks a severity rather than something actionable.
- `--accent-amber` — the bot's identity accent, and the "starred" color.
- `--money-{income,expense}` — signed-amount direction. Deliberately off the status scale: an expense is not an error.

`--status-critical-{bg,fg}` is a fifth severity above danger, for a value that is not merely out of range but clinically critical (the bloodwork `flag-C` cell). Unlike the four above it, it is a solid saturated fill rather than a tint, so its `-fg` is the text laid _on_ `-bg`.

Supporting scales: `--surface-{base,card,raised,badge,overlay,reading}`, `--text-{primary,secondary,muted,dim,reading}`, `--border-{default,subtle,hover}`, `--radius-{card,pill}`, `--text-{2xs,xs,sm,base}`.

- `--surface-overlay` — chrome floating _over_ content: a feed card's title overlay, the fixed status badge, an autocomplete popover, jump-to-latest.
- `--surface-reading` / `--text-reading` — a long-form reading pane (the chat transcript, the briefings reader) and its softened body text. The surface is card-colored in dark but pure white in light.
- `--border-hover` — one step stronger than `--border-default`, for the hover state of an interactive card or link-tile. Use it rather than a literal, or the hover affordance silently vanishes on white.

For chrome that sits on a surface the theme does _not_ control: `--on-accent-fg` (text on a filled accent), `--on-scrim-fg` (text on a scrim that stays dark in both themes), `--scrim-pill-{bg,fg}` (a pill over media, which flips scrim direction), `--shimmer-tint`, `--shadow-overlay`. The first two are deliberately one value in both themes, like `--status-dot-*`.

**The anti-pattern:** hardcode a dark-theme hex, then hand-write a `:global(:root[data-theme='light'])` rule beside it. Doing that per page is how the app accumulated roughly ten different reds for one meaning, and how several chips ended up with no light rule at all and rendered dark fills on white. Reach for a token. If a genuinely new meaning appears, add a token pair to both theme blocks — never a local override.

Legitimately exempt, and enforced by an allow comment rather than a habit:

- **Categorical palettes**, where the hue encodes a _kind_ rather than a severity — health encounter-type badges, the admin `SOURCE_COLOR` constants, the location activity/speed palettes. Collapsing these onto the status scale would erase the distinction they carry.
- **Data visualization** — Chart.js and MapLibre are handed a config object rather than reading the cascade, so they cannot resolve `var()` at all. Chart _chrome_ (grid, ticks, tooltip) is centralized in `$lib/chartTheme`, which is theme-aware; only series colors belong on the page. See the `dataviz` skill before picking them.
- **Third-party DOM** — `!important` rules restyling vendor markup (`.maplibregl-*`) are overriding another stylesheet, not expressing our design language.
- **Fixed-surface chrome** — lightbox, avatar fills, media letterboxes.

Mark an exemption in whichever form fits the shape of what you are exempting. Every form takes a reason after the colon — an unexplained exemption is indistinguishable from an oversight.

| Form                                             | Covers                                                | Reach for it when                                                                                             |
| ------------------------------------------------ | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `/* design-lint-allow: reason */`                | The next line carrying code (or its own line, inline) | One declaration. The comment may span several lines; blank and comment lines between it and the code are fine |
| `/* design-lint-allow-begin: reason */` … `-end` | Everything between the markers                        | A contiguous block — a categorical palette, a chart config                                                    |
| `<!-- design-lint-allow-file: reason -->`        | The whole file                                        | The file exists to hold literals. Blankets anything added later, so prefer a region                           |

Not linted at all: `*.test.ts`, which asserts on the very literals the rules forbid; hexes appearing inside comments, which are documentation rather than declarations; and HTML numeric entities like `&#9654;`, which only look like hex.

## Sizing and layout

- Type and spacing are `rem`; the root font-size percentage is the text-scale preference (`small` unscaled / `medium` 110% default / `large` 120%). Scaling the root is deliberate — type and the space around it grow together. Do not scale only the type tokens.
- A `<button>` does not inherit font. If you size a control in `em`, set `font: inherit` on it, or the `em` resolves against the UA's ~13px and the control stops tracking the text-scale preference.
- Fixed `px` is correct for borders and SVG chart labels, which should not scale.
- A row of small icon controls needs its touch targets reasoned about **together**. Reach the ~44px minimum with an out-of-flow `::before` overlay so the bar keeps its height (`SidebarToggle`, `.nav-icon-btn`), and widen the row's gap to match — overlays that overlap hand the tap in the seam to whichever wins the stacking order, which is the accidental-tap bug rather than a fix for it.
- A status chip needs a `min-width`, or its variable width shifts every column after it, row by row.
- A toolbar holding only a result count needs a `min-height` so it matches one holding filters.

## Page skeletons

**Module page** (`routes/<module>/+layout.svelte`): `AppShell` with a `ShellHeader` header snippet (`SidebarToggle` in `leading`, `HeaderNav` in `nav`, a `Cog` link to `<module>/settings` in `tools`) and a `Sidebar` when the module has a list/detail split. `routes/briefings/+layout.svelte` is the cleanest example.

**Module settings page** (`routes/<module>/settings/+page.svelte`): `SettingsLayout` wrapping `SettingsCard`s. Call `getModuleServices(<module>)` first and render the "Module disabled" banner when `module_enabled` is false, instead of the configuration UI.

**Every top-level route renders an `AppShell` and is listed in the `app-content-fill` class on `routes/+layout.svelte`'s `<main>`.** The two are one decision, not two: without the shell nothing pins the app nav, and without the class the document scrolls and carries the nav off the top of the screen — which is only noticeable on a phone or in the iOS app, where the nav is the only way back to another section. A single-page section (`/chat`, `/admin`, `/settings`) puts the shell in its `+page.svelte`; a section with sub-routes puts it in `+layout.svelte`. `ShellHeader`'s `title` is the page's name in the fixed bar.

**Money list page**: the record-table shell is defined once as globals in `routes/money/+layout.svelte` — `.money-toolbar`, `.money-notice-bar`, `.money-table`, `.money-table-header`, `.money-table-row`, `.money-sortable`, `.money-status`, `.money-amount`, `.money-kebab-spacer`, `.money-table-empty`. A page styles only its own columns (widths, alignment) and inherits everything else. The shell fixes the inline edge at `0.75rem` for every element on the page; do not set your own.

## Client-local preferences

Theme and text scale are localStorage-persisted per browser, not profile fields — they must apply before first paint, and profile data arrives after it. Both follow one shape: a `writable` seeded from `loadSetting`, a normalizer folding any unrecognized value onto the default, an `apply*` setting an attribute on `<html>`, and a matching branch in the single blocking `<script>` in `app.html`. Add to that script; never add a second one. Controls for these live in the **Appearance** card on `/settings`, which sits outside the `{#if profile}` block and applies on change — keep client-local controls out of the profile cards, whose fields need the shared Save button.

## Checks

```bash
npm run lint:design    # hardcoded colors + per-page theme overrides
npm run check          # svelte-check
npm run test           # vitest
npm run format         # prettier (run before committing)
```

`lint:design` compares against `scripts/design-lint-baseline.json`, which records permitted violations per file. **The baseline is currently empty — the tree is clean.** Keep it that way: fix the violation, or mark it with an allow comment and a reason. Adding a baseline entry is the last resort and needs one too.

`npm run lint:design -- --list [rule]` dumps every violation regardless of baseline — the triage view. `-- --update-baseline` regenerates.

A theme override that merely re-points at a token (`:global(:root[data-theme='light']) .card:hover { border-color: var(--border-default); }`) is still a violation, and usually a sign the value wants a token pair of its own. Several of those were also silently inert — restating in light exactly what the resting state already used, so the hover did nothing on white.
