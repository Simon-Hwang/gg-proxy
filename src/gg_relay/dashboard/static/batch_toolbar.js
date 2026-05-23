/* Plan 8 Task 10 / D8.6 — batch selection + toolbar dispatch.
 *
 * Vanilla JS (no jQuery / no framework). Maintains an in-memory
 * ``Set<sessionId>`` of selected kanban cards. The dashboard cookie
 * middleware (``DashboardCookieMiddleware``) injects the synthetic
 * ``X-API-Key`` for ``/api/v1/*`` mutations, so ``fetch`` only needs
 * ``credentials: 'same-origin'`` to carry the session cookie.
 *
 * Reload strategy: on a successful batch we dispatch a custom
 * ``kanban:reload`` event on ``<body>``. Any element with
 * ``hx-trigger="kanban:reload from:body"`` will refresh; if no such
 * listener is wired (e.g. polling-only mode), we fall back to a hard
 * ``window.location.reload()`` so the operator always sees the
 * post-action state without an extra wait for the 5s poll.
 */
(function () {
    'use strict';

    var selected = new Set();
    var lastClickedIdx = -1;

    function $(id) { return document.getElementById(id); }
    function getToolbar() { return $('batch-toolbar'); }
    function getCountEl() { return $('batch-count'); }
    function getResultEl() { return $('batch-result'); }

    function getAllCards() {
        return Array.prototype.slice.call(
            document.querySelectorAll('.kanban-card')
        );
    }

    function updateUI() {
        var toolbar = getToolbar();
        if (!toolbar) return;
        var count = selected.size;
        var cnt = getCountEl();
        if (cnt) cnt.textContent = String(count);
        toolbar.dataset.selectedCount = String(count);
        toolbar.classList.toggle('hidden', count === 0);

        var ids = ['btn-batch-cancel', 'btn-batch-retry', 'btn-batch-clear'];
        for (var i = 0; i < ids.length; i++) {
            var b = $(ids[i]);
            if (b) b.disabled = (count === 0);
        }
    }

    function setSelected(sid, isSelected, cardEl) {
        if (isSelected) {
            selected.add(sid);
        } else {
            selected.delete(sid);
        }
        if (cardEl) cardEl.classList.toggle('selected', isSelected);
        updateUI();
    }

    function clearSelection() {
        selected.forEach(function (sid) {
            var cb = document.querySelector(
                '.bulk-select[data-session-id="' + cssEscape(sid) + '"]'
            );
            if (cb) cb.checked = false;
            var card = document.querySelector(
                '.kanban-card[data-session-id="' + cssEscape(sid) + '"]'
            );
            if (card) card.classList.remove('selected');
        });
        selected.clear();
        lastClickedIdx = -1;
        updateUI();
    }

    /* Minimal CSS.escape polyfill — modern browsers support it, but
     * we still vendor a tiny replacement so attribute selectors built
     * from session ids (which are URL-safe but not necessarily CSS-
     * identifier-safe) don't blow up on the older runtimes some
     * operators are stuck on. */
    function cssEscape(str) {
        if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
            return CSS.escape(str);
        }
        return String(str).replace(/["\\]/g, '\\$&');
    }

    /* Checkbox click delegation. Handles both plain toggle and
     * shift-click range select. We bind on the body so the listener
     * survives HTMX swaps (kanban board re-renders on every 5s poll
     * + SSE update); the in-memory Set persists across swaps but
     * any selected cards that disappear from the new DOM simply lose
     * their visual highlight (the Set still holds the id until the
     * next dispatch / clear). */
    document.addEventListener('change', function (e) {
        var cb = e.target;
        if (!cb || !cb.classList || !cb.classList.contains('bulk-select')) {
            return;
        }
        var sid = cb.dataset.sessionId;
        var card = cb.closest('.kanban-card');
        var allCards = getAllCards();
        var idx = card ? allCards.indexOf(card) : -1;

        /* Shift-click range select: extend from the last clicked
         * checkbox to the current one, inclusive. Range always
         * *sets* checked=true regardless of the toggled state, which
         * mirrors GitHub / Gmail behaviour. */
        if (e.target instanceof HTMLInputElement && cb.dataset.shiftRange === '1') {
            // handled by click handler below
            return;
        }
        setSelected(sid, cb.checked, card);
        lastClickedIdx = idx;
    });

    /* Shift-click handler. Runs on ``click`` (before ``change`` so
     * we can decide whether to treat it as a range action) but we
     * only intercept when the shift key is held — otherwise the
     * native ``change`` handler above does the work. */
    document.addEventListener('click', function (e) {
        var cb = e.target.closest && e.target.closest('.bulk-select');
        if (!cb) return;
        if (!e.shiftKey || lastClickedIdx < 0) return;

        var allCards = getAllCards();
        var card = cb.closest('.kanban-card');
        var idx = card ? allCards.indexOf(card) : -1;
        if (idx < 0) return;

        var start = Math.min(idx, lastClickedIdx);
        var end = Math.max(idx, lastClickedIdx);
        for (var i = start; i <= end; i++) {
            var otherCard = allCards[i];
            var otherCb = otherCard.querySelector('.bulk-select');
            if (otherCb) {
                otherCb.checked = true;
                setSelected(
                    otherCb.dataset.sessionId,
                    true,
                    otherCard
                );
            }
        }
        lastClickedIdx = idx;
    });

    /* Toolbar button delegation. Wired on the body so the toolbar
     * partial can be moved between pages without rebinding. */
    document.addEventListener('click', function (e) {
        var t = e.target;
        if (!t || !t.id) return;
        if (t.id === 'btn-batch-cancel') {
            dispatchBatch('cancel');
        } else if (t.id === 'btn-batch-retry') {
            dispatchBatch('retry');
        } else if (t.id === 'btn-batch-clear') {
            clearSelection();
        }
    });

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function dispatchBatch(action) {
        if (selected.size === 0) return;
        /* > 5 ids on cancel → confirm. Retry creates new sessions
         * (non-destructive — original rows untouched), so the
         * confirm gate only fires for cancel. */
        if (action === 'cancel' && selected.size > 5) {
            var msg = 'Cancel ' + selected.size +
                ' sessions? This cannot be undone.';
            // eslint-disable-next-line no-alert
            if (!confirm(msg)) return;
        }
        var ids = Array.from(selected);
        var result = getResultEl();
        if (result) {
            result.innerHTML = '<span class="pending">Dispatching ' +
                ids.length + ' ' + escapeHtml(action) + '…</span>';
        }
        fetch('/api/v1/sessions/batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'same-origin',
            body: JSON.stringify({
                ids: ids,
                action: action,
                reason: 'dashboard_batch_' + action
            })
        }).then(function (resp) {
            if (!resp.ok) {
                return resp.text().then(function (text) {
                    if (result) {
                        result.innerHTML = '<span class="error">HTTP ' +
                            resp.status + ': ' + escapeHtml(text) +
                            '</span>';
                    }
                    return null;
                });
            }
            return resp.json();
        }).then(function (data) {
            if (data === null || data === undefined) return;
            renderResult(data);
            clearSelection();
            triggerReload();
        }).catch(function (err) {
            if (result) {
                result.innerHTML = '<span class="error">Network error: ' +
                    escapeHtml(err && err.message ? err.message : err) +
                    '</span>';
            }
        });
    }

    function renderResult(data) {
        var result = getResultEl();
        if (!result) return;
        var summary = data.summary || {};
        var ok = summary.ok || 0;
        var err = summary.error || 0;
        var items = data.items || [];
        var errors = items.filter(function (i) {
            return i.status === 'error';
        });
        var html = '<span class="success">' + ok + ' ok</span>';
        if (err > 0) {
            html += ', <span class="error">' + err + ' failed</span>';
            html += '<ul class="batch-errors">';
            errors.slice(0, 10).forEach(function (e) {
                html += '<li><code>' + escapeHtml(e.id) +
                    '</code> — ' + escapeHtml(e.error_code || 'error') +
                    ': ' + escapeHtml(e.error_message || '') +
                    '</li>';
            });
            if (errors.length > 10) {
                html += '<li>…and ' + (errors.length - 10) +
                    ' more</li>';
            }
            html += '</ul>';
        }
        result.innerHTML = html;
    }

    function triggerReload() {
        /* HTMX trigger fires only when something on the page actually
         * listens for ``kanban:reload from:body``. If HTMX is loaded
         * AND any element has registered for the trigger, we use it;
         * otherwise fall back to a hard navigation reload so the
         * operator always sees the post-batch state. */
        if (typeof window.htmx !== 'undefined' && window.htmx.trigger) {
            try {
                window.htmx.trigger(document.body, 'kanban:reload');
                return;
            } catch (e) {
                /* swallow — fall through to hard reload */
            }
        }
        if (typeof window.location !== 'undefined' &&
                typeof window.location.reload === 'function') {
            window.location.reload();
        }
    }

    /* Initial UI pass — toolbar starts hidden because count === 0. */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', updateUI);
    } else {
        updateUI();
    }
})();
