"""Interactive terminal UI (Textual): live plot, sessions, timeline, battery, GPU."""
import os
import subprocess
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Header, Select, Static, TabbedContent, TabPane

from . import fmt, gpumode, store
from .cli import call, session_rows, session_summary, status_line
from .paths import DB_FILE, GPUMODE_HELPER

BLOCKS = " ▁▂▃▄▅▆▇█"
GPU_STYLE = {-1: "green", 0: "green", 1: "yellow", 2: "red"}
KIND_GROUPS = {
    "All": None,
    "Sleep & power": ["boot", "shutdown", "unclean", "gap", "suspend", "resume", "lid", "lock"],
    "dGPU": ["gpu_wake", "gpu_sleep", "gpu_idle", "monitor"],
    "Battery": ["plug", "unplug", "session_end", "battery", "high_draw", "high_draw_end"],
    "Daemon": ["daemon_start", "daemon_stop", "config", "config_error"],
}


class PowerPlot(Widget):
    """Watts as vertical bars; bar color = dGPU state (green asleep, yellow awake, red busy)."""
    DEFAULT_CSS = "PowerPlot { height: 14; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.samples, self.t0, self.t1, self.red = [], 0.0, 1.0, 30.0

    def set_data(self, samples, t0, t1, red=30.0):
        self.samples, self.t0, self.t1, self.red = samples, t0, max(t1, t0 + 1), red
        self.refresh()

    def render(self):
        axis = 7
        width, height = max(self.size.width - axis, 10), max(self.size.height - 1, 3)
        cols = [[] for _ in range(width)]
        gpu = [-1] * width
        span = self.t1 - self.t0
        for ts, w, g in self.samples:
            i = int((ts - self.t0) / span * width)
            if 0 <= i < width:
                if w is not None:
                    cols[i].append(w)
                gpu[i] = max(gpu[i], g if g is not None else -1)
        vals = [sum(c) / len(c) if c else None for c in cols]
        top = max([v for v in vals if v is not None] + [self.red]) * 1.1
        out = Text()
        for row in range(height, 0, -1):
            label = f"{top * row / height:5.0f}W " if row in (height, (height + 1) // 2) else " " * axis
            out.append(label[:axis].rjust(axis), style="dim")
            for v, g in zip(vals, gpu):
                if v is None:
                    out.append("·" if row == 1 and g >= 0 else " ", style="dim")
                    continue
                level = v / top * height - (row - 1)
                ch = BLOCKS[8] if level >= 1 else BLOCKS[max(0, int(level * 8))]
                out.append(ch, style=GPU_STYLE[g])
            out.append("\n")
        left, right = fmt.clock(self.t0)[:5], fmt.clock(self.t1)[:5]
        out.append(" " * axis + left + " " * max(width - len(left) - len(right), 1) + right, style="dim")
        return out


def db():
    return store.connect(readonly=True) if os.path.exists(DB_FILE) else None


class BBL(App):
    TITLE = "BetterBatteryLookup"
    CSS = """
    #live-status, #session-info, #battery-info, #gpu-info { padding: 0 1; }
    #live-events, #session-events { padding: 0 1; height: auto; }
    #sessions-table { height: 12; }
    #timeline-table { height: 1fr; }
    Horizontal { height: auto; padding: 0 1; }
    Button { margin-right: 2; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh"), ("g", "gpu_details", "Read dGPU stats"),
                ("1", "tab('live')", "Live"), ("2", "tab('sessions')", "Sessions"),
                ("3", "tab('timeline')", "Timeline"), ("4", "tab('battery')", "Battery"), ("5", "tab('gpu')", "GPU")]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="live"):
            with TabPane("Live", id="live"):
                with VerticalScroll():
                    yield Static(id="live-status")
                    yield PowerPlot(id="live-plot")
                    yield Static(id="live-events")
            with TabPane("Sessions", id="sessions"):
                with VerticalScroll():
                    yield DataTable(id="sessions-table", cursor_type="row")
                    yield Static(id="session-info")
                    yield PowerPlot(id="session-plot")
                    yield Static(id="session-events")
            with TabPane("Timeline", id="timeline"):
                yield Select([(k, k) for k in KIND_GROUPS], value="All", allow_blank=False, id="kind-filter")
                yield DataTable(id="timeline-table", cursor_type="row")
            with TabPane("Battery", id="battery"):
                with VerticalScroll():
                    yield Static(id="battery-info")
                    yield DataTable(id="battery-table")
            with TabPane("GPU", id="gpu"):
                with VerticalScroll():
                    yield Static(id="gpu-info")
                    with Horizontal():
                        yield Button("Hybrid (reboot)", id="mode-hybrid")
                        yield Button("Integrated only (reboot)", id="mode-integrated", variant="warning")
        yield Footer()

    def on_mount(self):
        self.query_one("#sessions-table", DataTable).add_columns(
            "#", "kind", "start", "duration", "battery", "avg W", "dGPU awake", "slept")
        self.query_one("#timeline-table", DataTable).add_columns("time", "#", "kind", "message")
        self.query_one("#battery-table", DataTable).add_columns("session start", "full Wh", "health", "cycles")
        self.state = None
        self.gpu_details = None
        self.action_refresh()
        st = self.state
        self.set_interval(st["interval"] if st else 5, self.refresh_live)

    # ----------------------------------------------------------------- live

    def refresh_live(self):
        self.state = st = call("GetState")
        status = self.query_one("#live-status", Static)
        if not st:
            status.update("[b red]Daemon not running[/] - systemctl --user start betterbatterylookup")
            return
        self.sub_title = status_line(st)
        s = st["session"]
        lines = [f"[b]{status_line(st)}[/]"]
        if st["time_left_s"]:
            lines.append(f"Time left: {fmt.dur(st['time_left_s'])} ({st['time_left_minutes']} min avg)"
                         + (f", {fmt.dur(st['time_left_now_s'])} at current rate" if st["time_left_now_s"] else "")
                         + (f", {fmt.dur(st['time_left_session_s'])} at session avg" if st["time_left_session_s"] else ""))
        if st["has_battery"]:
            lines.append(f"Battery health {fmt.pct(st['health'])}" + (f", {st['cycles']} cycles" if st["cycles"] else ""))
        lines.append(f"Session #{s['id']} ({s['kind']}) since {fmt.clock(s['start'])}: "
                     + (f"avg {s['avg_w']:.1f} W, {s['energy_wh']:.1f} Wh, " if s["kind"] == "battery" else "")
                     + f"dGPU awake {fmt.dur(s['dgpu_awake_s'])}"
                     + (f", slept {fmt.dur(s['sleep_s'])}" if s["sleep_s"] else ""))
        g = st["gpu"]
        if g and g["last_wake_cause"]:
            lines.append(f"Last dGPU wake: {g['last_wake_cause']}")
        status.update("\n".join(lines))
        minutes = st["plot"]["window_minutes"]
        recent = call("GetRecent", minutes, signature="u") or []
        self.query_one("#live-plot", PowerPlot).set_data(
            recent, st["ts"] - minutes * 60, st["ts"], st["thresholds"]["red"])
        self.query_one("#live-events", Static).update(
            "[dim]Recent events[/]\n" + "\n".join(f"{fmt.clock(e['ts'])}  {e['message']}" for e in st["events"]))

    # ------------------------------------------------------------ sessions

    def refresh_sessions(self):
        t = self.query_one("#sessions-table", DataTable)
        bt = self.query_one("#battery-table", DataTable)
        t.clear()
        bt.clear()
        conn = db()
        if not conn:
            return
        for s in session_rows(conn):
            x = session_summary(s)
            bat = (f"{fmt.pct(s['start_pct'])} → {fmt.pct(s['end_pct']) if s['end'] else 'now'}"
                   if s["kind"] == "battery" else "")
            t.add_row(str(s["id"]), s["kind"], fmt.clock(s["start"], True), fmt.dur(x["duration"]), bat,
                      f"{x['avg_w']:.1f}" if x["avg_w"] else "", fmt.dur(s["dgpu_awake_s"]),
                      fmt.dur(s["sleep_s"]) if s["sleep_s"] else "", key=str(s["id"]))
            if s["kind"] == "battery" and s["full_wh"]:
                health = f"{100 * s['full_wh'] / s['design_wh']:.1f}%" if s["design_wh"] else ""
                bt.add_row(fmt.clock(s["start"], True), f"{s['full_wh']:.1f}", health, str(s["cycles"] or ""))
        conn.close()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table.id != "sessions-table" or event.row_key is None:
            return
        sid = int(event.row_key.value)
        conn = db()
        s = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        samples = [(r[0], r[1], r[2]) for r in
                   conn.execute("SELECT ts, watts, gpu FROM samples WHERE session=? ORDER BY ts", (sid,))]
        events = conn.execute("SELECT ts, message FROM events WHERE session=? ORDER BY ts, id", (sid,)).fetchall()
        conn.close()
        x = session_summary(s)
        end = s["end"] or (samples[-1][0] if samples else time.time())
        info = (f"[b]Session #{sid}[/] {s['kind']}  {fmt.clock(s['start'], True)} – "
                f"{fmt.clock(s['end'], True) if s['end'] else 'now'}  ({fmt.dur(x['duration'])})")
        if s["kind"] == "battery":
            info += (f"\n{fmt.pct(s['start_pct'])} → {fmt.pct(s['end_pct'])}"
                     + (f" (−{x['drop']:.0f}%)" if x["drop"] is not None else "")
                     + f", {s['energy_wh'] or 0:.1f} Wh, avg {x['avg_w'] or 0:.1f} W")
        info += f"\ndGPU awake {fmt.dur(s['dgpu_awake_s'])}" + (f", slept {fmt.dur(s['sleep_s'])}" if s["sleep_s"] else "")
        self.query_one("#session-info", Static).update(info)
        red = self.state["thresholds"]["red"] if self.state else 30.0
        self.query_one("#session-plot", PowerPlot).set_data(samples, s["start"], end, red)
        self.query_one("#session-events", Static).update(
            "[dim]Events[/]\n" + "\n".join(f"{fmt.clock(e['ts'])}  {e['message']}" for e in events))

    # ------------------------------------------------------------ timeline

    def refresh_timeline(self):
        t = self.query_one("#timeline-table", DataTable)
        t.clear()
        conn = db()
        if not conn:
            return
        kinds = KIND_GROUPS[self.query_one("#kind-filter", Select).value]
        q, p = "SELECT ts, session, kind, message FROM events", []
        if kinds:
            q += f" WHERE kind IN ({','.join('?' * len(kinds))})"
            p = kinds
        for r in conn.execute(q + " ORDER BY ts DESC, id DESC LIMIT 2000", p):
            t.add_row(fmt.clock(r["ts"], True), str(r["session"] or ""), r["kind"], r["message"])
        conn.close()

    def on_select_changed(self, event: Select.Changed):
        self.refresh_timeline()

    # ------------------------------------------------------------- battery/gpu

    def refresh_static(self):
        st = self.state
        b = self.query_one("#battery-info", Static)
        if st and st["has_battery"]:
            b.update(f"[b]Battery[/] {fmt.pct(st['percent'])} {st['status']}\n"
                     f"Health {fmt.pct(st['health'])} (full capacity vs design)"
                     + (f", {st['cycles']} charge cycles" if st["cycles"] else ", cycle count not reported")
                     + "\n\n[dim]Capacity per battery session:[/]")
        else:
            b.update("No battery found." if st else "Daemon not running.")
        self.refresh_gpu()

    def refresh_gpu(self):
        st, d = self.state, self.gpu_details
        m = gpumode.status()
        lines = []
        if st and st["gpu"]:
            g = st["gpu"]
            state = "busy" if g["busy"] else ("awake" if g["awake"] else "asleep")
            lines += [f"[b]{g['name']}[/]  {g['pci']}", f"State: {state} for {fmt.dur(st['ts'] - g['since'])} "
                      f"(runtime PM: {g['status']})"]
            if g["last_wake_cause"]:
                lines.append(f"Last wake: {g['last_wake_cause']}")
        elif st:
            lines.append("No discrete GPU found (or it is switched off: integrated-only mode).")
        if d:
            n = d.get("nvml")
            if n:
                lines.append(f"NVIDIA: {n.get('pstate', '')} {n.get('power_w', 0):.1f} W, {n.get('util', '?')}% util, "
                             f"{n.get('temp_c', '?')} °C")
                for p in n["procs"]:
                    lines.append(f"  rendering: {p['name']} ({p['pid']})"
                                 + (f" {p['vram_mib']} MiB" if p["vram_mib"] is not None else ""))
            elif st and st["gpu"] and not st["gpu"]["awake"]:
                lines.append("[dim]dGPU asleep - NVIDIA stats not read (reading would wake it).[/]")
            for pid, name in d.get("holders", {}).items():
                lines.append(f"  has it open: {name} ({pid})")
            if d.get("holders") is not None:
                lines.append("[dim]  privileged processes (kwin, btop) are not visible to a normal user[/]")
        else:
            lines.append("[dim]Press g to read dGPU users (NVIDIA stats only if it is already awake).[/]")
        lines += ["", f"[b]GPU mode[/] configured: {m['configured']}, running: {m['running']}"
                  + ("  [yellow]reboot to apply[/]" if m["pending_reboot"] else "")]
        if not m["helper_installed"]:
            lines.append(f"[dim]Switching needs the root helper ({GPUMODE_HELPER}): ./install.sh --system[/]")
        lines.append("[dim]Integrated only: dGPU fully off, nothing can wake it; its display ports stop working.[/]")
        self.query_one("#gpu-info", Static).update("\n".join(lines))
        self.query_one("#mode-hybrid", Button).disabled = not m["helper_installed"] or m["configured"] == "hybrid"
        self.query_one("#mode-integrated", Button).disabled = (not m["helper_installed"]
                                                               or m["configured"] == "integrated")

    def on_button_pressed(self, event: Button.Pressed):
        mode = event.button.id.removeprefix("mode-")
        with self.suspend():
            print(f"Switching GPU mode to {mode} (needs your password)...\n")
            r = subprocess.run(["pkexec", GPUMODE_HELPER, mode])
            input(f"\n{'Done - reboot to apply.' if r.returncode == 0 else 'Failed.'} Press Enter to return.")
        self.refresh_gpu()

    # -------------------------------------------------------------- actions

    def action_refresh(self):
        self.refresh_live()
        self.refresh_sessions()
        self.refresh_timeline()
        self.refresh_static()

    def action_gpu_details(self):
        self.gpu_details = call("GetGpuDetails")
        self.refresh_gpu()
        self.query_one(TabbedContent).active = "gpu"

    def action_tab(self, tab):
        self.query_one(TabbedContent).active = tab


def main():
    BBL().run()
