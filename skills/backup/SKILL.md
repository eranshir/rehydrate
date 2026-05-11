---
name: backup
description: Capture this Mac's user state into a self-describing snapshot that a future LLM can restore.
when_to_use: User asks to back up their Mac, capture machine state to an external drive, or run rehydrate backup. Trigger phrases include "back up my machine", "create a rehydrate snapshot", "/rehydrate:backup".
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/probe.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-packages.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-apps.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-repos.py *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../scripts/snapshot.py *)
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(df *)
  - Bash(hostname *)
  - Bash(date *)
  - Bash(du *)
  - Read
  - Write
---

## Purpose

This skill orchestrates a full rehydrate backup: it probes the source machine,
walks each enabled category of files, hands the results to `snapshot.py` to
copy objects into content-addressed storage, and writes a validated
`manifest.json` plus a narrative `RESTORE-GUIDE.md` to the backup drive.

Invoke it with `/rehydrate:backup`. The user must provide a backup drive path.
Use it whenever a user wants to create a rehydrate snapshot.

---

## Required input from the user

Ask the user for these before proceeding:

1. **Drive path** — absolute path to the mounted backup drive (e.g. `/Volumes/PortableSSD`).
   - Confirm the path exists and is writable.
   - Refuse if the path is `/`, `/System`, `/private`, `/usr`, `/etc`, `/var`, or any path that does not begin with `/Volumes` or `~/` (relative drives are not allowed).
2. **Snapshot label** — optional short label to append to the snapshot ID (e.g. `pre-upgrade`). Alphanumeric plus hyphens only. If omitted, no label is appended.

---

## Pre-flight checks

Run these checks before executing any backup step. Stop and report to the user if any check fails.

1. **Drive exists and is writable**
   ```bash
   ls <drive_path>
   ```
   Fail if the path does not exist or the `ls` command fails.

2. **At least 500 MB free on the drive**
   ```bash
   df -k <drive_path>
   ```
   Parse the `Available` column (kilobytes). Fail if `Available * 1024 < 524288000` (500 MB).

3. **`$HOME` is set and looks like a user home**
   Verify `$HOME` is set and does not equal `/`, `/root`, `/private`, `/System`, `/var`, or any system path.
   If `$HOME` is unset or looks like a system path, stop and tell the user.

4. **macOS only**
   Verify the OS is macOS (`uname -s` returns `Darwin`). If not, stop: rehydrate v0.1 supports macOS only.

---

## Execution steps

Follow these steps in order. Do not skip steps or change the order.

### Step 1 — Generate snapshot ID

```bash
SNAPSHOT_HOSTNAME=$(hostname -s)
SNAPSHOT_TS=$(date -u +%Y%m%dT%H%M%SZ)
```

Compose the ID:
- Without label: `<SNAPSHOT_HOSTNAME>-<SNAPSHOT_TS>`
- With label: `<SNAPSHOT_HOSTNAME>-<SNAPSHOT_TS>-<label>`

Call this value `SNAPSHOT_ID`.

Set the snapshot directory path:
```
SNAPSHOT_DIR=<drive_path>/llm-backup/snapshots/<SNAPSHOT_ID>
```

Create the directory:
```bash
mkdir -p <drive_path>/llm-backup/snapshots/<SNAPSHOT_ID>
```

### Step 2 — Run probe.py

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/probe.py \
  --out <SNAPSHOT_DIR>/probe.json
```

Verify `probe.json` was written. If the command exits non-zero, stop and report the error to the user.

### Step 3 — Walk enabled categories

#### Strategy dispatch table

Before walking, read `${CLAUDE_SKILL_DIR}/../../categories.yaml`. For each
category with `enabled: true`, consult its `strategy` field and invoke the
matching walker:

| strategy      | walker script                                        |
|---|---|
| file-list     | scripts/walk.py                                      |
| package-list  | scripts/walk-packages.py                             |
| app-list      | scripts/walk-apps.py                                 |
| repo-list     | scripts/walk-repos.py                                |
| full-snapshot | (added in #20)                                       |

#### Per-category walk commands

For each enabled category, branch on `strategy`:

**strategy: file-list**

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk.py \
  --category <name> \
  --out <SNAPSHOT_DIR>/walk-<name>.json
```

**strategy: package-list**

Package walkers write virtual files into a dedicated workdir, not into `$HOME`.
Choose a stable workdir path per category (e.g. under `<SNAPSHOT_DIR>/workdirs/<name>/`):

```bash
mkdir -p <SNAPSHOT_DIR>/workdirs/<name>

python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-packages.py \
  --out <SNAPSHOT_DIR>/walk-<name>.json \
  --workdir <SNAPSHOT_DIR>/workdirs/<name>
```

The JSON output's `workdir` field will confirm the path. Pass it as `--home`
to `snapshot.py` in Step 4 so the virtual paths resolve correctly against the
workdir rather than `$HOME`.

**strategy: app-list**

App walkers write a single inventory file into a dedicated workdir, not into `$HOME`.
Choose a stable workdir path per category (e.g. under `<SNAPSHOT_DIR>/workdirs/<name>/`):

```bash
mkdir -p <SNAPSHOT_DIR>/workdirs/<name>

python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-apps.py \
  --out <SNAPSHOT_DIR>/walk-<name>.json \
  --workdir <SNAPSHOT_DIR>/workdirs/<name>
```

The JSON output's `workdir` field will confirm the path. Pass it as `--home`
to `snapshot.py` in Step 4 so the virtual paths resolve correctly against the
workdir rather than `$HOME`.

**strategy: repo-list**

Repo walkers write an inventory file plus any captured secret files into a
dedicated workdir, not into `$HOME`. Choose a stable workdir path per category
(e.g. under `<SNAPSHOT_DIR>/workdirs/<name>/`):

```bash
mkdir -p <SNAPSHOT_DIR>/workdirs/<name>

python3 ${CLAUDE_SKILL_DIR}/../../scripts/walk-repos.py \
  --out <SNAPSHOT_DIR>/walk-<name>.json \
  --workdir <SNAPSHOT_DIR>/workdirs/<name>
```

The JSON output's `workdir` field will confirm the path. Pass it as `--home`
to `snapshot.py` in Step 4 so the virtual paths resolve correctly against the
workdir rather than `$HOME`.

**strategy: full-snapshot**

No walker is implemented yet. Warn the user and skip this category:
> "Skipping category '<name>' (strategy: full-snapshot) — walker not implemented yet (see issue #20)."

#### Verify walk output

After each walker invocation, verify `walk-<name>.json` was written. If the
command exits non-zero, stop and report the error.

If you need to enumerate enabled categories dynamically, read
`${CLAUDE_SKILL_DIR}/../../categories.yaml` and walk only those with `enabled: true`.

### Step 4 — Run snapshot.py

For each enabled category that was walked, pass `--walk-output` and `--category`
to `snapshot.py`. For `package-list`, `app-list`, and `repo-list` categories,
also pass `--home <workdir>` (use the `workdir` from the walk JSON); for all
other strategies pass `--home $HOME`.

```bash
python3 ${CLAUDE_SKILL_DIR}/../../scripts/snapshot.py \
  --walk-output <SNAPSHOT_DIR>/walk-dotfiles.json \
  --category dotfiles \
  --walk-output <SNAPSHOT_DIR>/walk-package-managers.json \
  --category package-managers \
  --home <SNAPSHOT_DIR>/workdirs/package-managers \
  --walk-output <SNAPSHOT_DIR>/walk-app-inventory.json \
  --category app-inventory \
  --home <SNAPSHOT_DIR>/workdirs/app-inventory \
  --probe-output <SNAPSHOT_DIR>/probe.json \
  --drive <drive_path>/llm-backup \
  --snapshot-id <SNAPSHOT_ID>
```

(The example above shows `dotfiles` + `package-managers` + `app-inventory`. Repeat or omit pairs
as determined by which categories are enabled.) If the command exits non-zero,
stop and report the error.

After this step the following files exist on the drive:
- `<SNAPSHOT_DIR>/manifest.json` — validated manifest
- `<SNAPSHOT_DIR>/parent.txt` — parent snapshot ID (or "none")
- `<drive_path>/llm-backup/objects/…` — content-addressed object store

### Step 5 — Generate RESTORE-GUIDE.md

Read the produced manifest:

```
Read <SNAPSHOT_DIR>/manifest.json
```

Read the template at:

```
Read ${CLAUDE_SKILL_DIR}/RESTORE-GUIDE.md.template
```

Substitute all `{placeholder}` values using the data from `manifest.json` and
the table below. Then write the result to `<SNAPSHOT_DIR>/RESTORE-GUIDE.md`.

| Placeholder | Source |
|---|---|
| `{snapshot_id}` | `manifest.snapshot_id` |
| `{created_at}` | `manifest.created_at` |
| `{drive_path}` | the drive path provided by the user |
| `{os}` | `manifest.source_machine.os` |
| `{os_version}` | `manifest.source_machine.os_version` |
| `{build}` | `manifest.source_machine.build` |
| `{hostname}` | `manifest.source_machine.hostname` |
| `{user}` | `manifest.source_machine.user` |
| `{arch}` | `manifest.source_machine.hardware.arch` |
| `{model}` | `manifest.source_machine.hardware.model` |
| `{categories_table}` | Build a Markdown table (see below) |

**Building `{categories_table}`:** For each category in `manifest.categories`,
produce one table row with: category name, file count
(`len(manifest.categories[name].files)`), and total bytes
(sum of `size` for all file entries). Format:

```
| Category | Files | Total bytes |
|---|---|---|
| dotfiles | 12 | 48302 |
```

### Step 6 — Report to user

After all steps succeed, report:

1. Full snapshot path: `<SNAPSHOT_DIR>`
2. Per-category file counts (from manifest)
3. Coverage summary: any skipped files (from `walk-*.json` coverage.skipped array)
4. Total size on drive:
   ```bash
   du -sh <drive_path>/llm-backup
   ```

---

## Safety / refusals

The skill MUST refuse and stop (before writing anything) if:

- `DRIVE_PATH` is `/`, `/System`, `/private`, `/usr`, `/etc`, `/var`, or any known system root.
- `DRIVE_PATH` does not begin with `/Volumes/` and is not a clearly external path. Ask the user to confirm if ambiguous.
- `$HOME` is unset, empty, or resolves to a system path (`/`, `/root`, `/private`).
- The OS is not macOS (Darwin).
- Less than 500 MB is free on the drive.
- The snapshot directory already exists (would indicate a collision; stop and tell the user).

The skill MUST ask for explicit confirmation before proceeding if the walk
output indicates the total file size will exceed 1 GB. Present the estimated
size and wait for "yes" or "proceed" before running snapshot.py.

---

## Recovery — interrupted backup

If the backup is interrupted mid-run:

- `probe.json` and `walk-*.json` in `<SNAPSHOT_DIR>` are partial intermediates. They are safe to delete.
- `manifest.json` is written atomically by `snapshot.py` via a `.tmp` rename. If present, it is complete.
- Objects in `<drive_path>/llm-backup/objects/` written so far are valid and will be deduped on the next run.
- To retry: delete `<SNAPSHOT_DIR>` entirely and run `/rehydrate:backup` again. The object store preserves any objects already copied, so the retry is fast.
- Do not attempt to resume a partial snapshot. `snapshot.py` refuses to write into an existing snapshot directory.
