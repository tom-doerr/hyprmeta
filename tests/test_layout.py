import json
import shlex

import pytest

from hyprmeta import layout, restore
from hyprmeta.cli import HyprmetaError

# ws 5 of the Aug 17 2026 restore: 14 real cells (x, y, w, h), a dwindle layout
AUG17_WS5 = [(1740, 2342, 413, 845), (1740, 3211, 413, 836), (1740, 4092, 840, 1128),
             (2177, 2342, 403, 416), (2177, 2782, 403, 405), (2177, 3211, 403, 406),
             (2177, 3641, 403, 406), (2604, 2342, 403, 1705), (2604, 4071, 403, 1149),
             (3031, 2342, 413, 765), (3031, 3131, 413, 446), (3031, 3601, 413, 446),
             (3031, 4071, 413, 557), (3031, 4652, 413, 568)]


def leaves(rects):
    return [restore.Leaf(f"0x{i}", r) for i, r in enumerate(rects)]


def test_build_tree_on_a_real_14_cell_layout():
    t = restore.build_tree(leaves(AUG17_WS5))
    assert t is not None
    assert len(restore.leaves_of(t)) == 14


def test_build_tree_on_a_subset_with_holes():
    subset = [r for i, r in enumerate(AUG17_WS5) if i not in (2, 5, 9, 13)]
    t = restore.build_tree(leaves(subset))
    assert t is not None and len(restore.leaves_of(t)) == 10


def test_build_tree_refuses_a_pinwheel():
    pinwheel = [(0, 0, 200, 100), (200, 0, 100, 200), (100, 200, 200, 100), (0, 100, 100, 200), (100, 100, 100, 100)]
    assert restore.build_tree(leaves(pinwheel), tol=5) is None


def test_build_tree_orientation():
    t = restore.build_tree(leaves([(0, 0, 100, 200), (110, 0, 100, 200)]))
    assert isinstance(t, restore.Node) and t.orient == "LR"
    t = restore.build_tree(leaves([(0, 0, 200, 100), (0, 110, 200, 100)]))
    assert t.orient == "TB"


# --------------------------------------------------------------------------- #
# inspect_terminal against a fake /proc
# --------------------------------------------------------------------------- #


@pytest.fixture
def fakeproc(monkeypatch):
    procs = {}  # pid -> (comm, ppid, pgrp, tpgid, argv, cwd)

    def add(pid, comm, ppid, tpgid=0, argv=None, cwd="/home/tom"):
        procs[pid] = (comm, ppid, pid, tpgid, argv or [comm], cwd)

    monkeypatch.setattr(layout, "_stat", lambda p: procs[p][:4] if p in procs else None)
    monkeypatch.setattr(layout, "_cmdline", lambda p: procs[p][4] if p in procs else [])
    monkeypatch.setattr(layout, "_cwd", lambda p: procs[p][5] if p in procs else None)
    monkeypatch.setattr(layout, "claude_session",
                        lambda p: {"session": "c1a0de00-0000-4000-8000-000000000001", "cwd": "/home/tom/git/x", "name": "n"})
    monkeypatch.setattr(layout, "codex_session", lambda p: "01a0cf81-eec0-7000-8000-000000000002")

    def kids():
        out = {}
        for pid, v in procs.items():
            out.setdefault(v[1], []).append(pid)
        return out

    return add, kids


def test_ghostty_idle_shell(fakeproc):
    add, kids = fakeproc
    add(100, "ghostty", 1)
    add(101, "sh", 100, tpgid=102, argv=["/bin/sh", "-c", "/usr/bin/zsh"])
    add(102, "zsh", 101, tpgid=102, cwd="/home/tom/git/y")
    assert layout.inspect_terminal(100, kids()) == {"cwd": "/home/tom/git/y", "run": {"kind": "shell"}}


def test_ghostty_command_in_foreground(fakeproc):
    add, kids = fakeproc
    add(100, "ghostty", 1)
    add(101, "sh", 100, tpgid=104)
    add(102, "zsh", 101, tpgid=104)
    add(104, "ssh", 102, tpgid=104, argv=["ssh", "-t", "nas", "tmux new-session -A -s base"], cwd="/home/tom")
    term = layout.inspect_terminal(100, kids())
    assert term["run"] == {"kind": "command", "argv": ["ssh", "-t", "nas", "tmux new-session -A -s base"]}


def test_ghostty_claude_session(fakeproc):
    add, kids = fakeproc
    add(100, "ghostty", 1)
    add(101, "sh", 100, tpgid=103)
    add(102, "zsh", 101, tpgid=103)
    add(103, "claude", 102, tpgid=103)
    term = layout.inspect_terminal(100, kids())
    assert term["run"]["kind"] == "claude"
    assert term["run"]["session"].startswith("c1a0de00")
    assert term["cwd"] == "/home/tom/git/x"  # from the session file, not the process


def test_alacritty_codex_behind_node_and_a_sandbox_helper(fakeproc):
    add, kids = fakeproc
    add(200, "alacritty", 1)
    add(201, "zsh", 200, tpgid=202)  # Alacritty: no sh wrapper
    add(202, "MainThread", 201, tpgid=202, argv=["node", "/x/bin/codex"])
    add(203, "codex", 202, argv=["/x/codex-linux-sandbox", "--sandbox-policy-cwd", "/"])
    add(204, "codex", 202, argv=["/x/vendor/codex"], cwd="/home/tom/git/nootropics")
    term = layout.inspect_terminal(200, kids())
    assert term["run"] == {"kind": "codex", "session": "01a0cf81-eec0-7000-8000-000000000002"}
    assert term["cwd"] == "/home/tom/git/nootropics"


def test_terminal_without_a_program(fakeproc):
    add, kids = fakeproc
    add(100, "ghostty", 1)
    assert layout.inspect_terminal(100, kids()) is None


def test_rollout_uuid_regex():
    name = "/h/.codex/sessions/2026/09/23/rollout-2026-09-23T03-26-47-01a0cbdf-5968-73b0-b350-107a4d866b7f.jsonl"
    assert layout.ROLLOUT_UUID.search(name).group(1) == "01a0cbdf-5968-73b0-b350-107a4d866b7f"


# --------------------------------------------------------------------------- #
# snapshots on disk
# --------------------------------------------------------------------------- #


def win(addr, ws, cls="com.mitchellh.ghostty", run=None, at=(0, 0), size=(100, 100), floating=False,
        grouped=(), title="t", cwd="/home/tom"):
    w = {"address": addr, "class": cls, "title": title, "workspace": ws, "monitor": 0, "at": list(at),
         "size": list(size), "floating": floating, "fullscreen": 0, "pinned": False, "grouped": list(grouped), "pid": 1}
    if cls in layout.TERMINALS:
        w["terminal"] = {"cwd": cwd, "run": run or {"kind": "shell"}}
    return w


def snap_of(windows, sig="aaaaaaaa_1", started=1_790_000_000, ts=1_790_000_100.0):
    return {"version": 1, "ts": ts, "instance": {"sig": sig, "started": started}, "monitors": [], "windows": windows}


def test_signature_ignores_titles_but_not_positions():
    a = snap_of([win("0x1", 5, title="◐ working")])
    b = snap_of([win("0x1", 5, title="✳ idle")])
    c = snap_of([win("0x1", 5, at=(10, 0))])
    assert layout.signature(a) == layout.signature(b)
    assert layout.signature(a) != layout.signature(c)


def test_save_keeps_one_directory_per_hyprland_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    old = snap_of([win("0x1", 5)], sig="oldoldold_1", started=1_790_000_000, ts=1_790_000_100.0)
    new = snap_of([win("0x9", 5)], sig="newnewnew_2", started=1_790_100_000, ts=1_790_100_100.0)
    p_old = layout.save(old)
    layout.save(new)
    assert p_old.exists() and len(list(p_old.parent.glob("2*.json"))) == 1
    prev = layout.previous_instance_latest("newnewnew_2")
    assert prev == p_old  # the crash-reboot case: the session before this one
    assert layout.load(prev)["windows"][0]["address"] == "0x1"
    assert layout.previous_instance_latest("oldoldold_1").parent.name.endswith("newnewne")
    snaps = layout.list_snapshots()  # per session: change ring newest first, then 10-min buckets
    assert len(snaps) == 4 and snaps[0].parent.name == "changes"
    assert layout.load(snaps[0])["windows"][0]["address"] == "0x9"  # newest session first


def test_change_ring_keeps_the_last_30_states(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for i in range(35):
        layout.save(snap_of([win(f"0x{i}", 5)], ts=1_790_000_100.0 + i))
    snaps = layout.list_snapshots()
    ring = [p for p in snaps if p.parent.name == "changes"]
    assert len(ring) == layout.CHANGES_KEEP
    assert layout.load(ring[0])["windows"][0]["address"] == "0x34"
    assert layout.load(ring[1])["windows"][0]["address"] == "0x33"  # one change back = undo


def test_load_refuses_unknown_versions(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 99}))
    with pytest.raises(HyprmetaError, match="version"):
        layout.load(p)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


CLAUDE = {"kind": "claude", "session": "live-claude"}
CODEX_DEAD = {"kind": "codex", "session": "dead-codex"}


def test_plan_restore_keeps_skips_and_launches():
    windows = [
        win("0xkeep", 5),
        win("0xbrowser", 5, cls="chromium"),
        win("0xscratch", -98),
        win("0xnoid", 5, run={"kind": "claude", "session": None}),
        win("0xlive", 5, run=CLAUDE),
        win("0xcodex", 6, run=CODEX_DEAD),
        win("0xshell", 6),
        win("0xalac", 6, cls="Alacritty", run={"kind": "command", "argv": ["btop"]}),
    ]
    plan = layout.plan_restore(snap_of(windows), present={"0xkeep"}, live={"live-claude"})
    assert [w["address"] for w in plan["keep"]] == ["0xkeep"]
    assert [w["address"] for w in plan["launch"]] == ["0xcodex", "0xshell", "0xalac"]
    reasons = {w["address"]: why for w, why in plan["skip"]}
    assert "not a terminal" in reasons["0xbrowser"]
    assert "special workspace" in reasons["0xscratch"]
    assert "unknown" in reasons["0xnoid"]
    assert "already running" in reasons["0xlive"]
    only6 = layout.plan_restore(snap_of(windows), set(), set(), only_ws={6})
    assert {w["workspace"] for w in only6["launch"]} == {6}


def test_launch_argv_resumes_sessions_and_keeps_a_shell():
    assert layout.launch_argv({"kind": "shell"}) is None
    argv = layout.launch_argv({"kind": "claude", "session": "abc"})
    assert argv == ["zsh", "-ic", "claude --resume abc; exec zsh -i"]
    assert layout.launch_argv({"kind": "codex", "session": "u-1"})[2] == "codex resume u-1; exec zsh -i"
    cmd = layout.launch_argv({"kind": "command", "argv": ["ssh", "-t", "nas", "tmux new-session -A -s base"]})
    inner = cmd[2].removesuffix("; exec zsh -i")
    assert shlex.split(inner) == ["ssh", "-t", "nas", "tmux new-session -A -s base"]


def test_launch_command_per_terminal_app():
    g = restore.launch_command(win("0x1", 5, run={"kind": "claude", "session": "abc"}, cwd="/home/tom/git/x"), "tok")
    assert g.startswith("[workspace 99 silent; float; size 480 300] env HYPRMETA_RESTORE=tok ")
    assert "ghostty-protected --working-directory=/home/tom/git/x -e zsh -ic" in g
    a = restore.launch_command(win("0x2", 5, cls="Alacritty", cwd="/tmp/a b"), "t2")
    assert "alacritty --working-directory '/tmp/a b'" in a and " -e " not in a  # plain shell
    with pytest.raises(HyprmetaError):
        restore.launch_command({"class": "kitty", "terminal": {"cwd": "/", "run": {"kind": "shell"}}}, "t")


def test_tiled_leaves_one_per_group_and_no_floats():
    ws = [win("0xa", 5), win("0xb", 5, grouped=["0xb", "0xc"]), win("0xc", 5, grouped=["0xb", "0xc"]),
          win("0xf", 5, floating=True)]
    assert [w["address"] for w in layout.tiled_leaves(ws)] == ["0xa", "0xb"]


def test_match_survivors_by_address_or_browser_title():
    snap = snap_of([win("0xsame", 5), win("0xold", 5, cls="chromium", title="(3) WhatsApp - Chromium"),
                    win("0xterm", 5, title="✳ something")])
    clients = [{"address": "0xsame", "class": "com.mitchellh.ghostty", "title": "x"},
               {"address": "0xnew", "class": "chromium", "title": "(7) WhatsApp - Chromium"},
               {"address": "0xother", "class": "com.mitchellh.ghostty", "title": "✳ something"}]
    m = restore.match_survivors(snap, clients, set())
    assert m == {"0xsame": "0xsame", "0xold": "0xnew"}  # terminals are never matched by title


def test_format_snapshot_mentions_sessions_and_commands():
    s = snap_of([win("0x1", 15, run={"kind": "claude", "session": "402781fa-aaaa"}, cwd="/home/tom/git/taxes"),
                 win("0x2", 15, run={"kind": "command", "argv": ["btop"]})])
    text = layout.format_snapshot(s, {15: "taxes"})
    assert "workspace 15  [taxes]" in text
    assert "claude  402781fa" in text and "$ btop" in text
