"""Resident picker: a pre-built GTK layer-shell window shown on a global shortcut.

Why a daemon: a one-shot picker pays interpreter start, imports, hyprctl and a
GTK init on every keypress (~90 ms with wofi). Here the window is built once at
login; a keypress only has to map it, so trigger-to-visible is bounded by the
display's frame interval, not by code.

Trigger paths, fastest first:
  1. Hyprland global shortcuts `hyprmeta:pick` / `hyprmeta:pick-move`
     (`bind = SUPER, SPACE, global, hyprmeta:pick`) — no process spawned.
  2. The Unix socket `$XDG_RUNTIME_DIR/hyprmeta.sock` (`toggle`, `show`,
     `show-move`, `hide`, `ping`, `quit`) — what `hyprmeta pick` uses.

The current meta is derived from a monitor snapshot kept fresh by Hyprland's
event socket, so showing the picker issues no hyprctl call.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

from .cli import (  # noqa: E402
    ASSETS,
    NEW_ENTRY,
    NEW_HINT,
    App,
    Config,
    Hypr,
    HyprmetaError,
    Monitor,
    Store,
    config_path,
    fuzzy_score,
    socket_path,
    state_path,
    validate_name,
)
from .shortcuts import GlobalShortcuts  # noqa: E402

log = logging.getLogger("hyprmeta")

NAMESPACE = "hyprmeta"
WIDTH = 560
MAX_LIST_HEIGHT = 7 * 46
SEARCH_ICON = "edit-find-symbolic"

# Rows that are labels, not choices (the new-name hint).
EXTRA_CSS = b"""
#hint { background-color: transparent; border: 1px solid transparent; padding: 4px 14px 0 14px; }
#hint #text { color: rgba(255, 255, 255, 0.55); text-shadow: none; }
"""

REFRESH_EVENTS = {
    "workspace", "workspacev2", "focusedmon", "focusedmonv2", "monitoradded",
    "monitoraddedv2", "monitorremoved", "createworkspace", "createworkspacev2",
    "destroyworkspace", "destroyworkspacev2", "moveworkspace", "moveworkspacev2",
}


def hypr_event_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise HyprmetaError("XDG_RUNTIME_DIR is not set")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        hypr_dir = os.path.join(runtime, "hypr")
        try:
            candidates = sorted(
                (os.path.join(hypr_dir, d) for d in os.listdir(hypr_dir)),
                key=os.path.getmtime,
                reverse=True,
            )
        except OSError as exc:
            raise HyprmetaError(f"HYPRLAND_INSTANCE_SIGNATURE unset and {hypr_dir} unreadable: {exc}")
        if not candidates:
            raise HyprmetaError("HYPRLAND_INSTANCE_SIGNATURE unset and no instance dir under $XDG_RUNTIME_DIR/hypr")
        sig = os.path.basename(candidates[0])
        log.info("HYPRLAND_INSTANCE_SIGNATURE unset; using newest instance %s", sig)
    return os.path.join(runtime, "hypr", sig, ".socket2.sock")


class MonitorCache:
    """A `hyprctl -j monitors` snapshot refreshed on Hyprland events, off the show path."""

    def __init__(self, hypr: Hypr) -> None:
        self.hypr = hypr
        self.monitors: list[Monitor] = []
        self._pending = False
        self.refresh()
        path = hypr_event_socket()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(path)
        self._sock.setblocking(False)
        GLib.io_add_watch(self._sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, self._on_event)

    def refresh(self) -> bool:
        self._pending = False
        try:
            self.monitors = self.hypr.monitors()
        except HyprmetaError as exc:
            log.warning("monitor refresh failed: %s", exc)
        return False  # one-shot when used as a GLib timeout

    def _on_event(self, fd: int, cond: GLib.IOCondition) -> bool:
        if cond & GLib.IO_HUP:
            log.error("Hyprland event socket closed; exiting so systemd restarts us")
            Gtk.main_quit()
            return False
        try:
            data = self._sock.recv(65536)
        except BlockingIOError:
            return True
        if not data:
            log.error("Hyprland event socket EOF; exiting so systemd restarts us")
            Gtk.main_quit()
            return False
        events = {line.split(">>", 1)[0] for line in data.decode(errors="replace").splitlines()}
        if events & REFRESH_EVENTS and not self._pending:
            self._pending = True
            GLib.timeout_add(30, self.refresh)  # coalesce a burst into one hyprctl call
        return True


class Picker:
    def __init__(self) -> None:
        self.hypr = Hypr()
        self.cache = MonitorCache(self.hypr)
        self.app: App | None = None
        self.mode = "pick"
        self.move = False
        self.current: str | None = None
        self.rows: list[tuple[str, str]] = []
        self._t_press: int | None = None
        self._t_receipt: int | None = None
        self._painted = True
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        screen = Gdk.Screen.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(ASSETS / "wofi.css"))
        Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        extra = Gtk.CssProvider()
        extra.load_from_data(EXTRA_CSS)
        Gtk.StyleContext.add_provider_for_screen(screen, extra, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        win = Gtk.Window()
        self.win = win
        win.set_app_paintable(True)
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_namespace(win, NAMESPACE)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        win.set_size_request(WIDTH, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_name("outer-box")
        self.entry = Gtk.Entry()
        self.entry.set_name("input")
        self.entry.set_placeholder_text("meta workspace")
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, SEARCH_ICON)
        scroll = Gtk.ScrolledWindow()
        self.scroll = scroll
        scroll.set_name("scroll")
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(MAX_LIST_HEIGHT)
        self.list = Gtk.ListBox()
        self.list.set_name("inner-box")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_activate_on_single_click(True)
        # Nothing holds keyboard focus until you type: GTK 3 hides an entry's
        # placeholder while it is focused, and the list needs no focus ring.
        self.list.set_can_focus(False)
        self.list.connect("row-activated", lambda lb, row: self._activate())
        scroll.add(self.list)
        outer.pack_start(self.entry, False, False, 0)
        outer.pack_start(scroll, True, True, 0)
        win.add(outer)

        self.entry.connect("changed", lambda e: self._populate(e.get_text()))
        win.connect("key-press-event", self._on_key)
        win.connect("draw", self._on_draw)
        win.connect("delete-event", lambda *a: self.hide() or True)
        outer.show_all()
        win.realize()  # build the surface now, not on the first keypress

    def _row(self, css_name: str, text: str, **meta: object) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_name(css_name)
        label = Gtk.Label(label=text)
        label.set_name("text")
        label.set_xalign(0.0)
        row.add(label)
        row.meta = meta  # type: ignore[attr-defined]
        if css_name == "hint":
            row.set_selectable(False)
            row.set_activatable(False)
        return row

    def _populate(self, query: str) -> None:
        for child in self.list.get_children():
            self.list.remove(child)
        if self.mode == "new":
            self.list.add(self._row("hint", NEW_HINT))
        else:
            width = max((len(n) for n, _ in self.rows), default=0) + 3
            scored = []
            for i, (name, age) in enumerate(self.rows):
                score = fuzzy_score(query, name) if query else 0.0
                if score is not None:
                    scored.append((-score, i, name, age))
            scored.sort()
            for _, _, name, age in scored:
                self.list.add(self._row("entry", f"{name:<{width}}{age}", name=name))
            q = query.strip()
            if q and not scored:  # nothing matches: Enter creates a meta with this name
                self.list.add(self._row("entry", f"＋  create “{q}”", create=q))
            self.list.add(self._row("entry", NEW_ENTRY, new=True))
        self.list.show_all()
        first = self.list.get_row_at_index(0)
        if first is not None and first.get_selectable():
            self.list.select_row(first)
        # Layer-shell windows are sized from their MINIMUM height, so make the
        # list's natural height (capped) the minimum, then let the window shrink
        # or grow to it.
        natural = self.list.get_preferred_height()[1]
        self.scroll.set_min_content_height(min(natural, MAX_LIST_HEIGHT))
        self.win.resize(WIDTH, 1)

    # ------------------------------------------------------------- show/hide
    def show(self, move: bool = False, t_press: int | None = None) -> None:
        self._t_press = t_press
        self._t_receipt = time.monotonic_ns()
        self._painted = False
        try:
            self.app = App(self.hypr, Config.load(config_path()), Store.load(state_path()), state_path())
        except HyprmetaError as exc:
            log.error("cannot open picker: %s", exc)
            return
        mons = self.cache.monitors
        offset = self.app.current_offset(mons) if len(mons) == len(self.app.cfg.base) else None
        self.current = self.app.store.name_for_offset(offset) if offset is not None else None
        now = time.time()
        self.rows = [(n, self.app.age_label(n, now)) for n in self.app.store.ordered() if n != self.current]
        self.mode = "pick"
        self.move = move
        self.entry.set_placeholder_text("move window to meta workspace" if move else "meta workspace")
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, SEARCH_ICON)
        self.entry.set_text("")
        self._populate("")
        self.win.show()
        self.win.set_focus(None)  # placeholder stays visible until the first keystroke

    def hide(self) -> None:
        self.win.hide()
        self.mode = "pick"

    def toggle(self, move: bool = False, t_press: int | None = None) -> None:
        if self.win.get_visible():
            self.hide()
        else:
            self.show(move, t_press)

    def _enter_new_mode(self) -> None:
        self.mode = "new"
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, None)
        self.entry.set_placeholder_text("＋ name for the new meta workspace")
        self.entry.set_text("")
        self._populate("")
        self.win.set_focus(None)

    # ---------------------------------------------------------------- input
    def _on_key(self, widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        key = event.keyval
        state = event.state
        if key == Gdk.KEY_Escape:
            self.hide()
            return True
        if key in (Gdk.KEY_Down, Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_Up):
            self._move_selection(-1 if key in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab) else 1)
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._activate(
                move=bool(state & Gdk.ModifierType.MOD1_MASK),
                follow=bool(state & Gdk.ModifierType.SHIFT_MASK),
            )
            return True
        # Anything else is typing: hand focus to the entry and let GTK deliver
        # this same event to it.
        if not self.entry.has_focus():
            self.entry.grab_focus_without_selecting()
        return False

    def _move_selection(self, delta: int) -> None:
        rows = [r for r in self.list.get_children() if r.get_selectable()]
        if not rows:
            return
        current = self.list.get_selected_row()
        i = rows.index(current) if current in rows else -1
        self.list.select_row(rows[(i + delta) % len(rows)])

    def _activate(self, move: bool = False, follow: bool = False) -> None:
        assert self.app is not None
        app = self.app
        if self.mode == "new":
            name = self.entry.get_text().strip()
            if not name:
                return
            self._run(lambda: (app.create(validate_name(name), None), app.switch(name)))
            return
        row = self.list.get_selected_row()
        if row is None:
            return
        meta = row.meta  # type: ignore[attr-defined]
        if meta.get("new"):
            self._enter_new_mode()
            return
        move = move or self.move
        if "create" in meta:
            name = str(meta["create"])
            self._run(lambda: (app.create(validate_name(name), None), self._go(name, move, follow)))
        else:
            name = str(meta["name"])
            self._run(lambda: self._go(name, move, follow))

    def _go(self, name: str, move: bool, follow: bool) -> None:
        assert self.app is not None
        if move:
            self.app.move_window(name, follow=follow)
        else:
            self.app.switch(name)

    def _run(self, fn) -> None:  # noqa: ANN001
        self.hide()  # respond visually first; the hyprctl work follows
        try:
            fn()
        except HyprmetaError as exc:
            log.error("%s", exc)

    # --------------------------------------------------------------- timing
    def _on_draw(self, widget: Gtk.Widget, cr) -> bool:  # noqa: ANN001
        if not self._painted and self._t_receipt is not None:
            self._painted = True
            now = time.monotonic_ns()
            parts = [f"receipt→draw {(now - self._t_receipt) / 1e6:.1f} ms"]
            if self._t_press is not None and 0 < now - self._t_press < 5_000_000_000:
                parts.insert(0, f"keypress→draw {(now - self._t_press) / 1e6:.1f} ms")
            log.info("picker shown: %s", ", ".join(parts))
        return False


class CommandServer:
    def __init__(self, picker: Picker) -> None:
        self.picker = picker
        path = socket_path()
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.sock.setblocking(False)
        GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._accept)

    def _accept(self, fd: int, cond: GLib.IOCondition) -> bool:
        try:
            conn, _ = self.sock.accept()
        except BlockingIOError:
            return True
        with conn:
            try:
                cmd = conn.recv(256).decode(errors="replace").strip()
                reply = self.handle(cmd)
                conn.sendall(reply.encode() + b"\n")
            except OSError as exc:
                log.warning("socket client error: %s", exc)
        return True

    def handle(self, cmd: str) -> str:
        p = self.picker
        if cmd == "toggle":
            p.toggle()
        elif cmd == "show":
            p.show()
        elif cmd == "show-move":
            p.show(move=True)
        elif cmd == "hide":
            p.hide()
        elif cmd == "ping":
            return "pong"
        elif cmd == "quit":
            GLib.idle_add(Gtk.main_quit)
        else:
            return f"unknown command {cmd!r}"
        return "ok"


def already_running() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(socket_path())
        s.sendall(b"ping")
        alive = s.recv(16).strip() == b"pong"
        s.close()
        return alive
    except (OSError, socket.timeout):
        return False


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if already_running():
        print("hyprmeta: a daemon is already running (socket answered)", file=sys.stderr)
        return 1
    Gtk.init([])
    try:
        picker = Picker()
    except HyprmetaError as exc:
        print(f"hyprmeta: {exc}", file=sys.stderr)
        return 1
    CommandServer(picker)
    shortcuts = GlobalShortcuts(
        {
            "pick": ("Open the hyprmeta meta-workspace picker", "Super+Space"),
            "pick-move": ("Move the focused window to a meta workspace", "Super+Shift+Space"),
        },
        on_pressed=lambda sid, ts: GLib.idle_add(picker.toggle, sid == "pick-move", ts),
    )
    try:
        shortcuts.start()
    except RuntimeError as exc:
        print(f"hyprmeta: {exc}", file=sys.stderr)
        return 1
    log.info("ready: global shortcuts hyprmeta:pick / hyprmeta:pick-move, socket %s", socket_path())
    Gtk.main()
    return 0
