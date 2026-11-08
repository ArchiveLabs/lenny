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

- [x] **#5–#9** `lenny/routes/api.py` — the five flagged `except OSError`
      blocks now log server-side (`logger.exception`) and return a fixed message;
      the `error` code field is unchanged. **Fixed the whole class**: all 22
      `except OSError`/`except Exception` handlers in `lenny/routes/` were checked;
      9 interpolated the exception into a response body (the 5 flagged + 4 more at
      api.py:1462/1483/1525/1809 that CodeQL did not flag), all 9 fixed. (commit `d2d64da`)
- [x] **#11** `lenny/routes/oauth.py:139` — comment pins the length-cap-before-regex
      order; regression test in `test_login_hint.py` proven red-first (green → swap
      operands → red, regex saw a 613-char value → restore → green). (commit `53abae8`)
- [x] **#12, #13** `tests/test_external_auth.py`, `tests/test_oauth2_endpoints.py`
      — rewrote both assertions to compare `urlparse(...).hostname` parsed from the
      URL constant instead of a hardcoded host literal. (commit `11a88c7`)

### Verdicts, not fixes (collected for the lead; no UI dismissals here)

- [x] **#1** `external_auth.py:113` — false positive (flow *name*, not a credential)
- [x] **#2, #3** `scripts/oauth2_client.py:229,271` — by design (registration CLI)
- [x] **#4** `ol_bootstrap.py:252` — by design; **confirmed** the documented run path
      (`make ol-login` → `ol_configure.sh`) captures stdout into a shell variable and
      does **not** tee it into a persistent log. No real finding.
- [x] **#10** `oauth2.py:205` — false positive (`description` is caller-controlled, not an exception)
- [x] **#14–#20** `api.py` redirects (479, 539, 548, 575, 580, 638, 693) — **all seven
      read individually**; all false positive (same-origin relative paths / `make_url()`)
- [x] **#21–#24** `oauth.py:290,380`, `oauth2.py:200,1047` — false positive
      (exact-match allowlist / same-origin path validators CodeQL can't follow)

## Definition of done

- [ ] Every one of the 24 alerts carries a verdict in the PR body
- [ ] The five `api.py` stack-trace sites return a fixed message and log the detail
- [ ] #11 has a regression test, proven red against the swapped order
- [ ] #12/#13 resolved deliberately (parsed-host comparison)
- [ ] PR body separates what was verified by RUNNING from what by READING
