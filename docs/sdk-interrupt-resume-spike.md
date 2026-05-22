# SDK Interrupt/Resume Spike Report

*Plan 5 Task 0 — D5.1=C Minimal spike*

- Generated: `2026-05-22T15:10:40.658723+00:00`
- claude-code-sdk: `0.0.25`
- Mode: **mock**
- Outcome: **I-INCONCLUSIVE**

## Scope

Per Plan 5 §6.5 / §7 Task 0, only items (a) and (b) are verified here.

- (a) ``ClaudeSDKClient.interrupt()`` halts the current SDK turn
- (b) A follow-up ``query(...)`` on the same client produces a new ``ResultMessage``

Items (c) long-pause `disconnect()/connect()` and (d) self-interrupt inside 
``can_use_tool`` are explicitly deferred to **Plan 6 Task 0 deep-verify**.

## Outcome classification

| Outcome | Meaning | Plan-6 implication |
|---|---|---|
| `I-OK` | (a) AND (b) both work | Plan 6 implements PAUSED state |
| `I-PARTIAL` | (a) works, (b) fails or drops context | Plan 6 uses soft-pause: cancel + new session with carry-over |
| `I-NONE` | (a) does not halt | Plan 6 removes PAUSED; pre-tool gate only |
| `I-INCONCLUSIVE` | ran in mock fallback | Re-run with real API key before Plan 6 Task 0 |

## Trial results

| # | Mode | Chunks before | Interrupt latency (s) | Chunks after | Resume OK | Error |
|---|---|---|---|---|---|---|
| 1 | mock | 3 | 0.000 | 0 | yes |  |
| 2 | mock | 3 | 0.000 | 0 | yes |  |
| 3 | mock | 3 | 0.000 | 0 | yes |  |
| 4 | mock | 3 | 0.000 | 0 | yes |  |
| 5 | mock | 3 | 0.000 | 0 | yes |  |

## Notes

- (none)

## Environment & how to re-run conclusively

The spike auto-detects environment and falls back to a mock SDK when
no real call is possible. To get a conclusive `I-OK` / `I-PARTIAL`
verdict, run from an account that satisfies *all three*:

1. `ANTHROPIC_API_KEY` exported (or `claude` CLI already logged in via `claude /login`).
2. Non-root shell — the CLI refuses `--dangerously-skip-permissions` under root which crashes the SDK reader; the spike removed that flag to stay portable, but root + interactive permission gating still blocks the prompt loop.
3. Network egress to `api.anthropic.com`.

Re-run::

    SPIKE_MODE=real python scripts/spike_sdk_interrupt_resume.py --trials 3

## Carry-forward to Plan 6 Task 0 deep-verify

- Items (c) `disconnect()/connect()` long-pause behaviour and (d) self-
  interrupt inside `can_use_tool` were *not* exercised here. Plan 6
  Task 0 must cover them before committing to the PAUSED design.
- If Plan 6 Task 0 lands `I-OK` for (a)+(b) but `I-PARTIAL` for (c),
  PAUSED is still viable for short pauses (≤ keep-alive); document
  the bound and require operators to confirm.
- If (d) cannot be made safe, route HITL purely through the host-side
  ToolPolicy + transport coordinator (already the v1 design) — i.e.
  never call `interrupt()` from the can_use_tool callback path.

## Raw JSON

```json
{
  "sdk_version": "0.0.25",
  "mode": "mock",
  "started_at": "2026-05-22T15:10:35.403058+00:00",
  "finished_at": "2026-05-22T15:10:40.658723+00:00",
  "trials": [
    {
      "trial": 1,
      "chunks_before_interrupt": 3,
      "interrupt_latency_s": 7.510185241699219e-06,
      "chunks_after_interrupt": 0,
      "resume_completed": true,
      "resume_text_excerpt": "(mock)",
      "error": null,
      "mode": "mock"
    },
    {
      "trial": 2,
      "chunks_before_interrupt": 3,
      "interrupt_latency_s": 7.957220077514648e-06,
      "chunks_after_interrupt": 0,
      "resume_completed": true,
      "resume_text_excerpt": "(mock)",
      "error": null,
      "mode": "mock"
    },
    {
      "trial": 3,
      "chunks_before_interrupt": 3,
      "interrupt_latency_s": 4.76837158203125e-06,
      "chunks_after_interrupt": 0,
      "resume_completed": true,
      "resume_text_excerpt": "(mock)",
      "error": null,
      "mode": "mock"
    },
    {
      "trial": 4,
      "chunks_before_interrupt": 3,
      "interrupt_latency_s": 5.21540641784668e-06,
      "chunks_after_interrupt": 0,
      "resume_completed": true,
      "resume_text_excerpt": "(mock)",
      "error": null,
      "mode": "mock"
    },
    {
      "trial": 5,
      "chunks_before_interrupt": 3,
      "interrupt_latency_s": 4.157423973083496e-06,
      "chunks_after_interrupt": 0,
      "resume_completed": true,
      "resume_text_excerpt": "(mock)",
      "error": null,
      "mode": "mock"
    }
  ],
  "outcome": "I-INCONCLUSIVE",
  "notes": []
}
```
