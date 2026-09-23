"""Resident picker: a pre-built GTK layer-shell window shown on a global shortcut.

Why a daemon: a one-shot picker pays interpreter start, imports, hyprctl and a
GTK init on every keypress (~90 ms with wofi). Here the window is built once at
login; a keypress only has to map it, so trigger-to-visible is bounded by the
display's frame interval, not by code.

Trigger paths, fastest first:
  1. Hyprland global shortcuts `hyprmeta:pick` / `hyprmeta:pick-move`
     (`bind = SUPER, SPACE, global, hyprmeta:pick`) — no process spawned.
  2. The Unix socket `$XDG_RUNTIME_DIR/hyprmeta.sock` (`toggle`, `show`,
     `show-move`, `peek` (show without a keyboard grab), `hide`, `ping`, `quit`)
     — what `hyprmeta pick` uses.

The current meta is derived from a monitor snapshot kept fresh by Hyprland's
event socket, so showing the picker issues no hyprctl call.
"""

from __future__ import annotations

import json
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
from .agents import (  # noqa: E402
    AgentTracker,
    actual_tags,
    agents_file,
    apply_tags,
    find_agent_processes,
    marks_markup,
    write_snapshot,
)
from .shortcuts import GlobalShortcuts  # noqa: E402

log = logging.getLogger("hyprmeta")

SCAN_INTERVAL_MS = 2000  # full scan: new/exited agents, Codex rollouts, stop confirmation
RESCAN_DEBOUNCE_MS = 150  # after a window opens, closes or moves

NAMESPACE = "hyprmeta"
WIDTH = 560
MAX_LIST_HEIGHT = 7 * 46
SEARCH_ICON = "edit-find-symbolic"
MARKS_GAP = 9  # px between the right-aligned marker column and the name (≈ one space)
PLUS = "＋"  # sits in the marker column of the create / new rows

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
        self._buf = b""  # a recv() can end mid-line (or mid UTF-8 sequence)
        self.on_refresh = None  # callable(monitors) after each refresh
        self.on_events = None  # callable(list[(event, data)]) for every batch of events
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
        if self.on_refresh is not None:
            self.on_refresh(self.monitors)
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
        *lines, self._buf = (self._buf + data).split(b"\n")
        parsed = []
        for raw in lines:
            name, _, payload = raw.decode(errors="replace").partition(">>")
            parsed.append((name, payload))
        events = {name for name, _ in parsed}
        if events & REFRESH_EVENTS and not self._pending:
            self._pending = True
            GLib.timeout_add(30, self.refresh)  # coalesce a burst into one hyprctl call
        if self.on_events is not None:
            self.on_events(parsed)
        return True


class Picker:
    def __init__(self) -> None:
        self.hypr = Hypr()
        self.cache = MonitorCache(self.hypr)
        self.app: App | None = None
        self.mode = "pick"
        self.move = False
        self.current: str | None = None
        self.rows: list[tuple[str, str, dict]] = []  # (name, age, agent summary)
        self._t_press: int | None = None
        self._t_receipt: int | None = None
        self._painted = True
        self.tracker: AgentTracker | None = None
        self._tags: dict[str, set[str]] = {}
        self._scan_pending = False
        self._build()
        self._setup_tracker()

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

    def _row(self, css_name: str, text: str, markup: bool = False, marks: str | None = None,
             **meta: object) -> Gtk.ListBoxRow:
        """A list row. `marks` (Pango) goes in a right-aligned column LEFT of the text,
        sized by one SizeGroup per populate: each row's markers sit right against its
        own name and the names still line up, whatever the glyph widths."""
        row = Gtk.ListBoxRow()
        row.set_name(css_name)
        label = Gtk.Label()
        if markup:
            label.set_markup(text)
        else:
            label.set_text(text)
        label.set_name("text")
        label.set_xalign(0.0)
        if marks is None:
            row.add(label)
        else:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=MARKS_GAP)
            marks_label = Gtk.Label()
            marks_label.set_markup(marks)
            marks_label.set_name("text")
            marks_label.set_xalign(1.0)
            self._marks_group.add_widget(marks_label)
            box.pack_start(marks_label, False, False, 0)
            box.pack_start(label, True, True, 0)
            row.add(box)
        row.meta = meta  # type: ignore[attr-defined]
        if css_name == "hint":
            row.set_selectable(False)
            row.set_activatable(False)
        return row

    def _populate(self, query: str) -> None:
        for child in self.list.get_children():
            self.list.remove(child)
        self._marks_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        if self.mode == "new":
            self.list.add(self._row("hint", NEW_HINT))
        else:
            width = max((len(n) for n, _, _ in self.rows), default=0) + 3
            scored = []
            for i, (name, age, summary) in enumerate(self.rows):
                score = fuzzy_score(query, name) if query else 0.0
                if score is not None:
                    scored.append((-score, i, name, age, summary))
            scored.sort()
            for _, _, name, age, summary in scored:
                self.list.add(self._row("entry", f"{name:<{width}}{age}", marks=marks_markup(summary), name=name))
            q = query.strip()
            if q and not scored:  # nothing matches: Enter creates a meta with this name
                self.list.add(self._row("entry", f"create “{q}”", marks=PLUS, create=q))
            self.list.add(self._row("entry", "new meta workspace", marks=PLUS, new=True))
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
        summary = self.tracker.meta_summary() if self.tracker is not None else {}
        self.rows = [
            (n, self.app.age_label(n, now), summary.get(n, {}))
            for n in self.app.store.ordered()
            if n != self.current
        ]
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
        # a peek() dropped the keyboard grab; every real show must have it back
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.EXCLUSIVE)

    def peek(self) -> None:
        """Show WITHOUT taking the keyboard — for screenshots and tests.

        A normal show grabs the keyboard exclusively, so an automated screenshot
        swallows whatever the user is typing at that moment (it happened).
        """
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        self.show()

    def toggle(self, move: bool = False, t_press: int | None = None) -> None:
        if self.win.get_visible():
            self.hide()
        else:
            self.show(move, t_press)

    # ------------------------------------------------------------- agents
    def _setup_tracker(self) -> None:
        try:
            cfg = Config.load(config_path())
        except HyprmetaError as exc:
            log.warning("agent tracking off: %s", exc)
            return
        store = Store.load(state_path())
        self.tracker = AgentTracker(cfg.base, store.metas)
        try:
            previous = json.loads(agents_file().read_text())
        except FileNotFoundError:
            previous = None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable agent snapshot %s: %s", agents_file(), exc)
            previous = None
        self._full_scan(restore=previous)
        self.cache.on_events = self._on_hypr_events
        self.cache.on_refresh = self._on_monitors
        self._on_monitors(self.cache.monitors)
        GLib.timeout_add(SCAN_INTERVAL_MS, self._periodic_scan)
        log.info("agent tracking on: %d windows, %s", len(self.tracker.windows), agents_file())

    def _on_monitors(self, monitors) -> None:  # noqa: ANN001
        """Keep meta names and the current meta (display only) in sync."""
        if self.tracker is None:
            return
        store = Store.load(state_path())
        self.tracker.set_metas(store.metas)
        try:
            cfg = Config.load(config_path())
            app = App(self.hypr, cfg, store, state_path())
            offset = app.current_offset(monitors) if len(monitors) == len(cfg.base) else None
        except HyprmetaError:
            offset = None
        name = store.name_for_offset(offset) if offset is not None else None
        if name != self.tracker.current_meta:
            self.tracker.set_current_meta(name)
            self._publish(write=True)

    def _on_hypr_events(self, events) -> None:  # noqa: ANN001
        """Fast paths: focus and title changes need neither hyprctl nor /proc."""
        if self.tracker is None:
            return
        now = time.time()
        before = self.tracker.visible_state()
        rescan = False
        for name, payload in events:
            if name == "activewindowv2":
                addr = payload.strip()
                self.tracker.focus(f"0x{addr}" if addr else None, now)
            elif name == "windowtitlev2":
                addr, _, title = payload.partition(",")
                self.tracker.set_title(f"0x{addr.strip()}", title, now)
            elif name in ("openwindow", "closewindow", "movewindowv2"):
                rescan = True
        if self.tracker.visible_state() != before:
            self._publish(write=True)
        if rescan:
            self._request_scan()

    def _request_scan(self) -> None:
        if not self._scan_pending:
            self._scan_pending = True
            GLib.timeout_add(RESCAN_DEBOUNCE_MS, self._scan_once)

    def _scan_once(self) -> bool:
        """Debounced one-shot scan. Returns False so GLib drops the timer —
        a callback returning True REPEATS, which once saturated the main loop."""
        self._scan_pending = False
        self._full_scan()
        return False

    def _periodic_scan(self) -> bool:
        self._full_scan()
        return True  # keep repeating every SCAN_INTERVAL_MS

    def _full_scan(self, restore: dict | None = None) -> None:
        """Rebuild windows/agents from hyprctl clients + /proc; reconcile tags with the compositor."""
        if self.tracker is None:
            return
        try:
            clients = self.hypr.clients()
        except (HyprmetaError, ValueError) as exc:
            log.warning("clients scan failed: %s", exc)
            return
        before = self.tracker.visible_state()
        self.tracker.update(clients, find_agent_processes(), time.time())
        if restore is not None:
            self.tracker.restore(restore)
        # Diff against the tags Hyprland really has, so a restart or a stray
        # manual tag can never leave a stale border behind.
        self._tags = actual_tags(clients)
        self._publish(write=restore is not None or self.tracker.visible_state() != before)

    def _publish(self, write: bool) -> None:
        assert self.tracker is not None
        for line in self.tracker.events:
            log.info("finished: %s", line)
        self.tracker.events.clear()
        if write:
            try:
                write_snapshot(agents_file(), self.tracker.snapshot(time.time()))
            except OSError as exc:
                log.warning("cannot write %s: %s", agents_file(), exc)
        try:
            self._tags = apply_tags(self.hypr, self.tracker.tags_wanted(), self._tags)
        except HyprmetaError as exc:
            log.warning("tagwindow failed: %s", exc)

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
        elif cmd == "peek":
            p.peek()
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
