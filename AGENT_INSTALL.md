# Installing Guard Ram: instructions for an AI agent

You are a coding agent (Claude Code, Codex or another) and your user asked you to install
Guard Ram on their machine. Follow these steps in order. Where a step says **ASK**, stop,
show the user what you found, and wait for a clear yes before going on.

Guard Ram stops processes. Done carelessly, the install can stop something your user is
using. Done as described here, nothing is stopped for at least 40 minutes after the
install, and the user sees what will happen before it does.

## Rules for the whole install

- Run everything as the user. Never use `sudo`, and never install as root.
- Guard Ram needs no API key, no account and no network access after the clone. If
  anything asks for a key or a token, stop: it is not part of this install.
- Do not edit agent settings or the user's instruction files without a yes for that
  specific change.
- Do not relax a rule or a limit to make a warning go away. Report the warning.
- Keep the user informed in plain words: what you are about to do, what it changes,
  how to undo it.

## 1. Check the machine

Run each check and compare with the expected result:

```bash
uname -s                                          # Linux
systemctl --user is-system-running                # running or degraded
python3 -c 'import sys; print(sys.version_info >= (3, 11))'           # True
python3 -c 'import os; os.close(os.pidfd_open(os.getpid())); print("pidfd ok")'
command -v zenity || echo "no zenity: dialogs will be skipped, units for the user are kept"
```

If one of the first four fails, stop and tell the user Guard Ram cannot run here, and why.
A missing `zenity` is not blocking.

## 2. Get the code and run the tests

```bash
git clone <repository URL> guard-ram && cd guard-ram
python3 -m unittest discover -s tests
```

Expect `OK`. The tests use synthetic process tables and temporary files. Two of them
start and stop their own `sleep`, and nothing else on the machine is touched. If a test
fails, show its output to the user and go no further.

## 3. Show the user what Guard Ram sees

```bash
python3 guard_ram.py --dry-run
```

This reads `/proc` and prints, without stopping or writing anything:

- the cap line: the agents' summed memory against the cap (a third of physical memory by
  default), and system memory against its threshold.
- every unit, grouped by agent, with its category, state and size.
- "Decisions if the pass ran now". On a first run it should be empty, unless memory is
  already saturated.

Explain to the user, in a few lines, which units would be stopped after 40 minutes of
inactivity (typically MCP servers of abandoned sessions and forgotten background tasks),
and that what is started for them gets a dialog instead.

## 4. ASK: what must be protected, and the cap

Ask the user two questions, with the dry-run in front of them:

1. **Is anything listed as `task`, `mcp` or `orphan` something they use themselves?**
   For example a dev server they open in a browser, a daemon they rely on, a bridge used
   by an editor. For each one, add a rule to `~/.config/guard-ram/rules.toml` (create the
   file if needed, the format is in `docs/rules.example.toml`):

   ```toml
   [[rule]]
   pattern = 'the-daemon\.py'   # a regex on the command line
   category = "infra"           # never stopped for inactivity
   reason = "what it is, in a few words"
   ```

   Use `for-user` instead of `infra` when the user would rather be asked after an hour.
2. **Does the default cap suit them?** If they want a fixed value:

   ```toml
   [limits]
   agents_mb = 20480
   ```

Run `python3 guard_ram.py --dry-run` again and check that every unit is where the user
wants it. Never write rules the user did not agree to.

## 5. ASK: install

Tell the user what `install.py` writes, then run it:

```bash
python3 install.py
python3 install.py --check        # expect: ok: installed copy matches the checkout, timer running
```

It writes `~/.local/share/guard-ram/` (a copy of the checkout), the user units
`guard-ram.service` and `guard-ram.timer`, the link `~/.local/bin/guard-ram`, and the
skill `~/.claude/skills/guard-ram/` (linked from `~/.codex/skills/` when Codex is there).
It enables the timer: one pass every 15 seconds. Any file it replaces is kept as
`.bak-YYYY-MM-DD`.

## 6. ASK: the Claude Code hooks

Only if the user runs Claude Code. The hooks send a heartbeat after each tool call and
each prompt, and at the end of each turn, so that the MCP servers of a session in use are never stopped. Without
them, Guard Ram still works, but an MCP server that stays silent for 40 minutes in a
session that is still open can be stopped.

Show what would be added, then add it:

```bash
python3 install.py --config        # shows the hooks, changes nothing
python3 install.py --merge-hooks   # adds them to ~/.claude/settings.json
```

`--merge-hooks` keeps every other setting and hook, adds nothing twice, keeps the previous
file as `settings.json.bak-YYYY-MM-DD`, and refuses a file that is not valid JSON. Codex
needs no hook.

## 7. ASK: tell the user's future agent sessions

The skill is installed, but a line in the user's global instructions makes agents use it
without being asked. Offer to append this block to `~/.claude/CLAUDE.md` and, for Codex,
`~/.codex/AGENTS.md`:

```markdown
## Guard Ram

Everything you start is tracked by Guard Ram (every 15 s): idle for 20 min → marked
idle, 20 min later → stopped. What you start **for the user to use** (a server they
open, an app, a game): `guard-ram launch --category for-user --reason "..." -- <command>`;
they get a dialog after 1 h instead of a stop. Details: skill `guard-ram`.
If `~/.claude/guard-ram/last-stop.md` exists at the start of a session, read it, resume
the interrupted work, then archive it. Never disable the guard or relax its rules
without the user's agreement.
```

## 8. Verify

Wait one minute, then:

```bash
systemctl --user is-active guard-ram.timer            # active
guard-ram log -n 20                                   # "appeared" events, no "error"
journalctl --user -u guard-ram.service -p warning --since "-5min"   # nothing
guard-ram list                                        # same picture as the dry run
```

If the hooks were added, run any tool call, then check that
`~/.claude/guard-ram/heartbeats/` holds a file for your session.

## 9. Report to the user

In a few lines: what was installed and where, the cap in use, the rules you added, and:

- `guard-ram list`: what runs and what would be stopped now.
- `guard-ram log --pid <pid>`: why a process was stopped.
- `guard-ram launch --category for-user -- <command>`: start something that must not be
  stopped without asking.
- `python3 install.py --uninstall`: the way back.

## Troubleshooting

- **The timer is not active**: `systemctl --user enable --now guard-ram.timer`, then
  `journalctl --user -u guard-ram.service` for the reason.
- **No dialog ever appears**: check `systemctl --user show-environment | grep -E
  'DISPLAY|WAYLAND'`. If the variables are missing, **ASK** before running
  `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY`.
- **Something was stopped that should not have been**: `guard-ram log --pid <pid>` gives
  the reason and the last sign of life seen. Add a rule for it (step 4, with the user's
  yes). `guard-ram list` also flags any process restarted within 30 minutes of a stop.
- **A `rules` event in the log**: a value in a rules file was refused. Fix the file. Until
  then the built-in value is used.

## Uninstall

```bash
python3 install.py --uninstall
```

It stops the timer and removes the units, the link and the skill. Then remove the
heartbeat hooks from `~/.claude/settings.json` (or restore the `.bak` file **after
checking with the user** that nothing else changed since). State, journal and local
rules stay in `~/.claude/guard-ram/`, `~/.config/guard-ram/` and
`~/.local/share/guard-ram/`. Deleting them is permanent: **ASK** first.
