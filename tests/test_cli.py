import json
from pathlib import Path

import pytest

from hyprmeta import cli
from hyprmeta.cli import App, Config, Hypr, HyprmetaError, Store


class FakeHyprctl:
    """Answers `hyprctl -j monitors/workspaces/activewindow`, records batches."""

    def __init__(self, active=(4, 5, 6), focused="DP-2", ws_monitors=None, cursor=(100, 200)):
        self.names = ["DP-1", "DP-2", "HDMI-A-1"]
        self.active = dict(zip(self.names, active))
        self.focused = focused
        self.ws_monitors = ws_monitors or dict(zip(active, self.names))
        self.cursor = cursor
        self.batches: list[list[str]] = []
        self.active_window_monitor_id = 1

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
        if args[0] == "--batch":
            self.batches.append([d.removeprefix("dispatch ") for d in args[1].split("; ")])
            return "ok"
        raise AssertionError(f"unexpected hyprctl call {args}")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    state = tmp_path / "state.json"
    monkeypatch.setenv("HYPRMETA_CONFIG", str(cfg))
    monkeypatch.setenv("HYPRMETA_STATE", str(state))
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


def test_menu_lines_mru_first_with_current_marker(tmp_path):
    app = make_app(
        FakeHyprctl(active=(14, 15, 16)),
        tmp_path / "s.json",
        {"home": 0, "taxes": 10, "zeta": 20, "alpha": 30},
        recent=["taxes", "home"],
    )
    assert app.menu_lines() == [
        "taxes\t14 15 16  (current)",
        "home\t4 5 6",
        "alpha\t34 35 36",
        "zeta\t24 25 26",
        cli.NEW_ENTRY,
    ]


def test_resolve_pick_existing_typed_and_new_entry(tmp_path):
    app = make_app(FakeHyprctl(), tmp_path / "s.json", {"home": 0, "taxes": 10})
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "  music "

    assert app.resolve_pick("taxes\t14 15 16\n", prompt) == "taxes"
    # A typed query that matched nothing becomes a new meta.
    assert app.resolve_pick("bills", prompt) == "bills"
    assert app.store.metas["bills"] == 20
    # The explicit "+ new" entry asks for a name.
    assert app.resolve_pick(cli.NEW_ENTRY, prompt) == "music"
    assert app.store.metas["music"] == 30
    assert prompts == ["name for the new meta workspace"]
    with pytest.raises(HyprmetaError, match="nothing selected"):
        app.resolve_pick("", prompt)


def test_store_roundtrip_drops_recent_names_that_no_longer_exist(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"metas": {"home": 0}, "recent": ["gone", "home"]}))
    store = Store.load(p)
    assert store.recent == ["home"]
    store.rename("home", "base")
    store.save(p)
    assert Store.load(p).metas == {"base": 0}
    assert Store.load(p).recent == ["base"]
    with pytest.raises(HyprmetaError, match="reserved"):
        store.create(cli.NEW_ENTRY, 10)


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

    def fake_menu(command, lines, prompt_text=None):
        seen["command"] = command
        seen["lines"] = lines
        return "taxes\t14 15 16"

    monkeypatch.setattr(cli, "run_menu", fake_menu)
    assert cli.main(["pick", "--menu", "fzf"], hypr=Hypr(fake)) == 0
    assert seen["command"] == "fzf"
    assert seen["lines"][0] == "home\t4 5 6  (current)"
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
