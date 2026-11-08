#!/usr/bin/env python3
"""Install guard-ram for the current user (no root).

    ~/.local/share/guard-ram/guard_ram.py   the only copy that runs (a COPY of this checkout:
    ~/.local/share/guard-ram/rules.toml     switching branches does not change the running guard)
    ~/.config/systemd/user/guard-ram.{service,timer}   one pass every 15 s
    ~/.local/bin/guard-ram                  link to the installed copy
    ~/.claude/skills/guard-ram/SKILL.md     if ~/.claude exists (linked from ~/.codex/skills)

Rules of the machine: ~/.config/guard-ram/rules.toml (never touched here).
State and journal: ~/.claude/guard-ram/ (never touched here).

    python3 install.py              install or update, enable the timer
    python3 install.py --check      exit 1 if the installed copy differs from this checkout
                                    or if the timer is not running
    python3 install.py --config     print the hooks to add to ~/.claude/settings.json
    python3 install.py --merge-hooks  add those hooks to ~/.claude/settings.json (backed up first,
                                    existing hooks kept, nothing added twice)
    python3 install.py --remove-hooks remove them again (backed up first, other hooks kept)
    python3 install.py --uninstall  stop the timer, remove units, link and skill
                                    (the copy, the state and the journal stay)

Every file replaced is first kept as <name>.bak-YYYY-MM-DD. Agent settings are edited only by
--merge-hooks, and only when it is asked for; --config shows the same hooks without touching anything.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("guard_ram.py", "rules.toml", "README.md")
HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "guard-ram"
UNITS = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "systemd" / "user"
LINK = HOME / ".local" / "bin" / "guard-ram"
SKILL_CLAUDE = HOME / ".claude" / "skills" / "guard-ram"
SKILL_CODEX = HOME / ".codex" / "skills" / "guard-ram"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
HOOK_EVENTS = ("PostToolUse", "UserPromptSubmit", "Stop")
PYTHON_MIN = (3, 11)  # tomllib


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def back_up(path):
    if path.exists() and not path.is_symlink():
        copy = path.with_name(f"{path.name}.bak-{date.today().isoformat()}")
        if not copy.exists():
            shutil.copy2(path, copy)


def units():
    """The systemd units of the checkout, with the path of the installed copy."""
    return {name: (HERE / "systemd" / name).read_text().replace("@DATA@", str(DATA))
            for name in ("guard-ram.service", "guard-ram.timer")}


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def install():
    if sys.version_info < PYTHON_MIN:
        sys.exit(f"Python {'.'.join(map(str, PYTHON_MIN))} or later is required (tomllib)")
    if not Path("/proc/self/smaps_rollup").exists():
        sys.exit("Linux 4.14 or later is required (/proc/<pid>/smaps_rollup)")
    DATA.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = DATA / name
        back_up(target)
        shutil.copy2(HERE / name, target)
    (DATA / "guard_ram.py").chmod(0o755)
    UNITS.mkdir(parents=True, exist_ok=True)
    for name, text in units().items():
        target = UNITS / name
        if target.exists() and target.read_text() == text:
            continue
        back_up(target)
        target.write_text(text)
    LINK.parent.mkdir(parents=True, exist_ok=True)
    if LINK.is_symlink() or LINK.exists():
        back_up(LINK)
        LINK.unlink()
    LINK.symlink_to(DATA / "guard_ram.py")
    if (HOME / ".claude").is_dir():
        SKILL_CLAUDE.mkdir(parents=True, exist_ok=True)
        back_up(SKILL_CLAUDE / "SKILL.md")
        shutil.copy2(HERE / "skill" / "SKILL.md", SKILL_CLAUDE / "SKILL.md")
        if (HOME / ".codex").is_dir() and not SKILL_CODEX.exists():
            SKILL_CODEX.parent.mkdir(parents=True, exist_ok=True)
            SKILL_CODEX.symlink_to(SKILL_CLAUDE)
    systemctl("daemon-reload")
    r = systemctl("enable", "--now", "guard-ram.timer")
    if r.returncode:
        print(f"timer not enabled: {r.stderr.strip()}", file=sys.stderr)
    print(f"installed in {DATA}; timer: {systemctl('is-active', 'guard-ram.timer').stdout.strip()}")
    print("agent hooks: python3 install.py --config (show) or --merge-hooks (add)")
    if str(LINK.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: {LINK.parent} is not in this shell's PATH (often the case on a new account until the "
              f"next login): run {LINK} or open a new login shell")
    return check(quiet=True)


def check(quiet=False):
    problems = []
    for name in FILES:
        target = DATA / name
        if not target.exists():
            problems.append(f"{target} missing")
        elif digest(target) != digest(HERE / name):
            problems.append(f"{target} differs from the checkout")
    for name, text in units().items():
        target = UNITS / name
        if not target.exists() or target.read_text() != text:
            problems.append(f"{target} missing or different")
    if systemctl("is-active", "guard-ram.timer").stdout.strip() != "active":
        problems.append("guard-ram.timer is not running")
    if not quiet or problems:
        print("\n".join(problems) if problems else "ok: installed copy matches the checkout, timer running")
    return 1 if problems else 0


def hooks():
    hook = {"type": "command", "command": f"python3 {DATA / 'guard_ram.py'} heartbeat", "timeout": 5}
    return {"PostToolUse": [{"matcher": "", "hooks": [hook]}],
            "UserPromptSubmit": [{"hooks": [hook]}],
            "Stop": [{"hooks": [hook]}]}


def merge_hooks(settings=CLAUDE_SETTINGS):
    """Add the heartbeat hooks to a Claude Code settings file. Keeps every other key and hook,
    adds nothing that is already there, backs the file up, and refuses a file that is not JSON."""
    settings = Path(settings)
    try:
        data = json.loads(settings.read_text()) if settings.exists() else {}
    except ValueError as e:
        sys.exit(f"{settings} is not valid JSON ({e}): fix it first, nothing was changed")
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        sys.exit(f"{settings}: unexpected structure, nothing was changed")
    added = []
    for event, entries in hooks().items():
        current = data.setdefault("hooks", {}).setdefault(event, [])
        present = any("guard_ram.py heartbeat" in h.get("command", "")
                      for e in current if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict))
        if not present:
            current.extend(entries)
            added.append(event)
    if not added:
        print(f"{settings}: the hooks are already there, nothing changed")
        return 0
    settings.parent.mkdir(parents=True, exist_ok=True)
    back_up(settings)
    tmp = settings.with_name(settings.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    json.loads(tmp.read_text())
    tmp.replace(settings)
    print(f"{settings}: hooks added for {', '.join(added)} (previous file kept as .bak-{date.today().isoformat()})")
    return 0


def remove_hooks(settings=CLAUDE_SETTINGS):
    """Remove the heartbeat hooks from a Claude Code settings file, and nothing else."""
    settings = Path(settings)
    if not settings.exists():
        print(f"{settings} does not exist, nothing to remove")
        return 0
    try:
        data = json.loads(settings.read_text())
    except ValueError as e:
        sys.exit(f"{settings} is not valid JSON ({e}): fix it first, nothing was changed")
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        sys.exit(f"{settings}: unexpected structure, nothing was changed")

    def ours(entry):
        return isinstance(entry, dict) and any(isinstance(h, dict) and "guard_ram.py heartbeat" in h.get("command", "")
                                               for h in entry.get("hooks", []))

    removed = 0
    for event in list(data.get("hooks", {})):
        entries = data["hooks"][event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not ours(e)]
        removed += len(entries) - len(kept)
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]
    if not removed:
        print(f"{settings}: no Guard Ram hook found, nothing changed")
        return 0
    back_up(settings)
    tmp = settings.with_name(settings.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    json.loads(tmp.read_text())
    tmp.replace(settings)
    print(f"{settings}: {removed} Guard Ram hook entries removed (previous file kept as .bak-{date.today().isoformat()})")
    return 0


def config():
    print(json.dumps({"hooks": hooks()}, indent=2))
    print("\n# Merge into ~/.claude/settings.json, next to the existing hooks.", file=sys.stderr)
    return 0


def uninstall():
    systemctl("disable", "--now", "guard-ram.timer")
    for name in ("guard-ram.service", "guard-ram.timer"):
        (UNITS / name).unlink(missing_ok=True)
    systemctl("daemon-reload")
    if LINK.is_symlink():
        LINK.unlink()
    if SKILL_CODEX.is_symlink():
        SKILL_CODEX.unlink()
    (SKILL_CLAUDE / "SKILL.md").unlink(missing_ok=True)
    try:
        SKILL_CLAUDE.rmdir()  # only if empty: a backup kept next to it stays
    except OSError:
        pass
    print(f"uninstalled. Left in place: {DATA}, ~/.claude/guard-ram/ (state, journal) and ~/.config/guard-ram/. "
          "If the hooks were merged: python3 install.py --remove-hooks")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--config", action="store_true")
    g.add_argument("--merge-hooks", action="store_true")
    g.add_argument("--remove-hooks", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.config:
        return config()
    if a.merge_hooks:
        return merge_hooks()
    if a.remove_hooks:
        return remove_hooks()
    if a.uninstall:
        return uninstall()
    return install()


if __name__ == "__main__":
    sys.exit(main())
