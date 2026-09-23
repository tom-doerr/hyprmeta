"""Agent tracking: which terminals run Claude Code / Codex, and whether they finished.

Signals (no hooks needed):
- Claude Code rewrites the terminal title: `✳ <summary>` while idle, a spinner
  glyph (◐◓◑◒ / braille ⠏…) while working.
- Codex keeps its rollout `.jsonl` open; its tail carries `task_started`,
  `task_complete` and `turn_aborted` events.
- Process ancestry (`/proc`) ties an agent pid to the ghostty window pid that
  Hyprland reports in `hyprctl clients -j`.

"Finished since you looked": an agent going running → idle (or exiting while
running) stamps `finish_ts` on its window. A window is `unseen` while
`finish_ts > last_focus_ts`; a meta workspace is `unseen` while any of its
windows has `finish_ts > seen_ts`, where `seen_ts` is the last time that meta
was the one on screen.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .cli import Hypr, HyprmetaError

AGENT_COMMS = {"claude": "claude", "codex": "codex"}
IDLE_GLYPHS = {"✳"}
SPINNER_GLYPHS = set("◐◓◑◒◴◵◶◷") | {chr(c) for c in range(0x2800, 0x2900)}  # braille
CODEX_RUNNING = {"task_started", "user_message"}
CODEX_IDLE = {"task_complete", "turn_aborted"}
TAG_DONE = "agent-done"
TAG_RUNNING = "agent-running"


def agents_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(runtime) / "hyprmeta" / "agents.json"


# --------------------------------------------------------------------------- #
# pure classifiers
# --------------------------------------------------------------------------- #


def claude_state_from_title(title: str) -> str | None:
    """'running' / 'idle' from a Claude Code window title, None if not Claude's."""
    if not title:
        return None
    first = title[0]
    if first in SPINNER_GLYPHS:
        return "running"
    if first in IDLE_GLYPHS:
        return "idle"
    return None


def codex_state_from_rollout_tail(tail: str) -> str | None:
    """'running' / 'idle' from the last task events in a rollout tail; None if none seen."""
    state = None
    for line in tail.splitlines():
        if '"event_msg"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except (ValueError, AttributeError):
            continue
        kind = payload.get("type")
        if kind in CODEX_RUNNING:
            state = "running"
        elif kind in CODEX_IDLE:
            state = "idle"
    return state


# --------------------------------------------------------------------------- #
# /proc helpers
# --------------------------------------------------------------------------- #


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def parent_of(pid: int) -> int | None:
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return None
    # comm can contain spaces; ppid is the 4th field after the ')'
    try:
        return int(stat[stat.rindex(")") + 2 :].split()[1])
    except (ValueError, IndexError):
        return None


def find_agent_processes() -> list[tuple[int, str]]:
    """(pid, kind) for every claude/codex process on the box."""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        comm = _read(f"/proc/{name}/comm")
        if comm is None:
            continue
        kind = AGENT_COMMS.get(comm.strip())
        if kind:
            out.append((int(name), kind))
    return out


def window_pid_for(pid: int, window_pids: set[int], max_depth: int = 12) -> int | None:
    """Walk up the parent chain until we hit a pid Hyprland knows as a window."""
    cur: int | None = pid
    for _ in range(max_depth):
        if cur is None or cur <= 1:
            return None
        if cur in window_pids:
            return cur
        cur = parent_of(cur)
    return None


def codex_rollout_path(pid: int) -> str | None:
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if "/.codex/sessions/" in target and target.endswith(".jsonl"):
            return target
    return None


def codex_state(pid: int, tail_bytes: int = 65536) -> str | None:
    path = codex_rollout_path(pid)
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    return codex_state_from_rollout_tail(tail)


# --------------------------------------------------------------------------- #
# tracker
# --------------------------------------------------------------------------- #


@dataclass
class Agent:
    pid: int
    kind: str  # claude | codex
    state: str | None  # running | idle | None (unknown)


@dataclass
class WindowState:
    address: str
    pid: int
    workspace: int
    title: str
    meta: str | None
    agents: list[Agent] = field(default_factory=list)
    last_focus_ts: float = 0.0
    finish_ts: float = 0.0
    finished_by: str | None = None  # kind of the agent that finished last

    @property
    def running(self) -> int:
        return sum(1 for a in self.agents if a.state == "running")

    @property
    def unseen(self) -> bool:
        return self.finish_ts > self.last_focus_ts


class AgentTracker:
    """Pure bookkeeping; the daemon feeds it clients lists, focus events and time."""

    def __init__(self, base: list[int], metas: dict[str, int], now: float | None = None) -> None:
        self.base = base
        self.metas = dict(metas)
        self.windows: dict[str, WindowState] = {}
        self.seen_ts: dict[str, float] = {}  # meta -> last time it was on screen
        self.current_meta: str | None = None
        self.started = time.time() if now is None else now

    # -- configuration -------------------------------------------------------
    def set_metas(self, metas: dict[str, int]) -> None:
        self.metas = dict(metas)

    def meta_for_workspace(self, ws: int) -> str | None:
        for i, b in enumerate(self.base):
            off = ws - b
            if off < 0:
                continue
            for name, meta_off in self.metas.items():
                if meta_off == off:
                    return name
        return None

    # -- inputs -------------------------------------------------------------
    def focus(self, address: str, now: float) -> None:
        w = self.windows.get(address)
        if w is not None:
            w.last_focus_ts = now

    def set_current_meta(self, name: str | None, now: float) -> None:
        self.current_meta = name
        if name is not None:
            self.seen_ts[name] = now

    def update(self, clients: Iterable[dict], agents: list[tuple[int, str]], now: float,
               state_of=None) -> bool:
        """Rebuild window/agent state from a `hyprctl clients -j` list.

        `agents` = [(pid, kind)], `state_of(kind, pid, title)` -> state.
        Returns True when anything user-visible changed.
        """
        state_of = state_of or default_state_of
        before = self.snapshot()
        window_pids = {}
        seen_addrs = set()
        for c in clients:
            addr = str(c.get("address"))
            pid = int(c.get("pid") or 0)
            ws = int((c.get("workspace") or {}).get("id") or 0)
            title = str(c.get("title") or "")
            seen_addrs.add(addr)
            window_pids[pid] = addr
            w = self.windows.get(addr)
            if w is None:
                w = WindowState(addr, pid, ws, title, self.meta_for_workspace(ws), last_focus_ts=self.started)
                self.windows[addr] = w
            else:
                w.pid, w.workspace, w.title = pid, ws, title
                w.meta = self.meta_for_workspace(ws)
        for addr in list(self.windows):
            if addr not in seen_addrs:
                del self.windows[addr]

        # attach agents to windows
        by_window: dict[str, list[Agent]] = {addr: [] for addr in self.windows}
        for pid, kind in agents:
            wpid = window_pid_for(pid, set(window_pids))
            if wpid is None:
                continue
            addr = window_pids[wpid]
            by_window[addr].append(Agent(pid, kind, state_of(kind, pid, self.windows[addr].title)))
        for addr, new_agents in by_window.items():
            w = self.windows[addr]
            old = {a.pid: a for a in w.agents}
            for a in new_agents:
                prev = old.get(a.pid)
                if prev is not None and prev.state == "running" and a.state == "idle":
                    w.finish_ts, w.finished_by = now, a.kind
            for pid, prev in old.items():  # exited while running counts as finished/stopped
                if prev.state == "running" and pid not in {a.pid for a in new_agents}:
                    w.finish_ts, w.finished_by = now, prev.kind
            w.agents = new_agents
        return self.snapshot() != before

    # -- outputs ------------------------------------------------------------
    def meta_summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {
            name: {"running": 0, "unseen": 0, "agents": 0, "windows": 0} for name in self.metas
        }
        for w in self.windows.values():
            if w.meta is None or w.meta not in out:
                continue
            m = out[w.meta]
            m["windows"] += 1
            m["agents"] += len(w.agents)
            m["running"] += w.running
            if w.finish_ts > self.seen_ts.get(w.meta, self.started):
                m["unseen"] += 1
        return out

    def snapshot(self) -> dict:
        return {
            "metas": self.meta_summary(),
            "windows": {
                addr: {
                    **asdict(w),
                    "running": w.running,
                    "unseen": w.unseen,
                }
                for addr, w in self.windows.items()
                if w.agents or w.finish_ts
            },
            "current_meta": self.current_meta,
        }

    def tags_wanted(self) -> dict[str, set[str]]:
        """address -> tags that should be on that window."""
        out = {}
        for addr, w in self.windows.items():
            tags = set()
            if w.unseen:
                tags.add(TAG_DONE)
            if w.running:
                tags.add(TAG_RUNNING)
            out[addr] = tags
        return out

    def restore(self, data: dict, now: float) -> None:
        """Carry focus/finish stamps across a daemon restart (windows that still exist)."""
        for addr, w in (data.get("windows") or {}).items():
            if addr in self.windows:
                self.windows[addr].last_focus_ts = float(w.get("last_focus_ts") or 0)
                self.windows[addr].finish_ts = float(w.get("finish_ts") or 0)
                self.windows[addr].finished_by = w.get("finished_by")
        for name, ts in (data.get("seen_ts") or {}).items():
            self.seen_ts[name] = float(ts)


def default_state_of(kind: str, pid: int, title: str) -> str | None:
    if kind == "claude":
        return claude_state_from_title(title)
    if kind == "codex":
        return codex_state(pid)
    return None


def write_snapshot(path: Path, tracker: AgentTracker) -> None:
    data = tracker.snapshot()
    data["seen_ts"] = tracker.seen_ts
    data["ts"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def apply_tags(hypr: Hypr, wanted: dict[str, set[str]], current: dict[str, set[str]]) -> dict[str, set[str]]:
    """Diff wanted vs current tags and dispatch the changes in one batch."""
    dispatches = []
    for addr, tags in wanted.items():
        have = current.get(addr, set())
        for t in tags - have:
            dispatches.append(f"tagwindow +{t} address:{addr}")
        for t in have - tags:
            dispatches.append(f"tagwindow -{t} address:{addr}")
    if dispatches:
        hypr.batch(dispatches)
    return {addr: set(tags) for addr, tags in wanted.items()}


# --------------------------------------------------------------------------- #
# waybar rendering
# --------------------------------------------------------------------------- #

COLOR_RUNNING = "#a6e3a1"
COLOR_DONE = "#f9e2af"
COLOR_DIM = "#7f849c"
COLOR_CURRENT = "#cdd6f4"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_waybar(snapshot: dict, order: list[str]) -> dict:
    """One line per meta: name, running count, unseen-finished count."""
    metas = snapshot.get("metas", {})
    current = snapshot.get("current_meta")
    width = max((len(n) for n in order), default=4)
    lines = []
    tips = []
    any_unseen = any_running = False
    for name in order:
        m = metas.get(name, {"running": 0, "unseen": 0, "agents": 0, "windows": 0})
        label = _esc(f"{name:<{width}}")
        if name == current:
            label = f'<span foreground="{COLOR_CURRENT}" weight="bold">{label}</span>'
        else:
            label = f'<span foreground="{COLOR_DIM}">{label}</span>'
        marks = []
        if m["running"]:
            marks.append(f'<span foreground="{COLOR_RUNNING}">⟳{m["running"]}</span>')
            any_running = True
        if m["unseen"]:
            marks.append(f'<span foreground="{COLOR_DONE}">✓{m["unseen"]}</span>')
            any_unseen = True
        if not marks and m["agents"]:
            marks.append(f'<span foreground="{COLOR_DIM}">·{m["agents"]}</span>')
        lines.append(f"{label}  {' '.join(marks)}".rstrip())
        tips.append(f"{name}: {m['windows']} windows, {m['agents']} agents, {m['running']} running, {m['unseen']} finished unseen")
    cls = "attention" if any_unseen else ("running" if any_running else "idle")
    return {"text": "\n".join(lines), "tooltip": "\n".join(tips), "class": cls}
