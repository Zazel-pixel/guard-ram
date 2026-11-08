---
name: guard-ram
description: Guard Ram for coding agents (Claude Code, Codex) on this machine — inventories everything the agents start, stops units idle for 40 min, asks the user before stopping what was started for them, and stops by priority when memory saturates. Use it before starting a server, an app or a game the user will use (so that it is not stopped), when a process disappeared without explanation, before a memory-hungry task, or when the user asks what is running, why something was stopped, or to change a priority. Triggered by "guard-ram", "Guard Ram", "who killed", "process stopped", "what is running".
---

# Guard Ram (`guard-ram`)

Installed copy: `~/.local/share/guard-ram/` (`guard_ram.py`, `rules.toml`, `README.md`).
Rules of this machine: `~/.config/guard-ram/rules.toml`.
Runs every 15 s (`guard-ram.timer`). State and journal: `~/.claude/guard-ram/`.

## What it does with what you start

| Category | Priority | Idle after | Then |
|---|---|---|---|
| orphan (its agent is gone) | 1 | 20 min | stopped 20 min later |
| mcp (tool server of a session) | 2 | 20 min | stopped 20 min later; kept while its session is in use |
| task (anything else) | 3 | 20 min | stopped 20 min later |
| for-user (window, shell, marked) | 4 | 60 min | dialog to the user: "Stop" or "Keep"; without an answer, kept |
| infra, agent | 5 | 20 min | never stopped for inactivity |

"Active" means CPU (at least 2 %, 10 % for for-user), I/O (at least 1 MB, or above
its background noise on two passes in a row while writing; pipes and sockets
included: reading without writing does not count), a new incoming connection from
elsewhere, or a descendant that works. An open, silent connection does not count. The
MCP servers of a Claude session live while the session is used (hook heartbeats).
Suspended time does not count.

Saturation (agents above their cap, a third of RAM by default, or system memory at
90 %): idle units first (over 20 min), then lowest priority, agents last of all and
only above the cap. If the memory is taken by something else, only idle units go.
for-user units do not count in the cap, only in the system threshold.

## What you must do

**You start something the user will use** (a server they open in their browser, an
editor, a game, a graphical tool):

```bash
guard-ram launch --category for-user --reason "demo server of the project" -- npm run dev
```

Already running: `guard-ram mark <pid> --category for-user --reason "..."`.
A specific priority: `--priority 1..5`. Remove a mark: `--category none`.

**A long, silent job that must survive** (slow download, waiting for a service):
`guard-ram keep <pid> --hours 3 --reason "..."`. Do not overuse it: this is exactly
what the guard must be able to stop.

**Before a memory-hungry task**: `guard-ram list` (cap, units, priorities, what would
be stopped now). `--all` shows small units, `--json` for a script.

**A process disappeared**: `guard-ram log --pid <pid>` or
`guard-ram log --event stop -n 20`. Each line gives the reason and the last sign of
life seen. A stop followed by a restart: `guard-ram log --event restarted_after_stop`.

## Do not

- Do not disable `guard-ram.timer`, raise the limits or relax the rules
  (`rules.toml`, `~/.config/guard-ram/rules.toml`) without the user's agreement.
- Do not mark as `infra` or `for-user` what is not for the user, to escape being
  stopped: that is working around the guard.
- If `~/.claude/guard-ram/last-stop.md` exists at the start of a session: read it,
  resume, then archive it (`mv … last-stop-YYYY-MM-DD.md`).
