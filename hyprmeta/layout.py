"""Layout snapshots: where every window is, and what every terminal runs.

A snapshot records each window's workspace, position, size, floating state and
tab group, and for each terminal (ghostty, Alacritty):
  - its working directory, and
  - what runs in its FOREGROUND (the terminal's tpgid): an idle shell, a command
    (argv), a Claude Code session (id from ~/.claude/sessions/<pid>.json) or a
    Codex session (UUID from the newest rollout file the process holds open).

The daemon saves one on every change, on disk, in a directory per Hyprland
instance, so a crash-reboot can never overwrite the last pre-crash state:
  ~/.local/state/hyprmeta/layouts/<start>_<sig8>/latest.json   (every change)
  ~/.local/state/hyprmeta/layouts/<start>_<sig8>/<YYYYmmdd-HHMM>.json  (10-min history, 2 days)
  ~/.local/state/hyprmeta/layouts/<start>_<sig8>/changes/<ms>.json      (last 30 distinct states)
`restore.py` turns a snapshot back into windows.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Iterable

from .agents import _codex_rollouts
from .cli import Hypr, HyprmetaError

GHOSTTY = "com.mitchellh.ghostty"
ALACRITTY = "Alacritty"
TERMINALS = frozenset({GHOSTTY, ALACRITTY})  # window classes whose foreground we record
SHELLS = frozenset({"zsh", "bash", "fish"})
ROLLOUT_UUID = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$")
HISTORY_BUCKET_S = 600
CHANGES_KEEP = 30  # the last N distinct states per session: undo for a window closed by mistake
HISTORY_KEEP_S = 2 * 86400
INSTANCES_KEEP_S = 14 * 86400
VERSION = 1


def layouts_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "hyprmeta" / "layouts"


# --------------------------------------------------------------------------- #
# /proc
# --------------------------------------------------------------------------- #


def _stat(pid: int) -> tuple[str, int, int, int] | None:
    """(comm, ppid, pgrp, tpgid) or None if the process is gone."""
    try:
        s = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    comm = s[s.index("(") + 1 : s.rindex(")")]
    f = s[s.rindex(")") + 2 :].split()
    return comm, int(f[1]), int(f[2]), int(f[5])


def _cmdline(pid: int) -> list[str]:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            st = _stat(int(d))
            if st is not None:
                kids.setdefault(st[1], []).append(int(d))
    return kids


def claude_session(pid: int) -> dict | None:
    """Claude Code writes ~/.claude/sessions/<pid>.json: {sessionId, cwd, name, ...}."""
    try:
        data = json.loads((Path.home() / ".claude" / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    if int(data.get("pid", -1)) != pid or not data.get("sessionId"):
        return None
    return {"session": data["sessionId"], "cwd": data.get("cwd"), "name": data.get("name")}


def codex_session(pid: int) -> str | None:
    """UUID of the NEWEST rollout the process holds open (it also holds stale ones)."""
    best, best_mtime = None, -1.0
    for path in _codex_rollouts(pid):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    m = ROLLOUT_UUID.search(best or "")
    return m.group(1) if m else None


def _find_agent(pid: int, kids: dict[int, list[int]], depth: int = 4) -> tuple[str, int] | None:
    """(kind, pid) of a claude / codex process at or under `pid`."""
    st = _stat(pid)
    if st is None:
        return None
    if st[0] == "claude":
        return "claude", pid
    if st[0] == "codex":
        argv0 = (_cmdline(pid) or [""])[0]
        if not os.path.basename(argv0).startswith("codex-linux-sandbox"):
            return "codex", pid
    if depth > 0:
        for k in kids.get(pid, []):
            hit = _find_agent(k, kids, depth - 1)
            if hit:
                return hit
    return None


def inspect_terminal(window_pid: int, kids: dict[int, list[int]]) -> dict | None:
    """What a terminal window runs: {cwd, run: {kind: shell|command|claude|codex, ...}}.

    ghostty runs `/bin/sh -c /usr/bin/zsh`, Alacritty runs zsh directly; either way
    the shell's tpgid (terminal foreground process group) says what is in front.
    """
    top = next(iter(kids.get(window_pid, [])), None)
    if top is None:
        return None  # no program at all (a launch that never got a frame)
    shell = top
    # ghostty runs `/bin/sh -c /usr/bin/zsh`: descend through the sh wrapper
    while (st := _stat(shell)) and st[0] == "sh" and kids.get(shell):
        nxt = kids[shell][0]
        nst = _stat(nxt)
        if nst is None or nst[0] not in SHELLS | {"sh"}:
            break
        shell = nxt
    st = _stat(shell)
    if st is None:
        return None
    fg = st[3] if st[3] > 0 else shell
    if fg in (shell, top) or _stat(fg) is None:
        return {"cwd": _cwd(shell), "run": {"kind": "shell"}}
    agent = _find_agent(fg, kids)
    if agent and agent[0] == "claude":
        sess = claude_session(agent[1])
        return {"cwd": (sess or {}).get("cwd") or _cwd(agent[1]),
                "run": {"kind": "claude", "session": (sess or {}).get("session"),
                        "name": (sess or {}).get("name")}}
    if agent and agent[0] == "codex":
        return {"cwd": _cwd(agent[1]) or _cwd(fg), "run": {"kind": "codex", "session": codex_session(agent[1])}}
    return {"cwd": _cwd(fg), "run": {"kind": "command", "argv": _cmdline(fg)}}


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #


def current_instance(hypr: Hypr) -> dict:
    instances = json.loads(hypr._run(["-j", "instances"]))
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    inst = next((i for i in instances if i.get("instance") == sig), None) if sig else None
    if inst is None:
        if not instances:
            raise HyprmetaError("hyprctl reports no Hyprland instance")
        inst = max(instances, key=lambda i: int(i.get("time", 0)))
    return {"sig": str(inst["instance"]), "started": int(inst.get("time", 0))}


def take_snapshot(hypr: Hypr, clients: list[dict] | None = None, instance: dict | None = None) -> dict:
    clients = hypr.clients() if clients is None else clients
    monitors = json.loads(hypr._run(["-j", "monitors"]))
    kids = children_map()
    windows = []
    for c in clients:
        w = {
            "address": c.get("address"),
            "class": c.get("class"),
            "title": c.get("title"),
            "workspace": int((c.get("workspace") or {}).get("id") or 0),
            "monitor": c.get("monitor"),
            "at": list(c.get("at") or [0, 0]),
            "size": list(c.get("size") or [0, 0]),
            "floating": bool(c.get("floating")),
            "fullscreen": int(c.get("fullscreen") or 0),
            "pinned": bool(c.get("pinned")),
            "grouped": list(c.get("grouped") or []),
            "pid": c.get("pid"),
        }
        if c.get("class") in TERMINALS and c.get("pid"):
            w["terminal"] = inspect_terminal(int(c["pid"]), kids)
        windows.append(w)
    return {
        "version": VERSION,
        "ts": time.time(),
        "instance": instance or current_instance(hypr),
        "monitors": [{k: m.get(k) for k in ("id", "name", "x", "y", "width", "height", "scale", "transform")}
                     | {"workspace": (m.get("activeWorkspace") or {}).get("id")} for m in monitors],
        "windows": windows,
    }


def signature(snap: dict) -> str:
    """What must change for a new save: layout + what terminals run; not titles/time."""
    keep = ("address", "workspace", "at", "size", "floating", "fullscreen", "grouped", "terminal")
    return json.dumps([{k: w.get(k) for k in keep} for w in snap["windows"]], sort_keys=True)


def instance_dir(snap: dict) -> Path:
    inst = snap["instance"]
    started = time.strftime("%Y%m%d-%H%M%S", time.localtime(inst["started"]))
    return layouts_dir() / f"{started}_{inst['sig'][:8]}"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def save(snap: dict) -> Path:
    d = instance_dir(snap)
    _atomic_write(d / "latest.json", snap)
    bucket = int(snap["ts"] // HISTORY_BUCKET_S * HISTORY_BUCKET_S)
    _atomic_write(d / f"{time.strftime('%Y%m%d-%H%M', time.localtime(bucket))}.json", snap)
    _atomic_write(d / "changes" / f"{int(snap['ts'] * 1000)}.json", snap)
    for old in sorted((d / "changes").glob("*.json"))[:-CHANGES_KEEP]:
        old.unlink()
    return d / "latest.json"


def prune(now: float | None = None) -> None:
    now = time.time() if now is None else now
    root = layouts_dir()
    if not root.exists():
        return
    for d in root.iterdir():
        latest = d / "latest.json"
        if not latest.exists():
            continue
        if now - latest.stat().st_mtime > INSTANCES_KEEP_S:
            shutil.rmtree(d)
            continue
        for f in d.glob("2*.json"):
            if now - f.stat().st_mtime > HISTORY_KEEP_S:
                f.unlink()


def list_snapshots() -> list[Path]:
    """Every saved snapshot, newest first (each instance's latest, then its history)."""
    root = layouts_dir()
    if not root.exists():
        return []
    out: list[Path] = []
    for d in sorted(root.iterdir(), reverse=True):
        if (d / "latest.json").exists():
            out.extend(sorted((d / "changes").glob("*.json"), reverse=True))  # newest first; [0] == latest
            out.extend(sorted(d.glob("2*.json"), reverse=True))
    return out


def previous_instance_latest(current_sig: str) -> Path | None:
    """The last state of the Hyprland session BEFORE this one — what a crash-reboot lost."""
    root = layouts_dir()
    if not root.exists():
        return None
    dirs = [d for d in root.iterdir() if (d / "latest.json").exists() and not d.name.endswith(current_sig[:8])]
    if not dirs:
        return None
    return max(dirs, key=lambda d: (d / "latest.json").stat().st_mtime) / "latest.json"


def load(path: Path) -> dict:
    snap = json.loads(path.read_text())
    if snap.get("version") != VERSION:
        raise HyprmetaError(f"{path}: snapshot version {snap.get('version')} (expected {VERSION})")
    return snap


# --------------------------------------------------------------------------- #
# reading a snapshot
# --------------------------------------------------------------------------- #


def describe_run(term: dict | None) -> str:
    if not term:
        return "terminal (nothing running)"
    run, cwd = term["run"], term.get("cwd") or "?"
    home = str(Path.home())
    cwd = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    kind = run["kind"]
    if kind == "claude":
        sid = (run.get("session") or "?")[:8]
        return f"claude  {sid}  in {cwd}"
    if kind == "codex":
        return f"codex   {(run.get('session') or '?')[:8]}  in {cwd}"
    if kind == "command":
        return f"$ {shlex.join(run.get('argv') or [])[:60]}  in {cwd}"
    return f"shell  in {cwd}"


def format_snapshot(snap: dict, meta_of: dict[int, str] | None = None) -> str:
    meta_of = meta_of or {}
    age = time.time() - snap["ts"]
    lines = [f"snapshot {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(snap['ts']))} "
             f"({age:.0f} s ago), {len(snap['windows'])} windows"]
    by_ws: dict[int, list[dict]] = {}
    for w in snap["windows"]:
        by_ws.setdefault(w["workspace"], []).append(w)
    for ws in sorted(by_ws):
        meta = f"  [{meta_of[ws]}]" if ws in meta_of else ""
        lines.append(f"workspace {ws}{meta}")
        for w in sorted(by_ws[ws], key=lambda w: (w["at"][0], w["at"][1])):
            x, y = w["at"]
            wd, ht = w["size"]
            flags = (" float" if w["floating"] else "") + (f" tab{len(w['grouped'])}" if w["grouped"] else "")
            what = describe_run(w.get("terminal")) if w["class"] in TERMINALS else f"{w['class']}  {w['title'][:50]!r}"
            lines.append(f"  {x:>5},{y:<5} {wd:>4}x{ht:<4}{flags:<7} {what}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# restore planning (pure)
# --------------------------------------------------------------------------- #


def launch_argv(run: dict) -> list[str] | None:
    """The ghostty `-e` command that brings a terminal back; None = plain shell.

    Agents and commands run inside `zsh -ic` (your usual PATH/aliases) and hand
    over to an interactive shell afterwards, so the window survives an exit.
    """
    kind = run["kind"]
    if kind == "claude":
        inner = f"claude --resume {shlex.quote(run['session'])}"
    elif kind == "codex":
        inner = f"codex resume {shlex.quote(run['session'])}"
    elif kind == "command":
        inner = shlex.join(run["argv"])
    else:
        return None
    return ["zsh", "-ic", f"{inner}; exec zsh -i"]


def live_sessions(kids: dict[int, list[int]] | None = None) -> set[str]:
    """Claude + Codex session ids that are running right now, anywhere."""
    out: set[str] = set()
    sessions = Path.home() / ".claude" / "sessions"
    for f in sessions.glob("*.json") if sessions.exists() else []:
        try:
            data = json.loads(f.read_text())
            if _stat(int(data["pid"])) is not None and data.get("sessionId"):
                out.add(data["sessionId"])
        except (OSError, ValueError, KeyError):
            continue
    for d in os.listdir("/proc"):
        if d.isdigit():
            st = _stat(int(d))
            if st and st[0] == "codex":
                sid = codex_session(int(d))
                if sid:
                    out.add(sid)
    return out


def plan_restore(snap: dict, present: set[str], live: set[str], only_ws: set[int] | None = None) -> dict:
    """Which snapshot windows exist already, which to launch, which cannot come back.

    `present` = window addresses alive now (same Hyprland instance keeps them);
    `live` = session ids running now. Never plans a session that is live.
    """
    keep, launch, skip = [], [], []
    for w in snap["windows"]:
        if only_ws is not None and w["workspace"] not in only_ws:
            continue
        if w["address"] in present:
            keep.append(w)
            continue
        if w["workspace"] <= 0:
            skip.append((w, "special workspace (scratchpad) — not restored"))
            continue
        if w["class"] not in TERMINALS:
            skip.append((w, "not a terminal (browsers restore their own windows)"))
            continue
        term = w.get("terminal")
        if not term:
            skip.append((w, "the terminal ran nothing (a launch that never got a frame)"))
            continue
        run = term["run"]
        if run["kind"] in ("claude", "codex"):
            if not run.get("session"):
                skip.append((w, f"{run['kind']} session id unknown"))
                continue
            if run["session"] in live:
                skip.append((w, f"{run['kind']} session {run['session'][:8]} is already running"))
                continue
        launch.append(w)
    return {"keep": keep, "launch": launch, "skip": skip}


def format_plan(plan: dict) -> str:
    lines = []
    for w in plan["launch"]:
        lines.append(f"  open   ws {w['workspace']:<3} {describe_run(w.get('terminal'))}")
    for w in plan["keep"]:
        lines.append(f"  keep   ws {w['workspace']:<3} {w['class']}  {str(w['title'])[:50]!r}")
    for w, why in plan["skip"]:
        lines.append(f"  skip   ws {w['workspace']:<3} {w['class']}  {str(w['title'])[:40]!r}: {why}")
    return "\n".join(lines) or "  (nothing in scope)"


def tiled_leaves(windows: Iterable[dict]) -> list[dict]:
    """One entry per layout cell: tiled windows, one representative per tab group."""
    out, seen_groups = [], set()
    for w in windows:
        if w["floating"]:
            continue
        if w["grouped"]:
            key = tuple(w["grouped"])
            if key in seen_groups:
                continue
            seen_groups.add(key)
        out.append(w)
    return out
