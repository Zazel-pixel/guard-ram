#!/usr/bin/env python3
"""Tests of guard-ram's decision logic, on synthetic process tables.
Nothing is read from /proc and nothing is stopped, except in StopProcesses, which starts
and stops its own `sleep`. Run: python3 -m unittest discover -s tests -v
"""
import importlib.util, json, subprocess, tempfile, time, unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("rg", Path(__file__).resolve().parent.parent / "guard_ram.py")
rg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rg)
HZ = rg.HZ


def proc(pid, ppid, comm, cmd, env=None, cpu=0, io=0, pss=10, start=1000, tty=0, gui=False):
    env = env or {}
    p = rg.P(pid=pid, ppid=ppid, start=start, comm=comm, argv0=cmd.split()[0], cmd=cmd, tty=tty,
             cpu=cpu, env={k: v for k, v in env.items() if k in rg.USEFUL_ENV},
             marked_env=any(k in ("CLAUDECODE", "CLAUDE_PID", "CODEX_SHELL", "GUARD_RAM_CATEGORY") for k in env))
    p.pss, p.io, p.gui = pss, io, gui
    return p


class World:
    """A process table that ages pass after pass."""

    def __init__(self, procs, mem_total=64000, mem_available=40000):
        self.procs = {p.pid: p for p in procs}
        self.mem_total, self.mem_available = mem_total, mem_available
        self.t = 1_000_000.0
        self.state, self.marks, self.heartbeats = {}, {}, {}
        self.cgroups = {pid: "/other.scope" for pid in self.procs}
        for pid in (50, 90, 100, 101, 102):
            self.cgroups[pid] = "/app-claude.scope"
        self.cats, self.rules = rg.load_rules(local=None)  # hermetic: without the machine's rules
        self.limits = {"agents_mb": 20480, "system_pct": 90}

    def run_pass(self, advance_s=15, self_pid=999999):
        self.t += advance_s
        roots, detached, tracked, children, by_cgroup = rg.scope(self.procs, self.cgroups.get)
        snap = rg.Snapshot(self.procs, tracked, roots, detached, children, self.mem_total, self.mem_available,
                           self.t, by_cgroup)
        units, decisions, events, new = rg.analyse(snap, self.state, self.marks, self.cats, self.rules,
                                                   self.heartbeats, self_pid=self_pid, limits=self.limits)
        self.state = new
        return {u.pid: u for u in units.values()}, decisions, events

    def wait(self, minutes, work=None, self_pid=999999):
        """Advance `minutes` in 15 s steps; `work(world)` is called at every step."""
        everything, u = [], {}
        for _ in range(int(minutes * 4)):
            if work:
                work(self)
            u, d, _ = self.run_pass(self_pid=self_pid)
            everything += d
        return u, everything


def session(agent_pid=100, agent_cpu=0):
    """Desktop app -> disclaimer -> claude agent, with an MCP server and a task."""
    return [
        proc(1, 0, "systemd", "/usr/lib/systemd/systemd --user"),
        proc(50, 1, "claude-desktop", "/usr/lib/claude-desktop/claude-desktop", env={"CLAUDECODE": "1"}),
        proc(90, 50, "disclaimer", "/usr/lib/claude-desktop/resources/disclaimer --pgroup -- claude",
             env={"CLAUDECODE": "1"}),
        proc(agent_pid, 90, "claude", "/opt/claude-desktop/claude-code/2.1.0/claude --output-format",
             cpu=agent_cpu, pss=400, start=1000),
        proc(agent_pid + 1, agent_pid, "python", "/x/docling-mcp-server --transport stdio",
             env={"CLAUDECODE": "1", "CLAUDE_PID": str(agent_pid)}, pss=380, start=1000 + 5 * HZ),
        proc(agent_pid + 2, agent_pid, "bash", "/bin/bash -c npm run dev",
             env={"CLAUDECODE": "1", "CLAUDE_PID": str(agent_pid)}, pss=50, start=1000 + 3600 * HZ),
    ]


def write(p, nbytes):
    p.io += nbytes
    p.io_w += nbytes


def about(decisions, pid, kind=None):
    return [d for d in decisions if d[1].pid == pid and (kind is None or d[0] == kind)]


class Scope(unittest.TestCase):
    def test_ancestors_of_the_agent_out_of_scope(self):
        u, _, _ = World(session()).run_pass()
        self.assertNotIn(50, u)
        self.assertNotIn(90, u)
        self.assertEqual(u[100].category, "agent")
        self.assertEqual(u[101].category, "mcp")
        self.assertEqual(u[102].category, "task")

    def test_detached_process_tracked_and_orphan(self):
        procs = session() + [proc(300, 1, "python", "python3 server.py",
                                  env={"CLAUDECODE": "1", "CLAUDE_PID": "4242"})]
        u, _, _ = World(procs).run_pass()
        self.assertEqual(u[300].category, "orphan")

    def test_detached_graphical_with_agent_gone_stays_for_user(self):
        procs = session() + [proc(310, 1, "Unity", "/opt/Unity -projectPath x",
                                  env={"CLAUDE_PID": "4242"}, gui=True, pss=9000)]
        u, _, _ = World(procs).run_pass()
        self.assertEqual(u[310].category, "for-user")

    def test_infra_rule(self):
        procs = session() + [proc(320, 1, "codex-code-mode-host", "/usr/lib/chatgpt/resources/codex-code-mode-host",
                                  env={"CLAUDECODE": "1"})]
        u, _, _ = World(procs).run_pass()
        self.assertEqual(u[320].category, "infra")

    def test_process_without_marker_out_of_scope(self):
        u, _, _ = World(session() + [proc(330, 1, "firefox", "/usr/bin/firefox")]).run_pass()
        self.assertNotIn(330, u)

    def test_launch_with_category_makes_its_own_unit(self):
        procs = session() + [proc(105, 102, "node", "node server.js", start=5000 * HZ,
                                  env={"CLAUDECODE": "1", "GUARD_RAM_CATEGORY": "for-user"})]
        u, _, _ = World(procs).run_pass()
        self.assertEqual(u[105].category, "for-user")
        self.assertNotIn(105, u[102].members)

    def test_detached_without_variables_found_by_cgroup(self):
        procs = session() + [proc(340, 1, "node", "node forgotten-server.js", start=5000 * HZ)]
        w = World(procs)
        w.cgroups[340] = "/app-claude.scope"
        u, _, _ = w.run_pass()
        self.assertEqual(u[340].category, "task")
        self.assertIn("cgroup", u[340].reason)

    def test_cgroup_ignores_the_app_helpers(self):
        procs = session() + [proc(341, 1, "zygote", "/usr/lib/claude-desktop/helper", start=500),  # born before the agent
                             proc(342, 1, "chrome_crashpad", "chrome_crashpad_handler", start=5000 * HZ),
                             proc(343, 1, "node", "node elsewhere.js", start=5000 * HZ)]           # other cgroup
        w = World(procs)
        w.cgroups.update({341: "/app-claude.scope", 342: "/app-claude.scope"})
        u, _, _ = w.run_pass()
        for pid in (341, 342, 343):
            self.assertNotIn(pid, u)

    def test_cgroup_does_not_take_the_app_terminals(self):
        procs = session() + [proc(344, 50, "bash", "/bin/bash --init-file panel.sh", tty=34816, start=5000 * HZ)]
        w = World(procs)
        w.cgroups[344] = "/app-claude.scope"
        u, _, _ = w.run_pass()
        self.assertNotIn(344, u)


class Inactivity(unittest.TestCase):
    def test_new_unit_never_stopped_on_first_pass(self):
        _, d, _ = World(session()).run_pass()
        self.assertEqual(d, [])

    def test_task_idle_at_20_stopped_at_40(self):
        w = World(session())
        w.run_pass()
        u, everything = w.wait(25)
        self.assertEqual(u[102].state, "idle")
        self.assertEqual(about(everything, 102), [])
        u, everything = w.wait(16)
        self.assertTrue(about(everything, 102, "stop"))

    def test_cpu_activity_resets_the_countdown(self):
        w = World(session())
        w.run_pass()
        w.wait(30)

        def works(world):
            world.procs[102].cpu += int(0.5 * HZ)  # 0.5 s of CPU per 15 s ≈ 3 %

        u, _ = w.wait(1, works)
        self.assertEqual(u[102].state, "active")
        _, everything = w.wait(30)
        self.assertEqual(about(everything, 102), [])

    def test_isolated_io_burst_ignored(self):
        w = World(session())
        w.run_pass()
        n = [0]

        def burst(world):  # 100 KB one pass in six, no CPU
            n[0] += 1
            if n[0] % 6 == 0:
                write(world.procs[102], 100 * 1024)

        _, everything = w.wait(41, burst)
        self.assertTrue(about(everything, 102, "stop"))

    def test_regular_polling_learned_as_noise(self):
        """An idle `claude mcp serve`: ~80 KB re-read on every pass, no computing."""
        w = World(session())
        w.run_pass()

        def poll(world):
            world.procs[102].io += 80 * 1024

        _, everything = w.wait(45, poll)
        self.assertTrue(about(everything, 102, "stop"))

    def test_reading_only_is_noise(self):
        """Measured in November 2026: an idle `claude mcp serve` re-reads 121 KB per pass and writes nothing."""
        w = World(session())
        w.run_pass()

        def poll(world):
            world.procs[102].io += 121 * 1024

        _, everything = w.wait(41, poll)
        self.assertTrue(about(everything, 102, "stop"))

    def test_server_that_answers_stays_active(self):
        w = World(session())
        w.run_pass()

        def answers(world):  # reads a request, writes a small reply
            world.procs[102].io += 150 * 1024
            write(world.procs[102], 8 * 1024)

        _, everything = w.wait(60, answers)
        self.assertEqual(about(everything, 102), [])

    def test_slow_download_stays_active(self):
        w = World(session())
        w.run_pass()

        def downloads(world):  # ~33 KB/s, almost no CPU
            write(world.procs[102], 500 * 1024)

        _, everything = w.wait(90, downloads)
        self.assertEqual(about(everything, 102), [])

    def test_sustained_io_counts(self):
        w = World(session())
        w.run_pass()

        def flow(world):  # ~13 KB/s without CPU: above the noise cap
            write(world.procs[102], 200 * 1024)

        _, everything = w.wait(60, flow)
        self.assertEqual(about(everything, 102), [])

    def test_large_isolated_io_counts(self):
        w = World(session())
        w.run_pass()
        w.wait(30)
        w.procs[102].io += 5 * 1024 * 1024
        u, _ = w.wait(0.25)
        self.assertEqual(u[102].state, "active")

    def test_background_hum_does_not_count(self):
        w = World(session())
        w.run_pass()

        def hums(world):
            world.procs[102].cpu += 1         # 0.01 s per 15 s
            world.procs[102].io += 2 * 1024   # 2 KB per 15 s

        _, everything = w.wait(41, hums)
        self.assertTrue(about(everything, 102, "stop"))

    def test_new_outside_connections_count(self):
        w = World(session())
        w.run_pass()
        n = [0]

        def browser(world):  # a tab that reloads: one new connection per pass
            n[0] += 1
            world.procs[102].peers = {f"s{n[0]}": None}

        _, everything = w.wait(60, browser)
        self.assertEqual(about(everything, 102), [])

    def test_open_silent_connection_does_not_count(self):
        w = World(session())
        w.procs[102].peers = {"forgotten-tab": None}
        w.run_pass()
        _, everything = w.wait(41)
        self.assertTrue(about(everything, 102, "stop"))

    def test_connection_inside_the_unit_does_not_count(self):
        """A game editor: its own worker processes keep reconnecting to it."""
        procs = session() + [proc(310, 1, "Unity", "/opt/Unity -projectPath x", env={"CLAUDE_PID": "100"},
                                  gui=True, pss=9000),
                             proc(311, 310, "UnityWorker", "/opt/AssetImportWorker", env={"CLAUDE_PID": "100"})]
        w = World(procs)
        w.run_pass()
        n = [0]

        def worker(world):
            n[0] += 1
            world.procs[310].peers = {f"w{n[0]}": 311}

        u, everything = w.wait(61, worker)
        self.assertTrue(about(everything, 310, "ask"), "the editor ends up reported as idle")

    def test_activity_of_a_descendant_protects_the_unit(self):
        w = World(session() + [proc(103, 102, "node", "node vite", env={"CLAUDECODE": "1"}, start=2000)])
        w.run_pass()

        def works(world):
            write(world.procs[103], 200 * 1024)

        _, everything = w.wait(60, works)
        self.assertEqual(about(everything, 102), [])

    def test_mcp_inherits_from_a_used_session_the_task_does_not(self):
        w = World(session())
        w.run_pass()

        def heartbeat(world):
            world.heartbeats[100] = world.t  # a hook fired on every pass

        u, everything = w.wait(60, heartbeat)
        self.assertEqual(about(everything, 101), [])
        self.assertEqual(u[101].state, "active")
        self.assertTrue(about(everything, 102, "stop"), "a forgotten task does not inherit from the session")

    def test_agent_background_writes_do_not_keep_its_mcp(self):
        w = World(session())
        w.run_pass()

        def session_log(world):  # the idle agent writes its files, no heartbeat, no computing
            write(world.procs[100], 200 * 1024)

        _, everything = w.wait(41, session_log)
        self.assertTrue(about(everything, 101, "stop"))

    def test_agent_writing_a_reply_keeps_its_mcp(self):
        w = World(session())
        w.run_pass()

        def writes_reply(world):
            world.procs[100].cpu += 4 * HZ  # ~27 % of a core

        _, everything = w.wait(60, writes_reply)
        self.assertEqual(about(everything, 101), [])

    def test_mcp_of_an_abandoned_session_stopped(self):
        w = World(session())
        w.run_pass()
        _, everything = w.wait(41)
        self.assertTrue(about(everything, 101, "stop"))

    def test_agent_and_infra_never_stopped_for_inactivity(self):
        w = World(session() + [proc(320, 1, "codex-code-mode-host", "/x/codex-code-mode-host",
                                    env={"CLAUDECODE": "1"})])
        w.run_pass()
        u, everything = w.wait(180)
        self.assertEqual(about(everything, 100), [])
        self.assertEqual(about(everything, 320), [])
        self.assertEqual(u[100].state, "idle")

    def test_for_user_asked_after_one_hour_never_stopped(self):
        w = World(session() + [proc(310, 1, "Unity", "/opt/Unity -projectPath x", env={"CLAUDE_PID": "100"},
                                    gui=True, pss=9000)])
        w.run_pass()
        _, everything = w.wait(59)
        self.assertEqual(about(everything, 310), [])
        u, everything = w.wait(2)
        self.assertTrue(about(everything, 310, "ask"))
        self.assertFalse(about(everything, 310, "stop"))
        w.state["units"][u[310].key]["asked"] = {"t": w.t}  # the dialog is open
        _, everything = w.wait(5)
        self.assertEqual(about(everything, 310), [], "no second dialog while the first is open")
        w.state["units"][u[310].key].pop("asked")
        w.state["units"][u[310].key]["answer_t"] = w.t  # the user said "keep"
        _, everything = w.wait(59)
        self.assertEqual(about(everything, 310), [], "not asked again within the hour")
        _, everything = w.wait(2)
        self.assertTrue(about(everything, 310, "ask"))

    def test_suspend_gap_does_not_count(self):
        w = World(session())
        w.run_pass()
        w.wait(10)
        _, d, events = w.run_pass(advance_s=3 * 3600)
        self.assertEqual(about(d, 102), [])
        self.assertTrue(any(e["event"] == "resumed" for e in events))
        _, everything = w.wait(29)
        self.assertEqual(about(everything, 102), [], "10 + 29 min observed: not 40 yet")
        _, everything = w.wait(2)
        self.assertTrue(about(everything, 102, "stop"))

    def test_keep_mark(self):
        w = World(session())
        w.run_pass()
        w.marks[w.procs[102].key] = {"until": w.t + 2 * 3600}
        _, everything = w.wait(90)
        self.assertEqual(about(everything, 102), [])

    def test_unit_of_the_guard_itself_protected(self):
        w = World(session())
        w.run_pass(self_pid=102)
        _, everything = w.wait(60, self_pid=102)
        self.assertEqual(about(everything, 102), [])

    def test_journal_records_the_transitions(self):
        w = World(session())
        _, _, e0 = w.run_pass()
        self.assertEqual({e["pid"] for e in e0 if e["event"] == "appeared"}, {100, 101, 102})
        events = []
        for _ in range(4 * 21):
            events += w.run_pass()[2]
        self.assertTrue(any(e["event"] == "idle" and e["pid"] == 102 for e in events))
        del w.procs[102]
        _, _, e1 = w.run_pass()
        self.assertTrue(any(e["event"] == "gone" and e["pid"] == 102 for e in e1))


class Trace(unittest.TestCase):
    def test_stop_then_restart_flagged(self):
        w = World(session())
        w.run_pass()
        _, everything = w.wait(41)
        u = next(d[1] for d in everything if d[0] == "stop" and d[1].pid == 102)
        # what run_pass() records after the stop
        w.state["stopped"] = [{"sig": rg.signature(u.cmd), "cwd": u.cwd, "t": w.t, "pid": 102, "reason": "x",
                               "agent": 100, "session": ""}]
        del w.procs[102]
        w.run_pass()
        w.procs[202] = proc(202, 100, "bash", "/bin/bash -c npm run dev", env={"CLAUDECODE": "1"},
                            start=99999 * HZ)
        _, _, events = w.run_pass()
        e = [e for e in events if e["event"] == "restarted_after_stop"]
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["old_pid"], 102)

    def restart_by(self, session_of_stopped, new_agent, new_session):
        """A unit of session `session_of_stopped` was stopped; the same command, in the same folder,
        then appears under `new_agent` with `new_session`. Returns the restart events."""
        w = World(session())
        w.run_pass()
        w.state["stopped"] = [{"sig": rg.signature("python3 -m some_mcp_server"), "cwd": "", "t": w.t, "pid": 101,
                               "reason": "x", "agent": 100, "session": session_of_stopped}]
        w.procs[new_agent] = proc(new_agent, 1, "claude", "claude --model haiku -p", start=99990 * HZ)
        w.procs[new_agent + 1] = proc(new_agent + 1, new_agent, "python3", "python3 -m some_mcp_server",
                                      env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": new_session}, start=99991 * HZ)
        _, _, events = w.run_pass()
        return [e for e in events if e["event"] == "restarted_after_stop"]

    def test_same_command_from_another_session_not_flagged(self):
        """Seen on a real machine: a 16-second `claude --model haiku -p` of another session, started in the
        same folder, loaded the same MCP servers. That is not the stopped session asking for them back."""
        self.assertEqual(self.restart_by("session-a", 500, "session-b"), [])

    def test_session_resumed_by_a_new_agent_flagged(self):
        self.assertEqual(len(self.restart_by("session-a", 600, "session-a")), 1)

    def test_other_agent_without_sessions_not_flagged(self):
        """Codex children carry no session id: then the agents are compared."""
        w = World(session())
        w.run_pass()
        w.state["stopped"] = [{"sig": rg.signature("python3 -m some_mcp_server"), "cwd": "", "t": w.t, "pid": 101,
                               "reason": "x", "agent": 100, "session": ""}]
        w.procs[700] = proc(700, 1, "codex", "codex app-server", start=99990 * HZ)
        w.procs[701] = proc(701, 700, "python3", "python3 -m some_mcp_server", env={"CODEX_SHELL": "1"},
                            start=99991 * HZ)
        _, _, events = w.run_pass()
        self.assertEqual([e for e in events if e["event"] == "restarted_after_stop"], [])

    def test_unknown_owner_flagged(self):
        """A detached process without agent: nothing proves the restart is someone else's, so it counts."""
        w = World(session())
        w.run_pass()
        w.state["stopped"] = [{"sig": rg.signature("node forgotten-server.js"), "cwd": "", "t": w.t, "pid": 340,
                               "reason": "x", "agent": None, "session": ""}]
        w.procs[341] = proc(341, 1, "node", "node forgotten-server.js", env={"CLAUDECODE": "1"}, start=99990 * HZ)
        _, _, events = w.run_pass()
        self.assertEqual(len([e for e in events if e["event"] == "restarted_after_stop"]), 1)

    def test_late_restart_not_flagged(self):
        w = World(session())
        w.run_pass()
        w.state["stopped"] = [{"sig": rg.signature("/bin/bash -c npm run dev"), "cwd": "", "t": w.t, "pid": 102}]
        w.wait(31)
        w.procs[202] = proc(202, 100, "bash", "/bin/bash -c npm run dev", env={"CLAUDECODE": "1"},
                            start=99999 * HZ)
        _, _, events = w.run_pass()
        self.assertFalse([e for e in events if e["event"] == "restarted_after_stop"])

    def test_last_signal_kept(self):
        w = World(session())
        w.run_pass()
        w.procs[102].cpu += 2 * HZ
        u, _, _ = w.run_pass()
        self.assertTrue(u[102].info["signal"].startswith("CPU"))
        _, everything = w.wait(41)
        d = about(everything, 102, "stop")[0]
        self.assertTrue(d[1].info["signal"].startswith("CPU"), "the stop says what the last sign of life was")


class Saturation(unittest.TestCase):
    def test_order_idle_then_priority_agents_last(self):
        procs = session() + [
            proc(300, 1, "python", "python3 orphan.py", env={"CLAUDE_PID": "4242"}, pss=2000),
            proc(104, 100, "node", "node build.js", env={"CLAUDECODE": "1"}, pss=19000, start=9000 * HZ),
        ]
        w = World(procs)
        w.run_pass()

        def build(world):
            world.procs[104].cpu += 10 * HZ

        w.wait(5, build)
        _, d, _ = w.run_pass()
        stops = [x[1].pid for x in d if x[0] == "stop"]
        self.assertEqual(stops[:1], [300], "the idle orphan goes first")
        self.assertNotIn(100, stops, "the agent is untouched when the rest is enough")
        self.assertLess(stops.index(101), stops.index(104), "idle MCP before the active build")

    def test_for_user_outside_the_agents_cap(self):
        w = World(session() + [proc(310, 1, "Unity", "/opt/Unity -projectPath x", env={"CLAUDE_PID": "100"},
                                    gui=True, pss=19000)])
        w.run_pass()
        _, d, _ = w.run_pass()
        self.assertEqual(d, [])

    def test_no_stop_without_history_if_the_system_is_fine(self):
        procs = session() + [proc(104, 100, "node", "node big.js", env={"CLAUDECODE": "1"}, pss=25000)]
        _, d, _ = World(procs).run_pass()
        self.assertEqual(d, [])

    def test_system_saturated_by_agents_acts_even_without_history(self):
        procs = session() + [proc(104, 100, "node", "node big.js", env={"CLAUDECODE": "1"}, pss=18000)]
        _, d, _ = World(procs, mem_total=64000, mem_available=3000).run_pass()
        self.assertTrue(any(x[0] == "stop" and x[1].pid == 104 for x in d))
        self.assertFalse(any(x[1].pid == 100 for x in d), "the agent is untouched when the rest is enough")

    def test_pressure_from_outside_stops_only_idle_units(self):
        procs = session() + [proc(104, 100, "node", "node build.js", env={"CLAUDECODE": "1"}, pss=3000,
                                  start=9000 * HZ)]
        w = World(procs, mem_total=64000, mem_available=3000)  # 95 % used, mostly outside the agents

        def build(world):
            world.procs[104].cpu += 10 * HZ

        w.run_pass()
        _, everything = w.wait(25, build)
        pids = {x[1].pid for x in everything if x[0] == "stop"}
        self.assertIn(101, pids, "the MCP server idle for 20 min may go")
        self.assertNotIn(104, pids, "the active build stays")
        self.assertNotIn(100, pids, "no agent for pressure from elsewhere")
        events = w.run_pass()[2]
        self.assertTrue(any("outside" in e.get("reason", "") for e in events if e["event"] == "saturation"))

    def test_codex_desktop_server_never_stopped(self):
        procs = session() + [
            proc(400, 1, "ChatGPT", "/usr/lib/chatgpt/ChatGPT"),
            proc(401, 400, "codex", "/usr/lib/chatgpt/resources/codex app-server", pss=30000),
        ]
        w = World(procs)
        w.run_pass()
        _, d, _ = w.run_pass()
        self.assertFalse(any(x[1].pid == 401 for x in d))

    def restarted_build(self, mem_available):
        """A build stopped once in saturation, then started again by the same agent."""
        w = World(session(), mem_available=mem_available)
        w.run_pass()
        w.state["stopped"] = [{"sig": rg.signature("node build.js"), "cwd": "", "t": w.t, "pid": 103,
                               "reason": "saturation", "agent": 100, "session": ""}]
        w.procs[104] = proc(104, 100, "node", "node build.js", env={"CLAUDECODE": "1", "CLAUDE_PID": "100"},
                            pss=22000, start=99999 * HZ)
        w.cgroups[104] = "/app-claude.scope"
        return w.run_pass()

    def test_restart_spared_over_the_cap(self):
        """Seen on a real machine: Unity compilations stopped over the agents' cap, started again at
        once by the agent, stopped again: 162 times in two days. Over the cap alone, the restart stays."""
        _, d, events = self.restarted_build(mem_available=40000)
        self.assertTrue(any(e["event"] == "restarted_after_stop" for e in events))
        stops = [x[1].pid for x in d if x[0] == "stop"]
        self.assertNotIn(104, stops, "the restarted build is not stopped a second time")
        self.assertNotIn(100, stops, "nor is the agent, to make room for it")
        self.assertIn(101, stops, "the rest still goes first")
        sat = [e for e in events if e["event"] == "saturation"]
        self.assertIn("spared", sat[-1]["reason"])

    def test_restart_stopped_when_the_system_is_full(self):
        _, d, _ = self.restarted_build(mem_available=3000)
        stops = [x[1].pid for x in d if x[0] == "stop"]
        self.assertIn(104, stops, "a saturated system still reaches it")
        self.assertLess(stops.index(101), stops.index(104), "but after everything else")
        self.assertLess(stops.index(102), stops.index(104))

    def test_agent_as_last_resort(self):
        procs = session()
        procs[3].pss = 30000
        w = World(procs)
        w.run_pass()
        _, d, _ = w.run_pass()
        stops = [x[1].pid for x in d if x[0] == "stop"]
        self.assertEqual(stops[-1], 100)


class StopProcesses(unittest.TestCase):
    def test_stop_a_real_process_through_pidfd(self):
        child = subprocess.Popen(["sleep", "300"])
        try:
            time.sleep(0.2)
            p = rg.read_base(child.pid)
            res = rg.stop_processes([p], grace=2)
            child.wait(timeout=5)
            self.assertEqual(res[0][1], "stopped by SIGTERM")
        finally:
            child.kill()
            child.wait()

    def test_recycled_pid_untouched(self):
        child = subprocess.Popen(["sleep", "300"])
        try:
            time.sleep(0.2)
            p = rg.read_base(child.pid)
            p.start += 1  # as if the pid now named another process
            res = rg.stop_processes([p], grace=1)
            self.assertEqual(res[0][1], "already gone")
            self.assertIsNone(child.poll(), "the newcomer received no signal")
        finally:
            child.kill()
            child.wait()


class Rules(unittest.TestCase):
    def test_broken_file_no_stop_for_inactivity(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write("[categories.task\npriority = ")
            f.flush()
            msgs = []
            cats, rules = rg.load_rules(f.name, warn=msgs.append, local=None)
        self.assertTrue(msgs)
        self.assertTrue(all(c["stop_min"] is False for c in cats.values()))
        self.assertEqual(cats["for-user"]["priority"], 4)

    def test_local_rules_come_first_and_complete(self):
        with tempfile.TemporaryDirectory() as d:
            shipped, local = Path(d) / "shipped.toml", Path(d) / "local.toml"
            shipped.write_text('[categories.task]\nidle_min = 20\n[[rule]]\npattern = "demo"\ncategory = "task"\n')
            local.write_text('[categories.task]\nstop_min = 60\n[[rule]]\npattern = "demo"\ncategory = "infra"\n')
            cats, rules = rg.load_rules(shipped, warn=lambda m: None, local=local)
        self.assertEqual((cats["task"]["idle_min"], cats["task"]["stop_min"]), (20, 60))
        self.assertEqual(rules[0][1], "infra", "the local rule comes first")

    def test_broken_local_rules_suspend_stops(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "local.toml"
            local.write_text("[[rule\n")
            msgs = []
            cats, _ = rg.load_rules(rg.RULES, warn=msgs.append, local=local)
        self.assertTrue(msgs)
        self.assertTrue(all(c["stop_min"] is False for c in cats.values()))

    def test_invalid_values_replaced(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write('[categories.task]\nidle_min = "20"\nstop_min = 0\npriority = 9\n'
                    '[categories.for-user]\npriority = 4\n')
            f.flush()
            msgs = []
            cats, _ = rg.load_rules(f.name, warn=msgs.append, local=None)
        self.assertEqual((cats["task"]["idle_min"], cats["task"]["stop_min"], cats["task"]["priority"]), (20, 20, 3))
        self.assertEqual(len(msgs), 3)
        self.assertIs(cats["for-user"]["stop_min"], False, "a partial block keeps the built-in protections")
        self.assertTrue(cats["for-user"]["ask"])


class Display(unittest.TestCase):
    def test_multi_line_command_shown_on_one_line(self):
        """Found by the clean-machine install test: agents run multi-line scripts."""
        self.assertEqual(rg.redact("python3 -c '\nimport os\n\tprint(1)'"), "python3 -c ' import os print(1)'")

    def test_secrets_masked(self):
        self.assertEqual(rg.redact("tool --token=abc123 --api-key xyz"), "tool --token=*** --api-key ***")


class Journal(unittest.TestCase):
    def test_unexpected_lines_do_not_break_the_log(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "journal.jsonl"
            f.write_text('{"t": "2026-01-01T00:00:00", "event": "stop", "pid": 1}\n'
                         '{"t": "2026-01-01T00:00:01", "evt": "something else"}\n'
                         'not json\n[1, 2]\n')
            events = list(rg.journal_events([f]))
        self.assertEqual([e["event"] for e in events], ["stop", None])


class Limits(unittest.TestCase):
    def test_default_cap_is_a_third_of_memory(self):
        with tempfile.TemporaryDirectory() as d:
            shipped = Path(d) / "shipped.toml"
            shipped.write_text("")
            limits = rg.load_limits(30000, shipped, local=None)
        self.assertEqual(limits, {"agents_mb": 10000, "system_pct": 90})

    def test_local_limits_override_and_bad_values_refused(self):
        with tempfile.TemporaryDirectory() as d:
            shipped, local = Path(d) / "shipped.toml", Path(d) / "local.toml"
            shipped.write_text("[limits]\nsystem_pct = 85\n")
            local.write_text("[limits]\nagents_mb = 8192\nsystem_pct = 120\n")
            msgs = []
            limits = rg.load_limits(30000, shipped, local=local, warn=msgs.append)
        self.assertEqual(limits, {"agents_mb": 8192, "system_pct": 85})
        self.assertEqual(len(msgs), 1)

    def test_cap_applies_to_saturation(self):
        procs = session() + [proc(104, 100, "node", "node build.js", env={"CLAUDECODE": "1"}, pss=3000,
                                  start=9000 * HZ)]
        w = World(procs)
        w.limits = {"agents_mb": 2048, "system_pct": 90}
        w.run_pass()
        _, d, _ = w.run_pass()
        self.assertTrue([x for x in d if x[0] == "stop"], "above a 2 GB cap, something must go")


if __name__ == "__main__":
    unittest.main(verbosity=1)
