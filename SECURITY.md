# Security

guard-ram stops processes. This page says what it can touch and what it trusts.

## What it can stop

- Only processes of the user it runs as: `/proc/<pid>` entries owned by another
  uid are skipped, and signals to them would be refused anyway. It needs no root
  and must not be run as root.
- Only processes in scope: descendants of an agent (`claude`, `codex`),
  processes carrying an agent environment variable, or processes reparented to
  systemd inside the cgroup of the application that runs the agents and born
  after its first agent. The application itself and the ancestors of agents are
  never in scope.
- Signals go through a `pidfd` opened before the process identity (pid and start
  time) is checked, so a recycled pid never receives a signal meant for a
  process that has exited.
- Never itself or its own ancestors.

## What it trusts

- **Environment variables and marks are not authenticated.** Any process of the
  same user can set `GUARD_RAM_CATEGORY=infra` to avoid being stopped for
  inactivity, or `CLAUDECODE=1` to enter the scope. That process could already
  signal or kill the same processes itself, so this grants nothing new. Treat
  guard-ram as housekeeping, not as a security boundary.
- **Rules files.** `rules.toml` and `~/.config/guard-ram/rules.toml` are code-
  adjacent configuration: whoever can write them decides what is stopped. Each
  value is type- and range-checked, and an unreadable file suspends every stop
  for inactivity instead of falling back to aggressive defaults.
- **Command lines are logged.** The journal stores the first 200 characters of
  each command line, with obvious secrets (`--token=`, `password=`, `api_key=`…)
  masked. A secret passed some other way on a command line can end up in
  `~/.claude/guard-ram/journal.jsonl`. Pass secrets through the environment.
- **The dialog text** contains a command line; it is shown with `--no-markup`.

## Reporting

Please open a private security advisory on the repository rather than a public
issue.
