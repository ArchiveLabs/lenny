# CodeQL alert cleanup — issue #266

CodeQL default setup was enabled 2026-11-06. Its first scan (commit `bd6229e5`)
produced **24 open alerts** (7 high, 17 medium). Triage (#266) concluded that none
of the 7 high-rated alerts is a live vulnerability. This PR is the cleanup: it lands
the handful of genuine one-line defects, pins the one already-mitigated hotspot with
a regression test, and records a verdict for every remaining alert so the
code-scanning page becomes signal rather than backlog.

Dismissal in the GitHub UI is **not** done here — that is a posture decision and
belongs to Mek. This PR collects the verdicts; Mek runs the dismissals.

## Plan (checklist)

### Real code fixes

- [ ] **#5–#9** `lenny/routes/api.py` (lines ~1607, 1648, 1863, 2083, 2191) —
      five `except OSError as exc:` blocks return `f"...: {exc}"` to the caller,
      leaking a filesystem path. Log the exception server-side (`logger.exception`),
      return a fixed message, keep the `error` code field unchanged.
- [ ] **#11** `lenny/routes/oauth.py:139` — `len(value) > 254 or not _EMAIL_HINT.match(value)`
      is safe only because Python short-circuits the length cap before the regex.
      Add a comment pinning the order, and a regression test that fails if the order
      is swapped (proven red-first).
- [ ] **#12, #13** `tests/test_external_auth.py:848`, `tests/test_oauth2_endpoints.py:787`
      — substring checks flagged by `py/incomplete-url-substring-sanitization`. Rewrite
      to compare a host parsed from the known URL constant instead of a hardcoded
      host literal.

### Verdicts, not fixes (recorded in the PR body)

- [ ] **#1** `external_auth.py:113` — false positive (flow *name*, not a credential)
- [ ] **#2, #3** `scripts/oauth2_client.py:229,271` — by design (registration CLI)
- [ ] **#4** `ol_bootstrap.py:252` — by design, **but first confirm** the documented
      run path does not tee stdout into a persistent log (if it does: stop, tell Mek)
- [ ] **#10** `oauth2.py:205` — false positive (`description` is caller-controlled, not an exception)
- [ ] **#14–#20** `api.py` redirects (479, 539, 548, 575, 580, 638, 693) — **read all seven**
      individually, one verdict each
- [ ] **#21–#24** `oauth2.py:200,1047`, `oauth.py:290,380` — false positive (exact-match allowlist)

## Definition of done

- [ ] Every one of the 24 alerts carries a verdict in the PR body
- [ ] The five `api.py` stack-trace sites return a fixed message and log the detail
- [ ] #11 has a regression test, proven red against the swapped order
- [ ] #12/#13 resolved deliberately (parsed-host comparison)
- [ ] PR body separates what was verified by RUNNING from what by READING
