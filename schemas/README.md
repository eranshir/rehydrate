# Schema Evolution Policy

This document governs how `manifest.schema.json` changes over time.

## Versioning

The manifest carries a top-level `schema_version` field whose value follows [Semantic Versioning 2.0.0](https://semver.org/). The current version is **0.1.0**.

### What constitutes a change?

| Change type | Version bump |
|---|---|
| Add a new optional field to an existing object | Minor (`0.x.0`) |
| Add a new category under `categories` | Minor |
| Remove a field or rename a field | Major (`x.0.0`) |
| Change the type of an existing field | Major |
| Add a new required field to an existing object | Major |
| Tighten a constraint (e.g., narrower pattern, smaller max) | Major |
| Loosen a constraint (e.g., wider pattern, larger max) | Minor |

### Compatibility expectations

- A **minor bump** is additive only. Restore scripts written against `0.1.0` must still be able to process `0.2.0` manifests — they may not understand new optional fields, but they must not reject the document.
- A **major bump** signals a breaking change. Restore scripts must check `schema_version` and refuse to process manifests whose major version they do not recognise.
- The **patch** component is reserved for schema wording or description corrections that carry no semantic change. No tooling should branch on the patch version.

### Pre-1.0 stability

While the major version is `0`, any minor bump may include limited breaking changes within the `categories` section (new categories, revised category payloads) provided that:
1. The change is announced in the associated GitHub issue.
2. No existing field's type or requirement changes.

Once the project reaches `1.0.0`, the full compatibility policy above applies without exception.

## Categories map

The `categories` object is intentionally open: restore scripts must ignore categories they do not recognise rather than failing. Each category payload is discriminated by its `strategy` field.

Currently defined strategies:

| Category | Strategy | Defined in |
|---|---|---|
| `dotfiles` | `file-list` | `manifest.schema.json#/$defs/dotfiles_category` |

Future categories (e.g., `ssh-keys`, `brew`, `app-sessions`) will be added as minor-version bumps.

## Content addressing

Each file entry carries an `object_hash` — the SHA-256 hex digest of the file's byte content (or the symlink target string for symlinks). The corresponding object lives on the backup drive at:

```
<drive>/objects/<aa>/<bb>/<full-hash>
```

where `<aa>` is the first two hex characters, `<bb>` is the next two, and `<full-hash>` is the full 64-character hex string. Example:

```
hash  = a3f1e2d4c5b6a7f8e9d0c1b2a3f4e5d6c7b8a9f0e1d2c3b4a5f6e7d8c9b0a1f2
path  = objects/a3/f1/a3f1e2d4c5b6a7f8e9d0c1b2a3f4e5d6c7b8a9f0e1d2c3b4a5f6e7d8c9b0a1f2
```

This layout mirrors git's loose object store and allows efficient cross-snapshot deduplication: if two snapshots reference the same `object_hash`, they share a single on-disk object with no duplication.

The restore process should:
1. Resolve `object_hash` → object path on the backup drive.
2. Verify the SHA-256 of the object matches `object_hash` before writing.
3. Write the content to `$REHYDRATE_TARGET/$HOME_RELATIVE_PATH` and apply `mode` and `mtime`.
