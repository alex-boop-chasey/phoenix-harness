# Phoenix Harness design system

One design system for the whole app: Project builder, Session manager and everything
added later use these tokens and components. No page-specific colours, fonts or one-off styles; if a
component is missing, add it here and to the shared CSS first. The canonical stylesheet is
`hands/static/app.css` (since step 04); the static mock in `hands/static/mock/` (`mock.css`) is the
design reference for pages not built yet — screenshots in
`~/ops/shots/03-mock/` via `bash tools/ui-shots.sh --mock <label>`.

Hard rules (from the dashboard CSP): no inline scripts, no `style` attributes, no external fonts or
images (`default-src 'none'`). Styles come from the stylesheet; dynamic values go through CSS classes
or `element.style` from JS. All agent/user text is rendered with `textContent`.

xterm.js (Session manager, step 11) injects two `<style>` elements, which the CSP blocks. The CSP is
unchanged: the terminal is opened with xterm's `documentOverride` whose `createElement("style")`
returns a hidden holder element that forwards its `textContent` into a constructed `CSSStyleSheet`
adopted by the document (CSSOM, not governed by `style-src`). Browsers without constructable
stylesheets (Safari < 16.4) get a readable but uncoloured terminal and a note in the footer.
Truecolor cells would need style attributes, so the session environment never sets `COLORTERM`.

## Direction: "Workbench"

Quiet, warm-neutral surfaces, a single blue accent for selection and primary actions, semantic
green/amber/red only for status. System font stack, dense but breathable 14 px type, 12 px card
radius, hairline borders and soft shadows. Light and dark are first-class and derived from the same
tokens; the terminal follows the theme (light terminal in light mode) instead of being a dark island.

Directions considered (step 03 decision):

1. **Workbench** (built, recommended): evolves the current dashboard's warm greys so existing users
   feel at home; neutral enough that log text, diffs and terminal colours stay the loudest thing on
   screen; cheap to keep consistent across three very different pages.
2. **Studio**: cool slate greys, indigo accent, more whitespace and larger type — prettier at a
   glance, but less dense (fewer steps/sessions visible per screen) and the terminal page fights
   the airy layout.
3. **Console**: dark-only, monospace headings, high-contrast "hacker" look — strong identity, but
   reads poorly in daylight on a phone, and a dark-only app clashes with light file previews and
   diffs.

Approval: **approved by Alex on 2026-10-17** (step 03). Notes from the approval:

- Alex uses **Safari on Mac**, not Chrome. Every UI feature must work in current Safari; the
  screenshot rig's Chromium is a proxy, not the target.
- The tab-strip "+" menu in the mock uses CSS anchor positioning (`position-anchor`/`anchor()`).
  Safari's support for it is recent and not something we rely on: in step 04 the menu is positioned
  **by JS by default** (`position: fixed`, offset computed from the button's `getBoundingClientRect()`
  on open/scroll/resize), with anchor positioning at most as a progressive enhancement.

## Tokens

Defined on `:root` in `mock.css`. Dark values apply under `prefers-color-scheme: dark` (unless
`<html data-theme="light">`) and always under `<html data-theme="dark">`, so a manual toggle can be
added later without touching components.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#f4f4f1` | `#121211` | page background |
| `--surface` | `#ffffff` | `#1b1b1a` | cards, sidebar, tab strip, inputs |
| `--surface-2` | `#ecebe6` | `#262623` | code/log blocks, table heads, hover fills |
| `--ink` / `--ink-2` / `--ink-3` | `#1b1b19` / `#5b5b55` / `#8a8a82` | `#ebeae5` / `#b1b0a8` / `#7f7e77` | text / secondary / placeholder & disabled |
| `--line` / `--line-2` | `#e0dfd9` / `#c6c5bd` | `#2f2e2b` / `#45443f` | hairlines / control borders |
| `--accent`, `--accent-ink`, `--accent-soft` | `#2457b8`, `#fff`, `#e4ecfa` | `#7fa9f0`, `#0f1a2e`, `#1f2b44` | links, selection, primary button, building/running |
| `--ok` / `--ok-soft` | `#1a7a47` / `#e1f3e8` | `#62c78f` / `#173224` | passed, done, live |
| `--warn` / `--warn-soft` | `#9a5500` / `#fbeedb` | `#e5a549` / `#3a2a10` | plan ready, stopped, paused, waiting |
| `--bad` / `--bad-soft` | `#b42318` / `#fbe5e3` | `#f28b82` / `#3f1d1a` | failed, escalation, destructive |
| `--term-bg`, `--term-fg`, `--term-dim`, `--term-cursor` | `#fbfbf9`, ink, ink-3, accent | `#121211`, `#e6e5df`, ink-3, accent | terminal frame |
| `--term-red/green/yellow/blue/magenta/cyan` | bad / ok / warn / accent / `#7b3fa0` / `#0e7490` | bad / ok / warn / accent / `#c79ae6` / `#5fc1d6` | ANSI colours |
| `--shadow-1`, `--shadow-2` | subtle card / popover | stronger (dark) | elevation |
| `--focus` | 2 px surface + 2 px accent ring | same | every focusable element |

Type: `--sans` system-ui stack, `--mono` ui-monospace stack. Scale `--fs-0..5` = 12, 13, 14, 16, 20,
24 px; body 14/1.5; tables and lists 13; meta and badges 12; terminal and logs 12–13 mono.
Spacing `--sp-1..6` = 4, 8, 12, 16, 24, 32. Radius `--r-1..3` = 6, 8, 12, `--r-pill`. Sidebar 248 px,
phone top bar 48 px. Breakpoint: ≤ 900 px is the small layout.

Contrast: all text pairs meet WCAG AA (ink-2 on surface ≥ 4.5:1 in both themes; ink-3 is used only
for placeholders, disabled items and decoration). Status is never colour alone: every badge has a
text label and a dot, step icons use ✓ ✗ ○ plus colour.

## Layout

- **App shell** (`.app`): left sidebar + content. Sidebar holds app sections only (Project builder,
  Session manager; a "Later" group reserves space for Environment variables, MCP
  servers), usage meters and the account footer. **Never** per-section controls.
- **≤ 900 px**: sidebar becomes an off-canvas drawer (`.nav-toggle` + `.scrim`) opened from a top bar
  (☰, brand, current section). Grids collapse to one column; the builder's step rail moves to the
  top; tables scroll horizontally inside `.table-wrap`.
- **Project builder**: `.tabs` strip across the top (sticky), one `.tab` per open project with name,
  stage badge and ×; a dashed `+` at the end opens the `.menu` popover (New project; Reopen: closed
  projects with their last stage). Many tabs scroll horizontally; the selected tab is the page's
  content. The popover is `position: fixed` with its offset computed by JS from the `+` button
  (so the scrolling strip cannot clip it and it works in Safari); CSS anchor positioning is only an
  optional enhancement.
- **Session manager**: no tabs; page header (`.page-head`) with title and the page's
  own actions.

## Components

| Component | Class(es) | Notes |
|---|---|---|
| Sidebar nav | `.sidebar .nav a`, `[aria-current="page"]`, `.soon` | icon + label; current item = accent-soft fill + accent text; "soon" items are inert |
| Usage meters | `.meter .bar .fill(.warn/.bad)` | width via class (`.w-42`) or `element.style` in JS |
| Tabs | `.tabs[role=tablist] .tab[role=tab]`, `.tname`, `.badge`, `.x`, `.tab-add`, `.menu` | selected tab: bg, border, 2 px accent top rule; × shows on hover/selected |
| Badges | `.badge .b-building .b-planning .b-ready .b-done .b-passed .b-ok .b-failed .b-escalation .b-stopped .b-paused .b-idle .b-pending .b-closed`, `.pulse` | dot + text; live states pulse (disabled under reduced motion) |
| Buttons | `.btn`, `.primary`, `.danger`, `.quiet`, `.sm`, `.loading`, `:disabled` | one primary per view; destructive = red outline, red fill on hover |
| Forms | `.field label .hint .error`, `.input .textarea .select`, `[aria-invalid]`, `.check`, `.search` | focus = accent border + soft ring; errors inline below the field (`.input`, `.error`, `.seg`, `.btn.quiet/.sm` are in app.css since step 07) |
| Cards | `.card .card-head .card-body(.tight)`, `.grid-2`, `.with-rail .rail`, `.kv` | heads carry the title + right-aligned tools |
| Tables | `.table-wrap table`, `th`, `td.num`, `tr[aria-selected]` | muted header on surface-2; row hover tint; selected row accent-soft |
| Log viewer | `pre.log` + `.t .ok .bad .hi`, `.log-tools` | 12 px mono on surface-2, wraps, max-height with scroll; Follow / Copy / Open full |
| Step rail | `ol.steps li(.cur .passed) .ic(.ok .bad .pend) .sid .act` + `.checklist` | current step: accent-soft with spinner and live activity line |
| Notices | `.notice(.bad .warn .ok)` | left rule + soft fill; escalations use `.bad` with the ack action inside |
| Env editor | `.envedit .envrows .envrow(.removed) .input(.mono) .seg .btn.quiet.sm .error(.ek .ev) .hint` | one row per variable: Key (mono, read-only once saved), Value (`type=password` while Secret), Secret/Public `.seg`, remove ×; errors sit under their field (`aria-invalid` on the input); removed saved rows show struck through with Undo until Save; phone: key + toggle on one line, value below (since step 07) |
| Dialog | `dialog.dialog` (h2, optional `.notice.warn`, body `p.muted`, `.row` with Cancel + `.btn.danger`/`.primary`) | native `<dialog>` + `showModal()`, backdrop at 35 % black; built by `confirmDialog()` in app.js, falls back to `window.confirm` where `showModal` is missing; focus starts on Cancel, Esc cancels (since step 05) |
| Empty states | `.empty .glyph` | glyph, one-line title, hint, optional action |
| Toasts | `.toasts .toast(.bad) .dot .x` | bottom-right (full-width on phone), inverted colours, `aria-live=polite` |
| Loading | `.spinner`, `.btn.loading`, `.skeleton` | spinner in buttons/rows; skeleton bars for first paint |
| Session list | `.slist .sitem[aria-current] .dot(.live .wait .bad) .smain .sname .smeta .when(.live)` | `button.sitem` per session: status dot, title (first prompt), folder · state, "live" or relative time; empty state inside the list (since step 11) |
| Terminal frame | `.term .term-head .title .badge .tools`, `div.term-body` (xterm.js host), `.term-foot .conn(.ok .warn .bad) .keys .kbd` | theme from `--term-*`: `sessions.js` builds the xterm theme from the tokens at run time and again when the colour scheme changes (ANSI black/white = `--term-fg`/`--term-bg`, bright black = `--term-dim`, selection = `--accent-soft`); font = `--mono` at `--fs-1`. Head = dot, title, state badge, folder · started, Resume / Stop / Delete; foot = connection state (`role=status`) and key legend. Phone: `.sessions.show-term` swaps the list for the terminal, `.show-sm` back button (since step 11) |
| New session dialog | `dialog.dialog` with `.field` (label, `.input.mono`, hint, `.error`) + `label.check` (checkbox, text, `.hint`) | working folder, default `~/ops`; "Live on Telegram" checkbox whose hint names the session holding the slot; server errors shown inline under their field (since step 12) |
| Channel badge | `.badge.s-telegram` (accent on accent-soft, ✈ glyph instead of the dot) | marks the session that is live on Telegram: "Telegram" inline after the title in the session list, "live on Telegram" / "Telegram" (stopped) in the terminal head next to the state badge; one per app at most. Head button `#s-telegram` toggles it ("Go live on Telegram" / "Leave Telegram") through the shared `confirmDialog` (since step 12) |
| Checkbox | `label.check > input[type=checkbox] + span(.hint)` | accent-coloured native box, label text with an optional muted hint on its own line |
| Sub-fields | `.subfields` (`.hidden` to collapse) | a group of fields revealed by a checkbox, with a left rule; used by the New project form's "Start from an existing repo" (repo, branch, ruleset) |
| Breadcrumb | `.crumbs a .sep .here` | clickable ancestors, bold current |
| View switch | `.seg button[aria-pressed]` | List / Grid |
| Quiet text button | `.btn.quiet.sm.text` | quiet button with text at `--fs-1` (`.btn.quiet.sm` alone is the larger icon button) |
| Select | `select.select` | native select in control style, accent focus ring |
| Toasts | `#toasts.toasts .toast(.bad)` | in app.css; `toast(text, bad)` in app.js shows done/failed actions without blocking the page (since step 17) |

## States

- **Hover**: surface-2 fill (nav, menu items, rows, quiet buttons) or stronger border (buttons, cards).
- **Focus**: `:focus-visible` → `--focus` ring on every interactive element; inputs also tint the border.
- **Active/selected**: accent-soft fill + accent text (nav, tabs, rows, file cards, session items).
- **Disabled**: 45 % opacity, `not-allowed` cursor, no hover change.
- **Loading**: spinner (16 px) or `.btn.loading`; skeleton rows while a list first loads; live
  badges pulse. Reduced motion slows/stops animations.
- **Error**: `.notice.bad` for page-level, `.field .error` + `aria-invalid` for fields, `.toast.bad`
  for transient failures.

## Accessibility

- Tabs follow the WAI-ARIA tabs pattern: `role=tablist/tab`, `aria-selected`; ← → move between tabs,
  Home/End jump, Delete closes the focused tab, the `+` button opens a `role=menu` navigated with
  ↑ ↓ / Esc. Sidebar is a `<nav>` of links with `aria-current="page"`; on phone the drawer toggle is a
  labelled control and the scrim closes it.
- Visible focus ring everywhere (`--focus`), never removed. Minimum 32 px tap targets on phone.
- Colour is never the only signal (text labels on badges, glyphs on steps). AA contrast in both themes.
- Terminal body is focusable (`tabindex=0`) with an `aria-label`; the footer explains that keys go to
  the session and how to interrupt. Toasts use `aria-live=polite`.
- Tables keep real `<th>`; file lists are tables (not div grids) so screen readers get columns.

## Approval record

- 2026-10-17 — Alex approved the **Workbench** direction as mocked in `~/ops/shots/03-mock/`
  (commit `5f3a00e`). No visual changes requested. Requirement added: Safari on Mac is the primary
  browser; the "+" menu (and anything else that leans on new CSS) must work there, with JS fallbacks
  used by default where Safari support is uncertain.
