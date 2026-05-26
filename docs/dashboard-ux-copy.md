# gg-relay Dashboard — UX Copy Guideline

Derived from `brand-voice` skill, distilled for UI microcopy.
This is the one-page rulebook every contributor follows when
writing button labels, error messages, empty states, placeholders,
and aria-label/title attributes in templates under
`src/gg_relay/dashboard/templates/`.

> **Voice in one sentence**: direct, compressed, concrete — an
> operator dashboard speaks like an operator, not a marketing team.

---

## Voice Profile (operational shorthand)

| Trait | Yes | No |
| --- | --- | --- |
| **Tone** | Sharp, dry, factual | Friendly emoji, exclamation, hype |
| **Tense** | Imperative ("Submit", "Approve", "Retry") | Gerund ("Submitting…", "Approving…") |
| **Pronouns** | Avoid "you/we" in labels; use "Your X" only for ownership scope | Coaching voice ("Let's…", "Now you can…") |
| **Length** | One verb + one noun where possible | Sentence-style labels on buttons |
| **Numbers** | Show absolute values, not percentages-of-percentages | Vague "many"/"some"/"few" |
| **Latin** | OK for accepted abbreviations (e.g., "i.e.", "USD") | "etc.", "in order to" |

---

## Button Labels

**Rules**:

1. **Imperative verb**, sentence case: `Submit`, `Approve`, `Retry`, `Cancel`, `Sign in`.
2. **One verb + one noun** when context is ambiguous: `Submit new session`, `Save template`, `Revoke key`.
3. **Never** a question ("Submit?"), suffix punctuation, or trailing ellipsis on submit buttons.
4. Ellipsis (`…`) **only** for actions that open a follow-up prompt: `Add filter…`, `Add tag…`.
5. Destructive actions get the verb + the noun being destroyed: `Delete template`, `Revoke key`. Never bare `Delete`.

**Examples**:

| Good | Bad |
| --- | --- |
| `Submit new session` | `Create Session` (Title Case, vague verb) |
| `Approve` | `Approve!` (exclamation) |
| `Switch to list` | `View as list` (vague) |
| `Open in Jaeger` | `Trace` (icon-only labels need a noun) |

---

## Error Messages

**Shape**: `<cause> + <next action>` — no blame, no exclamation.

| Good | Bad |
| --- | --- |
| `Session not found — check the ID or open the list.` | `Error! Invalid session.` |
| `API key revoked — sign in again to continue.` | `You are unauthorized.` |
| `Read-only role — submitter or admin needed to create a session.` | `Forbidden!` |
| `Request failed — please try again.` | `Oops, something went wrong.` |

**Toast-specific**:

- Error toasts: ≤ 80 characters, end with a period only when the message is a full sentence.
- Success toasts: confirm the *outcome*, not the action — `Template saved` (not `Template was saved successfully`).
- Info toasts: factual only — `Polling every 5 s` (not `Tip: we poll every 5 s`).

---

## Empty States

**Shape**: title (3–5 words) + body (one sentence explaining how to get out of the empty state) + primary CTA.

**Tone**: helpful, not cute. Never apologetic.

| Good | Bad |
| --- | --- |
| `No favorites yet.` / `Star a session to pin it here.` | `Nothing here yet 😢` |
| `No sessions yet.` / `Get started by submitting your first session.` | `Welcome! Why not create something amazing?` |
| `No prompt templates.` / `Save a prompt you reuse to seed future sessions.` | `Templates make your life easier.` |

---

## Placeholders

- Lowercase, no period.
- Hint at the *example shape*, not "type here".
- ≤ 40 characters.
- Use the actual entity name: `e.g., my-bug-fix-task`.

| Good | Bad |
| --- | --- |
| `Search sessions, prompts, owners…` | `Type to search` |
| `e.g., refactor the parser` | `Enter a prompt` |
| `Filter by owner (dashboard-alice)` | `Owner` |

---

## aria-label / title

- **Icon-only buttons** must have an `aria-label` matching the action ("Toggle theme", "Toggle navigation", "Dismiss notification").
- **Decorative icons** must have `aria-hidden="true"` on the icon glyph itself (not the parent).
- `title=` attributes are for **supplementary hover help**, never as the *only* accessible name.

---

## Sentence-End Punctuation

| Context | Punctuation |
| --- | --- |
| Standalone button label | None |
| Sentence in an empty state body | Period |
| Error message (sentence form) | Period |
| Error message (fragment, "Read-only role") | Em-dash continuation OK, period at end |
| Toast | Period only if full sentence |
| Tooltip / `title=` | None |

---

## Forbidden Words & Patterns

Delete on sight:

- `Oops`, `Whoops`, `Uh-oh`
- `Awesome`, `Cool`, `Great`
- `Please` (most of the time — "Please try again" → "Try again")
- `Sorry` (in error messages — replace with cause)
- `successfully` (redundant — "Template saved" not "Template saved successfully")
- `simply`, `easily`, `just` (filler)
- Emoji in UI strings (icons are SVG/Unicode characters with `aria-hidden`)
- LinkedIn-style hype: "thrilled to", "excited to", "the future of"

---

## Forbidden Strings (Locked by Tests)

These exact strings are asserted by tests; **do not rename without
updating the test first**. See [forbidden-strings.txt](#) for the
full list, or grep `tests/integration/test_dashboard*.py`.

Highest-risk renames:

- `Sign in`, `Sessions`, `Queued`, `Running`, `Paused`, `Done`,
  `My Favorites`, `Prompt Templates`, `No favorites yet.`,
  `No comments yet.`, `Submit new session`, `Open in Jaeger`,
  `Skip to main content`, `Forbidden.`
- CSS class names: `cta-primary`, `btn-cta`, `kanban-filters`,
  `owner-badge`, `comment-item`, `comment-form`,
  `role-admin`, `role-viewer`
- IDs: `kanban-chart`, `sessions-tbody`, `*-table`, `batch-toolbar`,
  `comments-list-*`, `comments-ul`, `main-content`, `hx-live`,
  `toast-stack`
- HTMX attributes: `hx-trigger`, `hx-post`, `hx-delete`,
  `hx-ext="json-enc"`, `every 5s`, `kanban:reload from:body`,
  `data-hx-announce`, `data-toast-on-error`

---

## Review Checklist

When you open a PR that touches template strings:

- [ ] No exclamation marks introduced
- [ ] No emoji in displayed strings
- [ ] Buttons are imperative + one noun
- [ ] Errors follow `<cause> + <next action>` shape
- [ ] Empty states have title + body + CTA
- [ ] Placeholders are example-shaped, not instructional
- [ ] Icon-only buttons have `aria-label`
- [ ] No forbidden strings touched without test update
- [ ] Toast text ≤ 80 chars
- [ ] No "Please", "Sorry", "successfully", "simply" leaked through
