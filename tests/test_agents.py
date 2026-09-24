import json
import re

import pytest

from hyprmeta import agents as ag


# --------------------------------------------------------------------------- #
# classifiers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind,title,state",
    [
        ("claude", "✳ Displays freeze issue", "idle"),
        ("claude", "◑ Hyperland main window mapping", "running"),
        ("claude", "◐ MinIO rename across devices", "running"),
        ("codex", "⠙ Clarify session settings and f", "running"),
        ("codex", "⠧ Scan scale cover | tom", "running"),
        ("codex", "[ ! ] Action Required | Audit in", "waiting"),
        ("codex", "[ . ] Action Required | Scan scale cover", "waiting"),
        ("codex", "Remove custom horizon on reset | nootrop", "idle"),
        ("claude", "Remove custom horizon on reset | nootrop", None),  # codex-style title, not claude's
        ("codex", "btop", None),
        ("claude", "", None),
        ("codex", "⠀ blank braille is not a spinner frame", None),
    ],
)
def test_title_state(kind, title, state):
    assert ag.title_state(kind, title) == state


def test_codex_rollout_tail_states():
    def ev(t):
        return json.dumps({"type": "event_msg", "payload": {"type": t}})

    other = json.dumps({"type": "response_item", "payload": {"type": "message"}})
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), other])) == "running"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), ev("task_complete")])) == "idle"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_complete"), ev("user_message")])) == "running"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), ev("turn_aborted")])) == "idle"
    assert ag.codex_state_from_rollout_tail("garbage\n" + other) is None


# --------------------------------------------------------------------------- #
# tracker
# --------------------------------------------------------------------------- #


def client(addr, pid, ws, title, focused=False):
    return {"address": addr, "pid": pid, "workspace": {"id": ws}, "title": title,
            "focusHistoryID": 0 if focused else 3}


@pytest.fixture
def world(monkeypatch):
    """A tracker plus a tiny fake world: window pid 100 (0xa, ws 14 = taxes) and
    pid 101 (0xme, ws 5 = home, focused); agent pids map to window pids."""
    parents = {200: 100, 201: 100, 300: 101}
    monkeypatch.setattr(ag, "window_pid_for",
                        lambda pid, wp, max_depth=14: parents.get(pid) if parents.get(pid) in wp else None)
    t = ag.AgentTracker([4, 5, 6], {"home": 0, "taxes": 10, "legal": 30}, now=1000.0, confirm_s=3.0)
    state = {"titles": {"0xa": "◑ working", "0xme": "✳ me"}, "focused": "0xme", "agents": [(200, "claude")]}

    def clients():
        return [client("0xa", 100, 14, state["titles"]["0xa"], state["focused"] == "0xa"),
                client("0xme", 101, 5, state["titles"]["0xme"], state["focused"] == "0xme")]

    def scan(now):
        t.update(clients(), state["agents"], now,
                 state_of=lambda kind, pids, title: ag.title_state(kind, title))

    return t, state, scan


def test_meta_for_workspace():
    t = ag.AgentTracker([4, 5, 6], {"home": 0, "taxes": 10, "legal": 30}, now=0.0)
    assert t.meta_for_workspace(14) == "taxes"
    assert t.meta_for_workspace(16) == "taxes"
    assert t.meta_for_workspace(5) == "home"
    assert t.meta_for_workspace(35) == "legal"
    assert t.meta_for_workspace(2) is None
    assert t.meta_for_workspace(25) is None  # offset 20 has no meta


def test_finish_needs_a_stable_stop_then_flags_until_focused(world):
    t, state, scan = world
    scan(1001.0)
    assert t.meta_summary()["taxes"]["running"] == 1
    assert t.tags_wanted()["0xa"] == {ag.TAG_RUNNING}

    t.set_title("0xa", "✳ done", 1002.0)  # stopped, not yet confirmed
    t.tick(1004.0)
    assert t.windows["0xa"].finish_ts == 0.0
    t.tick(1005.5)  # stable for >= 3 s → a finish, stamped at the moment it stopped
    w = t.windows["0xa"]
    assert (w.finish_ts, w.finished_by, w.finish_state) == (1002.0, "claude", "idle")
    assert t.needs_attention(w)
    assert t.tags_wanted()["0xa"] == {ag.TAG_DONE}
    assert t.meta_summary()["taxes"] == {"running": 0, "done": 1, "waiting": 0, "idle": 0, "agents": 1, "windows": 1}
    assert any("claude idle in 0xa" in e for e in t.events)

    # Showing the meta does NOT clear it (user decision) ...
    t.set_current_meta("taxes")
    assert t.meta_summary()["taxes"]["done"] == 1
    # ... focusing the window does.
    t.focus("0xa", 1010.0)
    assert not t.needs_attention(w)
    assert t.tags_wanted()["0xa"] == set()
    assert t.meta_summary()["taxes"]["idle"] == 1
    # and leaving it keeps it seen
    t.focus("0xme", 1011.0)
    assert not t.needs_attention(w)


def test_title_flicker_is_not_a_finish(world):
    t, state, scan = world
    scan(1001.0)
    t.set_title("0xa", "✳ between tool calls", 1002.0)
    t.set_title("0xa", "◒ working again", 1003.0)
    t.tick(1010.0)
    assert t.windows["0xa"].finish_ts == 0.0


def test_a_window_focused_while_its_agent_finishes_is_never_flagged(world):
    t, state, scan = world
    state["focused"] = "0xa"
    scan(1001.0)
    t.set_title("0xa", "✳ done", 1002.0)
    t.tick(1006.0)
    assert t.windows["0xa"].finish_ts == 1002.0
    assert not t.needs_attention(t.windows["0xa"])  # you were looking at it
    t.focus("0xme", 1007.0)
    assert not t.needs_attention(t.windows["0xa"])  # still seen after you leave


def test_running_again_hides_the_flag_until_it_stops_again(world):
    t, state, scan = world
    scan(1001.0)
    t.set_title("0xa", "✳ done", 1002.0)
    t.tick(1006.0)
    assert t.tags_wanted()["0xa"] == {ag.TAG_DONE}
    t.set_title("0xa", "◐ auto-resumed", 1007.0)
    assert t.tags_wanted()["0xa"] == {ag.TAG_RUNNING}
    assert t.meta_summary()["taxes"]["done"] == 0
    t.set_title("0xa", "✳ done again", 1008.0)
    t.tick(1012.0)
    assert t.tags_wanted()["0xa"] == {ag.TAG_DONE}
    assert t.windows["0xa"].finish_ts == 1008.0


def test_waiting_counts_as_attention(world):
    t, state, scan = world
    state["agents"] = [(201, "codex")]
    state["titles"]["0xa"] = "⠋ Fix it | repo"
    scan(1001.0)
    t.set_title("0xa", "[ ! ] Action Required | repo", 1002.0)
    t.tick(1006.0)
    assert t.windows["0xa"].finish_state == "waiting"
    assert t.meta_summary()["taxes"]["waiting"] == 1


def test_exit_while_running_is_a_finish_exit_while_idle_is_not(world):
    t, state, scan = world
    scan(1001.0)
    state["agents"] = []
    scan(1002.0)
    assert t.windows["0xa"].finish_state == "exited"
    assert t.needs_attention(t.windows["0xa"])  # no agent left, still flagged until focused

    t.focus("0xa", 1003.0)
    state["agents"] = [(200, "claude")]
    state["titles"]["0xa"] = "✳ idle from the start"
    scan(1004.0)
    state["agents"] = []
    scan(1005.0)
    assert t.windows["0xa"].finish_ts == 1002.0  # unchanged: it never ran


def test_several_processes_of_one_kind_are_one_agent(world):
    t, state, scan = world
    state["agents"] = [(200, "codex"), (201, "codex")]
    state["titles"]["0xa"] = "⠋ Fix it | repo"
    scan(1001.0)
    assert list(t.windows["0xa"].agents) == ["codex"]
    assert t.windows["0xa"].agents["codex"].pids == [200, 201]
    assert t.meta_summary()["taxes"]["running"] == 1


def test_focus_is_synced_from_focus_history(world):
    t, state, scan = world
    state["focused"] = "0xa"
    scan(1001.0)
    assert t.focused == "0xa"


def test_closed_windows_are_dropped(world):
    t, state, scan = world
    scan(1001.0)
    t.update([], [], 1002.0, state_of=lambda *a: None)
    assert t.windows == {}


def test_restore_carries_stamps_and_the_armed_flag():
    t = ag.AgentTracker([4, 5, 6], {"taxes": 10}, now=1000.0, confirm_s=3.0)
    import hyprmeta.agents as mod

    mod_window_pid_for = mod.window_pid_for
    mod.window_pid_for = lambda pid, wp, max_depth=14: 100
    try:
        t.update([client("0xa", 100, 14, "✳ idle now")], [(200, "claude")], 1001.0,
                 state_of=lambda k, p, ti: ag.title_state(k, ti))
    finally:
        mod.window_pid_for = mod_window_pid_for
    t.restore({"windows": {"0xa": {"last_focus_ts": 900.0, "finish_ts": 0.0,
                                   "agents": [{"kind": "claude", "state": "running", "armed": True}]},
                           "0xgone": {"finish_ts": 1.0}}})
    assert t.windows["0xa"].last_focus_ts == 900.0
    assert t.windows["0xa"].agents["claude"].armed
    t.tick(1005.0)  # it finished while the daemon was down → flagged after confirmation
    assert t.needs_attention(t.windows["0xa"])


def test_snapshot_shape():
    t = ag.AgentTracker([4, 5, 6], {"taxes": 10}, now=1000.0)
    t.update([client("0xa", 100, 14, "x")], [], 1001.0)
    snap = t.snapshot(1002.0)
    assert snap["metas"]["taxes"]["windows"] == 1
    assert snap["windows"] == {}  # no agents, no finish → not listed
    assert json.loads(json.dumps(snap)) == snap


# --------------------------------------------------------------------------- #
# tags + rendering
# --------------------------------------------------------------------------- #


def test_actual_tags_filters_ours_and_strips_rule_marker():
    cs = [{"address": "0xa", "tags": ["agent-done", "foo", "agent-running*"]}, {"address": "0xb"}]
    assert ag.actual_tags(cs) == {"0xa": {"agent-done", "agent-running"}, "0xb": set()}


def test_apply_tags_diffs_and_batches():
    calls = []

    class H:
        def batch(self, ds):
            calls.append(list(ds))

    cur = ag.apply_tags(H(), {"0xa": {"agent-done"}, "0xb": set()}, {"0xb": {"agent-running"}})
    assert calls == [["tagwindow +agent-done address:0xa", "tagwindow -agent-running address:0xb"]]
    ag.apply_tags(H(), cur, cur)
    assert len(calls) == 1  # nothing to do → no batch


def test_marks_use_unambiguous_glyphs():
    m = ag.marks_markup({"running": 1, "done": 2, "waiting": 1, "idle": 3})
    assert "▶1" in m and "✓2" in m and "!1" in m and "○3" in m
    assert ag.marks_text({"running": 1, "done": 2, "waiting": 1, "idle": 3}) == "▶1 ✓2 !1 ○3"
    # one cell each in JetBrains Mono (⟳ fell back to an 11 px glyph vs 8 px cells)
    assert {g for _, g, _ in ag.MARKS} == {"▶", "✓", "!", "○"}
    assert "·" not in m  # read as a minus sign from a distance
    # unread states are blue: yellow/orange read as warnings (user decision)
    assert ag.COLOR_DONE == ag.COLOR_WAITING == "#89b4fa"
    assert "#f9e2af" not in m and "#fab387" not in m
    assert ag.marks_markup({"running": 0, "idle": 0}) == ""


def test_render_waybar_lines_and_class():
    snap = {
        "metas": {
            "taxes": {"running": 1, "done": 0, "waiting": 0, "idle": 1, "agents": 2, "windows": 3},
            "legal": {"running": 0, "done": 2, "waiting": 0, "idle": 0, "agents": 2, "windows": 2},
            "home": {"running": 0, "done": 0, "waiting": 0, "idle": 1, "agents": 1, "windows": 5},
            "empty": {"running": 0, "done": 0, "waiting": 0, "idle": 0, "agents": 0, "windows": 0},
        },
        "current_meta": "home",
    }
    order = ["home", "taxes", "legal", "empty"]
    out = ag.render_waybar(snap, order)
    lines = out["text"].split("\n")
    assert len(lines) == 4
    assert 'weight="bold"' in lines[0] and "○1" in lines[0]
    assert "▶1" in lines[1] and "✓2" in lines[2]
    # markers are right-aligned in a column LEFT of the names, names share one column
    visible = [re.sub(r"<[^>]+>", "", line) for line in lines]
    assert visible == ["   ○1 home", "▶1 ○1 taxes", "   ✓2 legal", "      empty"]
    assert {v.index(n) for v, n in zip(visible, order)} == {6}
    assert out["class"] == "attention"
    snap["metas"]["legal"]["done"] = 0
    assert ag.render_waybar(snap, ["taxes"])["class"] == "running"


def test_render_waybar_row_is_one_line():
    snap = {
        "metas": {
            "taxes": {"running": 1, "done": 0, "waiting": 0, "idle": 1, "agents": 2, "windows": 3},
            "home": {"running": 0, "done": 0, "waiting": 0, "idle": 1, "agents": 1, "windows": 5},
            "empty": {"running": 0, "done": 0, "waiting": 0, "idle": 0, "agents": 0, "windows": 0},
        },
        "current_meta": "home",
    }
    out = ag.render_waybar(snap, ["home", "taxes", "empty"], row=True)
    assert "\n" not in out["text"]
    # no alignment padding in a row; a meta without agents is just its name
    assert re.sub(r"<[^>]+>", "", out["text"]) == "○1 home  │  ▶1 ○1 taxes  │  empty"
    assert out["class"] == "running"
    assert out["tooltip"] == ag.render_waybar(snap, ["home", "taxes", "empty"])["tooltip"]


def test_row_wraps_between_entries_never_inside_one():
    snap = {
        "metas": {
            "taxes": {"running": 1, "idle": 1},
            "home": {"idle": 1},
            "empty": {},
            "a_very_long_meta_name": {},
        },
        "current_meta": "home",
    }
    order = ["home", "taxes", "empty", "a_very_long_meta_name"]

    def visible(wrap: int) -> list[str]:
        text = ag.render_waybar(snap, order, row=True, wrap=wrap)["text"]
        return [re.sub(r"<[^>]+>", "", line) for line in text.split("\n")]

    # "○1 home  │  ▶1 ○1 taxes" is 23 cells; "  │  empty" would make it 33
    assert visible(32) == ["○1 home  │  ▶1 ○1 taxes", "empty  │  a_very_long_meta_name"]  # 31 cells
    assert visible(33) == ["○1 home  │  ▶1 ○1 taxes  │  empty", "a_very_long_meta_name"]
    # an entry wider than the limit still gets its own line, whole
    assert visible(5)[-1] == "a_very_long_meta_name" and len(visible(5)) == 4
    # 0 = never wrap; so does a limit the whole row fits in
    assert len(visible(0)) == len(visible(1000)) == 1
    for wrap in (22, 31, 40):
        assert all(len(line) <= wrap for line in visible(wrap) if line != "a_very_long_meta_name")


def test_wrap_is_rejected_where_it_would_be_ignored():
    from hyprmeta import cli

    assert cli.main(["agents", "--waybar", "--wrap", "80"]) == 1  # no --row
    assert cli.main(["agents", "--waybar", "--row", "--wrap", "-1"]) == 1
