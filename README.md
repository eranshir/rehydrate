# rehydrate

> LLM-powered backup and restore for your Mac. Stores knowledge, not bytes.

Traditional backups store every byte. **rehydrate** stores the knowledge that the bytes can be
re-derived from. Apps, packages, runtimes, and repository contents shrink to an inventory list.
What stays full-size: secrets, profiles, sessions, locally-authored files, and custom configs —
anything that is not already on the public internet or reproducible from a canonical source.
The snapshot is self-describing: a manifest plus payload that a future LLM (different model,
possibly different OS version, possibly years later) can replay against a fresh machine.

## Status

**Development — v0.1 in progress.**

[![CI](https://github.com/eranshir/rehydrate/actions/workflows/ci.yml/badge.svg)](https://github.com/eranshir/rehydrate/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What rehydrate does

- **Captures what is truly local** — secrets, dotfiles, SSH keys, GPG keys, app sessions,
  browser profiles, launchd agents, macOS defaults, and locally-authored projects that have
  no git remote.
- **Treats apps, packages, and repos as knowledge to re-derive** — instead of copying gigabytes
  of binaries, rehydrate records a Brewfile, pip requirements, npm globals, cargo installs, and
  git remote URLs. The restore LLM re-derives them on the target machine.
- **Round-trip verified via sandboxed restore** — after every backup, `verify-sandbox.py` drives
  the full restore procedure into a temp directory, diffs the result against the snapshot, and
  confirms bytes match before reporting success.

## Quick start

**Install** (see [INSTALL.md](INSTALL.md) for full paths):

Path B — install from a local clone (current method before marketplace submission):

```bash
git clone https://github.com/eranshir/rehydrate.git ~/.claude/plugins/rehydrate
# then in Claude Code:
# /plugin install --plugin-dir ~/.claude/plugins/rehydrate
```

As of v0.1, the plugin is not yet submitted to the Claude Code marketplace. Once published,
the recommended path will be `/plugin install rehydrate` directly in Claude Code.

**Back up this machine:**

```bash
# In Claude Code — type the slash command:
/rehydrate:backup
```

The skill will prompt for a drive path (e.g. `/Volumes/PortableSSD`) and an optional snapshot
label, then walk all enabled categories and write the snapshot to the drive.

**Restore from a snapshot:**

```bash
# In Claude Code — type the slash command:
/rehydrate:restore
```

The skill will ask for the snapshot path, present a drift summary and a restore plan, require
explicit confirmation at two checkpoints, run a dry-run pass, then apply the live restore.

## Architecture at a glance

```
~/HOME ──walk-* scripts──> walk-output JSON ──snapshot.py──> drive
                                                              ├── objects/<aa>/<bb>/<sha256>   (content-addressed, deduped)
                                                              └── snapshots/<id>/manifest.json

drive ──restore-plan.py──> plan JSON ──restore-apply.py──> target
                                            └── verify-sandbox.py confirms bytes round-trip
```

Each walker corresponds to a strategy:

| Strategy | Walker script |
|---|---|
| `file-list` | `scripts/walk.py` |
| `package-list` | `scripts/walk-packages.py` |
| `app-list` | `scripts/walk-apps.py` |
| `repo-list` | `scripts/walk-repos.py` |
| `full-snapshot` | `scripts/walk-fullsnap.py` |

Objects are stored at `<drive>/llm-backup/objects/<aa>/<bb>/<sha256>` — a two-level fanout
matching Git's loose object layout. Cross-snapshot deduplication is automatic: identical file
content is stored once regardless of path or snapshot.

## Categories supported (v0.1)

| Category | Strategy | What is captured |
|---|---|---|
| `dotfiles` | file-list | Shell rc files, git config, tmux config, editor config |
| `ssh-keys` | file-list | `~/.ssh/` (private keys, known_hosts, authorized_keys) |
| `gnupg` | file-list | `~/.gnupg/` (config and keys) |
| `launchagents` | file-list | `~/Library/LaunchAgents/*.plist` |
| `defaults` | file-list | Key macOS preference plists (Dock, Finder, Terminal, iTerm2, etc.) |
| `browser-profiles` | file-list | Chrome, Firefox, Brave, Edge profiles (not Safari — iCloud handles that) |
| `app-sessions` | file-list | Claude, Codex, Gemini, Cursor, VS Code user settings and sessions |
| `package-managers` | package-list | Brewfile, pip requirements, npm globals, cargo, go, gem inventories |
| `app-inventory` | app-list | `/Applications` bundle metadata, resolved to cask names where possible |
| `dev-projects` | repo-list | Git repos with remotes — remote URLs + gitignored secrets |
| `local-only-projects` | full-snapshot | Project dirs with no git remote — full file tree |
| `custom-content` | file-list | User-classified folders (populated at backup time via prompts) |

iCloud-trusted categories (Mail, Notes, Photos, Safari, Keychain, Contacts, Calendar,
Reminders, Voice Memos, Messages, App Store) are excluded by default. See [SECURITY.md](SECURITY.md).

## Documentation

- [INSTALL.md](INSTALL.md) — installation paths (marketplace, local clone, skills-only)
- [USAGE.md](USAGE.md) — end-to-end walkthrough (backup, restore, diff, GC, customisation)
- [SECURITY.md](SECURITY.md) — trust model, plaintext secrets caveat, no-PII logging
- [docs/adr/001-architecture.md](docs/adr/001-architecture.md) — nine architectural decisions and their rationale

## License

[MIT](LICENSE)
