#!/usr/bin/env python3
"""Guard Ram for coding agents (Claude Code, Codex): inventory, inactivity, priorities.

Scope: every root agent (Claude Code, Codex) and everything it started, including
what detached from the tree (reparented to systemd) but still carries an agent's
environment variables (CLAUDECODE, CLAUDE_PID, CODEX_*). Everything is grouped
into "units": a root process and its descendants.

Every 15 s (guard-ram.timer):
  - a unit with no activity (CPU, writes, new incoming connection, heartbeat of
    its session) for 20 min becomes "idle"; 20 min later it is stopped;
  - a "for-user" unit may stay idle for 1 h, then a dialog asks the user whether
    to stop it; without an answer it is kept;
  - agents and infrastructure are never stopped for inactivity;
  - above 20 GB (summed PSS) or 90 % of system memory, units are stopped in
    order: idle first, then lowest priority, agents last of all.
Every decision goes to ~/.claude/guard-ram/journal.jsonl.

Subcommands: list, log, mark, keep, launch, heartbeat (hook).
Without a subcommand: one pass (what the timer runs). --dry-run: a pass that
stops and writes nothing.
"""
import argparse, fcntl, json, os, re, signal, subprocess, sys, time, tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".claude" / "guard-ram"
STATE_FILE = STATE_DIR / "state.json"
MARKS_FILE = STATE_DIR / "marks.json"
JOURNAL = STATE_DIR / "journal.jsonl"
HEARTBEATS = STATE_DIR / "heartbeats"
ANSWERS = STATE_DIR / "answers"
LOCK = STATE_DIR / ".lock"
LAST_STOP = STATE_DIR / "last-stop.md"
RULES = HERE / "rules.toml"
LOCAL_RULES = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "guard-ram" / "rules.toml"

DEFAULT_SYSTEM_PCT = 90       # system memory used (1 - MemAvailable/MemTotal) that triggers a saturation
DEFAULT_AGENTS_SHARE = 3       # without [limits] agents_mb, the agents' cap is a third of physical memory
TARGET_MARGIN_MB = 2 * 1024    # after stopping, aim 2 GB under the cap
TARGET_MARGIN_PCT = 5          # and 5 points under the system threshold
MAX_GAP_S = 120                # beyond this (suspend, timer stopped), unobserved time does not count
ASK_TIMEOUT_S = 15 * 60        # how long the dialog stays up before counting as "keep"
ASK_AGAIN_S = 60 * 60          # after "keep", ask again an hour later
AFK_S = 5 * 60                 # user away from the keyboard: do not ask
NOISE_MAX_KB = 40              # learned background noise is capped: a steady flow above ~8 KB/s (120 KB per pass) always counts
PSS_VALID_S = 60               # PSS re-read at most once a minute while RSS does not move
APP_HELPERS = {"chrome_crashpad", "crashpad_handler", "chrome_crashpad_handler"}
MIN_WRITE_KB = 4               # below 1 MB of I/O, at least this much must also have been written
STRONG_IO_KB = 1024            # I/O that counts on its own, without waiting for a second pass
AGENT_BUSY_PCT = 10            # CPU % of an agent writing a reply: its session is in use
RESTART_WINDOW_S = 30 * 60     # restarted less than 30 min after being stopped: probable false positive
JOURNAL_MAX = 10 * 1024 * 1024   # 5 generations: a few days of history
STATE_VERSION = 1
UID = os.getuid()
HZ = os.sysconf("SC_CLK_TCK")
SELF = os.getpid()

MARKERS = (b"CLAUDECODE=", b"CLAUDE_PID=", b"CODEX_CLI_PATH=", b"CODEX_SHELL=",
           b"CODEX_APP_TOOLS_PIPE_PATH=", b"CODEX_THREAD_ID=", b"CODEX_SANDBOX=",
           b"GUARD_RAM_CATEGORY=")
USEFUL_ENV = ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "GUARD_RAM_CATEGORY", "GUARD_RAM_PRIORITY",
              "GUARD_RAM_REASON")
SHELLS = {"bash", "zsh", "fish", "sh", "dash"}
GUI_LIBS = (b"libgtk-3", b"libgtk-4", b"libQt5Gui", b"libQt6Gui", b"libwayland-client",
            b"libX11.so", b"libSDL2", b"libglfw")

CATEGORY_DEFAULTS = {"priority": 3, "idle_min": 20, "stop_min": 20, "ask": False,
                     "cpu_pct": 2.0, "io_kb": 64, "inherit_agent": False}
CATEGORIES = ("orphan", "mcp", "task", "for-user", "infra", "agent")
# built-in values: the same as rules.toml, so that losing that file removes no protection
BUILT_IN_CATEGORIES = {
    "orphan": {"priority": 1},
    "mcp": {"priority": 2, "inherit_agent": True},
    "task": {"priority": 3},
    "for-user": {"priority": 4, "idle_min": 60, "stop_min": False, "ask": True, "cpu_pct": 10},
    "infra": {"priority": 5, "stop_min": False},
    "agent": {"priority": 5, "stop_min": False},
}

SECRETS = re.compile(r"(?i)((?:--?|\b)(?:api[-_]?key|token|secret|password|passwd|auth)[=: ]+)\S+")


# ---------------------------------------------------------------- reading /proc

@dataclass
class P:
    pid: int
    ppid: int
    start: int          # jiffies since boot
    comm: str
    argv0: str
    cmd: str
    tty: int
    cpu: int            # user + system jiffies
    env: dict
    marked_env: bool    # carries an agent variable
    pss: int = 0        # MB
    io: int = 0         # bytes (rchar + wchar)
    io_w: int = 0       # bytes written (wchar): files, pipes, sockets
    cwd: str = ""
    sockets: set = field(default_factory=set)      # TCP inodes held
    incoming: list = field(default_factory=list)   # (inode, local port, remote port) accepted on a listening port
    peers: dict = field(default_factory=dict)      # incoming inode -> pid on the other end (None: unknown)
    gui: bool = False

    @property
    def key(self):
        return f"{self.pid}:{self.start}"


def read_base(pid):
    base = f"/proc/{pid}"
    try:
        if os.stat(base).st_uid != UID:
            return None
        stat = open(base + "/stat").read()
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        f = stat[stat.rindex(")") + 2:].split()
        if f[0] == "Z":
            return None
        raw = open(base + "/cmdline", "rb").read()
        if not raw:
            return None
        argv = raw.rstrip(b"\0").split(b"\0")
        try:
            environ = open(base + "/environ", "rb").read()
        except OSError:
            environ = b""
    except (OSError, ValueError, IndexError):
        return None
    env, marked = {}, False
    for v in environ.split(b"\0"):
        if v.startswith(MARKERS):
            marked = True
        k, _, val = v.partition(b"=")
        ks = k.decode(errors="replace")
        if ks in USEFUL_ENV:
            env[ks] = val.decode(errors="replace")
    return P(pid=pid, ppid=int(f[1]), start=int(f[19]), comm=comm,
             argv0=argv[0].decode(errors="replace"),
             cmd=b" ".join(argv).decode(errors="replace"),
             tty=int(f[4]), cpu=int(f[11]) + int(f[12]), env=env, marked_env=marked)


def tcp_table():
    """inode -> (state, local port, remote port) for IPv4 and IPv6; 0A = listening, 01 = established."""
    t = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(name).read().splitlines()[1:]
        except OSError:
            continue
        for l in lines:
            c = l.split()
            if len(c) > 9 and c[3] in ("0A", "01"):
                t[c[9]] = (c[3], int(c[1].rsplit(":", 1)[1], 16), int(c[2].rsplit(":", 1)[1], 16))
    return t


PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20


def read_pss(p):
    try:
        for l in open(f"/proc/{p.pid}/smaps_rollup"):
            if l.startswith("Pss:"):
                return int(l.split()[1]) // 1024
    except OSError:
        pass
    return 0


def read_details(p, tcp, gui_cache, pss_cache=None):
    """pss_cache: key -> [pss, rss, time]. smaps_rollup is expensive (150 ms for a 10 GB editor):
    it is re-read when the measure is over a minute old or when RSS (statm, free) has moved."""
    base = f"/proc/{p.pid}"
    try:
        rss = int(open(base + "/statm").read().split()[1]) * PAGE_MB
    except (OSError, ValueError, IndexError):
        rss = 0
    c = pss_cache.get(p.key) if pss_cache is not None else None
    if c and time.time() - c[2] < PSS_VALID_S and abs(rss - c[1]) <= max(64, 0.1 * c[1]):
        p.pss = c[0]
    else:
        p.pss = read_pss(p)
        if pss_cache is not None:
            pss_cache[p.key] = [p.pss, rss, time.time()]
    try:
        for l in open(base + "/io"):
            if l.startswith(("rchar:", "wchar:")):
                n = int(l.split()[1])
                p.io += n
                if l.startswith("w"):
                    p.io_w = n
    except OSError:
        pass
    try:
        p.cwd = os.readlink(base + "/cwd")
    except OSError:
        pass
    listening, established = set(), []
    try:
        for fd in os.scandir(base + "/fd"):
            try:
                target = os.readlink(fd.path)
            except OSError:
                continue
            inode = target[8:-1] if target.startswith("socket:[") else None
            s = tcp.get(inode) if inode else None
            if s:
                p.sockets.add(inode)
                (listening.add(s[1]) if s[0] == "0A" else established.append((inode, s[1], s[2])))
    except OSError:
        pass
    # connections accepted on one of its listening ports
    p.incoming = [e for e in established if e[1] in listening]
    if p.key in gui_cache:
        p.gui = gui_cache[p.key]
    else:
        try:
            maps = open(base + "/maps", "rb").read()
            p.gui = any(l in maps for l in GUI_LIBS) and "--headless" not in p.cmd
        except OSError:
            p.gui = False
        gui_cache[p.key] = p.gui


def read_meminfo():
    info = {}
    for l in open("/proc/meminfo"):
        k, _, v = l.partition(":")
        info[k] = int(v.split()[0]) // 1024
    return info["MemTotal"], info["MemAvailable"]


def agent_kind(p):
    if p.comm == "claude" or "/claude/versions/" in p.argv0:
        return "claude"
    if p.comm == "codex":
        return "codex"
    return None


@dataclass
class Snapshot:
    procs: dict
    tracked: set           # pids in scope
    roots: set             # root agents
    detached: set          # detached roots (reparented) carrying an agent variable
    children: dict
    mem_total: int
    mem_available: int
    t: float
    by_cgroup: set = field(default_factory=set)


def read_cgroup(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read().strip().rsplit("::", 1)[-1]
    except OSError:
        return None


def scope(procs, cgroup_of=read_cgroup):
    """Root agents, detached processes and the tracked set, from the process table.
    Also returns those found only through their cgroup (cleared environment)."""
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    agents = {pid for pid, p in procs.items() if agent_kind(p)}
    ancestors, roots = set(), set()
    for a in agents:
        x, nested = procs[a].ppid, False
        while x in procs:
            nested |= x in agents
            ancestors.add(x)
            x = procs[x].ppid
        if not nested:
            roots.add(a)
    ancestors -= agents

    def subtree(start):
        seen, stack = set(), list(start)
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(children.get(x, []))
        return seen

    t0 = subtree(roots)
    marked = {pid for pid, p in procs.items() if p.marked_env} - ancestors
    detached = {pid for pid in marked - t0
                if procs[pid].ppid not in marked and procs[pid].ppid not in t0}
    # Detached AND environment cleared (env -i, sudo...): it stays in the cgroup of the application
    # that runs the agents. Only processes reparented to systemd, born after the first agent of that
    # cgroup (the application's own helpers are born before), and not known helpers, are kept.
    by_cgroup = set()
    if cgroup_of and roots:
        first = {}
        for r in roots:
            c = cgroup_of(r)
            if c:
                first[c] = min(first.get(c, procs[r].start), procs[r].start)
        managers = {pid for pid, p in procs.items() if p.comm == "systemd"} | {1}
        in_tree = subtree(detached)
        for pid, p in procs.items():
            if (pid in t0 or pid in ancestors or pid in in_tree or p.marked_env
                    or p.ppid not in managers or p.comm in APP_HELPERS):
                continue
            c = cgroup_of(pid)
            if c in first and p.start >= first[c]:
                by_cgroup.add(pid)
    detached |= by_cgroup
    tracked = (t0 | subtree(detached)) - ancestors
    return roots, detached, tracked, children, by_cgroup


def scan(gui_cache, pss_cache=None):
    procs = {}
    for e in os.scandir("/proc"):
        if e.name.isdigit():
            p = read_base(int(e.name))
            if p:
                procs[p.pid] = p
    roots, detached, tracked, children, by_cgroup = scope(procs)
    tcp = tcp_table()
    for pid in tracked:
        read_details(procs[pid], tcp, gui_cache, pss_cache)
    # who is on the other end of each incoming connection (among tracked processes only)
    owner = {i: pid for pid in tracked for i in procs[pid].sockets}
    by_ports = {(s[1], s[2]): i for i, s in tcp.items() if s[0] == "01"}
    for pid in tracked:
        p = procs[pid]
        p.peers = {i: owner.get(by_ports.get((rport, lport))) for i, lport, rport in p.incoming}
    total, available = read_meminfo()
    return Snapshot(procs, tracked, roots, detached, children, total, available, time.time(), by_cgroup)


# ---------------------------------------------------------------- rules

def validate(name, key, v, default, warn):
    """A rules.toml value of the right type and within bounds, else the built-in value."""
    number = isinstance(v, (int, float)) and not isinstance(v, bool)
    ok = {
        "priority": isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5,
        "idle_min": number and v >= 1,
        "stop_min": v is False or (number and v >= 1),
        "ask": isinstance(v, bool), "inherit_agent": isinstance(v, bool),
        "cpu_pct": number and v > 0,
        "io_kb": number and v > 0,
    }.get(key, False)
    if not ok:
        warn(f"{name}.{key} = {v!r} refused, built-in value {default!r} kept")
    return v if ok else default


def read_toml(path, warn):
    """(content, readable). A missing file is readable and empty; a broken file is not."""
    try:
        return tomllib.loads(Path(path).read_text()), True
    except FileNotFoundError:
        return {}, True
    except (OSError, tomllib.TOMLDecodeError) as e:
        warn(f"{path} unreadable ({e}): no stop for inactivity, saturation only")
        return {}, False


def load_rules(path=RULES, warn=None, local=LOCAL_RULES):
    """Shipped rules (`path`), then the machine's rules (`local`, outside the repository): its
    categories override key by key, its [[rule]] entries are tried before the shipped ones."""
    warn = warn or (lambda m: print(f"guard-ram: {m}", file=sys.stderr))
    if Path(path).exists():
        raw, readable = read_toml(path, warn)
    else:
        warn(f"{path} missing: no stop for inactivity, saturation only")
        raw, readable = {}, False
    own_raw, own_readable = read_toml(local, warn) if local else ({}, True)
    # a broken file: built-in values, and no stop for inactivity while it stays broken
    cautious = not (readable and own_readable)

    def table(d, key, kind):
        v = d.get(key, kind())
        return v if isinstance(v, kind) else kind()

    cats = {}
    for name in CATEGORIES:
        base = {**CATEGORY_DEFAULTS, **BUILT_IN_CATEGORIES[name]}
        own = {**table(table(raw, "categories", dict), name, dict),
               **table(table(own_raw, "categories", dict), name, dict)}
        cats[name] = {k: validate(name, k, own[k], d, warn) if k in own else d for k, d in base.items()}
        if cautious:
            cats[name]["stop_min"] = False
    rules = []
    for r in table(own_raw, "rule", list) + table(raw, "rule", list):
        try:
            rules.append((re.compile(r["pattern"]), r["category"], r.get("reason", "")))
        except (KeyError, TypeError, re.error) as e:
            warn(f"rule ignored ({e})")
    return cats, rules


def load_limits(mem_total, path=RULES, local=LOCAL_RULES, warn=None):
    """[limits] of the shipped then local rules: agents_mb (cap on the agents' summed PSS, 0 or
    absent = a third of physical memory) and system_pct (system memory threshold)."""
    warn = warn or (lambda m: print(f"guard-ram: {m}", file=sys.stderr))
    limits = {"agents_mb": mem_total // DEFAULT_AGENTS_SHARE, "system_pct": DEFAULT_SYSTEM_PCT}
    for f in (path, local):
        if not f:
            continue
        raw, _ = read_toml(f, lambda m: None)  # unreadable files are already reported by load_rules
        own = raw.get("limits", {}) if isinstance(raw.get("limits", {}), dict) else {}
        v = own.get("agents_mb")
        if v is not None:
            if isinstance(v, int) and not isinstance(v, bool) and (v == 0 or v >= 256):
                limits["agents_mb"] = v or mem_total // DEFAULT_AGENTS_SHARE
            else:
                warn(f"limits.agents_mb = {v!r} refused (0 or at least 256), {limits['agents_mb']} kept")
        v = own.get("system_pct")
        if v is not None:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 50 <= v <= 99:
                limits["system_pct"] = v
            else:
                warn(f"limits.system_pct = {v!r} refused (50 to 99), {limits['system_pct']} kept")
    return limits


# ---------------------------------------------------------------- analysis

@dataclass
class Unit:
    key: str
    pid: int
    members: list
    agent: int | None
    agent_type: str | None
    session: str
    cwd: str
    cmd: str
    category: str = "task"
    priority: int = 3
    reason: str = ""
    pss: int = 0
    active_now: bool = False
    idle_s: float = 0
    state: str = "active"
    protected: bool = False
    info: dict = field(default_factory=dict)


def redact(cmd, n=200):
    """One line, obvious secrets masked: agents often run multi-line scripts (`bash -c`, `python -c`)."""
    return SECRETS.sub(r"\1***", " ".join(cmd.split()))[:n]


def build_units(snap, marks, self_pid=SELF):
    procs, tracked, children = snap.procs, snap.tracked, snap.children
    roots = set(snap.roots)
    unit_roots = set(roots) | set(snap.detached)
    for r in roots:
        unit_roots |= {c for c in children.get(r, []) if c in tracked}
    for pid in tracked:
        p = procs[pid]
        parent = procs.get(p.ppid)
        category = p.env.get("GUARD_RAM_CATEGORY")
        if p.key in marks or (category and (parent is None or parent.env.get("GUARD_RAM_CATEGORY") != category)):
            unit_roots.add(pid)
    agent_of = {}
    for pid in tracked:
        x = pid
        while x in procs and x not in roots:
            x = procs[x].ppid
        agent_of[pid] = x if x in roots else None
    own_lineage, x = set(), self_pid
    while x in procs:
        own_lineage.add(x)
        x = procs[x].ppid
    units = {}
    for r in unit_roots:
        members, stack = [], [r]
        while stack:
            x = stack.pop()
            members.append(x)
            stack.extend(c for c in children.get(x, []) if c in tracked and c not in unit_roots)
        p = procs[r]
        agent = agent_of.get(r)
        if agent is None and "CLAUDE_PID" in p.env:
            try:
                agent = int(p.env["CLAUDE_PID"])
            except ValueError:
                agent = None
        agent_type = agent_kind(procs[agent]) if agent in procs else None
        u = Unit(key=p.key, pid=r, members=members, agent=agent, agent_type=agent_type,
                 session=p.env.get("CLAUDE_CODE_SESSION_ID", ""), cwd=p.cwd,
                 cmd=redact(p.cmd), pss=sum(procs[m].pss for m in members))
        parent_agent = agent_of.get(r) if p.ppid == agent_of.get(r) else None
        u.info = {
            "root_agent": r in roots,
            "by_cgroup": r in snap.by_cgroup,
            "agent_gone": agent is not None and (agent not in procs or not agent_kind(procs[agent])),
            "gui": any(procs[m].gui for m in members),
            "shell": p.comm in SHELLS and p.tty != 0 and " -c " not in f" {p.cmd} ",
            # tool server: direct child of the agent, started with it or named "mcp"
            "mcp": parent_agent is not None and p.comm not in SHELLS
                   and (abs(p.start - procs[parent_agent].start) <= 180 * HZ or "mcp" in p.cmd.lower()),
        }
        u.protected = bool(set(members) & own_lineage)
        units[u.key] = u
    for u in units.values():
        if u.info["root_agent"] and not u.session:
            u.session = next((v.session for v in units.values() if v.agent == u.pid and v.session), "")
    return units


def classify(u, snap, cats, rules, marks):
    p = snap.procs[u.pid]
    m = marks.get(u.key) or {}
    if m.get("category"):
        u.category, u.reason = m["category"], m.get("reason") or "mark"
    elif p.env.get("GUARD_RAM_CATEGORY"):
        u.category, u.reason = p.env.get("GUARD_RAM_CATEGORY"), p.env.get("GUARD_RAM_REASON") or "guard-ram launch"
    elif u.info["root_agent"]:
        u.category, u.reason = "agent", "root agent"
    else:
        for pattern, cat, reason in rules:
            if pattern.search(p.cmd):
                u.category, u.reason = cat, reason or f"rule {pattern.pattern}"
                break
        else:
            graphical = u.info["gui"] or u.info["shell"]
            if u.info["agent_gone"]:
                u.category, u.reason = ("for-user", "window or shell, agent gone") if graphical \
                    else ("orphan", f"agent {u.agent} gone")
            elif u.info["mcp"]:
                u.category, u.reason = "mcp", "tool started with the session"
            elif u.info["by_cgroup"] and not graphical:
                u.category, u.reason = "task", "detached without agent variables, found by its cgroup"
            elif graphical:
                u.category, u.reason = "for-user", "graphical window" if u.info["gui"] else "interactive shell"
            else:
                u.category, u.reason = "task", "started by an agent"
    if u.category not in cats:
        u.reason += f" (unknown category '{u.category}', treated as task)"
        u.category = "task"
    u.priority = cats[u.category]["priority"]
    prio = m.get("priority") or p.env.get("GUARD_RAM_PRIORITY")
    if prio:
        try:
            u.priority = max(1, min(5, int(prio)))
        except ValueError:
            pass


def cpu_pct(p, proc_state, dt):
    cpu0 = (proc_state.get(p.key) or (0, 0))[0]
    return (p.cpu - cpu0) / HZ / dt * 100 if dt > 0 else 0


def activity(u, snap, proc_state, conn_state, dt, c, burst_before, noise_kb=0.0):
    """(signal, burst, io_kb): what proves the unit served since the previous pass (None if
    nothing), whether its I/O passed the threshold on this pass (for the noise filter), and its I/O."""
    members, sig, total_io, written = set(u.members), None, 0, 0
    for pid in u.members:
        p = snap.procs[pid]
        prev = proc_state.get(p.key) or (0, 0, 0)
        io0, iow0 = prev[1], (prev[2] if len(prev) > 2 else p.io_w)
        known = set(conn_state.get(p.key, ()))
        # an open, silent connection is not use, nor a member talking to its own unit
        new = [i for i, peer in p.peers.items() if i not in known and peer not in members]
        pct, dio = cpu_pct(p, proc_state, dt), p.io - io0
        if sig is None and new:
            sig = f"new incoming connection (pid {p.pid})"
        if sig is None and pct >= c["cpu_pct"]:
            sig = f"CPU {pct:.0f} % (pid {p.pid})"
        if sig is None and dio >= STRONG_IO_KB * 1024:
            sig = f"I/O {dio // 1024} KB (pid {p.pid})"
        total_io += max(0, dio)
        written += max(0, p.io_w - iow0)
    # I/O alone below 1 MB: above the threshold AND three times the unit's learned background noise
    # (polling a settings file...), on two passes in a row
    threshold = max(c["io_kb"], 3 * min(noise_kb, NOISE_MAX_KB)) * 1024
    # reading without writing anything (polling a settings file) is not serving: a server that
    # answers, a download, a build all write
    burst = total_io >= threshold and written >= MIN_WRITE_KB * 1024
    if sig is None and burst and burst_before:
        sig = f"I/O {total_io // 1024} KB on two passes in a row"
    return sig, burst, total_io / 1024


def agent_heartbeats():
    """agent pid -> time of the last heartbeat sent by the hooks."""
    r = {}
    try:
        for f in os.scandir(HEARTBEATS):
            m = re.match(r"(?:claude|codex)-(\d+)\.json$", f.name)
            if m:
                r[int(m.group(1))] = f.stat().st_mtime
    except OSError:
        pass
    return r


def same_owner(record, u):
    """A restart counts against the guard only if it comes from the session, or the agent, that lost
    the process. The same command started by another agent in the same folder (a one-shot
    `claude -p` that loads every MCP server for a few seconds) proves nothing."""
    if record.get("session") and u.session:
        return record["session"] == u.session
    if record.get("agent") is not None and u.agent is not None:
        return record["agent"] == u.agent
    return True  # owner unknown on one side (a detached process without agent): assume the worst


def signature(cmd):
    """Command line without its long numbers (pids, ports, timestamps): to recognise a restart."""
    return re.sub(r"\d{3,}", "#", cmd)


def event(name, u, **extra):
    return {"event": name, "unit": u.key, "pid": u.pid, "cat": u.category, "prio": u.priority,
            "pss": u.pss, "agent": u.agent, "agent_type": u.agent_type, "session": u.session,
            "cwd": u.cwd, "cmd": u.cmd, "members": len(u.members), **extra}


def analyse(snap, state, marks, cats, rules, heartbeats, self_pid=SELF, limits=None):
    """Computes units, states and decisions. Pure: stops nothing, writes nothing."""
    limits = limits or {"agents_mb": snap.mem_total // DEFAULT_AGENTS_SHARE, "system_pct": DEFAULT_SYSTEM_PCT}
    t = snap.t
    events, decisions = [], []
    previous = state.get("t")
    dt = t - previous if previous else 0
    shift = 0
    if previous and dt > MAX_GAP_S:
        shift = dt - 15
        events.append({"event": "resumed", "gap_s": round(dt), "reason": "unobserved time, countdown shifted"})
        dt = 15
    old_units = state.get("units", {})
    proc_state = state.get("proc", {})
    conn_state = state.get("conn", {})
    recent = [x for x in state.get("stopped", []) if t - x["t"] <= RESTART_WINDOW_S + shift]
    units = build_units(snap, marks, self_pid)
    for u in units.values():
        classify(u, snap, cats, rules, marks)
    new_units, agent_used = {}, {}
    # agents first: their MCP servers inherit their use
    for u in sorted(units.values(), key=lambda u: not u.info["root_agent"]):
        c = cats[u.category]
        a = old_units.get(u.key)
        if a is None:
            a = {"first": t, "active": t, "used": t, "state": "active", "signal": "new unit", "signal_t": t}
            events.append(event("appeared", u, reason=u.reason))
            sig, burst = "new unit", False
            fingerprint = signature(u.cmd)
            for x in recent:
                if x["sig"] == fingerprint and x["cwd"] == u.cwd and same_owner(x, u):
                    events.append(event("restarted_after_stop", u, old_pid=x["pid"], stop_reason=x.get("reason", ""),
                                        reason=f"started again {round((t - x['t']) / 60)} min after being stopped: "
                                               f"probable false positive"))
                    # its owner asked for it again: saturation will pick it only as a last resort
                    a["restarted"] = True
                    break
        else:
            a = dict(a)
            if shift:
                a["active"] += shift
                if a.get("answer_t"):
                    a["answer_t"] += shift
                if a.get("asked"):
                    a["asked"] = {"t": a["asked"]["t"] + shift}
                for k in ("used", "signal_t"):
                    if a.get(k):
                        a[k] += shift
            sig, burst, io_kb = activity(u, snap, proc_state, conn_state, dt, c, a.get("burst", False),
                                         a.get("noise_kb", 0.0))
            if sig is None or sig.startswith("I/O") and "two passes" in sig:
                # background noise is learned on passes without real work
                a["noise_kb"] = round(0.9 * a.get("noise_kb", 0.0) + 0.1 * io_kb, 1)
        a["burst"] = burst
        fresh_heartbeat = u.info["root_agent"] and heartbeats.get(u.pid, 0) >= (previous or t) - 1
        if fresh_heartbeat:
            sig = sig or "session heartbeat (hook)"
        if (marks.get(u.key) or {}).get("until", 0) > t:
            sig = sig or "reprieve (guard-ram keep)"
        ok = sig is not None
        if ok:
            a["active"], a["signal"], a["signal_t"] = t, sig, t
        if u.info["root_agent"]:
            # real use of the session: a hook heartbeat, or the agent clearly computing.
            # Its background writes (session log) do not count: its MCP servers do not inherit them.
            if fresh_heartbeat or (previous and cpu_pct(snap.procs[u.pid], proc_state, dt) >= AGENT_BUSY_PCT):
                a["used"] = t
            agent_used[u.pid] = a.get("used", a["first"])
        elif c["inherit_agent"] and u.agent_type == "claude" and agent_used.get(u.agent, 0) > a["active"]:
            a["active"], a["signal"], a["signal_t"] = agent_used[u.agent], "its session is in use", agent_used[u.agent]
        u.active_now = ok
        u.info["signal"], u.info["signal_t"] = a.get("signal"), a.get("signal_t")
        u.info["restarted"] = a.get("restarted", False)
        u.idle_s = max(0, t - a["active"])
        idle_after, stop_after = c["idle_min"] * 60, c["stop_min"]
        old_state = a.get("state", "active")
        if u.idle_s < idle_after:
            u.state = "active"
            if old_state != "active":
                events.append(event("active_again", u, reason=f"activity again (was {old_state})"))
            a.pop("asked", None)
        else:
            u.state = "asked" if a.get("asked") else "idle"
            if old_state == "active":
                events.append(event("idle", u, reason=f"no activity for {round(u.idle_s / 60)} min"))
            if u.protected or a.get("asked"):
                pass
            elif c["ask"]:
                if t - a.get("answer_t", 0) >= ASK_AGAIN_S:
                    decisions.append(("ask", u, "dialog to the user"))
            elif stop_after is not False and u.idle_s >= idle_after + stop_after * 60:
                decisions.append(("stop", u, f"idle for {round(u.idle_s / 60)} min "
                                             f"(thresholds {c['idle_min']} + {stop_after} min)"))
        a["state"] = u.state
        a.update(cat=u.category, prio=u.priority, pid=u.pid, cmd=u.cmd[:120])
        new_units[u.key] = a
    for key, a in old_units.items():
        if key not in units:
            events.append({"event": "gone", "unit": key, "pid": a.get("pid"), "cat": a.get("cat"),
                           "cmd": a.get("cmd", ""), "life_min": round((t - a.get("first", t)) / 60)})
    # without history, nobody is known to be idle: act only if the system itself saturates
    pct = 100 * (snap.mem_total - snap.mem_available) / snap.mem_total
    if previous or pct >= limits["system_pct"]:
        decisions += saturation(snap, units, decisions, events, heartbeats, limits)
    new_state = {"version": STATE_VERSION, "t": t, "units": new_units, "stopped": recent,
                 "proc": {snap.procs[p].key: [snap.procs[p].cpu, snap.procs[p].io, snap.procs[p].io_w]
                          for p in snap.tracked},
                 "conn": {snap.procs[p].key: list(snap.procs[p].peers) for p in snap.tracked if snap.procs[p].peers}}
    return units, decisions, events, new_state


def agents_total(snap, units):
    """PSS counted against the agents' cap: the whole scope except what was started for the user.
    for-user units (an editor, windows) count only towards the system threshold."""
    for_user = {m for u in units.values() if u.category == "for-user" for m in u.members}
    return sum(snap.procs[p].pss for p in snap.tracked if p not in for_user)


def saturation(snap, units, decisions, events, heartbeats, limits):
    cap, threshold = limits["agents_mb"], limits["system_pct"]
    total = agents_total(snap, units)
    used = snap.mem_total - snap.mem_available
    pct = 100 * used / snap.mem_total
    if total <= cap and pct < threshold:
        return []
    chosen = {d[1].key for d in decisions if d[0] == "stop"}
    freed = sum(units[k].pss for k in chosen)
    target_mb = cap - min(TARGET_MARGIN_MB, cap // 10)
    target_sys = snap.mem_total * (threshold - TARGET_MARGIN_PCT) / 100
    cause = (f"system memory at {pct:.0f} % (threshold {threshold} %)" if pct >= threshold
             else f"agents at {total} MB (cap {cap} MB)")
    events.append({"event": "saturation", "reason": cause, "pss_total": total, "ram_pct": round(pct, 1)})

    def enough():
        not_for_user = sum(units[k].pss for k in chosen if units[k].category != "for-user")
        return total - not_for_user <= target_mb and used - freed <= target_sys

    out = []
    # Pressure from elsewhere (browser, virtual machine, model server): the agents' cap holds, and
    # even stopping everything would not be enough. Then only idle units are stopped, never what is
    # in use and never agents.
    outside = total <= cap and used - total > target_sys
    if outside:
        events[-1]["reason"] += "; cause outside the agents: only idle units are stopped"
    # idle (20-minute state, not the last 15 seconds) first, then lowest priority, then longest
    # idle, then largest. What its owner started again after a stop comes after everything else:
    # stopping it again only makes the agent start it a third time (seen with Unity compilations).
    # Over the agents' cap alone, it is spared, and so are the agents; only a saturated system
    # (pct >= threshold) reaches it.
    system_full = pct >= threshold
    pool = [u for u in units.values()
            if u.key not in chosen and not u.protected and not u.info["root_agent"]
            and not (outside and u.state == "active")]
    spared = [u for u in pool if u.info.get("restarted") and not system_full]
    candidates = sorted((u for u in pool if u not in spared),
                        key=lambda u: (u.info.get("restarted", False), u.state == "active", u.priority,
                                       -u.idle_s, -u.pss))
    for u in candidates:
        if enough():
            return out
        again = "started again after a stop, " if u.info.get("restarted") else ""
        out.append(("stop", u, f"saturation: {cause}; priority {u.priority}, {again}"
                               f"{'active' if u.state == 'active' else 'idle for ' + duration(u.idle_s)}"))
        chosen.add(u.key)
        freed += u.pss
    if spared and not enough():
        events[-1]["reason"] += (f"; {len(spared)} unit(s) started again after a stop spared, "
                                 f"cap left exceeded")
        return out
    if outside or total <= cap:
        return out
    # last resort: agents, the session in use last of all; never the Codex desktop app server
    agents = sorted((u for u in units.values() if u.info["root_agent"] and not u.protected
                     and not snap.procs[u.pid].argv0.startswith("/usr/lib/chatgpt/")),
                    key=lambda u: (heartbeats.get(u.pid, 0) > snap.t - 60, -u.idle_s, -u.pss))
    for u in agents:
        if enough():
            break
        out.append(("stop", u, f"saturation: {cause}; agent, last resort"))
        chosen.add(u.key)
        freed += u.pss + sum(v.pss for v in units.values() if v.agent == u.pid and v.key not in chosen)
    return out


# ---------------------------------------------------------------- actions

def write_journal(events):
    if not events:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if JOURNAL.stat().st_size > JOURNAL_MAX:
            for i in (4, 3, 2, 1):
                src = Path(f"{JOURNAL}.{i}")
                if src.exists():
                    src.rename(f"{JOURNAL}.{i + 1}")
            JOURNAL.rename(f"{JOURNAL}.1")
    except OSError:
        pass
    h = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(JOURNAL, "a") as f:
        for e in events:
            f.write(json.dumps({"t": h, **e}, ensure_ascii=False) + "\n")


def still_there(p):
    try:
        stat = open(f"/proc/{p.pid}/stat").read()
        f = stat[stat.rindex(")") + 2:].split()
        return f[0] != "Z" and int(f[19]) == p.start
    except (OSError, ValueError, IndexError):
        return False


def stop_processes(procs, grace=5):
    """SIGTERM, up to `grace` s of waiting, then SIGKILL; descendants (most recent) first.
    Returns [(process, what happened)] for the journal."""
    alive = [p for p in sorted(procs, key=lambda p: -p.start) if p.pid != SELF]
    result, pidfd = {}, {}
    for p in alive:
        # pidfd opened BEFORE the identity check: if the pid is recycled afterwards, the signal does
        # not reach the newcomer (the pidfd refers to the old process, already dead)
        try:
            pidfd[p.pid] = os.pidfd_open(p.pid)
        except OSError:
            pass
        if p.pid not in pidfd or not still_there(p):
            result[p.pid] = "already gone"
            continue
        try:
            signal.pidfd_send_signal(pidfd[p.pid], signal.SIGTERM)
            result[p.pid] = "SIGTERM"
        except OSError as e:
            result[p.pid] = f"SIGTERM refused ({e.strerror})"
    end = time.time() + grace
    while time.time() < end and any(still_there(p) for p in alive):
        time.sleep(0.25)
    for p in alive:
        if p.pid in pidfd and result.get(p.pid) == "SIGTERM" and still_there(p):
            try:
                signal.pidfd_send_signal(pidfd[p.pid], signal.SIGKILL)
                result[p.pid] = f"survived SIGTERM for {grace} s: SIGKILL"
            except OSError as e:
                result[p.pid] = f"SIGKILL refused ({e.strerror})"
        elif result.get(p.pid) == "SIGTERM":
            result[p.pid] = "stopped by SIGTERM"
    for fd in pidfd.values():
        os.close(fd)
    return [(p, result[p.pid]) for p in alive]


def user_idle_s():
    try:
        r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                            "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                            "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                           capture_output=True, text=True, timeout=3)
        return int(re.search(r"(\d+)", r.stdout).group(1)) / 1000
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return 0


def ask_user(u):
    ANSWERS.mkdir(parents=True, exist_ok=True)
    who = (u.agent_type or "an agent") + (f" (session {u.session[:8]})" if u.session else "")
    text = (f"{u.cmd[:160]}\n\npid {u.pid}, {u.pss} MB, started by {who}\n"
            f"directory: {u.cwd or '?'}\n"
            f"idle for {round(u.idle_s / 60)} min ({u.reason}).\n\nStop it?")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_ask", u.key, text],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def cmd_ask(key, text):
    """Started detached by ask_user: shows the dialog, writes the answer."""
    started = time.time()
    try:
        r = subprocess.run(["zenity", "--question", "--title=Guard Ram", f"--text={text}",
                            "--ok-label=Stop", "--cancel-label=Keep", "--no-markup",
                            "--width=520", f"--timeout={ASK_TIMEOUT_S}"], timeout=ASK_TIMEOUT_S + 30)
        answer = {0: "stop", 1: "keep", 5: "timeout"}.get(r.returncode, "error")
        if answer == "keep" and time.time() - started < 2:
            answer = "error"  # closed at once: not shown, so not an answer from the user
    except (OSError, subprocess.SubprocessError):
        answer = "error"
    ANSWERS.mkdir(parents=True, exist_ok=True)
    name = key.replace(":", "_")
    tmp = ANSWERS / f".{name}.tmp"
    tmp.write_text(json.dumps({"answer": answer, "t": time.time()}))
    tmp.rename(ANSWERS / f"{name}.json")
    return 0


def read_answers():
    r = {}
    try:
        for f in os.scandir(ANSWERS):
            if f.name.endswith(".json"):
                try:
                    r[f.name[:-5].replace("_", ":")] = (json.loads(Path(f.path).read_text()), f.path)
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    return r


def resume_note(stopped, cause):
    note = [f"# Stopped by guard-ram — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
            f"{cause}.", "", "Stopped, in order (idle first, then lowest priority):"]
    note += [f"- {u.pss} MB — {u.category} (p{u.priority}) — pid {u.pid} — "
             f"session {u.session[:8] or '?'} — {u.cwd} — {u.cmd[:120]}" for u in stopped]
    note += ["", "To do: read the end of the conversation and resume where it stopped,",
             "avoiding what made memory grow (run it smaller, or one at a time).",
             "Details: `guard-ram log --event stop`. Archive this file once resumed."]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_STOP.write_text("\n".join(note) + "\n")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


class Lock:
    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def run_pass(dry_run=False):
    warnings = []
    cats, rules = load_rules(warn=warnings.append)
    to_stop, stopped = [], []
    with Lock():
        state = load_json(STATE_FILE, {})
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            state = {}
        marks = load_json(MARKS_FILE, {})
        if not isinstance(marks, dict):
            marks = {}
        gui_cache = dict(state.get("gui", {}))
        pss_cache = dict(state.get("pss", {}))
        snap = scan(gui_cache, pss_cache)
        heartbeats = agent_heartbeats()
        limits = load_limits(snap.mem_total, warn=warnings.append)
        units, decisions, events, new = analyse(snap, state, marks, cats, rules, heartbeats, limits=limits)
        # near a threshold, decide only on fresh PSS: re-read, then analyse again
        pct = 100 * (snap.mem_total - snap.mem_available) / snap.mem_total
        if agents_total(snap, units) >= 0.85 * limits["agents_mb"] or pct >= limits["system_pct"] - 5:
            for pid in snap.tracked:
                pr = snap.procs[pid]
                pr.pss = read_pss(pr)
                pss_cache[pr.key] = [pr.pss, pss_cache.get(pr.key, [0, 0])[1], time.time()]
            units, decisions, events, new = analyse(snap, state, marks, cats, rules, heartbeats, limits=limits)
        events += [{"event": "rules", "reason": m} for m in warnings]
        new["gui"] = {k: v for k, v in gui_cache.items() if int(k.split(":")[0]) in snap.procs}
        new["pss"] = {k: v for k, v in pss_cache.items() if int(k.split(":")[0]) in snap.procs}
        answers = read_answers()
        for key, (ans, path) in answers.items():
            u = units.get(key)
            if dry_run:
                continue
            if u is not None:
                events.append(event("answer", u, answer=ans.get("answer")))
                new["units"][key].pop("asked", None)
                if ans.get("answer") == "stop":
                    decisions.append(("stop", u, "the user accepted the stop (dialog)"))
                elif ans.get("answer") == "error":
                    # the dialog was not shown: try again in 5 min
                    new["units"][key]["answer_t"] = snap.t - ASK_AGAIN_S + 300
                else:
                    new["units"][key]["answer_t"] = snap.t
            os.unlink(path)
        if dry_run:
            return snap, units, decisions, limits
        seen = set()
        for d in decisions:
            u = d[1]
            if d[0] == "ask":
                if user_idle_s() >= AFK_S:
                    continue
                ask_user(u)
                new["units"][u.key]["asked"] = {"t": snap.t}
                events.append(event("ask", u, reason=f"idle for {round(u.idle_s / 60)} min"))
            elif d[0] == "stop" and u.key not in seen:
                seen.add(u.key)
                group = [u]
                if u.info["root_agent"]:
                    group += [v for v in units.values() if v.agent == u.pid and v.key not in seen]
                for v in group:
                    seen.add(v.key)
                    to_stop += [snap.procs[m] for m in v.members]
                    stopped.append(v)
                    reason = d[2] if v is u else f"with its agent {u.pid}: {d[2]}"
                    st = v.info.get("signal_t")
                    events.append(event("stop", v, reason=reason, last_signal=v.info.get("signal"),
                                        last_signal_min_ago=round((snap.t - st) / 60) if st else None))
                    new.setdefault("stopped", []).append({"sig": signature(v.cmd), "cwd": v.cwd, "t": snap.t,
                                                          "pid": v.pid, "reason": reason,
                                                          "agent": v.agent, "session": v.session})
        # a dialog left unanswered well past its timeout: keep
        for key, a in new["units"].items():
            q = a.get("asked")
            if q and snap.t - q["t"] > ASK_TIMEOUT_S + 120 and key not in answers:
                a.pop("asked")
                a["answer_t"] = snap.t
        for key in list(marks):
            if int(key.split(":")[0]) not in snap.procs:
                del marks[key]
        try:
            write_json(STATE_FILE, new)
            write_json(MARKS_FILE, marks)
            write_journal(events)
        except OSError as e:  # disk full, permissions: still stop what must be stopped
            print(f"guard-ram: cannot write ({e})", file=sys.stderr)
    if to_stop:
        grace = 20 if any(u.category == "for-user" for u in stopped) else 5
        result = stop_processes(to_stop, grace)
        try:
            write_journal([{"event": "stop_result", "pid": p.pid, "cmd": redact(p.cmd, 120), "reason": r}
                           for p, r in result])
        except OSError:
            pass
        if any(u.info["root_agent"] for u in stopped):
            resume_note(stopped, next((e["reason"] for e in events if e["event"] == "saturation"), "saturation"))
    if HEARTBEATS.exists():
        for f in HEARTBEATS.glob("*.json"):
            m = re.match(r"(?:claude|codex)-(\d+)\.json$", f.name)
            if m and int(m.group(1)) not in snap.procs:
                f.unlink(missing_ok=True)
    return snap, units, decisions, limits


# ---------------------------------------------------------------- display

def duration(s):
    s = int(s)
    return f"{s // 3600} h {s % 3600 // 60:02d}" if s >= 3600 else f"{s // 60} min"


def show(snap, units, decisions, limits, everything=False):
    total = agents_total(snap, units)
    for_user_mb = sum(snap.procs[p].pss for p in snap.tracked) - total
    pct = 100 * (snap.mem_total - snap.mem_available) / snap.mem_total
    print(f"Agents' cap: {total} MB of {limits['agents_mb']} MB (+ {for_user_mb} MB started for the user) · "
          f"system memory {pct:.0f} % (threshold {limits['system_pct']} %) · {len(snap.tracked)} processes · "
          f"{len(units)} units · {len(snap.roots)} root agents")
    groups = {}
    for u in units.values():
        groups.setdefault(u.pid if u.info["root_agent"] else u.agent, []).append(u)

    def header(a):
        if a in snap.procs and agent_kind(snap.procs[a]):
            r = next((x for x in units.values() if x.pid == a), None)
            s = f", session {r.session[:8]}" if r and r.session else ""
            d = f", {r.cwd}" if r and r.cwd else ""
            return f"{agent_kind(snap.procs[a])} pid {a}{s}{d}"
        return f"agent {a} gone" if a else "no known agent (Codex or detached)"

    hidden = 0
    for a, group in sorted(groups.items(), key=lambda kv: -sum(u.pss for u in kv[1])):
        group.sort(key=lambda u: (not u.info["root_agent"], u.priority, -u.pss))
        print(f"\n▸ {header(a)} — {sum(u.pss for u in group)} MB")
        for u in group:
            if not everything and u.pss < 5 and u.state == "active" and not u.info["root_agent"]:
                hidden += 1
                continue
            st = u.state + (f" {duration(u.idle_s)}" if u.state != "active" else "")
            n = f"×{len(u.members)}" if len(u.members) > 1 else ""
            print(f"  p{u.priority} {u.category:9} {st:17} {u.pss:6} MB  pid {u.pid:<8} {n:4} {u.cmd[:90]}")
    if hidden:
        print(f"\n({hidden} active units under 5 MB hidden: --all)")
    restarts = restarts_24h()
    if restarts:
        print(f"\n⚠ {len(restarts)} restart(s) less than 30 min after a stop, over 24 h (probable false positives):")
        for e in restarts[-5:]:
            print(f"  {e['t'][:16]} pid {e.get('pid')} ← {e.get('old_pid')} — {(e.get('cmd') or '')[:80]}")
        print("  details: guard-ram log --event restarted_after_stop")
    if decisions:
        print("\nDecisions if the pass ran now:")
        for d in decisions:
            print(f"  {d[0]:5} pid {d[1].pid} ({d[1].category}, p{d[1].priority}) — {d[2]}")


# ---------------------------------------------------------------- commands

def process(pid):
    p = read_base(pid)
    if p is None:
        sys.exit(f"pid {pid} not found (or not mine)")
    return p


def cmd_mark(a):
    cats, _ = load_rules()
    category = a.category
    if category not in cats and category != "none":
        sys.exit(f"unknown category: {a.category} (known: {', '.join(cats)}, none)")
    p = process(a.pid)
    with Lock():
        marks = load_json(MARKS_FILE, {})
        if category == "none":
            marks.pop(p.key, None)
        else:
            marks[p.key] = {**marks.get(p.key, {}), "category": category, "priority": a.priority,
                            "reason": a.reason or "mark", "t": time.time(),
                            "by": os.environ.get("CLAUDE_CODE_SESSION_ID", "")}
        write_json(MARKS_FILE, marks)
    write_journal([{"event": "mark", "pid": p.pid, "unit": p.key, "cat": category, "prio": a.priority,
                    "reason": a.reason, "cmd": redact(p.cmd)}])
    print(f"pid {p.pid} marked {category}" + (f", priority {a.priority}" if a.priority else ""))
    return 0


def cmd_keep(a):
    p = process(a.pid)
    with Lock():
        marks = load_json(MARKS_FILE, {})
        marks.setdefault(p.key, {})["until"] = time.time() + a.hours * 3600
        write_json(MARKS_FILE, marks)
    write_journal([{"event": "keep", "pid": p.pid, "unit": p.key, "hours": a.hours,
                    "reason": a.reason, "cmd": redact(p.cmd)}])
    print(f"pid {p.pid} counted as active for {a.hours} h")
    return 0


def cmd_launch(a):
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        sys.exit("usage: guard-ram launch --category for-user [--priority N] [--reason R] -- command ...")
    env = dict(os.environ, GUARD_RAM_CATEGORY=a.category)
    if a.priority:
        env["GUARD_RAM_PRIORITY"] = str(a.priority)
    if a.reason:
        env["GUARD_RAM_REASON"] = a.reason
    os.execvpe(cmd[0], cmd, env)


def journal_events(paths):
    for f in paths:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for l in lines:
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if isinstance(e, dict):  # a line written by hand or by another tool must not break the log
                e.setdefault("event", None)
                e.setdefault("t", "")
                yield e


def restarts_24h():
    limit = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() - 86400))
    return [e for e in journal_events((Path(f"{JOURNAL}.1"), JOURNAL))
            if e["event"] == "restarted_after_stop" and e["t"] >= limit]


def cmd_log(a):
    out = [e for e in journal_events([Path(f"{JOURNAL}.{i}") for i in (5, 4, 3, 2, 1)] + [JOURNAL])
           if not (a.event and e["event"] != a.event) and not (a.pid and e.get("pid") != a.pid)]
    for e in out[-a.n:]:
        extra = " ".join(f"{k}={e[k]}" for k in ("cat", "prio", "pss", "answer", "gap_s", "life_min", "old_pid",
                                                  "last_signal", "last_signal_min_ago")
                         if e.get(k) not in (None, ""))
        print(f"{e['t'][:19]} {e['event'] or '?':12} pid {e.get('pid', '-')!s:<8} {extra}  "
              f"{e.get('reason') or ''}  {(e.get('cmd') or '')[:80]}")
    return 0


def cmd_heartbeat():
    """Hook (PostToolUse, UserPromptSubmit, Stop): the session is in use."""
    try:
        data = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}
    try:
        pid, name = int(os.environ.get("CLAUDE_PID") or 0), "claude"
        if not pid:
            x = os.getppid()
            while x > 1:
                stat = open(f"/proc/{x}/stat").read()
                comm = stat[stat.index("(") + 1:stat.rindex(")")]
                if comm in ("claude", "codex"):
                    pid, name = x, comm
                    break
                x = int(stat[stat.rindex(")") + 2:].split()[1])
        if pid:
            HEARTBEATS.mkdir(parents=True, exist_ok=True)
            tmp = HEARTBEATS / f".{name}-{pid}.tmp"
            tmp.write_text(json.dumps({"t": time.time(), "session": data.get("session_id", ""),
                                       "cwd": data.get("cwd", ""),
                                       "hook": data.get("hook_event_name", "")}))
            tmp.replace(HEARTBEATS / f"{name}-{pid}.json")
    except Exception:
        pass  # a hook must never get in the session's way
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["heartbeat"]:
        return cmd_heartbeat()
    if argv[:1] == ["_ask"] and len(argv) == 3:
        return cmd_ask(argv[1], argv[2])
    ap = argparse.ArgumentParser(prog="guard-ram", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="one pass that stops and writes nothing")
    sp = ap.add_subparsers(dest="cmd")
    l = sp.add_parser("list", help="everything the agents started, with state and priority")
    l.add_argument("--all", action="store_true", help="also show small active units")
    l.add_argument("--json", action="store_true")
    j = sp.add_parser("log", help="history of decisions")
    j.add_argument("-n", type=int, default=40)
    j.add_argument("--event", help="appeared, idle, active_again, stop, stop_result, ask, answer, saturation, gone…")
    j.add_argument("--pid", type=int)
    m = sp.add_parser("mark", help="set the category (and priority) of a process")
    m.add_argument("pid", type=int)
    m.add_argument("--category", required=True, help="for-user, infra, task, mcp, orphan, or none")
    m.add_argument("--priority", type=int, choices=range(1, 6))
    m.add_argument("--reason")
    k = sp.add_parser("keep", help="count a process as active for N hours")
    k.add_argument("pid", type=int)
    k.add_argument("--hours", type=float, default=2)
    k.add_argument("--reason")
    n = sp.add_parser("launch", help="run a command with its category: launch --category for-user -- cmd")
    n.add_argument("--category", default="for-user")
    n.add_argument("--priority", type=int, choices=range(1, 6))
    n.add_argument("--reason")
    n.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "mark":
        return cmd_mark(a)
    if a.cmd == "keep":
        return cmd_keep(a)
    if a.cmd == "launch":
        return cmd_launch(a)
    if a.cmd == "log":
        return cmd_log(a)
    if a.cmd == "list" or a.dry_run:
        snap, units, decisions, limits = run_pass(dry_run=True)
        if getattr(a, "json", False):
            print(json.dumps([{"unit": u.key, "pid": u.pid, "category": u.category, "priority": u.priority,
                               "state": u.state, "idle_s": round(u.idle_s), "pss_mb": u.pss,
                               "agent": u.agent, "agent_type": u.agent_type, "session": u.session,
                               "cwd": u.cwd, "cmd": u.cmd, "members": u.members, "reason": u.reason}
                              for u in units.values()], ensure_ascii=False, indent=1))
        else:
            show(snap, units, decisions, limits, everything=getattr(a, "all", False))
        return 0
    try:
        run_pass()
    except Exception as e:
        try:
            write_journal([{"event": "error", "reason": f"{type(e).__name__}: {e}"}])
        finally:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
