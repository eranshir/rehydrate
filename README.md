# rehydrate

> LLM-powered backup and restore for your Mac. Stores **knowledge**, not bytes.

Traditional backups store every byte. **rehydrate** stores the knowledge that the bytes can be re-derived from. Apps, packages, runtimes, even repo contents (via git remotes) shrink to a list. What stays full-size: secrets, profiles, sessions, locally-authored files, custom configs — anything that isn't already on the public internet.

The unique artifact is a **self-describing snapshot**: a manifest plus payload that a future LLM (different model, possibly different OS version, possibly years later) can replay against a fresh machine.

## Status

**v0.1 in development.** See [the parent plan issue](https://github.com/eranshir/rehydrate/issues/1) for the full roadmap.

## Slash commands (planned)

- `/rehydrate:backup` — capture this machine into a snapshot on an external drive
- `/rehydrate:restore` — restore a snapshot to a target (sandboxed temp dir or live `$HOME`)

## Architecture overview

- **Plugin** packaging two skills: `backup` and `restore`
- **Hybrid manifest**: `manifest.json` (machine-readable inventory) + `RESTORE-GUIDE.md` (narrative rationale for the restore LLM)
- **Content-addressed `objects/`** for cross-snapshot dedup
- **Sandboxed verify**: test-restore into a temp tree, diff against the snapshot
- **iCloud-trusted exclusions**: skip Mail/Notes/Photos/Safari/Keychain/Contacts/Calendar/Reminders/Voice-Memos/Messages/AppStore by default
- **No-PII logging**: scripts log paths and hashes only, never file contents

## Security caveat (v0.1)

Secrets (`.env` files, SSH keys, API tokens) are captured **plaintext** on the backup drive. The drive must be physically trusted. Encrypted-at-rest and vault-backed secrets are planned for a later version.

## License

[MIT](LICENSE)
