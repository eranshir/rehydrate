---
name: restore
description: Restore this Mac from a rehydrate snapshot, with explicit drift warnings and a dry-run-first workflow.
when_to_use: User asks to restore from a rehydrate backup, rehydrate a fresh Mac, or run /rehydrate:restore. Trigger phrases include "restore my machine", "rehydrate this Mac", "/rehydrate:restore".
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/restore-plan.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/restore-apply.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/probe.py *)
  - Bash(ls *)
  - Bash(mkdir *)
  - Read
  - Write
---

<!-- Convention: scripts are referenced via ${CLAUDE_SKILL_DIR}/../../scripts/... -->

## 1. Purpose

This skill orchestrates a full restore from a rehydrate snapshot to a target directory.
It drives `restore-plan.py` and `restore-apply.py`, presents a human-readable plan with
drift warnings before any bytes are written, requires explicit user confirmation at two
checkpoints, and runs a dry-run pass before the live apply. It is always user-initiated;
`disable-model-invocation: true` prevents autonomous triggering.

---

## 2. Required Input from the User

Collect the following before proceeding. Do not assume defaults for items marked required.

| Input | Required | Default | Notes |
|---|---|---|---|
| Snapshot path | Yes | — | Full path to snapshot dir, e.g. `/Volumes/PortableSSD/llm-backup/snapshots/<id>` |
| Target path | No | `$HOME` | Where to write restored files. Non-`$HOME` targets are safer for testing. |
| Allow overwrites | No | No | Whether to allow files that differ on disk to be overwritten. Asked only if the plan contains `overwrite-needs-confirm` actions. |

**Recommendation:** When the user has not specified a target, suggest a sandboxed test
restore to a temp dir (e.g. `$(mktemp -d)`) before any live-home restore. This exercises
the full restore path with no risk. See ADR-003 for rationale.

---

## 3. Pre-flight Checks

Perform all checks before running any script. Fail with a clear message if any check fails.

1. **Snapshot dir exists and contains `manifest.json`**
   ```
   ls <snapshot>/manifest.json
   ```
   If missing: "Snapshot directory does not contain manifest.json. Verify the path."

2. **Object store exists at `<snapshot>/../objects/`**
   ```
   ls <snapshot>/../objects/
   ```
   If missing: "Object store not found at `<snapshot>/../objects/`. The snapshot may be incomplete or the drive may not be fully mounted."

3. **Target directory exists**
   - If target is `$HOME`: it always exists; skip creation check.
   - If target is any other path and does not exist: offer to create it only if it is clearly a temporary or sandbox path (contains `/tmp/`, `/var/folders/`, or was explicitly described by the user as a test dir). Otherwise require the user to create it manually.
   - Never auto-create `$HOME` or any parent of `$HOME`.

4. **Refuse immediately** (before running any script) if:
   - Target is `/` → hard stop, no override possible.
   - Target is `~/Library` or any path inside `~/Library` → hard stop, no override possible.
   - Target is `$HOME` and the user has not yet given the live-home confirmation phrase (see Step 4d below) → block until confirmed.

---

## 4. Execution Steps

Follow these steps in order. Do not skip or reorder them.

### Step 1 — Probe the current machine and load the snapshot manifest

Run probe to capture current machine state:
```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/probe.py
```

Read the snapshot's `manifest.json`:
```bash
Read <snapshot>/manifest.json
```

Extract `source_machine` from the manifest. Compare the following fields side-by-side:

| Field | Snapshot (source) | Current machine | Severity |
|---|---|---|---|
| OS version | `source_machine.os_version` | probe output | Info |
| Architecture | `source_machine.hardware.arch` | probe output | WARN |
| Hostname | `source_machine.hostname` | probe output | Info |
| User | `source_machine.user` | probe output | WARN |

Display the comparison table. Apply the drift severity policy (Section 5) immediately:
- If OS platform is different (e.g. snapshot is Linux, current is macOS): **STOP. Do not proceed.**
- If arch differs: display a prominent WARNING block and wait for the user to type `yes, I understand the arch mismatch` before continuing.
- Other mismatches: display as informational; continue.

### Step 2 — Run restore-plan.py

Generate the plan and write it to a deterministic temp path:
```bash
PLAN_ID=$(python3 -c "import uuid; print(uuid.uuid4().hex[:8])")
PLAN_PATH="/tmp/rehydrate-plan-${PLAN_ID}.json"

python3 ${CLAUDE_SKILL_DIR}/../../scripts/restore-plan.py \
    --snapshot <snapshot> \
    --target <target> \
    --out "${PLAN_PATH}"
```

If the script exits non-zero: show the error output and stop. Do not proceed.

### Step 3 — Present the plan to the user

Read the plan JSON:
```bash
Read ${PLAN_PATH}
```

Display a summary table:

```
Restore plan for snapshot: <snapshot_id>
Target: <target>

Action breakdown:
  create              : <N> files
  skip-identical      : <N> files (already current, no writes)
  overwrite-needs-confirm: <N> files (differ on disk)
  ─────────────────────────────
  total               : <N> files
```

Then, for each action type that has entries, show the first 10 paths:
```
First 10 paths to CREATE:
  - .zshrc
  - .ssh/config
  ...

First 10 paths requiring OVERWRITE CONFIRMATION:
  - .gitconfig
  ...
```

Repeat any drift warnings from Step 1 in this section.

### Step 4 — Explicit user confirmation (Checkpoint 1)

Present the full confirmation prompt. Do not proceed until the user responds affirmatively.

Construct the prompt:

> **Restore confirmation required.**
>
> Restoring snapshot `<snapshot_id>` to `<target>`.
>
> Drift:
> - [list each drift item with kind, source value, current value, and severity]
>
> This will:
> - CREATE `<N>` files
> - SKIP `<N>` identical files (no writes)
> - OVERWRITE `<N>` files (if you approve overwrites below)
>
> **[If target is `$HOME`]:** You are restoring to your live home directory. This is
> irreversible for any files that are overwritten. To proceed, type exactly:
> **`yes, live`**
>
> **[If any `overwrite-needs-confirm` actions exist]:** The plan contains `<N>` files
> that differ on disk. Do you want to allow overwrites? (yes/no)
> If no: the restore will skip these files and only create missing ones.
>
> **[Otherwise]:** Type `yes` to proceed, or anything else to cancel.

Decision logic:
- If target is `$HOME`: require the user to type exactly `yes, live`. Anything else cancels.
- If overwrites are requested: note whether the user approved. Pass `--overwrite` to restore-apply only if approved.
- If the user cancels: stop and instruct them to re-run `/rehydrate:restore` when ready.

### Step 5 — Dry-run apply

Run restore-apply with `--dry-run`:
```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/restore-apply.py \
    --plan "${PLAN_PATH}" \
    --snapshot <snapshot> \
    --target <target> \
    --dry-run \
    [--live]          # include only if target is $HOME \
    [--overwrite]     # include only if user approved overwrites
```

Show the output to the user. If the script exits non-zero: stop and report the error. Do not proceed to the live apply.

### Step 6 — Final confirmation (Checkpoint 2)

> **Dry-run complete.** The above is exactly what will happen during the live apply.
>
> Proceed with the live restore? Type `yes` to apply, anything else to cancel.

If the user cancels: stop and confirm no files were written.

### Step 7 — Live apply

Run restore-apply without `--dry-run`:
```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/restore-apply.py \
    --plan "${PLAN_PATH}" \
    --snapshot <snapshot> \
    --target <target> \
    [--live]          # include only if target is $HOME \
    [--overwrite]     # include only if user approved overwrites
```

Stream output as it runs. The script stops on the first failure; this is expected behavior (see Section 7).

### Step 8 — Final summary

Parse the log output (or re-read if written to a file) for `log_count` lines. Display:

```
Restore complete.
  created    : <N>
  skipped    : <N>
  overwritten: <N>
  failed     : <N>
```

If `failed > 0`: inform the user:
> Some files failed. The target is in a partially-applied state. Re-running
> `/rehydrate:restore` with the same snapshot and target is safe: `restore-plan.py`
> will recompute the plan against the partial target and only attempt what is still
> missing or incomplete.

---

## 5. Drift Severity Policy

| Drift kind | Condition | Action |
|---|---|---|
| `os_version_mismatch` — different OS platform (e.g. Linux vs macOS) | Always | **REFUSE. Hard stop. Wrong-platform snapshot.** |
| `arch_mismatch` | arch differs | **WARN loudly.** Require user to type `yes, I understand the arch mismatch` before continuing. |
| `os_version_mismatch` — same platform, major version differs | e.g. macOS 13 → macOS 15 | Warn. Display prominently. Allow user to continue without special phrase. |
| `os_version_mismatch` — same platform, minor/patch differs | e.g. 15.3 → 15.4 | Informational. Display in drift table. |
| `user_mismatch` | user differs | Informational. Display in drift table. |
| `hostname_mismatch` | hostname differs | Informational. Display in drift table. |

**Detecting platform difference:** If `source_machine.os` (if present) differs from the
current OS, or if the `os_version` string format is clearly from a different platform
(e.g. contains "Linux" or "Windows"), refuse and stop.

---

## 6. Safety Refusals

The following conditions cause an immediate hard stop before any script runs. These
mirror the checks inside `restore-apply.py` but are surfaced to the user earlier.

| Condition | Message |
|---|---|
| Target is `/` | "Refusing to restore to filesystem root. Target must be a user-owned directory." |
| Target is `~/Library` or any path inside it | "Refusing to restore into ~/Library. This directory is system-managed." |
| Target does not exist and cannot be safely auto-created | "Target directory does not exist. Create it first, then re-run /rehydrate:restore." |
| Target is `$HOME` and user has not typed `yes, live` | "Live home restore requires explicit confirmation. See the confirmation step." |

---

## 7. Recovery — Restore Failed Mid-Way

`restore-apply.py` stops immediately on the first failed file and does not roll back.
The target is in a partially-applied state; this is intentional (partial state is
better than no state for large restores).

**Recovery procedure:**
1. Inspect the error output for the failing path.
2. Resolve the underlying cause (permissions, disk full, missing object, etc.).
3. Re-run `/rehydrate:restore` with the same snapshot and target.
4. `restore-plan.py` will recompute the plan: files already restored will appear as
   `skip-identical` and will not be re-written. Only missing or changed files will
   be attempted again.

No manual cleanup is needed before retrying. The plan is always recomputed from the
current state of the target directory, not from a checkpoint file.

---

## 8. Post-restore: Refreshing macOS Preferences (`defaults` category)

If the restored snapshot included the `defaults` category, the plist files in
`~/Library/Preferences/` will have been written to disk, but the macOS preferences
daemon (`cfprefsd`) caches them in memory and may not notice the change immediately.
After a restore that touches any `Library/Preferences/*.plist` file, run
`killall cfprefsd` to force the daemon to reload from disk, or log out and back in.
Apps that were already open (Finder, Dock, System Settings) should be restarted
afterwards so they pick up the refreshed preferences.
