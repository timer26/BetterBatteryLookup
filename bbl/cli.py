"""`bbl` command line: status, log, sessions, export, gpu-mode, daemon, tui."""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time

from . import DBUS_IFACE, DBUS_NAME, DBUS_PATH, VERSION, fmt, gpumode, store
from .paths import CONFIG_FILE, DB_FILE

GPU_NAMES = {-1: "-", 0: "asleep", 1: "awake", 2: "busy"}


def call(method, *args, signature=""):
    """Call the daemon over D-Bus. Returns decoded JSON or None if it is not running."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        params = GLib.Variant(f"({signature})", args) if signature else None
        res = bus.call_sync(DBUS_NAME, DBUS_PATH, DBUS_IFACE, method, params,
                            GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 3000, None)
        return json.loads(res.unpack()[0])
    except GLib.Error:
        return None


def open_db():
    if not os.path.exists(DB_FILE):
        sys.exit(f"no history yet ({DB_FILE}) - is the daemon installed and running?")
    return store.connect(readonly=True)


def status_line(st):
    if st is None:
        return "BetterBatteryLookup: daemon not running"
    parts = [st["level"].upper()]
    if st["has_battery"]:
        parts.append(f"{fmt.pct(st['percent'])} {st['status']}")
        parts.append("on AC" if st["on_ac"] else fmt.watts(st["watts"]))
        if st["time_left_s"]:
            parts.append(f"{fmt.dur(st['time_left_s'])} left")
    g = st["gpu"]
    if g:
        state = "busy" if g["busy"] else ("awake" if g["awake"] else "asleep")
        parts.append(f"dGPU {state} {fmt.dur(st['ts'] - g['since'])}")
    return " | ".join(parts)


# ------------------------------------------------------------------- commands

def cmd_status(args):
    st = call("GetState")
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        print(status_line(st))
    return 0 if st else 1


def _parse_since(text):
    """'today', '2h', '30m', '14:05' or 'YYYY-MM-DD'."""
    now = time.time()
    if text == "today":
        return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    if text[-1] in "hmd" and text[:-1].replace(".", "").isdigit():
        return now - float(text[:-1]) * {"h": 3600, "m": 60, "d": 86400}[text[-1]]
    for f in ("%H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            t = time.strptime(text, f)
        except ValueError:
            continue
        if f == "%H:%M":
            lt = time.localtime(now)
            return time.mktime(lt[:3] + (t.tm_hour, t.tm_min, 0, 0, 0, -1))
        return time.mktime(t)
    sys.exit(f"cannot parse time {text!r} (use today, 2h, 30m, 14:05, 2026-11-16)")


def cmd_log(args):
    db = open_db()
    q, p = "SELECT ts, session, kind, message FROM events WHERE 1", []
    if args.since:
        q += " AND ts >= ?"
        p.append(_parse_since(args.since))
    if args.session:
        q += " AND session = ?"
        p.append(args.session)
    if args.kind:
        kinds = args.kind.split(",")
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        p += kinds
    rows = db.execute(q + " ORDER BY ts DESC, id DESC LIMIT ?", (*p, args.n)).fetchall()
    last_day = None
    for r in reversed(rows):
        day = time.strftime("%a %Y-%m-%d", time.localtime(r["ts"]))
        if day != last_day:
            print(f"\n== {day} ==")
            last_day = day
        print(f"{fmt.clock(r['ts'])}  #{r['session'] or '-':<4} {r['kind']:<13} {r['message']}")
    return 0


def session_rows(db):
    return db.execute("SELECT * FROM sessions ORDER BY start DESC").fetchall()


def session_summary(s, now=None):
    end = s["end"] or s["last_ts"] or now or time.time()
    awake = max(end - s["start"] - (s["sleep_s"] or 0), 1)
    return {
        "duration": end - s["start"],
        "drop": (s["start_pct"] - s["end_pct"]) if (s["start_pct"] is not None and s["end_pct"] is not None) else None,
        "avg_w": (s["energy_wh"] or 0) * 3600 / awake if s["kind"] == "battery" else None,
    }


def cmd_sessions(args):
    db = open_db()
    print(f"{'#':>4}  {'kind':<7} {'start':<16} {'duration':>10} {'battery':>12} {'avg W':>6} {'dGPU awake':>11} {'slept':>9}")
    for s in session_rows(db)[:args.n]:
        x = session_summary(s)
        bat = f"{fmt.pct(s['start_pct'])}->{fmt.pct(s['end_pct']) if s['end'] else 'now'}" if s["kind"] == "battery" else ""
        avg = f"{x['avg_w']:.1f}" if x["avg_w"] else ""
        print(f"{s['id']:>4}  {s['kind']:<7} {fmt.clock(s['start'], True):<16} {fmt.dur(x['duration']):>10} "
              f"{bat:>12} {avg:>6} "
              f"{fmt.dur(s['dgpu_awake_s']):>11} {fmt.dur(s['sleep_s']) if s['sleep_s'] else '':>9}")
    return 0


def cmd_export(args):
    db = open_db()
    rows = db.execute("SELECT ts, watts, pct, gpu, dgpu_w FROM samples WHERE session=? ORDER BY ts",
                      (args.session,)).fetchall()
    if not rows:
        sys.exit(f"no samples for session {args.session} (samples are written every flush_seconds)")
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    w = csv.writer(out)
    w.writerow(["time", "unix_ts", "watts", "battery_pct", "dgpu", "dgpu_watts"])
    for r in rows:
        w.writerow([fmt.clock(r["ts"], True) + time.strftime(":%S", time.localtime(r["ts"])), f"{r['ts']:.1f}",
                    r["watts"], r["pct"] and round(r["pct"], 2), GPU_NAMES.get(r["gpu"], r["gpu"]), r["dgpu_w"]])
    if args.output:
        out.close()
        print(f"wrote {len(rows)} samples to {args.output}")
    return 0


def cmd_gpu_mode(args):
    st = gpumode.status()
    if args.mode == "status":
        print(f"configured: {st['configured']}  running: {st['running']}"
              + ("  (reboot to apply)" if st["pending_reboot"] else ""))
        if not st["helper_installed"]:
            print("note: switching needs the root helper - run ./install.sh --system")
        return 0
    ok, out = gpumode.switch(args.mode)
    print(out)
    return 0 if ok else 1


def cmd_config(args):
    from . import config
    if args.path:
        print(CONFIG_FILE)
        return 0
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            f.write(config.DEFAULT_TOML)
    if not args.check:
        if sys.stdin.isatty():
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
            subprocess.run([editor, CONFIG_FILE])
        else:  # started from the panel widget: no terminal, use the desktop's text editor
            subprocess.Popen(["xdg-open", CONFIG_FILE], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
    try:
        config.load()
    except ValueError as e:
        print(f"config has errors (the daemon keeps its previous settings): {e}")
        return 1
    print(f"config OK - {CONFIG_FILE} (the daemon applies changes automatically)")
    return 0


def cmd_daemon(args):
    from . import daemon
    daemon.main(notify=not args.no_notify)
    return 0


def cmd_tui(args):
    try:
        from . import tui
    except ImportError as e:
        sys.exit(f"the TUI needs Textual ({e.name}): install python-textual")
    tui.main()
    return 0


TERMINALS = [["konsole", "-e"], ["xdg-terminal-exec"], ["gnome-terminal", "--"], ["kitty"],
             ["alacritty", "-e"], ["foot"], ["xterm", "-e"]]


def cmd_open_tui(args):
    """Open the TUI in a new terminal window (used by the panel widget)."""
    me = os.path.realpath(sys.argv[0])
    for term in TERMINALS:
        if shutil.which(term[0]):
            subprocess.Popen(term + [me, "tui"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
    sys.exit("no terminal emulator found")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bbl", description="BetterBatteryLookup - battery and dGPU power monitor")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="one-line current status")
    p.add_argument("--json", action="store_true", help="full daemon state as JSON")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("log", help="event timeline")
    p.add_argument("--since", help="today, 2h, 30m, 14:05, 2026-11-16")
    p.add_argument("--today", dest="since", action="store_const", const="today")
    p.add_argument("--session", type=int, help="only this session")
    p.add_argument("--kind", help="comma-separated: gpu_wake,suspend,resume,...")
    p.add_argument("-n", type=int, default=200, help="max events (default 200)")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("sessions", help="list saved sessions")
    p.add_argument("-n", type=int, default=30)
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("export", help="export a session's samples as CSV")
    p.add_argument("session", type=int)
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("gpu-mode", help="show or switch dGPU mode (switching needs a reboot)")
    p.add_argument("mode", nargs="?", default="status", choices=["status", "hybrid", "integrated"])
    p.set_defaults(fn=cmd_gpu_mode)

    p = sub.add_parser("config", help="edit the config file ($EDITOR), then validate it")
    p.add_argument("--path", action="store_true", help="only print the config file path")
    p.add_argument("--check", action="store_true", help="only validate, do not open an editor")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("daemon", help="run the background service (normally started by systemd)")
    p.add_argument("--no-notify", action="store_true", help="do not send desktop notifications")
    p.set_defaults(fn=cmd_daemon)

    sub.add_parser("tui", help="interactive terminal UI").set_defaults(fn=cmd_tui)
    sub.add_parser("open-tui", help="open the TUI in a new terminal window").set_defaults(fn=cmd_open_tui)

    args = ap.parse_args(argv)
    if not args.cmd:
        args.fn = cmd_tui if sys.stdout.isatty() else cmd_status
        args.json = False
    return args.fn(args)
