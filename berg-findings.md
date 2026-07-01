# Berg fork — field findings & triage

Fork-local tracking of issues found in real runs (GitHub Issues are disabled on this fork).
Upstream PRs are Berg's button; this file is the issue-first record the PRs reference.

## 2026-07-01 — from the gitlab-fde-intel-c7dcdc run (research-agent)

### 1. `looks_like_junk()` false-positives on any page containing "cloudflare" — FIXED
- **Symptom:** the junk-content heuristic flagged every page whose body contained the bare word
  "cloudflare" as a Cloudflare bot-challenge (it collided with its own `cf_signals` block). Hit ~4
  fetch batches in the run; worked around with curl/pandoc.
- **Fix:** removed the bare `"cloudflare"` token from `cf_signals` in `web/base.py`; the remaining
  signals ("just a moment", "checking your browser", "ray id", "attention required", …) still catch
  real challenge pages. Regression tests in `tests/test_web/test_junk_filter.py`.
- **Status:** fixed on `berg/build` (also the standalone `berg/vault-env-cf-fixes`). **Clean upstream
  PR candidate** (`fix(web):`). Awaiting Berg's submit.

### 2. Vault resolution leaks a run into the parent `~/tapestry/research` vault — FIXED (with caveat)
- **Symptom:** a fetcher and a depth-investigator resolved the parent `~/tapestry/research` vault
  instead of the run's nested vault; ~13 notes misfiled into the main vault. `Vault.discover()` walks
  up to the first `.hyperresearch/` marker, so cwd/marker ambiguity decides the target silently.
- **Fix:** added the `HYPERRESEARCH_VAULT` env override to `discover()` — pins the vault root
  explicitly and cwd-independently, beating any stray marker. Wired into the fleet role env.
- **Caveat (still true):** `HYPERRESEARCH_VAULT` binds at LAUNCH. A pre-existing / resumed session
  does not pick it up, so a long run started before the fix keeps resolving by walk-up. Fresh launches
  are correct. A deeper fix (explicit per-run vault handle threaded through subagents) would remove the
  walk-up ambiguity entirely.
- **Status:** fixed on `berg/build`. Env-override is an upstream candidate; the per-run-handle
  redesign is a larger proposal.

### 3. `polish-auditor` subagent times out (~57 min, 0 edits) — OPEN
- **Symptom:** the Layer-7 polish-auditor subagent ran ~57 minutes and produced 0 edits before timing
  out; the run's gates were completed manually and all 4 lint checks passed.
- **Likely cause:** the auditor is Opus, Read+Edit tool-locked, pointed at a large (~10k-word) final
  report; it appears to stall rather than converge. This is a pipeline/skill robustness issue (the
  `hyperresearch-15-polish` step / `hyperresearch-polish-auditor` agent), not a CLI bug.
- **Proposed direction:** add a hard wall-clock budget + a "no-op is acceptable" fast exit to the
  polish auditor; chunk very large drafts; or make the step skip cleanly when the draft already passes
  lint. Needs repro on a large report.
- **Status:** open. Skill-side (plugin), not the CLI. Filed for follow-up.
