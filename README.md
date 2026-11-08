# Guard Ram

**Claude, Codex: stop eating my RAM.**

A memory guard for the processes that coding agents (Claude Code, Codex) start on a
Linux workstation. One Python file, standard library only, no network, no root, no
API key.

Agents start things and forget them: MCP servers duplicated in every session, dev
servers, watchers, compilers, an editor opened "for the user". Each session can hold
a gigabyte that nobody uses. When memory runs out, the usual answer is to kill
everything. Guard Ram instead:

- **inventories** everything an agent started, including what detached itself from
  the process tree (double fork, `setsid`, reparented to systemd).
- **stops what has been idle**: after 20 min without activity a unit is marked
  *idle*, and 20 min later it is stopped (SIGTERM, then SIGKILL).
- **asks before stopping what was started for the user** (a window, an editor, a
  game, a dev server they browse): after 1 h idle, a dialog asks "Stop / Keep".
  Without an answer, it is kept.
- **stops by priority when memory saturates**: idle units first, then lowest
  priority first, agents only as a last resort.
- **logs every decision**, with the last sign of life it saw, and flags its own
  probable mistakes (a process restarted soon after being stopped).

**Have an agent install it:** point it to [AGENT_INSTALL.md](AGENT_INSTALL.md). The
steps check the machine, show you what would be stopped before anything is enabled,
and ask you before each change.

The details (state files, journal events) are in
[docs/how-it-works.md](docs/how-it-works.md).

## What it does not do

Read this before relying on it.

- **It can stop something you were about to use again.** "Idle" means no CPU, no
  writes, no new connection for 40 minutes. A session resumed after that finds its
  MCP servers gone: they do not come back until the session is restarted. The
  `restarted_after_stop` event and `guard-ram list` show when a stopped process was
  started again within 30 minutes, which is the best signal that it should not have
  been stopped.
- **Codex children are judged on their own activity.** Codex hooks do not say which
  thread a process belongs to, so an MCP server of a Codex thread does not inherit
  the thread's activity, unlike Claude Code sessions (see *Activity*).
- **Long, silent jobs of an active session are not protected by default.** A
  background command waiting on the network with almost no traffic is stopped after
  40 minutes unless it was marked (`guard-ram keep <pid> --hours 3`).
- **Inventory relies on markers.** A process is in scope if it descends from an
  agent, carries an agent environment variable (`CLAUDECODE`, `CLAUDE_PID`,
  `CODEX_*`), or was reparented to systemd while staying in the cgroup of the
  application that runs the agents. A process started through `systemd-run` (new
  cgroup) with a cleared environment escapes all three.
- **Markers can be forged.** Any process of the same user can set
  `GUARD_RAM_CATEGORY=infra` and never be stopped for inactivity. Guard Ram is a
  housekeeping tool against forgetful agents, not a security boundary. See
  [SECURITY.md](SECURITY.md).
- **Linux only**, systemd user session, Python 3.11 or later, kernel 5.3 or later
  (`pidfd`). The dialog uses `zenity`. Without a display it is skipped and the unit is
  kept. The "user away" check uses GNOME's idle monitor and is skipped elsewhere.

## Measurements

From the first day on one workstation (8 agent sessions, 1 Codex desktop app, an
editor of about 10 GB started by an agent), November 2026:

| | before (one cap, kill everything above it) | Guard Ram |
|---|---|---|
| on saturation | the whole tree of every session killed at once (13 agents in one event) | stop by priority, agents last |
| in scope | direct descendants of Claude Code agents | Claude Code, Codex, detached processes (editor, compilers, daemons) |
| forgotten processes | never stopped below the cap | 20 units stopped after 40 min idle on the first cycle (1.4 GB), all tools of 2-day-old Codex threads |
| cost per pass (every 15 s) | 0.5 s CPU | 0.08 s CPU |

Each activity signal was checked against real processes before being kept, and three
false negatives found that way were fixed (see [CHANGELOG.md](CHANGELOG.md)): an idle
agent writes its session log in the background (its MCP servers used to inherit
that), an editor reconnects to itself through its own worker processes, and an idle
`claude mcp serve` re-reads 121 KB of settings every 15 s without writing anything.

One false-positive mistake happened during the first deployment: the editor started
for the user was counted in the agents' cap, which triggered a saturation and stopped
six MCP servers of live sessions. Units started for the user now count only towards
the system threshold.

## Install

```bash
git clone <this repository> guard-ram && cd guard-ram
python3 -m unittest discover -s tests     # no root, nothing is stopped
python3 guard_ram.py --dry-run            # what it sees, and what it would stop now
python3 install.py                        # copy, systemd units, timer, skill
python3 install.py --merge-hooks          # Claude Code heartbeat hooks (--config only shows them)
python3 install.py --check                # exit 1 if the installed copy differs from this checkout
guard-ram list                            # what runs, per agent, and what would be stopped now
```

`install.py` installs a **copy** in `~/.local/share/guard-ram/`: switching branches in
the checkout never changes the running guard. It edits `~/.claude/settings.json` only
with `--merge-hooks`, after backing it up. `--uninstall` removes the timer, units, link
and skill, and keeps state and logs.

Machine-specific rules and limits go in `~/.config/guard-ram/rules.toml` (format in
[docs/rules.example.toml](docs/rules.example.toml)). They are never in the repository.

## How it decides

**Units.** The scope is cut into units. An agent is a unit on its own. So is each
direct child of an agent, each detached process, and each process started with
`guard-ram launch` or marked, each with its descendants. A unit is stopped as a whole.

**Categories** (from [rules.toml](rules.toml)):

| category | priority | inactive after | then |
|---|---|---|---|
| `orphan` (its agent is gone) | 1 | 20 min | stopped 20 min later |
| `mcp` (tool server of a session) | 2 | 20 min | stopped 20 min later, kept while its Claude session is used |
| `task` (anything else) | 3 | 20 min | stopped 20 min later |
| `for-user` (window, interactive shell, marked) | 4 | 60 min | dialog to the user, never stopped without an answer |
| `infra`, `agent` | 5 | 20 min | never stopped for inactivity |

Order: a mark (`guard-ram mark`, `guard-ram launch`) wins, then the first `[[rule]]`
whose regex matches the command line, then automatic classification.

**Activity.** A unit is active on a pass if one of its processes:

- used at least 2 % of a core (10 % for `for-user`).
- read or wrote at least 1 MB, or, below that, on two passes in a row, did I/O above
  64 KB and above three times the unit's learned background noise (capped, so a
  steady flow above ~8 KB/s always counts), **including at least 4 KB written**.
  Reading without writing is polling, not serving.
- received a **new** TCP connection, on a port it listens on, from a process
  **outside the unit**. An open, silent connection does not count: if it carries
  traffic, that traffic already shows up as I/O. A process connecting back to its own
  unit does not count.

An MCP server of a Claude Code session also counts as active while the session is
actually used: a hook heartbeat (prompt, tool call, end of turn) or the agent itself
computing (at least 10 %, a reply being written). The agent's own background writes
do not count. A task started by the session inherits nothing.

Time not observed (suspend, timer stopped) does not count as idle.

**Saturation** happens when the agents' summed PSS goes above the cap (`agents_mb`, a
third of physical memory by default), or system memory above `system_pct` (90 %).
Guard Ram stops until 2 GB under the cap and 5 points under the threshold, in this
order: inactive before active, then lowest priority, then longest idle, then largest.
Agents go only above the cap, with the session that sent a heartbeat in the last
minute last of all. The Codex desktop app server is never stopped. If the pressure
comes from outside the agents (a browser, a VM) and stopping everything would not be
enough, only inactive units are stopped.

## Commands

```bash
guard-ram list [--all] [--json]
guard-ram log [-n 40] [--event stop] [--pid 1234]
guard-ram mark <pid> --category for-user|infra|task|mcp|orphan|none [--priority 1-5] [--reason "..."]
guard-ram keep <pid> --hours 3 [--reason "..."]
guard-ram launch --category for-user [--priority 1-5] [--reason "..."] -- <command ...>
guard-ram --dry-run
```

The skill in [skill/SKILL.md](skill/SKILL.md) tells agents when to use them, mainly:
start what the user will use with `guard-ram launch --category for-user`.

## Files

| path | content |
|---|---|
| `~/.local/share/guard-ram/` | installed copy |
| `~/.config/guard-ram/rules.toml` | machine-specific rules and limits |
| `~/.claude/guard-ram/journal.jsonl` | every event (rotated at 10 MB, 5 generations) |
| `~/.claude/guard-ram/state.json` | state of every unit between passes |
| `~/.claude/guard-ram/marks.json` | marks and reprieves set by hand |
| `~/.claude/guard-ram/heartbeats/` | last heartbeat of each Claude Code session |
| `~/.claude/guard-ram/last-stop.md` | written only when an agent had to be stopped |

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests run the decision logic on synthetic process tables, and the installer's
hook merge on temporary files. Two of them start and stop a real `sleep`. Every fix
in the changelog has a test that fails when the fix is reverted. Prefer a measurement
on real processes over a guess: the [changelog](CHANGELOG.md) says how each threshold
was chosen.

## License

MIT, see [LICENSE](LICENSE).
