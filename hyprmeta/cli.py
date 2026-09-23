"""hyprmeta: meta workspaces for Hyprland.

A *meta workspace* is a named offset. Every monitor has a base workspace
(left to right, e.g. 4 5 6); switching to the meta workspace "taxes" with
offset 10 shows 14 15 16 across the same monitors. All state lives in two
small JSON files; every change to the compositor goes through `hyprctl`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

__all__ = ["main", "Hypr", "Store"]

ASSETS = Path(__file__).resolve().parent / "assets"

# Menu lines are "<glyph>  <name><two or more spaces><workspaces>".
GLYPH_CURRENT = "●"
GLYPH_OTHER = "○"
GLYPH_NEW = "＋"
NEW_ENTRY = f"{GLYPH_NEW}  new meta workspace"

Runner = Callable[[Sequence[str]], str]


class HyprmetaError(Exception):
    """A user-facing failure; the CLI prints it and exits 1."""


# --------------------------------------------------------------------------- #
# hyprctl access
# --------------------------------------------------------------------------- #


def _run_hyprctl(args: Sequence[str]) -> str:
    proc = subprocess.run(
        ["hyprctl", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise HyprmetaError(
            f"hyprctl {' '.join(args)} failed (rc {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


@dataclass(frozen=True)
class Monitor:
    name: str
    x: int
    active_ws: int
    focused: bool


class Hypr:
    """Thin wrapper over hyprctl. `run` is injectable for tests."""

    def __init__(self, run: Runner = _run_hyprctl) -> None:
        self._run = run

    def monitors(self) -> list[Monitor]:
        raw = json.loads(self._run(["-j", "monitors"]))
        mons = [
            Monitor(
                name=m["name"],
                x=int(m["x"]),
                active_ws=int(m["activeWorkspace"]["id"]),
                focused=bool(m.get("focused", False)),
            )
            for m in raw
        ]
        if not mons:
            raise HyprmetaError("hyprctl reports no monitors")
        return sorted(mons, key=lambda m: m.x)

    def workspace_monitors(self) -> dict[int, str]:
        raw = json.loads(self._run(["-j", "workspaces"]))
        return {int(w["id"]): w["monitor"] for w in raw}

    def active_window_monitor(self) -> str:
        raw = json.loads(self._run(["-j", "activewindow"]))
        if not raw or "monitor" not in raw:
            raise HyprmetaError("no focused window")
        # activewindow reports the monitor id, not its name.
        for m in json.loads(self._run(["-j", "monitors"])):
            if int(m["id"]) == int(raw["monitor"]):
                return str(m["name"])
        raise HyprmetaError(f"focused window is on unknown monitor id {raw['monitor']}")

    def cursor(self) -> tuple[int, int]:
        text = self._run(["cursorpos"]).strip()
        try:
            xs, ys = text.split(",")
            return int(xs), int(ys)
        except ValueError as exc:
            raise HyprmetaError(f"unparseable cursorpos output: {text!r}") from exc

    def batch(self, dispatches: Sequence[str]) -> None:
        if not dispatches:
            return
        self._run(["--batch", "; ".join(f"dispatch {d}" for d in dispatches)])


# --------------------------------------------------------------------------- #
# config + state
# --------------------------------------------------------------------------- #


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_path() -> Path:
    override = os.environ.get("HYPRMETA_CONFIG")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / "hyprmeta" / "config.json"


def state_path() -> Path:
    override = os.environ.get("HYPRMETA_STATE")
    if override:
        return Path(override)
    return _xdg("XDG_STATE_HOME", ".local/state") / "hyprmeta" / "state.json"


# `{assets}` expands to the package's assets directory (wofi config + styles).
DEFAULT_MENU = (
    "wofi --dmenu --conf {assets}/wofi.conf --style {assets}/wofi.css "
    '--prompt "meta workspace"'
)
# The new-name dialog shows NEW_HINT as its only line, which keeps wofi's focus
# on the list so the GTK placeholder (prompt) stays visible; --exec-search makes
# Enter return the typed text regardless of that line. Sizing: wofi 1.4 ignores
# --height/--lines for the surface once entries arrive; dynamic_lines with
# lines=3 measured 131 px = input + one hint row (lines=2 clips the row).
DEFAULT_MENU_NEW = (
    "wofi --dmenu --conf {assets}/wofi.conf --style {assets}/wofi-new.css "
    "--exec-search -D dynamic_lines=true -D lines=3 "
    '--prompt "＋ name for the new meta workspace"'
)
NEW_HINT = "ᴛʏᴘᴇ ᴀ ɴᴀᴍᴇ · ᴇɴᴛᴇʀ ᴄʀᴇᴀᴛᴇs ɪᴛ · ᴇsᴄ ᴄᴀɴᴄᴇʟs"


@dataclass
class Config:
    base: list[int]
    step: int = 10
    menu: str = DEFAULT_MENU
    menu_new: str = DEFAULT_MENU_NEW

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise HyprmetaError(
                f"no config at {path}; run `hyprmeta init` while the monitors "
                "show the workspaces you want as the base layout"
            )
        data = json.loads(path.read_text())
        base = [int(b) for b in data["base"]]
        if not base:
            raise HyprmetaError(f"{path}: `base` must list one workspace per monitor")
        step = int(data.get("step", 10))
        if step <= 0:
            raise HyprmetaError(f"{path}: `step` must be positive")
        return cls(
            base=base,
            step=step,
            menu=str(data.get("menu", DEFAULT_MENU)),
            menu_new=str(data.get("menu_new", DEFAULT_MENU_NEW)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"base": self.base, "step": self.step, "menu": self.menu, "menu_new": self.menu_new},
                indent=2,
            )
            + "\n"
        )


@dataclass
class Store:
    """Named offsets plus when each one was last opened.

    `recent` is the most-recently-opened order (index 0 = current), kept in
    sync with `last_used` (epoch seconds) on every `touch`.
    """

    metas: dict[str, int]
    recent: list[str]
    last_used: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.last_used is None:
            self.last_used = {}

    @classmethod
    def load(cls, path: Path) -> "Store":
        if not path.exists():
            return cls(metas={}, recent=[])
        data = json.loads(path.read_text())
        metas = {str(k): int(v) for k, v in data.get("metas", {}).items()}
        recent = [n for n in data.get("recent", []) if n in metas]
        last_used = {
            str(k): float(v) for k, v in data.get("last_used", {}).items() if str(k) in metas
        }
        return cls(metas=metas, recent=recent, last_used=last_used)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"metas": self.metas, "recent": self.recent, "last_used": self.last_used},
                indent=2,
            )
            + "\n"
        )
        os.replace(tmp, path)

    def offset(self, name: str) -> int:
        if name not in self.metas:
            raise HyprmetaError(
                f"unknown meta workspace {name!r} (known: {', '.join(self.ordered()) or 'none'})"
            )
        return self.metas[name]

    def name_for_offset(self, offset: int) -> str | None:
        for name, off in self.metas.items():
            if off == offset:
                return name
        return None

    def ordered(self) -> list[str]:
        """Most recently opened first; never-opened ones last, alphabetically."""
        assert self.last_used is not None
        used = sorted(
            (n for n in self.metas if n in self.last_used),
            key=lambda n: -self.last_used[n],  # type: ignore[index]
        )
        legacy = [n for n in self.recent if n in self.metas and n not in self.last_used]
        rest = sorted(n for n in self.metas if n not in self.last_used and n not in legacy)
        return [*used, *legacy, *rest]

    def touch(self, name: str, now: float | None = None) -> None:
        assert self.last_used is not None
        self.last_used[name] = time.time() if now is None else now
        self.recent = [name, *[n for n in self.recent if n != name]]

    def next_free_offset(self, step: int) -> int:
        used = set(self.metas.values())
        candidate = 0
        while candidate in used:
            candidate += step
        return candidate

    def create(self, name: str, offset: int) -> None:
        name = validate_name(name)
        if name in self.metas:
            raise HyprmetaError(f"meta workspace {name!r} already exists")
        owner = self.name_for_offset(offset)
        if owner is not None:
            raise HyprmetaError(f"offset {offset} is already used by {owner!r}")
        if offset < 0:
            raise HyprmetaError("offset must be >= 0")
        self.metas[name] = offset

    def remove(self, name: str) -> None:
        assert self.last_used is not None
        self.offset(name)
        del self.metas[name]
        self.recent = [n for n in self.recent if n != name]
        self.last_used.pop(name, None)

    def rename(self, old: str, new: str) -> None:
        assert self.last_used is not None
        new = validate_name(new)
        off = self.offset(old)
        if new in self.metas:
            raise HyprmetaError(f"meta workspace {new!r} already exists")
        del self.metas[old]
        self.metas[new] = off
        self.recent = [new if n == old else n for n in self.recent]
        if old in self.last_used:
            self.last_used[new] = self.last_used.pop(old)


def humanize_ago(seconds: float) -> str:
    """Coarse, one-unit relative time: `just now`, `5 min ago`, `3 h ago`, `2 d ago`."""
    s = max(0.0, seconds)
    if s < 45:
        return "just now"
    minutes = round(s / 60)
    if minutes < 90:
        return f"{minutes} min ago"
    hours = round(s / 3600)
    if hours < 36:
        return f"{hours} h ago"
    days = round(s / 86400)
    if days < 14:
        return f"{days} d ago"
    weeks = round(s / 604800)
    if weeks < 9:
        return f"{weeks} wk ago"
    months = round(s / 2629800)
    if months < 18:
        return f"{months} mo ago"
    return f"{round(s / 31557600)} yr ago"


def validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HyprmetaError("meta workspace name must not be empty")
    if name == NEW_ENTRY or name[0] in (GLYPH_CURRENT, GLYPH_OTHER, GLYPH_NEW):
        raise HyprmetaError(f"{name!r} is reserved")
    if "\t" in name or "\n" in name or "  " in name:
        raise HyprmetaError("meta workspace name must not contain tabs, newlines or double spaces")
    return name


# --------------------------------------------------------------------------- #
# core operations
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, hypr: Hypr, cfg: Config, store: Store, store_file: Path) -> None:
        self.hypr = hypr
        self.cfg = cfg
        self.store = store
        self.store_file = store_file

    # -- geometry ---------------------------------------------------------- #

    def monitors(self) -> list[Monitor]:
        mons = self.hypr.monitors()
        if len(mons) != len(self.cfg.base):
            raise HyprmetaError(
                f"config has {len(self.cfg.base)} base workspaces but Hyprland "
                f"reports {len(mons)} monitors ({', '.join(m.name for m in mons)}); "
                "re-run `hyprmeta init` or edit the config"
            )
        return mons

    def workspaces_for(self, offset: int) -> list[int]:
        return [b + offset for b in self.cfg.base]

    def current_offset(self, mons: Sequence[Monitor] | None = None) -> int | None:
        """The offset every monitor agrees on, or None when they disagree."""
        mons = list(mons) if mons is not None else self.monitors()
        offsets = {m.active_ws - b for m, b in zip(mons, self.cfg.base)}
        if len(offsets) != 1:
            return None
        return offsets.pop()

    def current_name(self) -> str | None:
        off = self.current_offset()
        if off is None:
            return None
        return self.store.name_for_offset(off)

    # -- actions ----------------------------------------------------------- #

    def resolve(self, name: str) -> str:
        """Expand `-` to the previously used meta workspace."""
        if name != "-":
            return name
        if len(self.store.recent) < 2:
            raise HyprmetaError("no previous meta workspace to switch back to")
        return self.store.recent[1]

    def switch(self, name: str) -> list[int]:
        name = self.resolve(name)
        offset = self.store.offset(name)
        mons = self.monitors()
        targets = self.workspaces_for(offset)
        focused = next((m for m in mons if m.focused), mons[0])
        cx, cy = self.hypr.cursor()

        dispatches: list[str] = []
        for mon, ws in zip(mons, targets):
            if mon.active_ws == ws:
                continue
            dispatches.append(f"focusmonitor {mon.name}")
            # focusworkspaceoncurrentmonitor moves an existing workspace over
            # (if it lives elsewhere) and creates it if it does not exist yet.
            dispatches.append(f"focusworkspaceoncurrentmonitor {ws}")
        if dispatches:
            dispatches.append(f"focusmonitor {focused.name}")
            dispatches.append(f"movecursor {cx} {cy}")
        self.hypr.batch(dispatches)

        self.store.touch(name)
        self.store.save(self.store_file)
        return targets

    def move_window(self, name: str, follow: bool = False) -> int:
        offset = self.store.offset(name)
        mons = self.monitors()
        mon_name = self.hypr.active_window_monitor()
        slot = next((i for i, m in enumerate(mons) if m.name == mon_name), None)
        if slot is None:
            raise HyprmetaError(f"focused window's monitor {mon_name!r} is not in the layout")
        ws = self.cfg.base[slot] + offset
        self.hypr.batch([f"movetoworkspacesilent {ws}"])
        if follow:
            self.switch(name)
        return ws

    def goto_slot(self, n: int) -> int:
        """Workspace `n` relative to the current meta (Super+N replacement)."""
        off = self.current_offset()
        if off is None:
            raise HyprmetaError("monitors disagree on the current meta workspace; switch first")
        ws = n + off
        self.hypr.batch([f"workspace {ws}"])
        return ws

    def move_to_slot(self, n: int, silent: bool = False) -> int:
        off = self.current_offset()
        if off is None:
            raise HyprmetaError("monitors disagree on the current meta workspace; switch first")
        ws = n + off
        self.hypr.batch([f"{'movetoworkspacesilent' if silent else 'movetoworkspace'} {ws}"])
        return ws

    def create(self, name: str, offset: int | None) -> int:
        if offset is None:
            offset = self.store.next_free_offset(self.cfg.step)
        self.store.create(name, offset)
        self.store.save(self.store_file)
        return offset

    # -- picker ------------------------------------------------------------ #

    def age_label(self, name: str, now: float | None = None) -> str:
        """`current`, `never opened`, or how long ago it was opened."""
        assert self.store.last_used is not None
        ts = self.store.last_used.get(name)
        if ts is None:
            return "never opened"
        return humanize_ago((time.time() if now is None else now) - ts)

    def menu_lines(self, now: float | None = None) -> list[str]:
        """One aligned line per meta: `●  taxes     current`, most recent first."""
        cur = self.current_name()
        names = self.store.ordered()
        width = max((len(n) for n in names), default=0) + 3
        lines = []
        for name in names:
            glyph = GLYPH_CURRENT if name == cur else GLYPH_OTHER
            age = "current" if name == cur else self.age_label(name, now)
            lines.append(f"{glyph}  {name:<{width}}{age}")
        lines.append(NEW_ENTRY)
        return lines

    @staticmethod
    def name_from_line(line: str) -> str:
        """Inverse of `menu_lines`; a typed query comes back unchanged."""
        line = line.strip()
        for glyph in (GLYPH_CURRENT, GLYPH_OTHER):
            prefix = f"{glyph}  "
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        return line.split("  ", 1)[0].strip()

    def resolve_pick(self, choice: str, prompt: Callable[[], str]) -> str:
        """Turn a menu selection into a meta name, creating one when asked."""
        choice = choice.rstrip("\n")
        if not choice:
            raise HyprmetaError("nothing selected")
        if choice == NEW_ENTRY:
            typed = prompt().strip()
            if not typed or typed == NEW_HINT:
                raise HyprmetaError("no name typed for the new meta workspace")
            name = validate_name(typed)
            self.create(name, None)
            return name
        name = self.name_from_line(choice)
        if name in self.store.metas:
            return name
        # A typed query that matched nothing: treat it as a new name.
        name = validate_name(name)
        self.create(name, None)
        return name


def expand_menu(command: str) -> str:
    return command.replace("{assets}", str(ASSETS))


class PickerLock:
    """One picker at a time: a second `pick` closes the first instead of stacking.

    The pid file holds the process-group id of the running picker, so killing
    it takes the menu child (wofi/fzf) down with the Python parent.
    """

    def __init__(
        self,
        path: Path | None = None,
        alive: Callable[[int], bool] | None = None,
        kill: Callable[[int], None] | None = None,
    ) -> None:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
        self.path = path or Path(runtime) / "hyprmeta-pick.pid"
        self._alive = alive or self._pid_alive
        self._kill = kill or self._killpg

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return b"hyprmeta" in cmdline

    @staticmethod
    def _killpg(pgid: int) -> None:
        import signal

        os.killpg(pgid, signal.SIGTERM)

    def running(self) -> int | None:
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if self._alive(pid) else None

    def close_running(self) -> bool:
        pid = self.running()
        if pid is None:
            return False
        self._kill(pid)
        try:
            self.path.unlink()
        except OSError:
            pass
        return True

    def __enter__(self) -> "PickerLock":
        try:
            os.setpgrp()
        except OSError:
            pass  # already a group leader; killpg(pid) still reaches us
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{os.getpid()}\n")
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def run_menu(command: str, lines: Sequence[str]) -> str:
    argv = shlex.split(expand_menu(command))
    if not argv:
        raise HyprmetaError("menu command is empty")
    # No trailing newline when there are no lines: wofi would show one empty,
    # selectable entry for it.
    stdin = "\n".join(lines) + "\n" if lines else ""
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        # Escape in the menu is a cancel, not an error worth a traceback.
        raise HyprmetaError("menu cancelled" if not proc.stderr.strip() else proc.stderr.strip())
    return proc.stdout.strip("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hyprmeta", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write the config from the workspaces the monitors show now")
    s.add_argument("--name", default="home", help="name for the offset-0 meta (default: home)")
    s.add_argument("--step", type=int, default=10)
    s.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("list", help="list meta workspaces (most recent first)")
    sub.add_parser("current", help="print the current meta workspace name")

    s = sub.add_parser("switch", help="show a meta workspace on every monitor")
    s.add_argument("name", help="meta name, or `-` for the previous one")
    s.add_argument("--create", action="store_true", help="create it if unknown")

    s = sub.add_parser("create", help="define a meta workspace")
    s.add_argument("name")
    s.add_argument("--offset", type=int, default=None, help="default: next free multiple of step")

    s = sub.add_parser("remove", help="forget a meta workspace (windows are untouched)")
    s.add_argument("name")

    s = sub.add_parser("rename")
    s.add_argument("old")
    s.add_argument("new")

    s = sub.add_parser("move", help="move the focused window to the same slot in another meta")
    s.add_argument("name")
    s.add_argument("--follow", action="store_true", help="switch there afterwards")

    s = sub.add_parser("pick", help="fuzzy-pick a meta workspace via the menu command")
    s.add_argument("--move", action="store_true", help="move the focused window instead of switching")
    s.add_argument("--follow", action="store_true", help="with --move: switch there afterwards")
    s.add_argument("--menu", default=None, help="override the menu command from the config")

    s = sub.add_parser("goto", help="workspace N relative to the current meta (for Super+N binds)")
    s.add_argument("n", type=int)

    s = sub.add_parser("moveto", help="move the focused window to slot N of the current meta")
    s.add_argument("n", type=int)
    s.add_argument("--silent", action="store_true")

    return p


def cmd_init(args: argparse.Namespace, hypr: Hypr) -> int:
    cfg_file = config_path()
    if cfg_file.exists() and not args.force:
        raise HyprmetaError(f"{cfg_file} exists; pass --force to overwrite")
    mons = hypr.monitors()
    cfg = Config(base=[m.active_ws for m in mons], step=args.step)
    cfg.save(cfg_file)
    store = Store.load(state_path())
    if args.name not in store.metas:
        if store.name_for_offset(0) is None:
            store.create(args.name, 0)
            store.touch(args.name)
            store.save(state_path())
    print(
        f"wrote {cfg_file}: base {' '.join(str(b) for b in cfg.base)} "
        f"({', '.join(m.name for m in mons)}), step {cfg.step}"
    )
    return 0


def main(argv: Sequence[str] | None = None, hypr: Hypr | None = None) -> int:
    args = build_parser().parse_args(argv)
    hypr = hypr or Hypr()
    try:
        if args.cmd == "init":
            return cmd_init(args, hypr)

        cfg = Config.load(config_path())
        store_file = state_path()
        app = App(hypr, cfg, Store.load(store_file), store_file)

        if args.cmd == "list":
            cur = app.current_name()
            for name in app.store.ordered():
                ws = " ".join(str(w) for w in app.workspaces_for(app.store.metas[name]))
                mark = "*" if name == cur else " "
                age = "current" if name == cur else app.age_label(name)
                print(f"{mark} {name}\t{app.store.metas[name]:>4}\t{ws}\t{age}")
            if cur is None:
                off = app.current_offset()
                where = "monitors disagree" if off is None else f"offset {off} has no name"
                print(f"(current: none — {where})", file=sys.stderr)
        elif args.cmd == "current":
            cur = app.current_name()
            if cur is None:
                raise HyprmetaError("no meta workspace is active")
            print(cur)
        elif args.cmd == "switch":
            name = app.resolve(args.name)
            if args.create and name not in app.store.metas:
                app.create(name, None)
            targets = app.switch(name)
            print(f"{name}: {' '.join(str(t) for t in targets)}")
        elif args.cmd == "create":
            off = app.create(args.name, args.offset)
            print(f"{args.name}: offset {off} -> {' '.join(str(t) for t in app.workspaces_for(off))}")
        elif args.cmd == "remove":
            app.store.remove(args.name)
            app.store.save(store_file)
        elif args.cmd == "rename":
            app.store.rename(args.old, args.new)
            app.store.save(store_file)
        elif args.cmd == "move":
            ws = app.move_window(args.name, follow=args.follow)
            print(f"moved to workspace {ws} ({args.name})")
        elif args.cmd == "pick":
            lock = PickerLock()
            if lock.close_running():
                print("closed the open picker")
                return 0
            menu = args.menu or cfg.menu
            menu_new = args.menu or cfg.menu_new
            with lock:
                choice = run_menu(menu, app.menu_lines())
                name = app.resolve_pick(choice, lambda: run_menu(menu_new, [NEW_HINT]))
            if args.move:
                ws = app.move_window(name, follow=args.follow)
                print(f"moved to workspace {ws} ({name})")
            else:
                targets = app.switch(name)
                print(f"{name}: {' '.join(str(t) for t in targets)}")
        elif args.cmd == "goto":
            print(app.goto_slot(args.n))
        elif args.cmd == "moveto":
            print(app.move_to_slot(args.n, silent=args.silent))
        else:  # pragma: no cover - argparse enforces the choices
            raise HyprmetaError(f"unknown command {args.cmd}")
    except HyprmetaError as exc:
        print(f"hyprmeta: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
