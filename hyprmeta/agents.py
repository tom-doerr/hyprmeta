"""Agent tracking: which terminals run Claude Code / Codex, and which need a look.

Signals, cheapest first (no hooks):

* The terminal TITLE. Both agents write their state into it, and Hyprland
  delivers every change as a `windowtitlev2` event, so this needs no polling.

  ===========  ==========================  ====================================
  agent        working                     stopped
  ===========  ==========================  ====================================
  Claude Code  `◐◓◑◒` spinner prefix        `✳ <topic>`
  Codex        braille spinner `⠋⠙⠹…`      `<task> | <dir>` (idle) or
                                           `[ ! ] Action Required | …` (waiting)
  ===========  ==========================  ====================================

* Codex fallback when its title says nothing: the NEWEST rollout `.jsonl` among
  the files the process holds open (`/proc/<pid>/fd` — a process holds several,
  including stale sessions); its last task event decides.
* `/proc` parent chains tie agent pids to the ghostty window pid that
  `hyprctl clients -j` reports. Codex's `codex-linux-sandbox` helpers share the
  `codex` comm and are skipped; several processes of one kind in one window are
  one agent.

Attention ("finished since you looked"): an agent seen RUNNING whose state then
stays idle/waiting for `IDLE_CONFIRM_S` (so title flicker never counts), or that
exits, stamps its window with `finish_ts`. The window needs attention while that
stamp is newer than the last moment the window had keyboard focus, it is not
focused right now, and nothing in it runs again. Only focus clears it — merely
showing its workspace does not.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .cli import Hypr

IDLE_CONFIRM_S = 3.0
AGENT_COMMS = {"claude": "claude", "codex": "codex"}
CLAUDE_IDLE = "✳"
# Claude's title spinner, its in-TUI spinner glyphs (defensive), Codex's braille.
SPINNERS = frozenset("◐◓◑◒✢✶✻✽") | frozenset(chr(c) for c in range(0x2801, 0x2900))
ACTION_REQUIRED = re.compile(r"^\[\s*\S\s*\]\s*Action Required")
CODEX_IDLE_TITLE = re.compile(r"^[^|]*\S \| \S")
CODEX_RUNNING_EVENTS = frozenset({"task_started", "user_message"})
CODEX_STOPPED_EVENTS = frozenset({"task_complete", "turn_aborted"})

TAG_DONE = "agent-done"
TAG_RUNNING = "agent-running"
OUR_TAGS = frozenset({TAG_DONE, TAG_RUNNING})

COLOR_RUNNING = "#a6e3a1"
# Unread states are BLUE, never yellow/orange: those read as warnings (user decision).
COLOR_DONE = "#89b4fa"
COLOR_WAITING = "#89b4fa"  # same family; the `!` glyph tells waiting from finished
COLOR_IDLE = "#9399b2"
COLOR_DIM = "#7f849c"
COLOR_CURRENT = "#cdd6f4"


def agents_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(runtime) / "hyprmeta" / "agents.json"


# --------------------------------------------------------------------------- #
# pure classifiers
# --------------------------------------------------------------------------- #


def title_state(kind: str, title: str) -> str | None:
    """`running` / `idle` / `waiting` from an agent's terminal title, None if it says nothing."""
    t = title.strip()
    if not t:
        return None
    if t[0] in SPINNERS:
        return "running"
    if ACTION_REQUIRED.match(t):
        return "waiting"
    if kind == "claude" and t[0] == CLAUDE_IDLE:
        return "idle"
    if kind == "codex" and CODEX_IDLE_TITLE.match(t):
        return "idle"
    return None


def codex_state_from_rollout_tail(tail: str) -> str | None:
    """`running` / `idle` from the last task event in a rollout tail; None if none seen."""
    state = None
    for line in tail.splitlines():
        if '"event_msg"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except (ValueError, AttributeError):
            continue
        kind = payload.get("type")
        if kind in CODEX_RUNNING_EVENTS:
            state = "running"
        elif kind in CODEX_STOPPED_EVENTS:
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
    try:  # comm may contain spaces; ppid is the 2nd field after the closing ')'
        return int(stat[stat.rindex(")") + 2 :].split()[1])
    except (ValueError, IndexError):
        return None


def find_agent_processes() -> list[tuple[int, str]]:
    """(pid, kind) for every claude/codex process, without Codex sandbox helpers."""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        comm = _read(f"/proc/{name}/comm")
        kind = AGENT_COMMS.get(comm.strip()) if comm else None
        if kind is None:
            continue
        if kind == "codex":
            argv0 = (_read(f"/proc/{name}/cmdline") or "").split("\0", 1)[0]
            if os.path.basename(argv0).startswith("codex-linux-sandbox"):
                continue
        out.append((int(name), kind))
    return out


def window_pid_for(pid: int, window_pids: set[int], max_depth: int = 14) -> int | None:
    """Walk up the parent chain until we hit a pid Hyprland knows as a window."""
    cur: int | None = pid
    for _ in range(max_depth):
        if cur is None or cur <= 1:
            return None
        if cur in window_pids:
            return cur
        cur = parent_of(cur)
    return None


def _codex_rollouts(pid: int) -> list[str]:
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return []
    out = []
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if "/.codex/sessions/" in target and target.endswith(".jsonl"):
            out.append(target)
    return out


def codex_rollout_state(pids: Iterable[int], tail_bytes: int = 65536) -> str | None:
    best, best_mtime = None, -1.0
    for pid in pids:
        for path in _codex_rollouts(pid):
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    if best is None:
        return None
    try:
        with open(best, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    return codex_state_from_rollout_tail(tail)


def default_state_of(kind: str, pids: list[int], title: str) -> str | None:
    state = title_state(kind, title)
    if state is None and kind == "codex":
        state = codex_rollout_state(pids)
    return state


# --------------------------------------------------------------------------- #
# tracker
# --------------------------------------------------------------------------- #


@dataclass
class Agent:
    kind: str  # claude | codex
    pids: list[int]
    state: str | None = None  # running | idle | waiting | None (title says nothing)
    since: float = 0.0  # when `state` began
    armed: bool = False  # seen running; a stable stop (or exit) counts as a finish


@dataclass
class WindowState:
    address: str
    pid: int
    workspace: int
    title: str
    meta: str | None
    agents: dict[str, Agent] = field(default_factory=dict)  # by kind
    last_focus_ts: float = 0.0  # last moment this window had keyboard focus
    finish_ts: float = 0.0
    finished_by: str | None = None
    finish_state: str | None = None  # idle | waiting | exited

    @property
    def running(self) -> int:
        return sum(1 for a in self.agents.values() if a.state == "running")


StateOf = Callable[[str, list[int], str], "str | None"]


class AgentTracker:
    """Pure bookkeeping; the daemon feeds it clients, focus/title events and time."""

    def __init__(self, base: list[int], metas: dict[str, int], now: float | None = None,
                 confirm_s: float = IDLE_CONFIRM_S) -> None:
        self.base = list(base)
        self.metas = dict(metas)
        self.confirm_s = confirm_s
        self.windows: dict[str, WindowState] = {}
        self.focused: str | None = None
        self.current_meta: str | None = None
        self.started = time.time() if now is None else now
        self.events: list[str] = []  # human-readable finish log lines, drained by the daemon

    # -- configuration -------------------------------------------------------
    def set_metas(self, metas: dict[str, int]) -> None:
        self.metas = dict(metas)
        for w in self.windows.values():
            w.meta = self.meta_for_workspace(w.workspace)

    def meta_for_workspace(self, ws: int) -> str | None:
        for b in self.base:
            off = ws - b
            if off < 0:
                continue
            for name, meta_off in self.metas.items():
                if meta_off == off:
                    return name
        return None

    # -- the state machine ----------------------------------------------------
    def _observe(self, w: WindowState, a: Agent, state: str | None, now: float) -> None:
        if state != a.state:
            a.state, a.since = state, now
        if state == "running":
            a.armed = True
        self._confirm(w, a, now)

    def _confirm(self, w: WindowState, a: Agent, now: float) -> None:
        if a.armed and a.state in ("idle", "waiting") and now - a.since >= self.confirm_s:
            a.armed = False
            self._stamp(w, a.kind, a.state, a.since)

    def _stamp(self, w: WindowState, kind: str, how: str, ts: float) -> None:
        w.finish_ts = max(w.finish_ts, ts)
        w.finished_by, w.finish_state = kind, how
        self.events.append(f"{kind} {how} in {w.address} (ws {w.workspace}, meta {w.meta}): {w.title[:60]!r}")

    def needs_attention(self, w: WindowState) -> bool:
        return w.finish_ts > w.last_focus_ts and w.address != self.focused and w.running == 0

    # -- inputs -------------------------------------------------------------
    def focus(self, address: str | None, now: float) -> None:
        """Keyboard focus moved. Both the window left and the one entered count as seen now."""
        if address == self.focused:
            return
        prev = self.windows.get(self.focused) if self.focused else None
        if prev is not None:
            prev.last_focus_ts = now
        self.focused = address
        cur = self.windows.get(address) if address else None
        if cur is not None:
            cur.last_focus_ts = now

    def set_title(self, address: str, title: str, now: float) -> None:
        """Fast path for `windowtitlev2`: re-derive title-based states, no /proc or hyprctl."""
        w = self.windows.get(address)
        if w is None:
            return
        w.title = title
        for a in w.agents.values():
            state = title_state(a.kind, title)
            if state is not None:
                self._observe(w, a, state, now)

    def set_current_meta(self, name: str | None) -> None:
        self.current_meta = name  # display only; showing a meta clears nothing

    def tick(self, now: float) -> None:
        for w in self.windows.values():
            for a in w.agents.values():
                self._confirm(w, a, now)

    def update(self, clients: Iterable[dict], agents: Iterable[tuple[int, str]], now: float,
               state_of: StateOf | None = None) -> None:
        """Full rebuild from `hyprctl clients -j` + the agent process list."""
        state_of = state_of or default_state_of
        present: dict[str, WindowState] = {}
        focused = self.focused
        for c in clients:
            addr = str(c.get("address"))
            ws = int((c.get("workspace") or {}).get("id") or 0)
            pid = int(c.get("pid") or 0)
            title = str(c.get("title") or "")
            w = self.windows.get(addr)
            if w is None:
                w = self.windows[addr] = WindowState(addr, pid, ws, title, None, last_focus_ts=now)
            w.pid, w.workspace, w.title = pid, ws, title
            w.meta = self.meta_for_workspace(ws)
            present[addr] = w
            if c.get("focusHistoryID") == 0:
                focused = addr
        for addr in list(self.windows):
            if addr not in present:
                del self.windows[addr]
        self.focus(focused, now)  # sync with the compositor in case an event was missed

        by_pid = {w.pid: addr for addr, w in present.items()}
        grouped: dict[str, dict[str, list[int]]] = {}
        for pid, kind in agents:
            wpid = window_pid_for(pid, set(by_pid))
            if wpid is not None:
                grouped.setdefault(by_pid[wpid], {}).setdefault(kind, []).append(pid)
        for addr, w in self.windows.items():
            kinds = grouped.get(addr, {})
            for kind in [k for k in w.agents if k not in kinds]:
                if w.agents[kind].armed:  # died or quit while working
                    self._stamp(w, kind, "exited", now)
                del w.agents[kind]
            for kind, pids in kinds.items():
                a = w.agents.get(kind)
                if a is None:
                    a = w.agents[kind] = Agent(kind, sorted(pids), None, now)
                a.pids = sorted(pids)
                self._observe(w, a, state_of(kind, a.pids, w.title), now)

    # -- outputs ------------------------------------------------------------
    def meta_summary(self) -> dict[str, dict[str, int]]:
        keys = ("running", "done", "waiting", "idle", "agents", "windows")
        out = {name: dict.fromkeys(keys, 0) for name in self.metas}
        for w in self.windows.values():
            m = out.get(w.meta) if w.meta else None
            if m is None:
                continue
            m["windows"] += 1
            m["agents"] += len(w.agents)
            m["running"] += w.running
            if self.needs_attention(w):
                waiting = any(a.state == "waiting" for a in w.agents.values())
                m["waiting" if waiting else "done"] += 1
            else:
                m["idle"] += len(w.agents) - w.running
        return out

    def tags_wanted(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for addr, w in self.windows.items():
            if self.needs_attention(w):
                out[addr] = {TAG_DONE}
            elif w.running:
                out[addr] = {TAG_RUNNING}
            else:
                out[addr] = set()
        return out

    def visible_state(self) -> str:
        """Everything a viewer can see; compare before/after to decide whether to publish."""
        tags = {a: sorted(t) for a, t in self.tags_wanted().items() if t}
        return json.dumps([self.meta_summary(), tags, self.current_meta, self.focused], sort_keys=True)

    def snapshot(self, now: float) -> dict:
        windows = {}
        for addr, w in self.windows.items():
            if not (w.agents or w.finish_ts):
                continue
            windows[addr] = {
                "address": addr, "pid": w.pid, "workspace": w.workspace, "title": w.title,
                "meta": w.meta, "last_focus_ts": w.last_focus_ts, "finish_ts": w.finish_ts,
                "finished_by": w.finished_by, "finish_state": w.finish_state,
                "attention": self.needs_attention(w), "running": w.running,
                "agents": [
                    {"kind": a.kind, "pids": a.pids, "state": a.state, "since": a.since, "armed": a.armed}
                    for a in w.agents.values()
                ],
            }
        return {"ts": now, "focused": self.focused, "current_meta": self.current_meta,
                "metas": self.meta_summary(), "windows": windows}

    def restore(self, data: dict) -> None:
        """Carry stamps (and 'was working') across a daemon restart, for windows that still exist."""
        for addr, old in (data.get("windows") or {}).items():
            w = self.windows.get(addr)
            if w is None or not isinstance(old, dict):
                continue
            w.last_focus_ts = float(old.get("last_focus_ts") or 0.0)
            w.finish_ts = float(old.get("finish_ts") or 0.0)
            w.finished_by = old.get("finished_by")
            w.finish_state = old.get("finish_state")
            for oa in old.get("agents") or []:
                a = w.agents.get(oa.get("kind")) if isinstance(oa, dict) else None
                if a is not None and (oa.get("armed") or oa.get("state") == "running"):
                    a.armed = True


def write_snapshot(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def actual_tags(clients: Iterable[dict]) -> dict[str, set[str]]:
    """Our tags as the compositor has them (a trailing `*` marks rule-set tags)."""
    return {
        str(c.get("address")): {t.rstrip("*") for t in (c.get("tags") or []) if t.rstrip("*") in OUR_TAGS}
        for c in clients
    }


def apply_tags(hypr: Hypr, wanted: dict[str, set[str]], current: dict[str, set[str]]) -> dict[str, set[str]]:
    """Diff wanted vs current tags and dispatch the changes in one batch."""
    dispatches = []
    for addr, tags in wanted.items():
        have = current.get(addr, set())
        for t in sorted(tags - have):
            dispatches.append(f"tagwindow +{t} address:{addr}")
        for t in sorted(have - tags):
            dispatches.append(f"tagwindow -{t} address:{addr}")
    if dispatches:
        hypr.batch(dispatches)
    return {addr: set(tags) for addr, tags in wanted.items()}


# --------------------------------------------------------------------------- #
# rendering (waybar sidebar + picker rows)
# --------------------------------------------------------------------------- #


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Every glyph must exist in JetBrains Mono so it is exactly one cell wide: the
# sidebar right-aligns markers with spaces, and a fallback-font glyph (⟳ is 11 px
# against an 8 px cell at 13 px) would shove that row's name out of its column.
MARKS = (
    ("running", "▶", COLOR_RUNNING),
    ("done", "✓", COLOR_DONE),
    ("waiting", "!", COLOR_WAITING),
    ("idle", "○", COLOR_IDLE),
)
LEGEND = "▶ working   ✓ finished since you focused it   ! waiting for you   ○ idle"


def marks_parts(m: dict) -> list[tuple[str, str]]:
    """(visible text, colour) per non-zero category, in display order."""
    return [(f"{glyph}{m[key]}", color) for key, glyph, color in MARKS if m.get(key)]


def marks_text(m: dict) -> str:
    """The markers as plain text — its len() is their width in cells."""
    return " ".join(text for text, _ in marks_parts(m))


def marks_markup(m: dict) -> str:
    """Pango: ▶N working · ✓N finished unseen · !N waiting for you · ○N idle."""
    return " ".join(f'<span foreground="{color}">{_esc(text)}</span>' for text, color in marks_parts(m))


def name_color(m: dict) -> str | None:
    """The meta name's colour: the count-weighted mean (per sRGB channel) of the
    marker colours of its NON-idle agents — 2 working + 1 finished = ⅔ green +
    ⅓ blue (user request). All agents idle = the idle colour; no agents = None."""
    weights = [(m[key], color) for key, _, color in MARKS if key != "idle" and m.get(key)]
    total = sum(n for n, _ in weights)
    if not total:
        return COLOR_IDLE if m.get("idle") else None
    rgb = (sum(n * int(color[1 + 2 * i : 3 + 2 * i], 16) for n, color in weights) / total for i in range(3))
    return "#" + "".join(f"{round(c):02x}" for c in rgb)


ROW_SEP = f'  <span foreground="{COLOR_DIM}">│</span>  '
ROW_SEP_CELLS = 5  # visible width of ROW_SEP


def pack_row(entries: list[str], widths: list[int], wrap: int) -> str:
    """Join entries with ROW_SEP, breaking the line before an entry that would
    push it past `wrap` cells (0 = never). Entries are never split: one wider
    than `wrap` gets a line to itself."""
    lines, line, used = [], [], 0
    for entry, width in zip(entries, widths):
        if line and wrap and used + ROW_SEP_CELLS + width > wrap:
            lines.append(ROW_SEP.join(line))
            line, used = [], 0
        used += (ROW_SEP_CELLS if line else 0) + width
        line.append(entry)
    if line:
        lines.append(ROW_SEP.join(line))
    return "\n".join(lines)


def render_waybar(snapshot: dict, order: list[str], row: bool = False, wrap: int = 0) -> dict:
    """One line per meta: markers right-aligned in a column, then the name.

    Right-aligning puts each row's markers directly against its own name while
    the names still form one column (user request: see what belongs to what).
    row=True puts every meta on ONE line instead, for a horizontal bar: same
    markers and names, no alignment padding, separated by a dim bar. wrap=N
    continues on a new line before a line would pass N cells; waybar grows the
    bar for the extra line and shrinks it back when it is gone.
    """
    metas = snapshot.get("metas", {})
    current = snapshot.get("current_meta")
    col = max((len(marks_text(metas.get(n, {}))) for n in order), default=0)
    lines, widths, tips = [], [], []
    attention = running = False
    for name in order:
        m = metas.get(name, {})
        # agent state colours the name; bold alone marks the current meta
        color = name_color(m) or (COLOR_CURRENT if name == current else COLOR_DIM)
        bold = ' weight="bold"' if name == current else ""
        label = f'<span foreground="{color}"{bold}>{_esc(name)}</span>'
        if row:
            marks = marks_markup(m)
            lines.append(f"{marks} {label}" if marks else label)
            mt = marks_text(m)
            widths.append(len(mt) + 1 + len(name) if mt else len(name))
        else:
            pad = " " * (col - len(marks_text(m)))
            lines.append(f"{pad}{marks_markup(m)}{' ' if col else ''}{label}")
        attention = attention or bool(m.get("done") or m.get("waiting"))
        running = running or bool(m.get("running"))
        tips.append(
            f"{name}: {m.get('running', 0)} working, {m.get('done', 0)} finished unseen, "
            f"{m.get('waiting', 0)} waiting, {m.get('idle', 0)} idle ({m.get('windows', 0)} windows)"
        )
    tips.append(LEGEND)
    cls = "attention" if attention else ("running" if running else "idle")
    text = pack_row(lines, widths, wrap) if row else "\n".join(lines)
    return {"text": text, "tooltip": "\n".join(tips), "class": cls}
