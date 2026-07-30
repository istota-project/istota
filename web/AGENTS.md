# Web UI design language

Rules for building and changing the SvelteKit frontend in `web/`. Read this before adding a page, a component, or a color.

Root `AGENTS.md` ("Web UI" section) carries the _rationale_ — why `HintPopover` is a Popover and not a Tooltip, why composer wrap detection measures at the single-row width, why the money table shell exists. This file carries the _rules and the inventory_. When the two disagree, the source files win and both docs are stale.

## Before you write anything

1. `cat src/lib/components/ui/index.ts` — the full primitive inventory. If a primitive fits, use it; do not hand-roll a second one.
2. `grep -n '^\s*--' src/app.css` — the token roster. Every color you write must be an existing token, or a new token pair added to **both** theme blocks in `app.css`.
3. `ls src/lib/platform/` — the native-shell facade. Anything that behaves differently inside the iOS app goes through it and nowhere else.
4. Find the nearest sibling page and read it. A new money page copies `routes/money/transactions`; a new module settings page copies `routes/feeds/settings`. Match its structure before inventing one.
5. `npm run lint:design` before you commit. It fails on new hardcoded colors and new per-page theme overrides.
6. `npm run format` — prettier, 2-space, single quotes, 100 cols. Never tabs.

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

| Component           | Use for                                  | Notes                                                                                                                                                                          |
| ------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Button`            | Every button                             | Variants: `primary`, `secondary`, `ghost`, `pill`, `subtle`, `danger`, `danger-icon`. Sizes `sm`/`md`. Never style a bare `<button>` as a primary action                       |
| `Select`            | Every dropdown                           | bits-ui backed. `fullWidth` for settings forms so it matches text inputs                                                                                                       |
| `Input`             | Any text/number/date input               | Rest props pass through, so `min`/`step`/`placeholder`/`autocomplete` work. `monospace` for paths/tokens/ids, `invalid` sets `aria-invalid`                                    |
| `TextArea`          | Multi-line input                         | Same contract, `rows` instead of `type`. Vertical resize only                                                                                                                  |
| `Field`             | A labelled form row                      | Label + control + `hint`/`warning`/`error`. `labelled={false}` renders a `<div>` — required when the slot holds a button, or several controls. `SettingsField` delegates to it |
| `AutocompleteInput` | Text input with a suggestion pool        | `monospace` for paths/tokens/ids, `onCommit` (blur) is the validation hook                                                                                                     |
| `Chip`              | Toggleable filter chips and small labels | `checked` for on/off state                                                                                                                                                     |
| `Badge`             | A small uppercase status pill            | Variants follow the status scale plus `partial` (part-done, off the severity ramp). A categorical badge sets `--badge-bg`/`--badge-fg` on the element                          |
| `IconButton`        | An icon-only button                      | `label` is required and becomes the `aria-label`. Sizes `sm`/`md`/`round`; `danger` for a destructive action                                                                   |
| `KebabMenu`         | Per-row and per-card actions             | Takes `KebabItem[]`. One kebab per row — do not line up bare icon buttons instead                                                                                              |

### Overlays and feedback

| Component       | Use for                                                             | Notes                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Modal`         | Any dialog                                                          | bits-ui `Dialog`. Never hand-roll a `.modal-backdrop`                                                                                                                         |
| `ConfirmDialog` | Every destructive, irreversible or session-ending action            | Imperative `title` with no "?", full "Are you sure…" `message`. `confirmVariant="danger"`. `challenge` adds a type-the-name gate for hard deletes. **Never `window.confirm`** |
| `NoticeBanner`  | A single-line collapsible notice with a variant-colored left border | `info` / `warn` / `danger` — a subset of the status scale, with no `success` (a banner that persists is not how a success is reported; use `notify()`)                        |
| `HintPopover`   | Optional guidance behind a "?"                                      | See the `SettingsField` rule below before reaching for it directly                                                                                                            |
| `NoticeDrawer`  | Nothing — `AppShell` mounts it. Call `notify()` instead             | The one transient-feedback surface, and the one component deliberately absent from the barrel: a second mount is two live regions for one notice. See the rule below          |

**Transient feedback goes through `notify()`, and only if it is out-of-band.** `import { notify, notifyError, notifySuccess, notifyWarning } from '$lib/stores/notices'` and call it from anywhere — a store, a component, an event handler. It renders in a band that slides down from under the section header, overlaying content without reflowing it. `AppShell` mounts the host, so no page renders anything.

The severities are `info` / `success` / `warning` / `error`. All but `error` auto-dismiss; an error stays until dismissed, as does any notice carrying an `action` (`{ label, run }`) — expiring a decision out from under a reaching finger removes the only way to take it. Concurrent notices queue, because the drawer is one slot; a pinned one hands the slot over after 30s once something is waiting behind it, so an unanswered error can't silence the channel for the life of the tab.

Repeats of the same message coalesce into a count rather than stacking. Pass `key` to coalesce notices whose wording differs but which are the same event — a progress line, then its outcome. On that path a repeat overrides only what it states: omit `severity` or `action` and the existing one is kept, so a progress update can't silently downgrade an error or delete the Retry button the first call offered. Keys are one global namespace, so prefix them by feature (`chat:room-delete`, `feeds:star`) rather than using a bare `sync`.

Notices clear on navigation — a notice comments on the surface that raised it. The corollary: raise one _after_ a `goto`, not before.

**Copying goes through `copyText`.** `import { copyText } from '$lib/clipboard'` — it writes the text, raises the confirmation, and handles the two failures that otherwise pass for success: `navigator.clipboard` is absent outside a secure context, and a write can be refused. All copies share one notice key, so copying several blocks in a row counts up rather than queueing identical banners. Do not call `navigator.clipboard.writeText` directly.

In the chat transcript, copy hangs off each message _block_ rather than the turn — a reply is an ordered list of prose groups separated by activity chips, and a tool trace is never something you want on the clipboard. What is copied is the block's markdown source, not the rendered html, which is also why fenced code carries no copy button of its own: the source keeps its fences, so copying the block already yields the code ready to paste.

**Out-of-band means it has no natural home on the page.** A background sync failed, a link was copied, an optimistic update rolled back silently. In-band state stays where it is: a failed send belongs on its own message bubble, a validation error under its field, and a page that failed to load in that page's banner where it can stay put and be re-read. Routing those through a notice double-reports the failure and then takes the report away after four seconds. Where a surface has no banner for such a failure, a notice beats silence — but reaching for one there is a sign the surface is missing a banner, not a licence to skip it.

### Settings pages

Every module settings page is `SettingsLayout` → `SettingsCard` → `SettingsField`. Do not build a settings page out of raw markup.

| Component        | Use for                                                                                                                                                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SettingsLayout` | The page frame: `title`, `description`, `loading`, `error`, `info`, `headerActions`. `title` is optional — omit it when the `ShellHeader` above already names the page, and the header row collapses |
| `SettingsCard`   | A titled group of fields, with its own `actions` and `status`. `actions` is for actions belonging to _that card_ ("Refresh all now") — not the page's save, see below                                |
| `HeaderSave`     | The app bar's slot for a page's single save. Pair with `useSettingsSave`                                                                                                                             |
| `SettingsField`  | One labelled control. `wide` for full-width, `checkbox` for checkbox rows, `labelled={false}` when the slot holds a `<button>` — see the rule below. A thin delegation to `ui/Field`; reach for `Field` directly outside a settings page                |
| `SecretField`    | A write-only credential input (bullet-masked, `configured` shows it is set without echoing it)                                                                                                       |
| `ServiceCard`    | A connected-service card on `/settings` or a module settings page                                                                                                                                    |
| `GarminCard`     | The Garmin connect/MFA/disconnect flow — a `custom_ui` service whose auth is an interactive exchange, not a set of writable fields                                                                   |

**A settings page has exactly one Save, and it lives in the app bar.** Call `useSettingsSave(() => ({ dirty, saving, save }))` during component init and render `<HeaderSave />` in the `ShellHeader`'s `tools` snippet — in the page itself for `/settings`, in the module `+layout.svelte` for a module settings page, where it renders nothing on every other page of the section. Return `null` from the callback to withdraw the contribution (a page whose module is switched off has nothing to save). Do not put a page-level Save on a `SettingsCard`.

**Contributors aggregate; they do not replace each other.** `/settings` edits the profile _and_ holds a `ServiceCard` per connected service, each writing through a different endpoint — so `ServiceCard` registers itself and has no button of its own, and one header Save covers the lot, asking only the parts actually holding edits. This is why `/money/settings` and `/location/settings` get a working header Save from their `ServiceCard`s alone, with no page-level registration. Before this, `/settings` carried three copies of the same-looking button (Identity, Preferences, Karakeep) and a fourth card would have added a fourth.

Two placement rules the component depends on. `<HeaderSave />` goes **before** the section's settings cog, so the cog keeps the bar's right edge and does not move as the save comes and goes. And the "Unsaved changes" badge is always in the layout, merely `visibility: hidden` when clean — `.header-tools` is right-aligned, so a badge that appeared as you type would shove everything to its left sideways mid-edit.

Still their own buttons, because they are per-record forms rather than the page's state: "save this source" / "save this shared block" on briefings settings, and the entity/service modals on money settings. Clearing a stored secret also stays immediate and separately confirmed — a deletion is not an edit awaiting a Save.

**`SettingsField`/`Field` supplementary text is three-slotted by whether the user must see it.** `hint` is optional guidance and renders in a `HintPopover` behind a "?" beside the label. `warning` and `error` render inline, because a hover popover is discoverable, not seen. A data-integrity notice ("this client is never invoiced automatically") is a `warning`, not a `hint` — do not put anything the user needs to act on behind hover.

**A `SettingsField` whose slot holds a button needs `labelled={false}`.** The component wraps its label text _and_ its slot in one `<label>`, and a `<button>` is a labelable element — so it becomes the label's implicit control and clicking the field's caption activates it. That shipped once as a caption reading "Tracking" that stopped background location tracking. `labelled={false}` renders a `<div>` instead. `<label>` stays the default and stays right for the ordinary one-input case, where clicking the caption should focus the input. (`HintPopover` dodges the same hazard by rendering its trigger as `<span role="button">`, but that only protects the "?".)

## Color

Every recurring meaning-bearing color is a token in `src/app.css`, defined in **both** the `:root` and `:root[data-theme='light']` blocks. Four roles:

- `--status-{danger,warn,success,info}-{fg,bg}` — severity. `-fg` for a bare status label, `-bg` for a filled chip's fill (pairs with the same `-fg`). Errors, warnings, status chips, badges, danger buttons, delete links.
- `--accent-blue` — the _interactive_ blue: primary buttons, active tabs, focus rings, in-app cross-references. Distinct from `--status-info-fg`, which marks a severity rather than something actionable.
- `--accent-amber` — the bot's identity accent, and the "starred" color. `--accent-amber-fill{,-hover,-fg}` is its filled-button form.
- `--status-dot-{ok,bad,warn,info}` — the admin dashboard's live-status dots. Off the `-fg` tint scale on purpose, and one value in both themes: a dot carries no text, so it needs saturation a tint cannot give it, and a theme-tracking dot reads as two states rather than one. (`--status-dot-info` is declared with no consumer yet.)
- `--link` — content links, and the single decision point for them. A surface joins by putting `prose` on its container, **not** by writing a local `a` rule; chat, the briefings reader and the feeds reader each had their own before, so briefing links were not visibly links and feed links were grey and permanently underlined. Hover is the underline only — the color already marks the link, and moving both reads as a state change.
- `--money-{income,expense}` — signed-amount direction. Deliberately off the status scale: an expense is not an error.

`--status-critical-{bg,fg}` is a fifth severity above danger, for a value that is not merely out of range but clinically critical (the bloodwork `flag-C` cell). Unlike the four above it, it is a solid saturated fill rather than a tint, so its `-fg` is the text laid _on_ `-bg`.

Supporting scales: `--surface-{base,card,raised,badge,overlay,reading}`, `--text-{primary,secondary,muted,dim,reading}`, `--border-{default,subtle,hover}`, `--radius-{card,pill}`, `--text-{2xs,xs,sm,base}`.

Not colors, but declared alongside them and read the same way: `--font-sans`, `--transition-fast`, `--chip-{padding-x,gap}`, `--chat-{row-inline,gutter,avatar,avatar-gap}`, `--sigil-filter` (tints the octopus mark per theme), and the geometry tokens `--safe-{top,bottom,left,right}` / `--app-height` / `--kb-height`. The last three groups are written by code, not by a stylesheet — see "Platform and the native shell".

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
- **Do not style scrollbars.** Touching `::-webkit-scrollbar`, `scrollbar-width` or `scrollbar-color` opts out of the fading overlay bar the platform draws and gets a permanent one whose track takes width. That was tolerable while pages scrolled as documents; the app is now one screen tall and scrolls inside its panes, so a styled bar becomes a fixed strip beside every list.
- **An edge-pinned element pads with `max(<its own padding>, var(--safe-*))`**, never with the inset alone — the inset is 0 on a device without a notch, and the element would then sit flush against the edge. A `position: fixed` overlay escapes every ancestor's padding, so the insets go on its **backdrop** (`.overlay-safe`), not on the panel inside it.
- **Never reintroduce `100vh` or `calc(100vh - Npx)`** for a full-height surface. Height comes from `--app-height` and `--kb-height`; see below.
- Below 640px the nav, the shell header and the settings wrapper all align on an inline edge of **0.75rem** — the same figure the money shell fixes. A page that sets its own drifts out of line with the bar above it.
- **Touch affordances reveal on tap, not on hover.** iOS synthesizes `:hover` on tap and then leaves it applied, so a hover-revealed control stays lit on the row you last touched. `chat/tapActivation.ts` is the shared primitive: a tap is a press within its slop and time bounds, and one active row at a time stands in for hover. Reveal instantly — a fade on an affordance the user has already reached for reads as lag.

## Page skeletons

**Module page** (`routes/<module>/+layout.svelte`): `AppShell` with a `ShellHeader` header snippet (`SidebarToggle` in `leading`, `HeaderNav` in `nav`, a `Cog` link to `<module>/settings` in `tools`) and a `Sidebar` when the module has a list/detail split. `routes/briefings/+layout.svelte` is the cleanest example.

**Module settings page** (`routes/<module>/settings/+page.svelte`): `SettingsLayout` wrapping `SettingsCard`s. Call `getModuleServices(<module>)` first and render the "Module disabled" banner when `module_enabled` is false, instead of the configuration UI.

**Every top-level route renders an `AppShell` and is listed in the `app-content-fill` class on `routes/+layout.svelte`'s `<main>`.** The two are one decision, not two: without the shell nothing pins the app nav, and without the class the document scrolls and carries the nav off the top of the screen — which is only noticeable on a phone or in the iOS app, where the nav is the only way back to another section. The class also locks document scrolling and stops overscroll chaining, so touch scrolling belongs to the panes rather than the page — a fill route that scrolls the document has lost its nav. A single-page section (`/`, `/chat`, `/admin`, `/settings`) puts the shell in its `+page.svelte`; a section with sub-routes puts it in `+layout.svelte`. `ShellHeader`'s `title` is the page's name in the fixed bar.

The one exception is `routes/+error.svelte`, which renders no `AppShell` — an error page has no section to be in — and instead flattens `main.app-content` from inside itself. Adding a route means adding it to that class list; several in-tree comments still count the routes by hand and go stale, so trust the class list, not the count beside it.

**Loading and empty states** use `.center-msg`, the one shared whole-pane status message, and the page holds its chrome back behind the load so the message centres on the pane rather than under a half-drawn header. The older `.loading` / `.error-msg` pair survives only for a line inside a card.

**Money list page**: the record-table shell is defined once as globals in `routes/money/+layout.svelte` — `.money-toolbar`, `.money-notice-bar`, `.money-table`, `.money-table-header`, `.money-table-row`, `.money-sortable`, `.money-status`, `.money-amount`, `.money-kebab-spacer`, `.money-table-empty`, `.money-control-input`, `.money-result-count`, `.money-sort-arrow`. A page styles only its own columns (widths, alignment) and inherits everything else. The shell fixes the inline edge at `0.75rem` for every element on the page; do not set your own.

## Platform and the native shell

The web app also runs inside a native iOS shell (its own repo, `istota-mobile` — a Capacitor WKWebView pointed at the deployment URL, so the URL is the only build-time coupling). `src/lib/platform/` is the facade over everything that differs there. Two rules govern it, and both exist because the web deploys in minutes while the shell binary lags a TestFlight cycle:

1. **Every export degrades to plain-web behaviour off the shell** — a no-op, or the browser's own control. Nothing may require the shell to work.
2. **Every shell-dependent capability is gated on the shell version that introduced it**, via `shellAtLeast()`. An older app must get a working page that says what it cannot do, not a control that silently fails.

**Detection is the `IstotaApp/<version>` user-agent token, not `window.Capacitor`.** The UA is present on the first request, survives SSR, and carries a version the injected bridge does not — and a page can carry a `Capacitor` shim without being in the shell at all, so a plugin object's presence is not evidence of the shell. Route every call through one accessor that checks both the gate and the plugin, so the two cannot diverge: shipping six of eight tracker calls gated on the plugin alone is a defect this rule already caught.

Capabilities and their gates: soft-keyboard geometry (0.2.0), camera / photo library / document picker (0.3.0), upload-from-disk (`IstotaUploader`, presence-detected), the background location tracker (0.6.0) and the QR scanner (0.7.0) — the last two gated separately, so a 0.6.0 app tracks but says it cannot scan. Where a capability's _shape_ changed rather than its existence, feature-detect the response instead of adding a version (the pickers returning a path rather than base64).

**The height model.** `--app-height` and `--kb-height` are written by `lib/viewport.ts`; body height is `calc(var(--app-height, 100dvh) - var(--kb-height, 0px))`. Four invariants worth knowing before touching it:

- `--app-height` is published **only in standalone**. A browser tab keeps `dvh`, because there `innerHeight` is the layout viewport and does not track a collapsing toolbar — which is what `dvh` was adopted for. It exists at all because of an iOS 26 WebKit bug where, after a keyboard dismissal, the visual viewport stays short and `dvh` follows it down.
- The published height is the **tallest viewport seen**, and short readings are never published. A keyboard can only make a viewport smaller, so a smaller reading is never evidence of how tall the app should be; holding a slightly-too-tall height briefly beats a band nothing later corrects. The one path that adopts a smaller baseline is a shrink outlasting the whole settle window with nothing focused — a split view or a resized window, not a keyboard.
- The keyboard gate takes focus as the signal and overrules a focused field only when **both** viewports are back within tolerance. Requiring both is what makes it right on either platform: an installed app resizes the layout viewport, a browser tab only the visual one. Testing one turned an intermittent gap into a permanent one.
- `--kb-height` bypasses the settle machinery entirely — it comes from the shell's `keyboardWillShow`, at the start of the animation rather than after it, and that number does not lie. It has to come from the shell because viewport units deliberately ignore virtual keyboards.

`?vpdebug=1` turns on an on-device readout of the shell version and the live geometry (`?vpdebug=0` clears it). It is the only way to see this subsystem working on a real phone, and it prints the version precisely so "a gate with no height behind it" is visible without a rebuild.

## Client-local preferences

Theme and text scale are localStorage-persisted per browser, not profile fields — they must apply before first paint, and profile data arrives after it. Both follow one shape: a `writable` seeded from `loadSetting`, a normalizer folding any unrecognized value onto the default, an `apply*` setting an attribute on `<html>`, and a matching branch in the single blocking `<script>` in `app.html`. Add to that script; never add a second one. Controls for these live in the **Appearance** card on `/settings`, which sits outside the `{#if profile}` block and applies on change — keep client-local controls out of the profile cards, whose fields need the shared Save button.

The theme is stated in three places that must agree: the token in `app.css`, the `theme-color` meta seeded by that same pre-paint script in `app.html`, and `applyTheme` in `stores/theme.ts`, which keeps the meta current as the user switches. A test asserts all three, so changing the dark surface color in one place fails there rather than shipping a browser chrome that no longer matches the app.

## Shipping a build

There is **no service worker**. Caching is HTTP headers plus SvelteKit's own version poll, and the server sends two classes: hashed `_app/immutable/` assets are immutable for a year, everything else — the HTML shell, `version.json`, the manifest, the icons — is `no-cache` and revalidates. Sending neither (the previous state) let an installed home-screen app pin the shell indefinitely and then ask for hashed chunks the server had already deleted.

A new build is surfaced, not forced: the root layout watches SvelteKit's `updated` state and shows a toast with a Reload button. It is a tap rather than an automatic reload because reloading discards a half-typed message. It also re-checks on `visibilitychange`, since the poll timer is throttled in a background tab and stopped outright in a suspended PWA — without that, a phone returning to the app after a day would be running whatever it had.

`static/` holds the manifest and the four icons. Manifest paths stay **relative** and head links go through `%sveltekit.assets%`, because the whole app is served under the `/istota` base and at varying route depths.

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
