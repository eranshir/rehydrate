# SECURITY.md — rehydrate security model

## Trust model

**The backup drive is plaintext.** In v0.1, physical custody of the drive is the
security boundary. Anyone with read access to the drive — mounted, cloned, or forensically
imaged — can read every captured file, including private keys and API tokens.

This is a deliberate scoping decision for v0.1, documented in
[ADR-001 §5](docs/adr/001-architecture.md#5-plaintext-secrets-on-backup-drive-in-v1).
Encrypted-at-rest secrets are planned for v2 (see "Planned for v2" below).

Recommended mitigation until v2: use a drive with FileVault encryption enabled (System
Settings > Privacy & Security > FileVault, or `diskutil apfs encryptVolume` for an external
drive), and ensure the drive is not left unlocked and unattended.

## What is captured plaintext

The following categories contain sensitive material and are stored as plaintext objects
in the content-addressed object store on the backup drive:

- **`ssh-keys`** — `~/.ssh/` including private key files (e.g. `id_ed25519`, `id_rsa`).
- **`gnupg`** — `~/.gnupg/` including secret keyring files.
- **`dev-projects`** — gitignored files captured alongside each repo: `.env`, `*.p8`,
  `credentials.json`, and any other file patterns marked with `captured_secrets` in the
  inventory. These commonly contain database URLs, API tokens, and OAuth credentials.
- **`browser-profiles`** — Chrome, Firefox, Brave, and Edge profile directories include
  `Login Data` (saved passwords, encrypted with OS-level keys that may not transfer),
  `Cookies`, and session storage.
- **`launchagents`** — user-level launchd plist files; may embed environment variables
  including secrets.
- **`defaults`** — macOS preference plists; some applications store tokens in preferences.
- **`app-sessions`** — Claude, VS Code, Cursor, and similar tool sessions and settings.

Every backup run prints a visible security notice reminding you of this assumption before
writing any data to the drive.

## What is NOT captured

The following are excluded from backup by default:

- **iCloud-trusted categories** — Mail, Notes, Photos, Safari, Keychain, Contacts,
  Calendar, Reminders, Voice Memos, Messages. iCloud already syncs these; rehydrate
  defers to iCloud as the canonical source and does not create a second copy.
- **App Store binaries** — App Store apps are tied to your Apple ID and re-download
  on a fresh machine via Purchased. Rehydrate captures only the inventory
  (`app-inventory` category), not the binaries.
- **Homebrew binaries and formulae source** — Rehydrate captures the Brewfile
  (package list), not the installed binaries. `brew bundle install` re-derives them.

Categories disabled in `categories.yaml` are also not captured. To verify exactly what
your current configuration will capture, review `categories.yaml` before running a backup.

## No-PII logging

All helper scripts log through `scripts/no_pii_log.py`. This module exposes functions
for logging file paths (with `$HOME` normalised to `~`), SHA-256 hashes, counts, and
error codes. It provides no function that accepts arbitrary string content — file contents
cannot be passed to any log function by accident.

Log output from backup and restore runs is safe to share in bug reports or paste into
support conversations. It will contain paths and hashes, never file contents.

This constraint is enforced structurally (no content-accepting API on the log module)
and verified by `scripts/tests/test_no_pii_log.py`.

## Drift safety

The restore skill compares the snapshot's source machine metadata against the current
machine before writing any files:

- **Different OS platform** (e.g. a Linux snapshot applied to macOS) — **hard refusal**,
  no override available.
- **Architecture mismatch** (e.g. arm64 snapshot on an x86_64 machine) — blocked until
  you type `yes, I understand the arch mismatch` explicitly. Proceed only if you have
  verified that the captured files are architecture-independent.
- **Major OS version difference** — displayed as a prominent warning; no special phrase
  required to continue.

## Reporting security issues

To report a security vulnerability, open a
[GitHub Security Advisory](https://github.com/eranshir/rehydrate/security/advisories/new)
in the rehydrate repository. Please do not file public issues for security matters.

If you are unsure whether a finding is a security issue, open a Security Advisory anyway —
it is private by default and we can triage from there.

## Planned for v2

- **Encrypted-at-rest secrets** — each secret object encrypted to an
  [age](https://github.com/FiloSottile/age) identity generated at setup time, with the
  private key stored in the macOS Keychain. Restore decrypts at lookup time.
- **Vault-backed secrets** — optional mode where secret objects are not stored on the drive;
  instead, the manifest records a 1Password item reference (or aegis-secret key), and restore
  retrieves from the vault at apply time.

See [ADR-001 §5](docs/adr/001-architecture.md#5-plaintext-secrets-on-backup-drive-in-v1)
for the full rationale and the v2 upgrade path design.
