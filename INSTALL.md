# INSTALL.md — rehydrate installation

## Path A: Claude Code plugin marketplace (recommended once published)

Once rehydrate is submitted to the Claude Code plugin marketplace, the install command will be:

```
/plugin install rehydrate
```

As of v0.1, the plugin has not yet been submitted. To submit or track submission status,
see [claude.ai/settings/plugins/submit](https://claude.ai/settings/plugins/submit).

## Path B: Install from a local clone

This is the current installation method for v0.1.

1. Clone the repository into your Claude plugins directory:

```bash
git clone https://github.com/eranshir/rehydrate.git ~/.claude/plugins/rehydrate
```

2. In Claude Code, install the plugin from the local directory. The exact CLI form depends
   on your Claude Code version. The expected form is:

```
/plugin install --plugin-dir ~/.claude/plugins/rehydrate
```

If that command is not available in your version, check the Claude Code documentation or
run `/help` in Claude Code to see the current plugin management commands. The plugin
directory must contain a `plugin.json` manifest (at `.claude-plugin/plugin.json`) for
Claude Code to recognize it.

After installation, the commands `/rehydrate:backup` and `/rehydrate:restore` should appear
in `/plugin list` output.

## Path C: Skills only (no plugin packaging)

If you want to use the backup and restore skills without plugin packaging (for development or
testing), you can copy the skill directories directly into your Claude skills folder:

```bash
cp -R ~/.claude/plugins/rehydrate/skills/backup/  ~/.claude/skills/rehydrate-backup/
cp -R ~/.claude/plugins/rehydrate/skills/restore/ ~/.claude/skills/rehydrate-restore/
```

**Limitations of this path:**

- Loses plugin metadata and namespace prefix. The slash commands will be `/rehydrate-backup`
  and `/rehydrate-restore` rather than `/rehydrate:backup` and `/rehydrate:restore`.
- The `${CLAUDE_SKILL_DIR}/../../scripts/` path references inside each `SKILL.md` rely on
  the skill living two directories above `scripts/`. If your skill directory does not sit
  at the expected relative depth, the script paths will not resolve and the skills will fail.
  You would need to adjust those paths manually.
- Plugin-level metadata (version, author, description shown in `/plugin list`) is not available.

This path is suitable for quick local experimentation, not for regular use.

## Requirements

- **macOS 13 or later.** Tested on macOS 26.x (Tahoe). Earlier versions may work but are not
  tested.
- **Python 3.10 or later.** Available via `python3` on macOS developer setups (installed with
  Xcode Command Line Tools). Verify: `python3 --version`.
- **No runtime pip dependencies.** Core scripts use only the Python standard library.
- **Optional test dependencies** (for running `scripts/tests/`):
  ```bash
  pip3 install jsonschema pyyaml
  ```
  These are only needed to run the test suite. The backup and restore skills do not require them.

## Verifying installation

After installing via Path A or B, verify that the plugin is active:

```
/plugin list
```

The output should include `rehydrate` with its version. The slash commands
`/rehydrate:backup` and `/rehydrate:restore` should be listed.

If `disable-model-invocation: true` in the skill frontmatter prevents a `--help`-style
invocation, invoke `/rehydrate:backup` directly and follow the interactive prompts — the
skill will guide you through the required inputs before performing any action.
