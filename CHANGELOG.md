# Changelog

## 1.0.0 — first public release

Guard Ram replaces a private script that killed the whole tree of every Claude Code
agent above a fixed memory cap. Everything below was measured on one workstation
before release. Each item names the test that fails if it is reverted.

**Scope and decisions**

- Codex and detached processes are in scope, not only the descendants of Claude Code
  agents. Idle units are stopped after 20 + 20 min, units started for the user get a
  dialog after 1 h instead, and saturation stops by priority.
- **Detached processes with a cleared environment** are found by cgroup: reparented to
  systemd, in the cgroup of the application running the agents, born after its first
  agent. Tests: `test_detached_without_variables_found_by_cgroup`,
  `test_cgroup_ignores_the_app_helpers`.
- **Configurable limits**: `[limits] agents_mb` (a third of physical memory by default)
  and `system_pct` (90). Tests: `test_default_cap_is_a_third_of_memory`,
  `test_cap_applies_to_saturation`.

**False negatives found on real processes**

- **An idle agent no longer keeps its MCP servers alive.** It writes its session log in
  the background, and its MCP servers inherited that and never stopped. They now
  inherit only from hook heartbeats or the agent computing at 10 % or more. Test:
  `test_agent_background_writes_do_not_keep_its_mcp`.
- **Only new connections from outside the unit count.** A game editor's own workers
  kept reconnecting to it, so it was never idle. Tests:
  `test_connection_inside_the_unit_does_not_count`,
  `test_open_silent_connection_does_not_count`.
- **Reading without writing is not activity.** An idle `claude mcp serve` re-read
  121 KB of settings per 15 s pass and wrote nothing. Below 1 MB, I/O counts only with
  at least 4 KB written, above three times the unit's learned background noise (capped
  at 40 KB), on two passes in a row. Tests: `test_reading_only_is_noise`,
  `test_regular_polling_learned_as_noise`, `test_slow_download_stays_active`,
  `test_isolated_io_burst_ignored`.

**False positives**

- **First deployment incident:** an editor started for the user (about 10 GB) counted
  in the agents' cap, which triggered a saturation that stopped six MCP servers of live
  sessions. Units for the user now count only towards the system threshold, and no
  saturation is acted on without history unless the system itself saturates. Tests:
  `test_for_user_outside_the_agents_cap`,
  `test_no_stop_without_history_if_the_system_is_fine`.
- **An adversarial review found ten defects.** The six that could stop the wrong thing
  or nothing at all were fixed: saturation sorted on the 20-minute state, no agent
  stopped for pressure from outside, the Codex desktop server never stopped, broken
  rules suspend stops, stop results logged, a failed dialog is not an answer. Tests:
  `test_pressure_from_outside_stops_only_idle_units`,
  `test_codex_desktop_server_never_stopped`, `test_broken_file_no_stop_for_inactivity`.
- **Signals through `pidfd`**, opened before the identity check, so a recycled pid is
  never signalled. Test: `test_recycled_pid_untouched`.

**Traceability**

- `restarted_after_stop`: a process started again within 30 min of being stopped, by the
  session that lost it, is logged as a probable false positive. On the first day it fired
  4 times, and every one was wrong: a 16-second `claude --model haiku -p` of another
  session, started in the same folder, had loaded the same MCP servers. The detector now
  compares sessions, then agents, and counts a restart whose owner is unknown. Tests:
  `test_stop_then_restart_flagged`, `test_same_command_from_another_session_not_flagged`,
  `test_session_resumed_by_a_new_agent_flagged`, `test_other_agent_without_sessions_not_flagged`,
  `test_unknown_owner_flagged`.
- Every stop records the last sign of life seen. Test: `test_last_signal_kept`.

**Cost**

- 0.5 s → 0.08 s of CPU per pass. `smaps_rollup` cost 150 ms for one large editor. PSS
  is now cached for a minute while RSS stays within 10 %, and re-read for every process
  near a threshold.

**Install**

- `install.py` installs a copy, the systemd units and the skill, and merges the Claude
  Code hooks only with `--merge-hooks`. [AGENT_INSTALL.md](AGENT_INSTALL.md) lets an
  agent run the install step by step, asking the user before each change. Tests:
  `test_keeps_existing_settings_and_hooks`, `test_running_twice_adds_nothing`,
  `test_invalid_json_left_untouched`.
- **The guide was followed step by step on a new account of a clean Ubuntu 24.04 VM**,
  with a simulated agent session. Everything worked, including a real stop of an orphan
  and the protection of a unit by a local rule. Five frictions were fixed:
  - multi-line command lines broke the listing and the journal display. They are now
    shown on one line (test `test_multi_line_command_shown_on_one_line`).
  - the test run ended on installer messages instead of `OK`.
  - `~/.local/bin` was not in the `PATH` of the new account. The installer now says so.
  - the guide's dry run hid small units.
  - nothing removed the hooks on uninstall. `--remove-hooks` does, and the empty skill
    folder is removed (test `test_removes_only_guard_ram_hooks`).
