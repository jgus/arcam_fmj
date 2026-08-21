---
name: load-infra
description: Capture the fork-only dev context before switching off the `infra` branch onto an upstream-staged branch (where CLAUDE.md, .envrc/direnv, flake.nix and .claude/ are absent). Run at the start of an `infra` session when you intend to work on a feature/upstream branch.
allowed-tools: Read, Bash
---

Run this **while still on `infra`**, before checking out any upstream-staged branch. After the switch `.claude/` is gone, so this skill is no longer invocable — but the conversation context you build now persists across the switch.

## Why

The fork-only files (`CLAUDE.md`, `.envrc`, `flake.nix`/`flake.lock`, `.claude/`) live only on `infra`. Branches staged for upstream PR omit them, so once you switch you lose: the project guidance (CLAUDE.md), direnv auto-activation of the nix devshell (`uv`/`python3` drop off PATH), the nix apps, and this skill.

## Steps

1. **Ensure CLAUDE.md is in context.** On `infra` the harness auto-loads it, so this is normally a no-op. If its content isn't already present, `Read CLAUDE.md` now so it survives the branch switch.

2. **Run dev tools without direnv.** The devshell still lives in infra's flake; reference it explicitly and wrap each command:
   ```
   nix develop '.?ref=infra' --option post-build-hook "" --command <cmd>
   ```
   This gives `uv`, `python3`, `nixfmt`, and `UV_PYTHON_PREFERENCE=only-system` — identical to direnv on `infra`. Examples:
   - Install test deps: `nix develop '.?ref=infra' --option post-build-hook "" --command uv sync --extra tests`
   - Run tests: `nix develop '.?ref=infra' --option post-build-hook "" --command uv run pytest`
   - Run the CLI: `nix develop '.?ref=infra' --option post-build-hook "" --command uv run arcam-fmj`

3. **Reach anything else from `infra`** without leaving your branch:
   - Read a fork-only/infra-only file: `git show infra:<path>` (e.g. `git show infra:CLAUDE.md`, `git show infra:flake.nix`).
   - Run an infra nix app: `nix run '.?ref=infra#<app>'` (`#fetch-specs`, `#fetch-firmware`).

Steps 2–3 are reference for use *after* the switch — don't act on or narrate them now. Once step 1 holds, reply with exactly: **Ready to switch branches when you are.** — nothing else.
