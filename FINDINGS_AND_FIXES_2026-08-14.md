# Findings, Fixes & Updated Settings — 2026-08-14

**Author:** Claude (Cowork) · **For review with:** Hermes + operator (Alex)
**Repo:** private GitHub repo managed by Hermes · working copy at
`E:\HermesOutput\buzz-hermes-harness` (WSL: `/mnt/e/HermesOutput/buzz-hermes-harness`)
**Status:** bridge live; full loop proven end-to-end against real files.

---

## TL;DR

We stood up a **chat → harness → Hermes** bridge and ran live smoke tests. Three
real bugs surfaced and were fixed. The most important: the harness used to grade
success on the model's *text*, so it reported `done` for a job that produced **no
file**. Success is now graded on **real files on disk**, and a live `env-report`
job now writes a genuine artifact to `E:\TEMP\HermesTemp\projects\env-report.md`,
verified by the harness. All changes are in the working copy and need a review +
commit to the private repo (see §6).

---

## 1. What is now proven working

| Check | Result |
|-------|--------|
| Bridge loop: chat drops job → watcher runs it → report returned | ✅ |
| Signed dispatch + audit trail (queen + worker keypairs, Schnorr, timestamps) | ✅ |
| 5-turn governor: stops at exactly 5 turns, checkpoints as `needs_review` | ✅ (observed: 4×continue → checkpoint) |
| Ground-truth success: real file required on disk, narration can't fake it | ✅ (verified 3 ways offline + live) |
| Live `env-report` job | ✅ wrote real `env-report.md` (355 B) to the projects dir |
| Problems surfaced automatically (`PROBLEM_*` reports) | ✅ |

Audit excerpt from the successful run (channel `9955a8b2-…`): signed `task` (queen)
→ `progress`/`result` `done` (worker-1), with the file present on disk afterward.

---

## 2. Findings (discovered during live testing)

**F1 — Service PATH missing `~/.local/bin` → Hermes not found.**
Under the systemd service the model call died instantly (`[Errno 2] No such file or
directory: 'hermes'`, 0 turns). `hermes` lives at `/home/dizcofuz/.local/bin/hermes`,
which is on the interactive shell PATH but not the service PATH. **Fixed** (§3).

**F2 — Success was graded on the model's reply, not the filesystem (critical).**
The first `env-report` run returned `done` in 1 turn, but `E:\TEMP\HermesTemp\projects`
was **empty**. The criteria (`contains`/`regex`) matched strings in Hermes's *text*
("env-report.md", "# Environment Report", "STATUS: done") even though no file was
created. A model that only *narrates* the work passed. **Fixed** with ground-truth
file criteria (§3).

**F3 — Hermes runs its tools in its own home dir, not the job workdir.**
The environment report Hermes generated listed its Current Directory as
`/mnt/c/Users/DizcoFuz` — not the workdir we pass to the subprocess. So relative-path
writes never reached the projects folder. **Fixed** by having jobs specify **absolute
paths** and pre-creating the workdir (§3). Confirmed: with an absolute target path the
file landed correctly.

**Historical note.** The very first `hello-buzz` attempt failed with
`No LLM provider configured` and succeeded on retry — a one-time provider-wiring race,
already resolved. Left visible in the audit trail for completeness.

---

## 3. Fixes completed

**F1 — PATH.** Added `bridge/watcher.env` (loaded by the service via
`EnvironmentFile`) that spells out a full PATH including `~/.local/bin` (hermes) and
`~/.cargo/bin` (buzz-cli). Requires a `systemctl restart hermes-bridge` to load
(done during the session).

**F2 — Ground-truth grading.** Added three filesystem criteria to `governor.py`,
evaluated against the task's real workdir (the governor runs in the worker process,
which has filesystem access):

| kind | fields | passes when |
|------|--------|-------------|
| `file_exists` | `value`: relative path | file exists and is non-empty |
| `file_contains` | `path`: file, `value`: substring | substring is in the file |
| `file_regex` | `path`: file, `value`: regex | regex matches file contents |

`TaskState` gained a `workdir` field; `worker.py` passes the job's workdir through.
Text criteria (`contains`/`regex`/`json_path_eq`) still exist but should be treated
as secondary evidence, not the gate.

**F3 — Absolute paths + workdir creation.** `worker.py` now `os.makedirs(workdir)`
before the first turn, the worker prompt explicitly instructs Hermes to *use its
tools to write real files* (not narrate), and all starter jobs name **absolute
paths** for what they write.

**Bonus — diagnosable failure reports.** `bridge/watcher.py` now pulls the `error`
field from signed worker events into a **"Failure detail"** section of the report,
so a failed job explains itself (this is how F1 and F3 were pinned down quickly).

---

## 4. Files changed / added this session

Reconciliation decision earlier in the session: **Hermes's tested versions of
`hermes_adapter.py`, `check_setup.py`, and the setup docs were kept as-is** (not
overwritten). The changes below are additive or in files Hermes had not modified.

**Modified**

| File | Change |
|------|--------|
| `harness/governor.py` | Added `file_exists` / `file_contains` / `file_regex` criteria; `TaskState.workdir` |
| `harness/worker.py` | Thread workdir into `TaskState`; `os.makedirs(workdir)`; prompt insists on real tool use; `import os` |
| `jobs.example.json` | Rewritten to self-contained jobs, then to **ground-truth criteria + absolute paths** |

**Added**

| File | Purpose |
|------|---------|
| `.gitignore` | Excludes secrets (`keys.json`, `.env`, `deploy/**/.env`) and bridge runtime files |
| `bridge/watcher.py` | The chat↔harness watcher daemon |
| `bridge/README.md` | How the bridge works + how to start it |
| `bridge/hermes-bridge.service` | systemd unit (auto-start) |
| `bridge/start-watcher.sh` | Foreground/nohup start script |
| `bridge/watcher.env.example` | Template for the service env |
| `bridge/watcher.env` | **PATH fix** (gitignored — regenerate from example on other hosts) |
| `bridge/{inbox,outbox,processing,done}/` | Runtime queues (contents gitignored) |
| `FINDINGS_AND_FIXES_2026-08-14.md` | This document |

**Unchanged on purpose:** `harness/hermes_adapter.py`, `harness/check_setup.py`,
`harness/buzz_bus.py`, `harness/orchestrator.py`, `harness/run_worker.py`,
`harness/gen_keys.py`, `harness/test_smoke.py`, `README.md`, `HERMES_SETUP.md`.

---

## 5. Settings that matter (confirmed on this box)

| Setting | Value / note |
|---------|--------------|
| Relay | `https://buzz.bhue.org` (buzz-cli REST — http/https, **not** wss) |
| Relay mode | Closed: `BUZZ_REQUIRE_AUTH_TOKEN` + `BUZZ_REQUIRE_RELAY_MEMBERSHIP=true` |
| Membership | queen = owner; worker-1..4 = members (already authorized) |
| Model string | `deepseek/deepseek-v4-flash-0731` — **must be provider-qualified** (bare form → "No LLM provider configured") |
| Hermes one-shot | `hermes -m {model} -z {prompt}` (top-level `-z`; prompt inline) |
| Hermes `terminal.backend` | `local` |
| **Hermes tool cwd** | Defaults to `/mnt/c/Users/DizcoFuz` — **jobs must use absolute paths** (or set `hermes config set terminal.cwd …`) |
| `hermes` binary | `/home/dizcofuz/.local/bin/hermes` (must be on the service PATH) |
| `buzz` binary | `~/.cargo/bin/buzz` |
| Governance | `max_turns: 5` (hard cap — do not raise without operator approval) |
| Workdir convention | `/mnt/e/TEMP/HermesTemp/projects` (Windows `E:\TEMP\HermesTemp\projects`) |

---

## 6. Action for Hermes — review & commit to the private repo

The working copy has uncommitted changes (a `.git` was initialized locally but the
commit must be finished natively from WSL, since the Windows mount blocks git's file
ops from the Cowork sandbox). Please review the diff and commit/push:

```bash
cd /mnt/e/HermesOutput/buzz-hermes-harness
rm -f .git/index.lock                 # clear a stale lock left by the sandbox
git status                            # review — confirm keys.json/.env are NOT listed
git add -A
git commit -m "Bridge + ground-truth criteria + workdir/abs-path fixes (2026-08-14)"
git push                              # to the private repo you manage
```

Before pushing, sanity-check that secrets are ignored:

```bash
git check-ignore keys.json deploy/compose/.env bridge/watcher.env   # all should print
```

---

## 7. Recommendations / next

- **Job convention:** always give Hermes **absolute paths** for files it must create,
  and gate on `file_exists`/`file_contains` (not just text). This is now the default
  for the starter jobs.
- **Optional hardening:** `hermes config set terminal.cwd /mnt/e/TEMP/HermesTemp/projects`
  to make relative paths land in the projects dir too (absolute paths remain the more
  precise approach).
- **Next smoke test:** `fizzbuzz-tested` — exercises writing code + running pytest,
  gated on the files existing *and* the tests passing.
- **Reporting:** `PROBLEM_*` reports now include a Failure-detail section; keep the
  watcher's improved version.
