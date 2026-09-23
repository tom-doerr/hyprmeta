import json

from hyprmeta import agents as ag


def test_claude_title_states():
    assert ag.claude_state_from_title("✳ Displays freeze issue") == "idle"
    assert ag.claude_state_from_title("◑ Hyperland main window mapping") == "running"
    assert ag.claude_state_from_title("⠏ thinking") == "running"
    assert ag.claude_state_from_title("btop") is None
    assert ag.claude_state_from_title("") is None


def test_codex_rollout_tail_states():
    def ev(t):
        return json.dumps({"type": "event_msg", "payload": {"type": t}})
    other = json.dumps({"type": "response_item", "payload": {"type": "message"}})
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), other])) == "running"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), ev("task_complete")])) == "idle"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_complete"), ev("user_message")])) == "running"
    assert ag.codex_state_from_rollout_tail("\n".join([ev("task_started"), ev("turn_aborted")])) == "idle"
    assert ag.codex_state_from_rollout_tail("garbage\n" + other) is None


def client(addr, pid, ws, title):
    return {"address": addr, "pid": pid, "workspace": {"id": ws}, "title": title}


def make_tracker():
    return ag.AgentTracker([4, 5, 6], {"home": 0, "taxes": 10, "legal": 30}, now=1000.0)


def test_meta_for_workspace():
    t = make_tracker()
    assert t.meta_for_workspace(14) == "taxes"
    assert t.meta_for_workspace(16) == "taxes"
    assert t.meta_for_workspace(5) == "home"
    assert t.meta_for_workspace(35) == "legal"
    assert t.meta_for_workspace(2) is None
    assert t.meta_for_workspace(25) is None  # offset 20 has no meta


def test_running_then_idle_marks_window_and_meta_unseen(monkeypatch):
    t = make_tracker()
    # pid 100 is the window; agent pid 200 is its descendant
    monkeypatch.setattr(ag, "window_pid_for", lambda pid, wp, max_depth=12: 100 if pid == 200 else None)
    states = {"claude": "running"}
    state_of = lambda kind, pid, title: states[kind]
    clients = [client("0xa", 100, 14, "◑ working")]
    assert t.update(clients, [(200, "claude")], now=1001.0, state_of=state_of) is True
    assert t.meta_summary()["taxes"] == {"running": 1, "unseen": 0, "agents": 1, "windows": 1}
    assert t.tags_wanted()["0xa"] == {ag.TAG_RUNNING}

    states["claude"] = "idle"
    assert t.update(clients, [(200, "claude")], now=1005.0, state_of=state_of) is True
    w = t.windows["0xa"]
    assert w.finish_ts == 1005.0 and w.finished_by == "claude"
    assert w.unseen is True  # last focus = tracker start (1000) < finish
    assert t.meta_summary()["taxes"]["unseen"] == 1
    assert t.tags_wanted()["0xa"] == {ag.TAG_DONE}

    # looking at the meta clears the meta-level flag, not the window's
    t.set_current_meta("taxes", now=1010.0)
    assert t.meta_summary()["taxes"]["unseen"] == 0
    assert t.windows["0xa"].unseen is True
    # focusing the window clears the window flag
    t.focus("0xa", now=1011.0)
    assert t.windows["0xa"].unseen is False
    assert t.tags_wanted()["0xa"] == set()

    # no change → update reports False
    assert t.update(clients, [(200, "claude")], now=1012.0, state_of=state_of) is False


def test_agent_exit_while_running_counts_as_finished(monkeypatch):
    t = make_tracker()
    monkeypatch.setattr(ag, "window_pid_for", lambda pid, wp, max_depth=12: 100)
    clients = [client("0xa", 100, 34, "task | dir")]
    t.update(clients, [(300, "codex")], now=1001.0, state_of=lambda k, p, ti: "running")
    t.update(clients, [], now=1002.0, state_of=lambda k, p, ti: None)
    assert t.windows["0xa"].finish_ts == 1002.0
    assert t.meta_summary()["legal"]["unseen"] == 1


def test_closed_windows_are_dropped():
    t = make_tracker()
    t.update([client("0xa", 100, 14, "x")], [], now=1001.0)
    t.update([], [], now=1002.0)
    assert t.windows == {}


def test_apply_tags_diffs_and_batches():
    calls = []

    class H:
        def batch(self, ds):
            calls.append(list(ds))

    cur = ag.apply_tags(H(), {"0xa": {"agent-done"}, "0xb": set()}, {"0xb": {"agent-running"}})
    assert calls == [["tagwindow +agent-done address:0xa", "tagwindow -agent-running address:0xb"]]
    assert cur == {"0xa": {"agent-done"}, "0xb": set()}
    ag.apply_tags(H(), cur, cur)
    assert len(calls) == 1  # nothing to do → no batch


def test_render_waybar_lines_and_class():
    snap = {
        "metas": {
            "taxes": {"running": 1, "unseen": 0, "agents": 2, "windows": 3},
            "legal": {"running": 0, "unseen": 2, "agents": 2, "windows": 2},
            "home": {"running": 0, "unseen": 0, "agents": 1, "windows": 5},
            "idle": {"running": 0, "unseen": 0, "agents": 0, "windows": 0},
        },
        "current_meta": "home",
    }
    out = ag.render_waybar(snap, ["home", "taxes", "legal", "idle"])
    lines = out["text"].split("\n")
    assert len(lines) == 4
    assert "weight=\"bold\"" in lines[0] and "·1" in lines[0]
    assert "⟳1" in lines[1] and "✓2" in lines[2]
    assert lines[3].endswith("</span>")  # no marks at all
    assert out["class"] == "attention"
    snap["metas"]["legal"]["unseen"] = 0
    assert ag.render_waybar(snap, ["taxes"])["class"] == "running"


def test_restore_keeps_stamps_for_surviving_windows():
    t = make_tracker()
    t.update([client("0xa", 100, 14, "x")], [], now=1001.0)
    t.restore({"windows": {"0xa": {"last_focus_ts": 900.0, "finish_ts": 950.0, "finished_by": "codex"},
                           "0xgone": {"finish_ts": 1.0}}, "seen_ts": {"taxes": 940.0}}, now=1002.0)
    assert t.windows["0xa"].unseen is True
    assert t.seen_ts == {"taxes": 940.0}
    assert t.meta_summary()["taxes"]["unseen"] == 1
