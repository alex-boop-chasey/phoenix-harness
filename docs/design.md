# Phoenix Harness design system

One design system for the whole app: Project builder, Session manager, File browser and everything
added later use these tokens and components. No page-specific colours, fonts or one-off styles; if a
component is missing, add it here and to the shared CSS first. The living reference is the static mock
in `hands/static/mock/` (`mock.css` holds the tokens and components) — screenshots in
`~/ops/shots/03-mock/` via `bash tools/ui-shots.sh --mock <label>`.

Hard rules (from the dashboard CSP): no inline scripts, no `style` attributes, no external fonts or
images (`default-src 'none'`). Styles come from the stylesheet; dynamic values go through CSS classes
or `element.style` from JS. All agent/user text is rendered with `textContent`.

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

Approval: _pending Alex (step 03)_.

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
  Session manager, File browser; a "Later" group reserves space for Environment variables, MCP
  servers), usage meters and the account footer. **Never** per-section controls.
- **≤ 900 px**: sidebar becomes an off-canvas drawer (`.nav-toggle` + `.scrim`) opened from a top bar
  (☰, brand, current section). Grids collapse to one column; the builder's step rail moves to the
  top; tables scroll horizontally inside `.table-wrap`.
- **Project builder**: `.tabs` strip across the top (sticky), one `.tab` per open project with name,
  stage badge and ×; a dashed `+` at the end opens the `.menu` popover (New project; Reopen: closed
  projects with their last stage). Many tabs scroll horizontally; the selected tab is the page's
  content. The popover is `position: fixed` + CSS anchor positioning (JS fallback computes the
  offset) so the scrolling strip cannot clip it.
- **Session manager / File browser**: no tabs; page header (`.page-head`) with title and the page's
  own actions.

## Components

| Component | Class(es) | Notes |
|---|---|---|
| Sidebar nav | `.sidebar .nav a`, `[aria-current="page"]`, `.soon` | icon + label; current item = accent-soft fill + accent text; "soon" items are inert |
| Usage meters | `.meter .bar .fill(.warn/.bad)` | width via class (`.w-42`) or `element.style` in JS |
| Tabs | `.tabs[role=tablist] .tab[role=tab]`, `.tname`, `.badge`, `.x`, `.tab-add`, `.menu` | selected tab: bg, border, 2 px accent top rule; × shows on hover/selected |
| Badges | `.badge .b-building .b-planning .b-ready .b-done .b-passed .b-ok .b-failed .b-escalation .b-stopped .b-paused .b-idle .b-pending .b-closed`, `.pulse` | dot + text; live states pulse (disabled under reduced motion) |
| Buttons | `.btn`, `.primary`, `.danger`, `.quiet`, `.sm`, `.loading`, `:disabled` | one primary per view; destructive = red outline, red fill on hover |
| Forms | `.field label .hint .error`, `.input .textarea .select`, `[aria-invalid]`, `.check`, `.search` | focus = accent border + soft ring; errors inline below the field |
| Cards | `.card .card-head .card-body(.tight)`, `.grid-2`, `.with-rail .rail`, `.kv` | heads carry the title + right-aligned tools |
| Tables | `.table-wrap table`, `th`, `td.num`, `tr[aria-selected]` | muted header on surface-2; row hover tint; selected row accent-soft |
| Log viewer | `pre.log` + `.t .ok .bad .hi`, `.log-tools` | 12 px mono on surface-2, wraps, max-height with scroll; Follow / Copy / Open full |
| Step rail | `ol.steps li(.cur .passed) .ic(.ok .bad .pend) .sid .act` + `.checklist` | current step: accent-soft with spinner and live activity line |
| Notices | `.notice(.bad .warn .ok)` | left rule + soft fill; escalations use `.bad` with the ack action inside |
| Empty states | `.empty .glyph` | glyph, one-line title, hint, optional action |
| Toasts | `.toasts .toast(.bad) .dot .x` | bottom-right (full-width on phone), inverted colours, `aria-live=polite` |
| Loading | `.spinner`, `.btn.loading`, `.skeleton` | spinner in buttons/rows; skeleton bars for first paint |
| Session list | `.slist .sitem[aria-current] .dot(.live .wait .bad) .smain .sname .smeta .when` | status dot, name, meta, relative time |
| Terminal frame | `.term .term-head .title .tools`, `pre.term-body` (`.p .d .g .r .y .m .c .cur`), `.term-foot .kbd` | theme from `--term-*`; head = name, live badge, tmux/size/started, Attach / Rename / Stop; foot = focus hint and key legend |
| Breadcrumb | `.crumbs a .sep .here` | clickable ancestors, bold current |
| View switch | `.seg button[aria-pressed]` | List / Grid |
| File list | table rows with `.ftype(.dir .code .img .doc)` + `.fname` | type chip, name link, size (`.num`), modified, permissions (hidden on phone) |
| File grid | `.fgrid .fcard[aria-selected] .thumb .fname .fmeta` | auto-fill 140 px cards |
| Preview pane | `.card.preview` (+ `pre.log` for text) | actions row: Download, Copy path, Rename · Delete (danger, right) |
| Upload | `.dropzone(.active)`, `.progress .fill` | drop target + per-file progress list with badges |

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

_To be filled in when Alex approves step 03 (date, notes, requested changes)._
