"""Turn a layout snapshot back into windows: reopen terminals, rebuild the tiling.

1. Launch every missing terminal SMALL and FLOATING on a parking workspace,
   tagged with an env token so its window can be identified. Small first
   frames matter on the GB10: a new window's first buffer comes from the
   scanout carveout, and a large one can fail (the window never maps) while a
   window that already exists can grow later. A launch that never maps is
   killed and reported, never left behind as an invisible process.
2. Rebuild each workspace with the machinery verified on the Jul 29 (36
   windows) and Aug 17 (44 windows) display-freeze restores: infer the dwindle
   split tree from the saved rectangles (guillotine cuts), park every involved
   window, move the tree's first leaf in, then per split focus the left/top
   sibling and move the right/bottom one in, VERIFYING orientation and side
   from read-back rectangles (togglesplit / swapwindow when dwindle chose
   otherwise); rebuild tab groups; finish with `resizeactive exact` passes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cli import Hypr, HyprmetaError
from .layout import ALACRITTY, GHOSTTY, TERMINALS, children_map, launch_argv, tiled_leaves

PARK_WS = 99
LAUNCH_SIZE = (480, 300)
TOKEN_ENV = "HYPRMETA_RESTORE"
TREE_TOL = 45  # px: gaps/borders between cells


# --------------------------------------------------------------------------- #
# pure: commands + split-tree inference
# --------------------------------------------------------------------------- #


def launch_command(w: dict, token: str, park_ws: int = PARK_WS) -> str:
    """The `hyprctl dispatch exec` line that reopens terminal `w`, tagged with `token`."""
    term = w["terminal"]
    cwd = term.get("cwd") or str(Path.home())
    argv = launch_argv(term["run"])
    if w["class"] == GHOSTTY:
        cmd = [str(Path.home() / "bin" / "ghostty-protected"), f"--working-directory={cwd}"]
    elif w["class"] == ALACRITTY:
        cmd = ["alacritty", "--working-directory", cwd]
    else:
        raise HyprmetaError(f"do not know how to launch {w['class']!r}")
    if argv:
        cmd += ["-e", *argv]
    width, height = LAUNCH_SIZE
    rules = f"[workspace {park_ws} silent; float; size {width} {height}]"
    return f"{rules} env {TOKEN_ENV}={token} {shlex.join(cmd)}"


def environ_token(pid: int) -> str | None:
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        return None
    prefix = TOKEN_ENV.encode() + b"="
    for item in raw.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode(errors="replace")
    return None


@dataclass
class Leaf:
    key: str  # snapshot address
    rect: tuple[int, int, int, int]
    addr: str | None = None  # current address


@dataclass
class Node:
    orient: str  # LR = side by side, TB = stacked
    a: "Leaf | Node"
    b: "Leaf | Node"


def build_tree(leaves: list[Leaf], tol: int = TREE_TOL) -> "Leaf | Node | None":
    """Guillotine cuts: dwindle layouts always split into two straight halves.

    Also works for a SUBSET of a layout (windows that did not come back leave
    holes): the leftmost edge of the far side is still a valid cut.
    """
    if len(leaves) == 1:
        return leaves[0]
    for axis in (0, 1):
        for cut in sorted({lf.rect[axis] for lf in leaves})[1:]:
            a = [lf for lf in leaves if lf.rect[axis] + lf.rect[axis + 2] <= cut + tol]
            b = [lf for lf in leaves if lf.rect[axis] >= cut - tol]
            if a and b and len(a) + len(b) == len(leaves):
                ta, tb = build_tree(a, tol), build_tree(b, tol)
                if ta and tb:
                    return Node("LR" if axis == 0 else "TB", ta, tb)
    return None


def rep(t: "Leaf | Node") -> Leaf:
    return t if isinstance(t, Leaf) else rep(t.a)


def leaves_of(t: "Leaf | Node") -> list[Leaf]:
    return [t] if isinstance(t, Leaf) else leaves_of(t.a) + leaves_of(t.b)


def norm_title(title: str) -> str:
    return re.sub(r" - Chromium$", "", re.sub(r"^\(\d+\) ", "", title or ""))


# --------------------------------------------------------------------------- #
# side effects
# --------------------------------------------------------------------------- #


@dataclass
class Restorer:
    hypr: Hypr
    log: Callable[[str], None] = print
    park_ws: int = PARK_WS
    report: list[str] = field(default_factory=list)
    original_view: tuple[str, int] | None = None  # (monitor, workspace) before parking was shown

    def dispatch(self, *args: str) -> bool:
        out = self.hypr._run(["dispatch", *args]).strip()
        if out != "ok":
            self.log(f"  !! dispatch {' '.join(args)} -> {out}")
            return False
        return True

    def rect(self, addr: str, clients: list[dict] | None = None) -> tuple[int, int, int, int] | None:
        for c in clients if clients is not None else self.hypr.clients():
            if c["address"] == addr:
                return (*c["at"], *c["size"])
        return None

    # -- 1. launch ------------------------------------------------------------
    def launch(self, windows: list[dict], timeout: float = 30.0) -> dict[str, str]:
        """snapshot address -> new window address, for every launch that mapped."""
        tokens = {}
        for w in windows:
            token = secrets.token_hex(6)
            tokens[token] = w
            self.dispatch("exec", launch_command(w, token, self.park_ws))
            time.sleep(0.15)  # do not stampede the compositor / GPU
        found: dict[str, str] = {}
        deadline = time.time() + timeout
        while time.time() < deadline and len(found) < len(tokens):
            for c in self.hypr.clients():
                tok = environ_token(int(c.get("pid") or 0))
                if tok in tokens and tok not in found:
                    found[tok] = c["address"]
            time.sleep(0.4)
        for tok, w in tokens.items():
            if tok not in found:
                killed = self._kill_token(tok)
                self.report.append(f"FAILED to open ws {w['workspace']} ({w['class']}): no window after "
                                   f"{timeout:.0f} s; killed {killed} stuck process(es)")
        if found:
            self._start_programs(found, tokens)  # drops (and closes) terminals whose program never started
        return {tokens[tok]["address"]: addr for tok, addr in found.items()}

    def _start_programs(self, found: dict[str, str], tokens: dict[str, dict], timeout: float = 15.0) -> None:
        """Show the parking workspace until every new terminal runs its program.

        ghostty starts its command only after its surface first RENDERS, and a
        hidden workspace never renders (verified Sep 24 2026: a small ghostty on
        a hidden workspace mapped in 1 s and still had no child after 5 s, while
        Alacritty started its shell at once). Rendering here, small, also keeps
        the first buffer inside the GB10 scanout carveout.
        """
        mons = json.loads(self.hypr._run(["-j", "monitors"]))
        focused = next((m for m in mons if m.get("focused")), mons[0])
        self.original_view = (focused["name"], int(focused["activeWorkspace"]["id"]))
        self.dispatch("focusworkspaceoncurrentmonitor", str(self.park_ws))
        pids = {c["address"]: int(c.get("pid") or 0) for c in self.hypr.clients()}
        waiting = dict(found)
        deadline = time.time() + timeout
        while waiting and time.time() < deadline:
            kids = children_map()
            for tok, addr in list(waiting.items()):
                if kids.get(pids.get(addr, 0)):
                    del waiting[tok]
            time.sleep(0.3)
        for tok, addr in waiting.items():
            w = tokens[tok]
            self.dispatch("closewindow", f"address:{addr}")
            killed = self._kill_token(tok)
            del found[tok]
            self.report.append(f"FAILED ws {w['workspace']} ({w['class']}): window appeared but its program never "
                               f"started within {timeout:.0f} s; closed it, killed {killed} process(es)")

    def restore_view(self) -> None:
        """Put the monitor used for parking back on the workspace it showed."""
        if self.original_view is None:
            return
        name, ws = self.original_view
        mons = json.loads(self.hypr._run(["-j", "monitors"]))
        mon = next((m for m in mons if m["name"] == name), None)
        if mon is not None and int(mon["activeWorkspace"]["id"]) == self.park_ws:
            self.dispatch("focusmonitor", name)
            self.dispatch("focusworkspaceoncurrentmonitor", str(ws))

    def _kill_token(self, token: str) -> int:
        n = 0
        for d in os.listdir("/proc"):
            if d.isdigit() and environ_token(int(d)) == token:
                try:
                    os.kill(int(d), signal.SIGTERM)
                    n += 1
                except OSError:
                    pass
        return n

    # -- 2. arrange ------------------------------------------------------------
    def dissolve_groups(self, addrs: set[str]) -> None:
        for _ in range(20):  # togglegroup is a TOGGLE: re-query every round
            g = next((c for c in self.hypr.clients() if c["address"] in addrs and c.get("grouped")), None)
            if g is None:
                return
            self.dispatch("focuswindow", f"address:{g['address']}")
            time.sleep(0.15)
            self.dispatch("togglegroup")
            time.sleep(0.3)

    def realize(self, t: "Leaf | Node", ws: int) -> None:
        if isinstance(t, Leaf):
            return
        ra, rb = rep(t.a), rep(t.b)
        self.dispatch("focuswindow", f"address:{ra.addr}")
        time.sleep(0.15)
        self.dispatch("movetoworkspacesilent", f"{ws},address:{rb.addr}")
        time.sleep(0.3)
        cl = self.hypr.clients()
        a_r, b_r = self.rect(ra.addr, cl), self.rect(rb.addr, cl)
        if a_r and b_r:
            actual = "LR" if abs(b_r[0] - a_r[0]) >= abs(b_r[1] - a_r[1]) else "TB"
            if actual != t.orient:
                self.dispatch("focuswindow", f"address:{rb.addr}")
                time.sleep(0.1)
                self.dispatch("togglesplit")
                time.sleep(0.25)
                cl = self.hypr.clients()
                a_r, b_r = self.rect(ra.addr, cl), self.rect(rb.addr, cl)
            if a_r and b_r and ((b_r[0] < a_r[0]) if t.orient == "LR" else (b_r[1] < a_r[1])):
                self.dispatch("focuswindow", f"address:{rb.addr}")
                time.sleep(0.1)
                self.dispatch("swapwindow", "r" if t.orient == "LR" else "d")
                time.sleep(0.25)
        self.realize(t.a, ws)
        self.realize(t.b, ws)

    def arrange(self, ws: int, snap_windows: list[dict], addr_of: dict[str, str], mine: str | None) -> None:
        cells = [w for w in tiled_leaves(snap_windows) if w["address"] in addr_of]
        floats = [w for w in snap_windows if w["floating"] and w["address"] in addr_of]
        members = {w["address"]: [m for m in w["grouped"] if m != w["address"] and m in addr_of]
                   for w in cells if w["grouped"]}
        involved = {addr_of[w["address"]] for w in cells + floats}
        involved |= {addr_of[m] for ms in members.values() for m in ms}
        others = [c for c in self.hypr.clients()
                  if c["workspace"]["id"] == ws and c["address"] not in involved and c["address"] != mine]
        for c in others:
            self.dispatch("movetoworkspacesilent", f"{self.park_ws},address:{c['address']}")
            self.report.append(f"ws {ws}: moved an unrelated {c['class']} {c['title'][:40]!r} to ws {self.park_ws}")
        if not cells:
            self._place_floats(floats, addr_of, ws)
            return
        leaves = [Leaf(w["address"], (*w["at"], *w["size"]), addr_of[w["address"]]) for w in cells]
        tree = build_tree(leaves)
        self.dissolve_groups(involved)
        for addr in involved:
            self.dispatch("settiled", f"address:{addr}")
            self.dispatch("movetoworkspacesilent", f"{self.park_ws},address:{addr}")
        time.sleep(0.4)
        if tree is None:
            for lf in leaves:
                self.dispatch("movetoworkspacesilent", f"{ws},address:{lf.addr}")
            self.report.append(f"ws {ws}: saved rectangles form no split tree — windows moved in WITHOUT layout")
        else:
            self.dispatch("movetoworkspacesilent", f"{ws},address:{rep(tree).addr}")
            time.sleep(0.3)
            self.realize(tree, ws)
        for lf in leaves:  # tab groups: leader first, then pull members in
            ms = members.get(lf.key)
            if not ms:
                continue
            self.dispatch("focuswindow", f"address:{lf.addr}")
            time.sleep(0.15)
            self.dispatch("togglegroup")
            time.sleep(0.25)
            for m in ms:
                ma = addr_of[m]
                self.dispatch("focuswindow", f"address:{lf.addr}")
                time.sleep(0.1)
                self.dispatch("movetoworkspacesilent", f"{ws},address:{ma}")
                time.sleep(0.3)
                cl = self.hypr.clients()
                if next((c for c in cl if c["address"] == ma), {}).get("grouped"):
                    continue
                lr, mr = self.rect(lf.addr, cl), self.rect(ma, cl)
                if lr and mr:
                    dx, dy = lr[0] - mr[0], lr[1] - mr[1]
                    d = ("r" if dx > 0 else "l") if abs(dx) >= abs(dy) else ("d" if dy > 0 else "u")
                    self.dispatch("focuswindow", f"address:{ma}")
                    time.sleep(0.1)
                    self.dispatch("moveintogroup", d)
                    time.sleep(0.25)
        for _ in range(3):
            for lf in leaves:
                self.dispatch("focuswindow", f"address:{lf.addr}")
                time.sleep(0.06)
                self.dispatch("resizeactive", "exact", str(lf.rect[2]), str(lf.rect[3]))
                time.sleep(0.06)
        self._place_floats(floats, addr_of, ws)
        cl = self.hypr.clients()
        devs = [max(abs(r[2] - lf.rect[2]), abs(r[3] - lf.rect[3]))
                for lf in leaves if (r := self.rect(lf.addr, cl))]
        self.report.append(f"ws {ws}: {len(leaves)} cells rebuilt, worst size deviation {max(devs, default=0)} px")

    def _place_floats(self, floats: list[dict], addr_of: dict[str, str], ws: int) -> None:
        for w in floats:
            a = addr_of[w["address"]]
            self.dispatch("movetoworkspacesilent", f"{ws},address:{a}")
            self.dispatch("setfloating", f"address:{a}")
            self.dispatch("movewindowpixel", f"exact {w['at'][0]} {w['at'][1]},address:{a}")
            self.dispatch("resizewindowpixel", f"exact {w['size'][0]} {w['size'][1]},address:{a}")


def match_survivors(snap: dict, clients: list[dict], taken: set[str]) -> dict[str, str]:
    """Non-terminal windows that came back on their own (a browser restoring its
    session): match by class + title without unread counts. Same instance =
    same address, which wins."""
    out: dict[str, str] = {}
    live = {c["address"]: c for c in clients}
    for w in snap["windows"]:
        if w["address"] in live:
            out[w["address"]] = w["address"]
    used = set(out.values()) | taken
    for w in snap["windows"]:
        if w["address"] in out or w["class"] in TERMINALS:
            continue
        for c in clients:
            if c["address"] not in used and c["class"] == w["class"] and norm_title(c["title"]) == norm_title(w["title"]):
                out[w["address"]] = c["address"]
                used.add(c["address"])
                break
    return out
