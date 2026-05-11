# Privacy Policy

**Last updated:** 2026-05-11
**Applies to:** the `rehydrate` Claude Code plugin, version 0.1.0 and later, distributed at <https://github.com/eranshir/rehydrate>.

## Summary

The rehydrate plugin **collects no user data**, sends **no telemetry**, and makes **no outbound network requests** of its own. Every operation is local to your Mac and the backup drive you select. The plugin author has no servers and receives nothing from your machine.

## What the plugin does with your data

When you invoke `/rehydrate:backup`:

- The plugin reads files from your home directory and other user-domain paths on your Mac, scoped to the categories you have enabled in `categories.yaml`.
- It runs local commands such as `brew bundle dump`, `npm list -g`, `pip freeze`, `git config --get remote.origin.url`, `defaults`, `sw_vers`, `sysctl`, and `hostname` to capture inventories and machine metadata.
- It writes the captured snapshot — file bytes, inventories, machine metadata — **only** to the backup drive path you explicitly specify.

When you invoke `/rehydrate:restore`:

- The plugin reads a snapshot from a drive path you specify.
- It writes files to a target path you specify, or to `$HOME` only if you give the explicit confirmation phrase `yes, live`.

The plugin's helper scripts use a shared logging module (`scripts/no_pii_log.py`) whose public API structurally forbids logging file contents. Logs contain filesystem paths (with your home directory replaced by `~`) and content hashes only. See [SECURITY.md](SECURITY.md) for the technical guarantee, which is enforced by an introspection test.

## What the plugin does NOT do

- No telemetry, analytics, crash reporting, or usage data leaves your machine.
- No data is sent to the plugin author, to Anthropic, or to any third party by the plugin itself.
- No cloud accounts are accessed.
- No background processes are installed; the plugin runs only when you invoke a slash command.
- No data is retained outside the snapshot directory you chose and the optional log file you may configure via the `REHYDRATE_LOG_FILE` environment variable.

The only outbound network activity that can be initiated through this plugin is `git clone` during post-restore steps — and only when you follow the restore guide's instructions to re-clone captured repositories. Those connections go to the git remotes recorded in your own snapshot (typically GitHub, GitLab, etc.).

## What a snapshot contains

A rehydrate snapshot may include, depending on which categories are enabled:

- Dotfiles, shell configuration, editor configuration
- SSH private and public keys, `known_hosts`, `authorized_keys`
- GnuPG configuration and keys
- LaunchAgents, macOS preference plists
- Browser profiles (Chrome, Firefox, Brave, Edge) — including bookmarks, history, saved passwords, and cookies
- Agent sessions for Claude, Codex, Gemini, Cursor, and VS Code
- Gitignored secret files inside your repos (`.env`, `*.p8`, `credentials.json`, etc.)
- Inventories of installed apps, package managers, and git repository remote URLs
- Full file trees of locally-authored projects without git remotes
- Machine metadata: OS version, hostname, username, hardware identifiers

These are stored **plaintext** on the backup drive. The drive is the security boundary. Anyone with physical or filesystem access to the drive can read every captured secret. The plugin documents this prominently in [SECURITY.md](SECURITY.md) and in the on-screen output of `/rehydrate:backup`.

## Claude / Anthropic

The slash commands `/rehydrate:backup` and `/rehydrate:restore` run inside Claude Code. The conversation between you and Claude that drives the plugin is processed by Anthropic according to its own published privacy practices. The plugin author has no access to that conversation, to your account, or to any data within it.

Anthropic's privacy policy is at <https://www.anthropic.com/privacy>.

## Children's privacy

rehydrate is a developer tool not intended for use by children under 13. The plugin author does not knowingly collect data from any user. As described above, the plugin collects no data.

## Changes to this policy

Material changes to this policy will be committed to the project's git history and reflected in the **Last updated** date above. There is no mailing list to notify; the canonical version is always the `PRIVACY.md` file at the project's GitHub repository.

## Contact

For privacy questions or concerns, open an issue at <https://github.com/eranshir/rehydrate/issues>.
