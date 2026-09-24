import json
from pathlib import Path

import pytest

from hyprmeta import cli
from hyprmeta.cli import App, Config, Hypr, HyprmetaError, Store


class FakeHyprctl:
    """Answers `hyprctl -j monitors/workspaces/activewindow`, records batches."""

    def __init__(self, active=(4, 5, 6), focused="DP-2", ws_monitors=None, cursor=(100, 200), simulate=False):
        self.names = ["DP-1", "DP-2", "HDMI-A-1"]
        self.active = dict(zip(self.names, active))
        self.focused = focused
        self.ws_monitors = ws_monitors or dict(zip(active, self.names))
        self.cursor = cursor
        self.batches: list[list[str]] = []
        self.active_window_monitor_id = 1
        self.clients = [{"address": "0xw", "monitor": 2, "workspace": {"id": 6}}]
        self.simulate = simulate  # apply focusmonitor / focusworkspaceoncurrentmonitor to `active`

    def __call__(self, args):
        args = list(args)
        if args[:2] == ["-j", "monitors"]:
            # Deliberately out of x order to prove sorting happens.
            order = [1, 0, 2]
            return json.dumps(
                [
                    {
                        "id": i,
                        "name": self.names[i],
                        "x": 1728 * i,
                        "activeWorkspace": {"id": self.active[self.names[i]], "name": ""},
                        "focused": self.names[i] == self.focused,
                    }
                    for i in order
                ]
            )
        if args[:2] == ["-j", "workspaces"]:
            return json.dumps([{"id": w, "monitor": m} for w, m in self.ws_monitors.items()])
        if args[:2] == ["-j", "activewindow"]:
            return json.dumps({"monitor": self.active_window_monitor_id})
        if args == ["cursorpos"]:
            return f"{self.cursor[0]}, {self.cursor[1]}"
        if args[:2] == ["-j", "clients"]:
            return json.dumps(self.clients)
        if args[0] == "--batch":
            batch = [d.removeprefix("dispatch ") for d in args[1].split("; ")]
            self.batches.append(batch)
            if self.simulate:
                cur = self.focused
                for d in batch:
                    verb, _, arg = d.partition(" ")
                    if verb == "focusmonitor":
                        cur = arg
                    elif verb == "focusworkspaceoncurrentmonitor":
                        self.active[cur] = int(arg)
                self.focused = cur
            return "ok"
        raise AssertionError(f"unexpected hyprctl call {args}")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    state = tmp_path / "state.json"
    monkeypatch.setenv("HYPRMETA_CONFIG", str(cfg))
    monkeypatch.setenv("HYPRMETA_STATE", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))  # never talk to a real daemon
    return cfg, state


def make_app(fake, store_file, metas=None, recent=None):
    cfg = Config(base=[4, 5, 6], step=10)
    store = Store(metas=dict(metas or {"home": 0}), recent=list(recent or ["home"]))
    return App(Hypr(fake), cfg, store, store_file)


# --------------------------------------------------------------------------- #


def test_monitors_sorted_left_to_right():
    fake = FakeHyprctl()
    mons = Hypr(fake).monitors()
    assert [m.name for m in mons] == ["DP-1", "DP-2", "HDMI-A-1"]
    assert [m.active_ws for m in mons] == [4, 5, 6]


def test_switch_focuses_each_monitor_then_restores_focus_and_cursor(tmp_path):
    fake = FakeHyprctl(focused="DP-2", cursor=(3237, 4720))
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.switch("taxes") == [14, 15, 16]
    assert fake.batches == [
        [
            "focusmonitor DP-1",
            "focusworkspaceoncurrentmonitor 14",
            "focusmonitor DP-2",
            "focusworkspaceoncurrentmonitor 15",
            "focusmonitor HDMI-A-1",
            "focusworkspaceoncurrentmonitor 16",
            "focusmonitor DP-2",
            "movecursor 3237 4720",
        ]
    ]
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved["recent"] == ["taxes", "home"]


def test_switch_skips_monitors_already_showing_the_target(tmp_path):
    fake = FakeHyprctl(active=(14, 5, 16), focused="DP-1")
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    app.switch("taxes")
    assert fake.batches == [
        ["focusmonitor DP-2", "focusworkspaceoncurrentmonitor 15", "focusmonitor DP-1", "movecursor 100 200"]
    ]


def test_switch_is_a_noop_when_already_there(tmp_path):
    fake = FakeHyprctl(active=(14, 15, 16))
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    app.switch("taxes")
    assert fake.batches == []


def test_switch_dash_goes_to_previous(tmp_path):
    fake = FakeHyprctl(active=(14, 15, 16))
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10}, recent=["taxes", "home"])
    assert app.switch("-") == [4, 5, 6]
    assert app.store.recent == ["home", "taxes"]


def test_switch_unknown_name_is_an_error(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json")
    with pytest.raises(HyprmetaError, match="unknown meta workspace 'nope'"):
        app.switch("nope")


def test_monitor_count_mismatch_is_an_error(tmp_path):
    fake = FakeHyprctl()
    app = make_app(fake, tmp_path / "s.json")
    app.cfg = Config(base=[4, 5])
    with pytest.raises(HyprmetaError, match="2 base workspaces but Hyprland reports 3"):
        app.switch("home")


def test_current_name_detected_from_live_workspaces(tmp_path):
    app = make_app(FakeHyprctl(active=(14, 15, 16)), tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.current_name() == "taxes"
    app = make_app(FakeHyprctl(active=(14, 5, 16)), tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.current_offset() is None
    assert app.current_name() is None
    app = make_app(FakeHyprctl(active=(24, 25, 26)), tmp_path / "s.json", {"home": 0})
    assert app.current_offset() == 20
    assert app.current_name() is None


def test_create_allocates_next_free_multiple_of_step(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json", {"home": 0, "taxes": 10, "odd": 30})
    assert app.create("music", None) == 20
    assert app.create("more", None) == 40
    with pytest.raises(HyprmetaError, match="already used by 'taxes'"):
        app.create("dup", 10)
    with pytest.raises(HyprmetaError, match="already exists"):
        app.create("home", None)


def test_move_window_uses_the_slot_of_the_focused_monitor(tmp_path):
    fake = FakeHyprctl()
    fake.active_window_monitor_id = 2  # HDMI-A-1, rightmost slot (base 6)
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.move_window("taxes") == 16
    assert fake.batches == [["movetoworkspacesilent 16"]]


def test_move_window_follow_switches_afterwards(tmp_path):
    fake = FakeHyprctl()
    fake.active_window_monitor_id = 0
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    app.move_window("taxes", follow=True)
    assert fake.batches[0] == ["movetoworkspacesilent 14"]
    assert "focusworkspaceoncurrentmonitor 15" in fake.batches[1]


def test_goto_and_moveto_are_relative_to_current_offset(tmp_path):
    fake = FakeHyprctl(active=(14, 15, 16))
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.goto_slot(5) == 15
    assert app.move_to_slot(6) == 16
    assert app.move_to_slot(4, silent=True) == 14
    assert fake.batches == [["workspace 15"], ["movetoworkspace 16"], ["movetoworkspacesilent 14"]]


def test_goto_refuses_when_monitors_disagree(tmp_path):
    app = make_app(FakeHyprctl(active=(14, 5, 16)), tmp_path / "s.json")
    with pytest.raises(HyprmetaError, match="disagree"):
        app.goto_slot(1)


def test_menu_lines_most_recently_opened_first_with_age(tmp_path):
    app = make_app(
        FakeHyprctl(active=(14, 15, 16)),
        tmp_path / "s.json",
        {"home": 0, "taxes": 10, "zeta": 20, "alpha": 30, "mid": 40},
        recent=["taxes", "home", "mid"],
    )
    now = 1_000_000.0
    app.store.last_used = {"taxes": now - 10, "home": now - 3 * 3600, "zeta": now - 2 * 86400}
    # `taxes` is current and therefore NOT listed: Enter on the top line = go back.
    assert app.menu_lines(now=now) == [
        "home    3 h ago",
        "zeta    2 d ago",
        "mid     never opened",  # legacy `recent` order, before the unknown ones
        "alpha   never opened",
        cli.NEW_ENTRY,
    ]


def test_menu_lists_everything_when_off_grid(tmp_path):
    app = make_app(FakeHyprctl(active=(14, 5, 16)), tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert [l.split("  ")[0] for l in app.menu_lines()] == ["home", "taxes", cli.NEW_ENTRY.split("  ")[0]]
    assert cli.NEW_ENTRY == "＋  new meta workspace"


def test_touch_records_time_and_reorders(tmp_path):
    store = Store(metas={"a": 0, "b": 10, "c": 20}, recent=[])
    store.touch("b", now=100.0)
    store.touch("a", now=200.0)
    assert store.ordered() == ["a", "b", "c"]
    store.touch("c", now=300.0)
    assert store.ordered() == ["c", "a", "b"]
    assert store.recent == ["c", "a", "b"]
    store.rename("c", "d")
    assert store.last_used == {"b": 100.0, "a": 200.0, "d": 300.0}
    store.remove("d")
    assert store.ordered() == ["a", "b"]
    assert "d" not in store.last_used


def test_humanize_ago():
    assert cli.humanize_ago(3) == "just now"
    assert cli.humanize_ago(-5) == "just now"
    assert cli.humanize_ago(120) == "2 min ago"
    assert cli.humanize_ago(89 * 60) == "89 min ago"
    assert cli.humanize_ago(2 * 3600) == "2 h ago"
    assert cli.humanize_ago(35 * 3600) == "35 h ago"
    assert cli.humanize_ago(3 * 86400) == "3 d ago"
    assert cli.humanize_ago(20 * 86400) == "3 wk ago"
    assert cli.humanize_ago(100 * 86400) == "3 mo ago"
    assert cli.humanize_ago(800 * 86400) == "2 yr ago"


def test_name_from_line_roundtrips_and_passes_typed_queries_through():
    assert App.name_from_line("taxes   3 h ago") == "taxes"
    assert App.name_from_line("two words   5 min ago") == "two words"
    assert App.name_from_line("●  old-format   current") == "old-format"
    assert App.name_from_line("bills") == "bills"
    assert App.name_from_line("  bills \n") == "bills"


def test_resolve_pick_existing_typed_and_new_entry(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json", {"home": 0, "taxes": 10})
    prompts = []

    def prompt():
        prompts.append(True)
        return "  music "

    assert app.resolve_pick("taxes   3 h ago\n", prompt) == "taxes"
    # A typed query that matched nothing becomes a new meta.
    assert app.resolve_pick("bills", prompt) == "bills"
    assert app.store.metas["bills"] == 20
    # The explicit "+ new" entry asks for a name.
    assert app.resolve_pick(cli.NEW_ENTRY, prompt) == "music"
    assert app.store.metas["music"] == 30
    assert prompts == [True]
    with pytest.raises(HyprmetaError, match="nothing selected"):
        app.resolve_pick("", prompt)


def test_store_roundtrip_drops_recent_names_that_no_longer_exist(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"metas": {"home": 0}, "recent": ["gone", "home"]}))
    store = Store.load(p)
    assert store.recent == ["home"]
    store.touch("home", now=42.0)
    store.rename("home", "base")
    store.save(p)
    assert Store.load(p).metas == {"base": 0}
    assert Store.load(p).recent == ["base"]
    assert Store.load(p).last_used == {"base": 42.0}
    with pytest.raises(HyprmetaError, match="reserved"):
        store.create(cli.NEW_ENTRY, 10)
    with pytest.raises(HyprmetaError, match="reserved"):
        store.create("● fake", 10)
    with pytest.raises(HyprmetaError, match="double spaces"):
        store.create("two  spaces", 10)


# --------------------------------------------------------------------------- #
# CLI end to end through main()
# --------------------------------------------------------------------------- #


def test_main_init_then_switch_then_list(paths, capsys):
    cfg, state = paths
    fake = FakeHyprctl()
    hypr = Hypr(fake)

    assert cli.main(["init"], hypr=hypr) == 0
    assert json.loads(cfg.read_text())["base"] == [4, 5, 6]
    assert json.loads(state.read_text())["metas"] == {"home": 0}
    assert cli.main(["init"], hypr=hypr) == 1
    assert "pass --force" in capsys.readouterr().err

    assert cli.main(["switch", "taxes"], hypr=hypr) == 1
    assert "unknown meta workspace" in capsys.readouterr().err

    assert cli.main(["switch", "--create", "taxes"], hypr=hypr) == 0
    assert capsys.readouterr().out.strip() == "taxes: 14 15 16"
    assert fake.batches[-1][1] == "focusworkspaceoncurrentmonitor 14"

    fake.active = dict(zip(fake.names, (14, 15, 16)))
    assert cli.main(["list"], hypr=hypr) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("* taxes")
    assert out[1].startswith("  home")
    assert cli.main(["current"], hypr=hypr) == 0
    assert capsys.readouterr().out.strip() == "taxes"

    assert cli.main(["switch", "-"], hypr=hypr) == 0
    assert capsys.readouterr().out.strip() == "home: 4 5 6"


def test_main_without_config_points_at_init(paths, capsys):
    assert cli.main(["list"], hypr=Hypr(FakeHyprctl())) == 1
    assert "run `hyprmeta init`" in capsys.readouterr().err


def test_main_pick_with_fake_menu(paths, monkeypatch, capsys):
    cfg, state = paths
    Config(base=[4, 5, 6]).save(cfg)
    Store(metas={"home": 0, "taxes": 10}, recent=["home"]).save(state)
    fake = FakeHyprctl()
    seen = {}

    def fake_menu(command, lines):
        seen["command"] = command
        seen["lines"] = lines
        return "○  taxes   14 · 15 · 16"

    monkeypatch.setattr(cli, "run_menu", fake_menu)
    assert cli.main(["pick", "--menu", "fzf"], hypr=Hypr(fake)) == 0
    assert seen["command"] == "fzf"
    assert seen["lines"] == ["taxes   never opened", cli.NEW_ENTRY]
    assert capsys.readouterr().out.strip() == "taxes: 14 15 16"
    assert fake.batches[0][1] == "focusworkspaceoncurrentmonitor 14"


def test_run_menu_reports_cancel(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(HyprmetaError, match="cancelled"):
        cli.run_menu("wofi --dmenu", ["a"])


def test_expand_menu_points_at_shipped_assets():
    cmd = cli.expand_menu(cli.DEFAULT_MENU)
    assert "{assets}" not in cmd
    assert (cli.ASSETS / "wofi.conf").is_file()
    assert (cli.ASSETS / "wofi.css").is_file()
    assert (cli.ASSETS / "wofi-new.css").is_file()
    assert "wofi-new.css" in cli.expand_menu(cli.DEFAULT_MENU_NEW)


def test_picker_lock_toggles_a_running_picker(tmp_path):
    pidfile = tmp_path / "pick.pid"
    killed = []
    lock = cli.PickerLock(pidfile, alive=lambda pid: pid == 4242, kill=killed.append)

    assert lock.close_running() is False  # no pid file yet
    pidfile.write_text("999\n")  # stale: not alive
    assert lock.close_running() is False
    pidfile.write_text("4242\n")  # live picker
    assert lock.close_running() is True
    assert killed == [4242]
    assert not pidfile.exists()


def test_picker_lock_writes_and_removes_own_pid(tmp_path):
    pidfile = tmp_path / "pick.pid"
    with cli.PickerLock(pidfile, alive=lambda pid: True, kill=lambda pid: None):
        assert pidfile.read_text().strip() == str(cli.os.getpid())
    assert not pidfile.exists()


def test_main_pick_second_call_closes_first(paths, monkeypatch, capsys):
    cfg, state = paths
    Config(base=[4, 5, 6]).save(cfg)
    Store(metas={"home": 0}, recent=["home"]).save(state)
    pidfile = cfg.parent / "pick.pid"
    pidfile.write_text("777\n")
    killed = []
    RealLock = cli.PickerLock
    monkeypatch.setattr(
        cli,
        "PickerLock",
        lambda: RealLock(pidfile, alive=lambda pid: pid == 777, kill=killed.append),
    )
    menu_calls = []
    monkeypatch.setattr(cli, "run_menu", lambda *a: menu_calls.append(a) or "")
    assert cli.main(["pick"], hypr=Hypr(FakeHyprctl())) == 0
    assert killed == [777]
    assert menu_calls == []  # the menu was never opened
    assert "closed the open picker" in capsys.readouterr().out


def test_new_dialog_stylesheet_is_self_contained():
    base = (cli.ASSETS / "wofi.css").read_text()
    new = (cli.ASSETS / "wofi-new.css").read_text()
    assert "@import url" not in new
    # Every rule block of the base sheet must be present verbatim.
    base_rules = base[base.index("* {"):]
    assert base_rules.strip() in new


def test_run_menu_sends_no_trailing_newline_for_empty_lines(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0
        stdout = "typed\n"
        stderr = ""

    def fake_run(argv, input, **kw):
        seen["input"] = input
        return Proc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.run_menu("wofi --dmenu", []) == "typed"
    assert seen["input"] == ""
    cli.run_menu("wofi --dmenu", ["a", "b"])
    assert seen["input"] == "a\nb\n"


def test_new_dialog_hint_or_empty_answer_is_an_error(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json", {"home": 0})
    with pytest.raises(HyprmetaError, match="no name typed"):
        app.resolve_pick(cli.NEW_ENTRY, lambda: cli.NEW_HINT + "\n")
    with pytest.raises(HyprmetaError, match="no name typed"):
        app.resolve_pick(cli.NEW_ENTRY, lambda: "   ")
    assert app.store.metas == {"home": 0}


def test_main_pick_new_entry_uses_menu_new_with_the_hint(paths, monkeypatch, capsys):
    cfg, state = paths
    Config(base=[4, 5, 6], menu="MAIN", menu_new="NEW").save(cfg)
    Store(metas={"home": 0}, recent=["home"]).save(state)
    calls = []

    def fake_menu(command, lines):
        calls.append((command, list(lines)))
        return cli.NEW_ENTRY if command == "MAIN" else "bills"

    monkeypatch.setattr(cli, "run_menu", fake_menu)
    RealLock = cli.PickerLock
    monkeypatch.setattr(cli, "PickerLock", lambda: RealLock(cfg.parent / "pick.pid"))
    fake = FakeHyprctl()
    assert cli.main(["pick"], hypr=Hypr(fake)) == 0
    assert calls == [("MAIN", [cli.NEW_ENTRY]), ("NEW", [cli.NEW_HINT])]
    assert capsys.readouterr().out.strip() == "bills: 14 15 16"
    assert json.loads(state.read_text())["metas"] == {"home": 0, "bills": 10}


def test_fuzzy_score_orders_sensibly():
    f = cli.fuzzy_score
    assert f("", "anything") == 0.0
    assert f("x", "taxes") is not None and f("q", "taxes") is None
    # prefix beats mid-word, contiguous beats scattered
    assert f("tax", "taxes") > f("tax", "syntax")
    assert f("sec", "security") > f("sec", "s-e-c")
    assert f("leg", "legal") > f("leg", "l.e.g.a.l")
    # case-insensitive
    assert f("TAX", "taxes") == f("tax", "taxes")
    # shorter target wins on equal structure
    assert f("home", "home") > f("home", "homework")


def test_daemon_send_returns_none_without_a_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cli.socket_path() == str(tmp_path / "hyprmeta.sock")
    assert cli.daemon_send("ping") is None


def test_main_pick_uses_the_daemon_when_it_answers(paths, monkeypatch, capsys):
    cfg, state = paths
    Config(base=[4, 5, 6]).save(cfg)
    Store(metas={"home": 0}, recent=["home"]).save(state)
    sent = []
    monkeypatch.setattr(cli, "daemon_send", lambda cmd: sent.append(cmd) or "ok")
    monkeypatch.setattr(cli, "run_menu", lambda *a: (_ for _ in ()).throw(AssertionError("menu must not run")))
    assert cli.main(["pick"], hypr=Hypr(FakeHyprctl())) == 0
    assert cli.main(["pick", "--move"], hypr=Hypr(FakeHyprctl())) == 0
    assert sent == ["toggle", "show-move"]
    assert "daemon: ok" in capsys.readouterr().out



# --------------------------------------------------------------------------- #
# preview support: switch without recording, restore, move a pinned window
# --------------------------------------------------------------------------- #

from hyprmeta.preview import PreviewSession  # noqa: E402


def test_switch_without_record_leaves_mru_and_state_alone(tmp_path):
    fake = FakeHyprctl()
    store_file = tmp_path / "s.json"
    app = make_app(fake, store_file, {"home": 0, "taxes": 10})
    assert app.switch("taxes", record=False) == [14, 15, 16]
    assert fake.batches and "focusworkspaceoncurrentmonitor 14" in fake.batches[0]
    assert app.store.recent == ["home"]
    assert not store_file.exists()


def test_show_workspaces_checks_the_monitor_count(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json")
    with pytest.raises(HyprmetaError, match="2 target workspaces for 3 monitors"):
        app.show_workspaces([1, 2])


def test_move_window_by_address_uses_that_windows_monitor(tmp_path):
    fake = FakeHyprctl()
    fake.active_window_monitor_id = 0  # the FOCUSED window is elsewhere; must not matter
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10})
    assert app.move_window("taxes", address="0xw") == 16  # 0xw is on monitor 2 = slot base 6
    assert fake.batches == [["movetoworkspacesilent 16,address:0xw"]]
    with pytest.raises(HyprmetaError, match="no window with address 0xnope"):
        app.move_window("taxes", address="0xnope")


def preview_world(tmp_path):
    fake = FakeHyprctl(simulate=True, focused="DP-2")
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10, "legal": 30}, recent=["home"])
    session = PreviewSession(app, app.monitors(), origin_window="0xorigin")
    return fake, app, session


def test_preview_shows_without_recording_and_skips_repeats(tmp_path):
    fake, app, session = preview_world(tmp_path)
    assert session.preview("taxes") is True
    assert list(fake.active.values()) == [14, 15, 16]
    assert fake.focused == "DP-2"  # focus goes back to the monitor that had it
    assert session.preview("taxes") is False  # already shown: no second batch
    assert session.preview(None) is False  # create / new rows
    assert len(fake.batches) == 1
    assert app.store.recent == ["home"]


def test_commit_records_without_switching_again(tmp_path):
    fake, app, session = preview_world(tmp_path)
    session.preview("legal")
    session.commit("legal")
    assert len(fake.batches) == 1  # the preview already showed it
    assert app.store.recent == ["legal", "home"]
    assert app.store.last_used["legal"] > 0
    assert session.preview("taxes") is False  # a closed session ignores late selections


def test_cancel_restores_the_exact_origin_even_off_grid(tmp_path):
    fake = FakeHyprctl(active=(14, 5, 16), simulate=True)  # drifted: no meta matches
    app = make_app(fake, tmp_path / "s.json", {"home": 0, "taxes": 10, "legal": 30})
    session = PreviewSession(app, app.monitors(), None)
    session.preview("legal")
    assert list(fake.active.values()) == [34, 35, 36]
    session.cancel()
    assert list(fake.active.values()) == [14, 5, 16]
    assert app.store.recent == ["home"]


def test_cancel_without_a_preview_does_nothing(tmp_path):
    fake, app, session = preview_world(tmp_path)
    session.cancel()
    assert fake.batches == []


def test_auto_commit_config(tmp_path):
    cfg_file = tmp_path / "c.json"
    Config(base=[4, 5, 6]).save(cfg_file)
    assert Config.load(cfg_file).auto_commit_s == 1.0
    data = json.loads(cfg_file.read_text())
    data["auto_commit_s"] = 0
    cfg_file.write_text(json.dumps(data))
    assert Config.load(cfg_file).auto_commit_s == 0.0
    data["auto_commit_s"] = -1
    cfg_file.write_text(json.dumps(data))
    with pytest.raises(HyprmetaError, match="auto_commit_s"):
        Config.load(cfg_file)
    del data["auto_commit_s"]  # configs written before the key existed
    cfg_file.write_text(json.dumps(data))
    assert Config.load(cfg_file).auto_commit_s == 1.0
