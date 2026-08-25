# Drills — the gate battery, not policy

These are **mechanism-verification fixtures**, built and used during the
engine's live extraction. They are not exemplar policy and they never run on
their own: every drill is `enabled: false` and most are `trigger: manual`.

How they are used: copy the drill(s) you need into a workspace's `automations/`,
fire them manually (`POST /api/automations/{slug}/run` or the scheduler for
the `schedule`-trigger ones), verify the gate they exercise, then **remove
them** — a drill left in a live workspace reads as dormant policy, which is
exactly the confusion it exists to prevent.

| Drill | Gate it exercises |
|---|---|
| `dedup-drill` | worker-side duplicate suppression |
| `inject-drill-empty` | `inject:` tool exits 0 with empty stdout → loud abort |
| `inject-drill-idle` | `INJECT_IDLE` sentinel → skip, run proceeds |
| `reply-drill-alpha` / `-bravo` | session continuity + reply routing across migrations |
| `runid-drill-alpha` / `-bravo` | same-second double fire → two run ids, two notifications |
| `seam-drill-deliver` | intent written while worker stopped → delivered on restart |
| `seam-drill-demote` | `urgent-only` demotion gate |
| `seam-drill-withhold` | `NOTHING_TO_REPORT` sentinel → no push |
| `step3-tick-drill` | scheduler liveness after a process split |
| `b4-namespace-drill` | turn prefix + dual-emitted session-id env vars |

Consumer note: the seam drills and `inject-drill-idle` reference a
`ledger-items` inject tool — a stand-in for a consumer's own ledger tool,
the original `inject:` exemplar. A consumer without that tool adapts the
`inject:` argv to any tool honoring the same contract (stdout text,
`INJECT_IDLE`, or non-zero exit); the engine behavior under test is identical.
