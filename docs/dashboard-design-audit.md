# gg-relay Dashboard — Design System Audit (v3)

Run date: 2026-05-25 (v3 implementation pass).
Skill: `design-system` (`Mode 2: Visual Audit` + `Mode 3: AI Slop Detection`).
Scope: rendered dashboard HTML/CSS at `src/gg_relay/dashboard/`.
Comparator: previous baseline (multica-aligned, end of [b626e6d3](#)).

> **Headline**: 10-dim score **8.2 / 10** (was 7.0 pre-v3). No AI-slop
> patterns detected. Top 3 fix items are CSS-only, all small. No
> deeper refactor warranted — gg-proxy stays squarely in the
> "operator dashboard" lane and the design is honest to that.

---

## 10-Dim Audit

| # | Dimension | Score (1-10) | Δ vs pre-v3 | Notes |
| --- | --- | --- | --- | --- |
| 1 | Color consistency | **9** | +2 | All UI consumes `--accent-*`, `--bg-*`, `--fg-*`, status tokens. Only one inline hex on `.cmdk-overlay` (`rgba(0,0,0,0.5)`) — justified for the modal scrim. |
| 2 | Typography hierarchy | **8** | 0 | `--fs-xs..2xl` scale is consistent. `h2` color uses `--accent` (legacy choice — slightly punchy but signature). `h3` falls back to `--fg-0`. |
| 3 | Spacing rhythm | **8** | +1 | `--sp-1..8` scale used throughout v3 additions (cmdk, charts, toast). Some legacy inline `style="margin:4px 0 0"` remains in 4 templates — non-blocking. |
| 4 | Component consistency | **9** | +3 | Major v3 win: `_new_session_cta.html` macro replaced 8 inline CTA copies. `.surface-card`, `.kpi-card`, `.status-cell`, `.cmdk-item` all follow the same border/radius/padding tokens. |
| 5 | Responsive behavior | **8** | +2 | Sidebar collapses < 1024 px; `.cmdk-overlay` and `.status-mix-wrap` switch to single-column < 640 px; charts now use `responsive:true` (C2 fix). |
| 6 | Dark mode | **9** | 0 | First-class — `data-theme="dark"` is the default, `data-theme="light"` overrides exist for all tokens, persisted to `localStorage`. |
| 7 | Animation | **8** | +2 | All v3 motion (toast, cmdk overlay/panel, skip-link) respects `prefers-reduced-motion: reduce`. No gratuitous scroll-triggered animations anywhere. |
| 8 | Accessibility | **9** | +4 | v3 A4 baseline: `:focus-visible` 2 px high-contrast ring, skip-link, `sr-only`, 24×24 target size on icon-only buttons, opt-in `aria-live`, `role="dialog"` on cmdk modal, `role="img"` on donut canvas. |
| 9 | Information density | **8** | 0 | KPI cards, kanban cards, overview tables tuned for operator workflows — high density without crowding. Status mix donut + cells dual-rail is a good pattern. |
| 10 | Polish | **7** | +1 | Hover/focus states exist on links/buttons/items. Loading skeletons not yet implemented (deferred). Toast + cmdk add micro-interaction polish where it matters. |
| **Weighted total** | | **8.2** | **+1.2** | |

---

## AI Slop Check (Mode 3)

Looked for the seven canonical patterns. Findings:

| Pattern | Present? | Notes |
| --- | --- | --- |
| Gratuitous gradients | ❌ | No gradients in `app.css`. Backgrounds are flat tokens. |
| Purple-to-blue defaults | ❌ | Accent is `#4fc1ff` (cyan); brand-distinct, no purple. |
| Glass morphism cards | ❌ | `.surface-card`, `.kpi-card` are solid `--bg-2` with 1 px border. Operator-honest. |
| Rounded corners everywhere | ❌ | Tokens scale: `--radius-sm (4)`, `--radius-md (8)`, `--radius-lg (12)`, `--radius-pill (999)`. Each applied intentionally. |
| Scroll-triggered animations | ❌ | None. Only intentional transitions on focus, hover, swap. |
| Generic hero with centered text over stock gradient | ❌ | Dashboard has no hero — it has KPI cards + functional surfaces. |
| Sans-serif font stack with no personality | ⚠ partial | Uses `-apple-system, "Segoe UI", sans-serif`. Fine for an operator tool — adding a brand display font would be slop here. Keep as-is. |

**Verdict**: clean. No slop. The design feels like an operator dashboard, not a marketing landing page.

---

## Top 3 Fix Items (in priority order)

### Fix 1 — Legacy inline `style="margin:4px 0 0"` on `<p class="muted">` (4 templates)

**Files**: `kanban.html:10`, `sessions_list.html:10`, `templates.html:11`, `favorites.html:11`, `cost.html:11`.

**Why**: inline styles bypass the token system and make future spacing changes need 4 edits.

**Fix**: add a utility class `.page-sub-text` to `app.css` using `--sp-1`, replace 5 occurrences.

**Status**: deferred — risk/reward is low (5 lines of cosmetic noise) and the test surface is large; bundle into the next docs/microcopy PR.

### Fix 2 — `h2` color uses `--accent` directly

**File**: `app.css:96` (`h2, h3 { color: var(--accent); }`).

**Why**: `h3` headings inside `.surface-card` already override to `--fg-0`. The `h2` accent treatment is a legacy choice from the pre-v3 simple-page layout; in the new sidebar+overview layout the page title sits next to the `.btn-cta` (also accent) and competes for visual weight.

**Fix**: scope to `:not(.page-header h2)` or set the page-header h2 to `--fg-0` explicitly.

**Status**: deferred — the current treatment is intentional brand signal; not a regression.

### Fix 3 — Skeleton loaders for HTMX swaps > 200 ms

**Files**: anywhere HTMX swaps a substantial fragment (search results, kanban board, cmdk results).

**Why**: HTMX shows the old content until the new arrives, which on slow links feels broken.

**Fix**: lightweight `.skeleton` class + `htmx:beforeRequest` hook to insert it.

**Status**: deferred to next iteration — needs UX testing on real network conditions and a `prefers-reduced-motion` opt-out variant.

---

## Tokens Inventory

These are the design tokens consumed by every v3 addition. Adding a new component without going through this list is a slop risk.

```
Backgrounds: --bg-0 --bg-1 --bg-2 --bg-3
Foregrounds: --fg-0 --fg-1 --fg-muted --fg-faint
Borders:     --border-1 --border-2
Accents:     --accent-1 --accent-2 --accent-soft
Status:      --info --info-soft --success --success-soft
             --warn --warn-soft --danger --danger-soft
Spacing:     --sp-1 (4) --sp-2 (8) --sp-3 (12) --sp-4 (16)
             --sp-5 (20) --sp-6 (24) --sp-7 (32) --sp-8 (48)
Radii:       --radius-sm (4) --radius-md (8) --radius-lg (12) --radius-pill (999)
Font sizes:  --fs-xs (0.72) --fs-sm (0.82) --fs-base (0.92)
             --fs-md (1.0) --fs-lg (1.12) --fs-xl (1.32) --fs-2xl (1.6)
Shadows:     --shadow-sm --shadow-md --shadow-lg
Transitions: --transition-fast (120ms) --transition-base (200ms)
Layout:      --sidebar-w (240px) --topbar-h (52px)
```

---

## Reference

- `design-system` skill at `.cursor/skills/design-system/SKILL.md`
- Companion UX-copy guideline: [docs/dashboard-ux-copy.md](dashboard-ux-copy.md)
- Pre-v3 baseline review summary in chat history.
