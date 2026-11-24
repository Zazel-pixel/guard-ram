# How guard-ram works

The [README](../README.md) says what guard-ram does and does not do. This page covers
the details: how the scope is built, what is stored between passes, and which events go
to the journal.

## A pass

`guard-ram.timer` starts `guard_ram.py` every 15 seconds. One pass:

1. reads every `/proc/<pid>` of the user (other users' processes are skipped).
2. builds the scope and cuts it into units.
3. classifies each unit and measures its activity since the previous pass.
4. decides: mark idle, ask, stop for inactivity, stop for saturation.
5. writes the state, the marks and the journal, then sends the signals.

The whole pass holds a lock (`~/.claude/guard-ram/.lock`), shared with `mark` and
`keep`. It costs 0.1 to 0.2 s of CPU (measured on a busy workstation, November 2026).

## Scope

- **Root agents**: processes named `claude` (desktop app or CLI) or `codex` (desktop
  app server, CLI) with no agent among their ancestors. Their ancestors (the desktop
  application, its launcher) are never in scope.
- **Their whole tree.**
- **Detached processes** (double fork, `setsid`, reparented to systemd) that still carry
  an agent environment variable: `CLAUDECODE`, `CLAUDE_PID`, `CODEX_SHELL`,
  `CODEX_CLI_PATH`, `CODEX_APP_TOOLS_PIPE_PATH`, `CODEX_THREAD_ID`, `CODEX_SANDBOX`,
  `GUARD_RAM_CATEGORY`.
- **Detached processes with a cleared environment** (`env -i`, `sudo`): reparented to
  systemd, still in the cgroup of the application that runs the agents, and born after
  the first agent of that cgroup (the application's own helpers are born before, and
  `crashpad` is excluded). The terminals of the application's panel have the
  application as parent and are never taken.

A process started through `systemd-run` with a cleared environment escapes all of these.

## Units

The scope is cut into units, and a unit is stopped as a whole:

- an agent, on its own.
- each direct child of an agent, with its descendants.
- each detached process, with its descendants.
- each process started with `guard-ram launch` or marked with `guard-ram mark`.

A unit is **protected** if it contains the guard itself or one of its ancestors.

## Classification

In this order:

1. a mark (`marks.json`) or the `GUARD_RAM_CATEGORY` variable set by `launch`.
2. a root agent is `agent`.
3. the first `[[rule]]` whose `pattern` matches the command line, local rules first.
4. automatically: `orphan` if its agent is gone (`for-user` if it has a window or an
   interactive shell), `mcp` if it is a direct child of a Claude agent started within
   3 minutes of it or named `mcp`, `task` if found by cgroup, `for-user` if it has a
   window (GTK, Qt, Wayland, X11, SDL or GLFW loaded, not `--headless`) or is an
   interactive shell on a terminal, and `task` otherwise.

`GUARD_RAM_PRIORITY` or a mark can override the priority (1 to 5).

## Activity

A unit is active on a pass if one of its processes:

- used at least `cpu_pct` % of a core.
- read or wrote at least 1 MB.
- below 1 MB, on two passes in a row, did I/O above `io_kb` and above three times
  the unit's learned background noise (capped at 40 KB), with at least 4 KB written.
- received a new TCP connection, on a port it listens on, from a process outside the
  unit.

The background noise is a moving average of the unit's I/O on passes without real
work. It absorbs polling (re-reading a settings file) but its cap keeps a steady flow
above about 8 KB/s counting as activity.

An agent counts as **used** when a hook heartbeat arrived since the previous pass, or
when it uses at least 10 % of a core. `mcp` units of a Claude Code session
(`inherit_agent`) stay active while their agent is used. Codex children inherit
nothing: Codex hooks do not say which thread a process belongs to.

A unit seen for the first time counts as active. A gap of more than 2 minutes between
two passes (suspend, timer stopped) is removed from every countdown.

## Decisions

- idle for `idle_min` → state `idle`.
- then, after `stop_min` more minutes → stopped (SIGTERM, then SIGKILL after 5 s, or after
  20 s for `for-user`), unless `stop_min = false`.
- `for-user` (`ask = true`): a zenity dialog, "Stop" or "Keep", shown for 15 minutes.
  No dialog while the user has been away from the keyboard for 5 minutes. "Keep" or no
  answer: asked again an hour later. A dialog that closes within 2 seconds was not shown
  and is retried in 5 minutes.

**Saturation** is described in the README. Its two limits live in `[limits]` of
`rules.toml` and of the local rules: `agents_mb` (the agents' cap, 0 or absent for a
third of physical memory) and `system_pct` (90 by default, 50 to 99). It is acted on
without history only if the system itself is above `system_pct`.

## Files

All under `~/.claude/guard-ram/`:

| file | content |
|---|---|
| `state.json` | `{"version": 1, "t": …, "units": {"<pid>:<start>": {"first", "active", "used", "state", "signal", "signal_t", "burst", "noise_kb", "asked", "answer_t", "restarted", "cat", "prio", "pid", "cmd"}}, "proc": {"<pid>:<start>": [cpu, io, io_w]}, "conn": {…}, "gui": {…}, "pss": {…}, "stopped": [{"sig", "cwd", "t", "pid", "reason", "agent", "session"}]}` |
| `marks.json` | `{"<pid>:<start>": {"category", "priority", "reason", "until", "t", "by"}}` |
| `journal.jsonl` | one event per line (below), rotated at 10 MB, 5 generations |
| `heartbeats/claude-<pid>.json` | last heartbeat of a session: `{"t", "session", "cwd", "hook"}` |
| `answers/<pid>_<start>.json` | the answer of a dialog, read on the next pass |
| `last-stop.md` | written only when an agent had to be stopped, for the next session to resume |

Times in the state are Unix seconds, and the journal uses local ISO 8601 times. A process
is identified by `<pid>:<start>` (start time in clock ticks since boot), so a recycled
pid is never mistaken for the old process.

## Journal events

`appeared`, `idle`, `active_again`, `ask`, `answer`, `stop` (with `last_signal` and
`last_signal_min_ago`), `stop_result` (what each process did with the signal),
`saturation`, `gone` (with `life_min`), `restarted_after_stop` (the same command, in the same folder,
started again within 30 minutes by the session that lost it, or by the same agent when no
session is known: a probable false positive), `mark`, `keep`, `resumed` (gap in observation), `rules` (a refused value),
`error`. Each carries the unit, pid, category, priority, PSS, agent, session, working
directory and the first 200 characters of the command line, with obvious secrets
masked.

## Hooks

`python3 install.py --config` prints the hooks to merge into `~/.claude/settings.json`:
`PostToolUse` (every tool), `UserPromptSubmit` and `Stop` run
`guard_ram.py heartbeat`. It takes about 30 ms, never fails and prints nothing.
