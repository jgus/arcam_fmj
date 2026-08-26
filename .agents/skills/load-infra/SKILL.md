---
name: load-infra
description: Preserve fork-only development context before switching from the `infra` branch to an upstream-staged branch where the project instructions, Nix flake, and repo-local skills are absent. Use at the start of an `infra` session before switching branches.
allowed-tools: Read, Bash
---

Run this while still on `infra`, before checking out an upstream-staged branch. Do not switch branches as part of this skill.

The fork-only files (`AGENTS.md`/`CLAUDE.md`, `.envrc`, `flake.nix`/`flake.lock`, and `.agents/`/`.claude/`) live only on `infra`. Once they disappear, retain the following context for the rest of the session:

1. Ensure the project instructions are already in context from `AGENTS.md` or its `CLAUDE.md` compatibility link. If they are not, read `AGENTS.md`.

2. Without direnv, run development commands through the `infra` branch's devshell:

   ```console
   nix develop '.?ref=infra' --option post-build-hook "" --command <cmd>
   ```

   This provides `uv`, `python3`, `nixfmt`, and `UV_PYTHON_PREFERENCE=only-system`. Examples:

   - Install test dependencies: `nix develop '.?ref=infra' --option post-build-hook "" --command uv sync --extra tests`
   - Run tests: `nix develop '.?ref=infra' --option post-build-hook "" --command uv run pytest`
   - Run the CLI: `nix develop '.?ref=infra' --option post-build-hook "" --command uv run arcam-fmj`

3. Reach other `infra` files and apps without leaving the current branch:

   - Read a file: `git show infra:<path>`
   - Run an app: `nix run '.?ref=infra#<app>'`

Steps 2–3 are reference for after the branch switch. Do not execute or narrate them now. Once the project instructions are in context, reply with exactly: **Ready to switch branches when you are.**
