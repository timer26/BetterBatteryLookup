"""The background service: samples power and dGPU state, tracks sessions,
records events, sends notifications and serves state over D-Bus."""
import json
import logging
import os
import signal
import time
from collections import deque

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

try:
    gi.require_version("GUdev", "1.0")
    from gi.repository import GUdev  # noqa: E402
except (ImportError, ValueError):
    GUdev = None

from . import APP_NAME, DBUS_IFACE, DBUS_NAME, DBUS_PATH, VERSION, config, fmt, gpumode, nvml, sensors  # noqa: E402
from .paths import CONFIG_FILE  # noqa: E402
from .store import GPU_CODES, Store  # noqa: E402

log = logging.getLogger("bbl")

LOGIND = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
COMPOSITORS = {"kwin_wayland", "Xwayland", "Xorg", "gnome-shell", "sway", "Hyprland", "mutter"}

DBUS_XML = f"""
<node>
  <interface name="{DBUS_IFACE}">
    <method name="GetState"><arg type="s" direction="out"/></method>
    <method name="GetXfcePanel"><arg type="s" direction="out"/></method>
    <method name="GetRecent"><arg name="minutes" type="u" direction="in"/><arg type="s" direction="out"/></method>
    <method name="GetGpuDetails"><arg type="s" direction="out"/></method>
  </interface>
</node>
"""


class Daemon:
    def __init__(self, notify=True):
        self.notify_enabled = notify
        self.cfg = config.load()
        self.store = Store()
        self.batteries = sensors.batteries()
        self.chargers = sensors.ac_supplies()
        # charger "online" and lid state are ACPI calls (~1 ms each, EC traffic): AC comes
        # from udev events with a slow fallback poll, the lid is polled slowly
        self.ac = sensors.on_ac(self.chargers)
        self.slow_polls = {"ac": 0.0, "lid": 0.0}
        self.gpu = sensors.find_dgpu()
        self.procs = sensors.ProcTracker()
        self.boot_id = sensors.boot_id()
        self.loop = GLib.MainLoop()
        self.timers = []
        self.events = deque(maxlen=20)
        self.inhibit_fd = None
        self.stopping = False

        now = time.time()
        self.bat = sensors.read_battery(self.batteries)
        self.last_tick = now
        self.last_pct = self.bat and self.bat["percent"]
        self.last_susp = sensors.suspended_seconds()
        self.pre_sleep = None  # (ts, pct, kind) recorded at PrepareForSleep
        self.lid = sensors.lid_closed()
        self.locked = None
        self.connected = sensors.connectors()
        self.watts = None

        self.gpu_name = self.gpu.name() if self.gpu else None
        self.gpu_status = self.gpu.runtime_status() if self.gpu else None
        self.gpu_awake = self._awake(self.gpu_status)
        self.gpu_since = now
        self.gpu_busy = False
        self.gpu_info = {}  # last NVML read
        self.nvml_last = 0.0
        self.idle_notified = False
        self.last_wake_cause = None

        self.high_since = None
        self.high_below_since = None
        self.high_peak = 0.0
        self.high_logged = False

        self._make_ring()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _awake(status):
        return status is not None and status not in ("suspended", "gone")

    def _make_ring(self):
        n = int(self.cfg["plot"]["window_minutes"] * 60 / self.cfg["sampling"]["interval_seconds"]) + 1
        old = list(getattr(self, "ring", []))
        self.ring = deque(old[-n:], maxlen=n)

    def _kind(self, ac):
        return "battery" if (self.bat and not ac) else "ac"

    def event(self, kind, message, data=None, ts=None):
        ts = ts or time.time()
        log.info("[%s] %s", kind, message)
        self.store.add_event(ts, self.session_id, kind, message, data)
        self.events.appendleft({"ts": ts, "kind": kind, "message": message})

    def notify(self, summary, body, icon="battery"):
        if not self.notify_enabled:
            return
        self.session_bus.call(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            "Notify", GLib.Variant("(susssasa{sv}i)", (APP_NAME, 0, icon, summary, body, [], {}, -1)),
            None, Gio.DBusCallFlags.NONE, -1, None, None, None)

    def _avg_watts(self, seconds):
        cutoff = time.time() - seconds
        ws = [w for ts, w, _ in self.ring if ts >= cutoff and w]
        return sum(ws) / len(ws) if ws else None

    # ----------------------------------------------------------------- startup

    def _startup_records(self):
        now = time.time()
        interval = self.cfg["sampling"]["interval_seconds"]
        last_boot = self.store.get_meta("boot_id")
        last_ev = self.store.last_event()
        last_ts = max(filter(None, [self.store.last_sample_ts(), last_ev and last_ev["ts"]]), default=None)
        cur = self.store.current_session()
        kind = self._kind(sensors.on_ac())
        self.session_id = cur["id"] if cur else None

        if last_boot != self.boot_id:
            if last_boot and last_ev and last_ev["kind"] != "shutdown":
                self.event("unclean", "Previous boot ended without a clean shutdown (crash, forced power-off "
                           f"or empty battery?) - last data at {fmt.clock(last_ts, True)}", ts=last_ts)
            if cur:
                self.store.close_session(cur["id"], cur["last_ts"], None)
                self.session_id = None
            self.store.set_meta("boot_id", self.boot_id)
            self._open_session(kind, self.bat, announce=False)
            where = f"battery {fmt.pct(self.bat['percent'])}" if kind == "battery" else "on AC"
            self.event("boot", f"Boot - {where}", ts=sensors.boot_time())
            return

        gap = now - last_ts if last_ts else 0
        min_gap = max(3 * interval, 60)
        if last_ev and last_ev["kind"] not in ("daemon_stop", "shutdown") and gap > min_gap:
            self.event("gap", f"Gap {fmt.clock(last_ts)}-{fmt.clock(now)} ({fmt.dur(gap)}): daemon was not "
                       "running and did not stop cleanly (crashed or killed?)")
        elif gap > min_gap:
            self.event("daemon_start", f"Daemon started - no data {fmt.clock(last_ts)}-{fmt.clock(now)} "
                       f"({fmt.dur(gap)})")
        else:
            self.event("daemon_start", "Daemon started")
        if cur and cur["kind"] == kind:
            self.energy_wh, self.dgpu_awake_s, self.sleep_s = cur["energy_wh"], cur["dgpu_awake_s"], cur["sleep_s"]
            self.session = dict(cur)
        else:
            if cur:
                self.store.close_session(cur["id"], cur["last_ts"], None)
            self._open_session(kind, self.bat, announce=False)

    def _open_session(self, kind, bat, announce=True):
        now = time.time()
        pct = bat and bat["percent"]
        self.session_id = self.store.open_session(kind, self.boot_id, now, pct, bat)
        self.session = {"id": self.session_id, "kind": kind, "start": now, "start_pct": pct}
        self.energy_wh = self.dgpu_awake_s = self.sleep_s = 0.0
        if announce and kind == "battery":
            self.event("unplug", f"Unplugged - battery session #{self.session_id} started at {fmt.pct(pct)}")
        elif announce:
            self.event("plug", "Plugged in")

    def _close_session(self, now, bat):
        s = self.session
        pct = bat and bat["percent"]
        self.store.close_session(s["id"], now, pct, energy_wh=self.energy_wh,
                                 dgpu_awake_s=self.dgpu_awake_s, sleep_s=self.sleep_s, last_ts=now)
        if s["kind"] != "battery":
            return
        awake_time = max(now - s["start"] - self.sleep_s, 1)
        avg = self.energy_wh * 3600 / awake_time
        drop = (s["start_pct"] or 0) - (pct or 0)
        body = (f"{fmt.dur(now - s['start'])}, {fmt.pct(s['start_pct'])} -> {fmt.pct(pct)} (-{drop:.0f}%), "
                f"avg {avg:.1f} W, dGPU awake {fmt.dur(self.dgpu_awake_s)}")
        if self.sleep_s:
            body += f", slept {fmt.dur(self.sleep_s)}"
        self.event("session_end", f"Battery session #{s['id']} ended: {body}",
                   {"avg_w": avg, "drop_pct": drop, "energy_wh": self.energy_wh})
        if self.cfg["notify"]["session_summary"] and not self.stopping:
            self.notify(f"Battery session #{s['id']} ended", body)
        removed = self.store.trim(self.cfg["history"]["keep_sessions"])
        if removed:
            log.info("trimmed %d old sessions", removed)

    # ------------------------------------------------------------- the sampler

    def tick(self):
        now = time.time()
        self._check_resume()
        self.procs.update(now)
        if now - self.slow_polls["ac"] >= 30:
            self.slow_polls["ac"] = now
            self.ac = sensors.on_ac(self.chargers)
        ac = self.ac
        self.bat = bat = sensors.read_battery(self.batteries)
        kind = self._kind(ac)
        if kind != self.session["kind"]:
            self._close_session(now, bat)
            self._open_session(kind, bat)
        dt = min(now - self.last_tick, 3 * self.cfg["sampling"]["interval_seconds"])
        self.last_tick = now

        pct = bat and bat["percent"]
        self.watts = watts = bat["watts"] if (kind == "battery" and bat["watts"] > 0) else None
        if watts:
            self.energy_wh += watts * dt / 3600

        gpu_code = self._tick_gpu(now, dt)
        self._tick_high_draw(now, watts)
        self._tick_milestones(kind, pct)
        if now - self.slow_polls["lid"] >= 15:
            self.slow_polls["lid"] = now
            self._tick_lid()

        self.ring.append((now, watts, gpu_code))
        self.store.add_sample(now, self.session_id, watts, pct, gpu_code, self.gpu_info.get("power_w"))
        self.last_pct = pct
        return True

    def _tick_gpu(self, now, dt):
        if not self.gpu:
            return -1
        self.gpu_status = status = self.gpu.runtime_status()
        awake = self._awake(status)
        if awake and not self.gpu_awake:
            self._on_gpu_wake(now)
        elif not awake and self.gpu_awake:
            self.event("gpu_sleep", f"dGPU asleep - was awake {fmt.dur(now - self.gpu_since)}")
            self.gpu_since = now
            self.gpu_busy = False
            self.gpu_info = {}
        self.gpu_awake = awake
        if not awake:
            return GPU_CODES["suspended"]

        self.dgpu_awake_s += dt
        poll = self.cfg["sampling"]["nvml_poll_seconds"]
        if poll and status == "active" and now - self.nvml_last >= poll:
            self.nvml_last = now
            info = nvml.query(self.gpu)
            if info is not None:
                self.gpu_info = info
                self.gpu_busy = info.get("util", 0) >= self.cfg["levels"]["gpu_busy_percent"]

        idle_min = self.cfg["notify"]["dgpu_idle_minutes"]
        if idle_min and not self.idle_notified and not self.gpu_busy and now - self.gpu_since >= idle_min * 60:
            self.idle_notified = True
            users = self._gpu_users_text()
            power = f", drawing {self.gpu_info['power_w']:.0f} W" if "power_w" in self.gpu_info else ""
            idle = " without working" if "util" in self.gpu_info else ""
            msg = f"dGPU awake for {fmt.dur(now - self.gpu_since)}{idle}{power}."
            if self.last_wake_cause:
                msg += f" Woke up because of: {self.last_wake_cause}."
            if users:
                msg += f" Using it now: {users}."
            self.event("gpu_idle", msg)
            self.notify("dGPU is awake but idle", msg, icon="video-display")
        return GPU_CODES["busy" if self.gpu_busy else "active"]

    def _on_gpu_wake(self, now):
        window = self.cfg["sampling"]["interval_seconds"] + 5
        started = self.procs.started_between(now - window, now)
        seen, names = set(), []
        for ts, pid, name in reversed(started):
            if name not in seen and pid != os.getpid():
                seen.add(name)
                names.append(f"{name} (started {now - ts:.0f} s before)")
        self.last_wake_cause = ", ".join(names[:3]) if names else None
        cause = (f"likely {self.last_wake_cause}" if names else
                 "cause unknown - an app that was already running used it")
        self.event("gpu_wake", f"dGPU woke up - {cause}",
                   {"started": [[ts, pid, name] for ts, pid, name in started]})
        self.gpu_since = now
        self.idle_notified = False
        # first NVML read only after nvml_poll_seconds, so short wakes are not extended
        self.nvml_last = now

    def _gpu_users_text(self):
        procs = self.gpu_info.get("procs", {})
        names = [sensors.comm(pid) for pid in procs]
        return ", ".join(n for n in names if n not in COMPOSITORS)

    def _tick_high_draw(self, now, watts):
        red = self.cfg["levels"]["red_watts"]
        if watts and watts >= red:
            self.high_below_since = None
            if self.high_since is None:
                self.high_since, self.high_peak, self.high_logged = now, watts, False
            self.high_peak = max(self.high_peak, watts)
            if not self.high_logged and now - self.high_since >= self.cfg["events"]["high_draw_seconds"]:
                self.high_logged = True
                before = sensors.cpu_times()
                since = self.high_since

                def finish():
                    top = sensors.top_cpu(before, sensors.cpu_times(), 2)
                    procs = ", ".join(f"{n} {p:.0f}%" for n, p in top) or "nothing notable"
                    self.event("high_draw", f"High draw: above {red:.0f} W for {fmt.dur(time.time() - since)} "
                               f"(now {watts:.1f} W) - top CPU: {procs}", {"top": top})
                    return False
                GLib.timeout_add_seconds(2, finish)
        elif self.high_since is not None:
            self.high_below_since = self.high_below_since or now
            if now - self.high_below_since >= 10:
                if self.high_logged:
                    self.event("high_draw_end", f"High draw ended after {fmt.dur(self.high_below_since - self.high_since)}"
                               f" (peak {self.high_peak:.1f} W)")
                self.high_since = self.high_below_since = None

    def _tick_milestones(self, kind, pct):
        if kind != "battery" or pct is None or self.last_pct is None:
            return
        for m in sorted(self.cfg["events"]["battery_milestones"], reverse=True):
            if self.last_pct > m >= pct:
                avg = self._avg_watts(self.cfg["estimate"]["time_left_minutes"] * 60)
                left = f" - about {fmt.dur(self.bat['now_wh'] / avg * 3600)} left at {avg:.1f} W" if avg else ""
                self.event("battery", f"Battery {m}%{left}")

    def _tick_lid(self):
        lid = sensors.lid_closed()
        if lid is not None and lid != self.lid:
            self.event("lid", "Lid closed" if lid else "Lid opened")
        self.lid = lid

    # ---------------------------------------------------------- sleep/shutdown

    def _inhibit(self):
        if self.inhibit_fd is not None:
            return
        try:
            res, fds = self.system_bus.call_with_unix_fd_list_sync(
                *LOGIND, "Inhibit",
                GLib.Variant("(ssss)", ("sleep:shutdown", APP_NAME, "Record battery state", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
            self.inhibit_fd = fds.get(res.unpack()[0])
        except GLib.Error as e:
            log.warning("could not take sleep inhibitor: %s", e.message)

    def _release_inhibit(self):
        if self.inhibit_fd is not None:
            os.close(self.inhibit_fd)
            self.inhibit_fd = None

    def _on_prepare_sleep(self, *args):
        going = args[5].unpack()[0]
        if going:
            self._tick_lid()
            pct = self.bat and self.bat["percent"]
            self.pre_sleep = (time.time(), pct, self.session["kind"])
            where = f"battery {fmt.pct(pct)}" if self.session["kind"] == "battery" else "on AC"
            self.event("suspend", f"Suspend - {where}" + (", lid closed" if self.lid else ""))
            self.store.flush()
            self._release_inhibit()
        else:
            self._check_resume()
            self._inhibit()

    def _check_resume(self):
        """Detect system sleep from the BOOTTIME/MONOTONIC difference; works even
        if the PrepareForSleep signal was missed."""
        susp = sensors.suspended_seconds()
        slept = susp - self.last_susp
        if slept < 1:
            return
        self.last_susp = susp
        self.sleep_s += slept
        bat = sensors.read_battery(self.batteries)
        t0, pct0, kind0 = self.pre_sleep or (self.last_tick, self.last_pct, self.session["kind"])
        self.pre_sleep = None
        msg = f"Resume - slept {fmt.dur(slept)}"
        if kind0 == "battery" and not sensors.on_ac() and bat and pct0 is not None:
            drop = pct0 - bat["percent"]
            wh = drop / 100 * bat["full_wh"]
            msg += f", lost {drop:.1f}% ({wh:.1f} Wh, about {wh / (slept / 3600):.2f} W while asleep)"
            if self.cfg["notify"]["sleep_drain"] and drop >= 1:
                self.notify("Battery drain during sleep", msg.replace("Resume - ", ""))
        self.event("resume", msg, {"slept_s": slept})
        self.high_since = None
        self.last_tick = time.time()

    def _on_prepare_shutdown(self, *args):
        if not args[5].unpack()[0]:
            return
        now = time.time()
        pct = self.bat and self.bat["percent"]
        self.stopping = True
        self.event("shutdown", "Shutdown or reboot" + (f" - battery {fmt.pct(pct)}" if self.bat else ""))
        self._close_session(now, self.bat)
        self.store.flush()
        self._release_inhibit()

    def _on_screensaver(self, *args):
        locked = args[5].unpack()[0]
        if locked != self.locked:
            self.locked = locked
            self.event("lock", "Screen locked" if locked else "Screen unlocked")

    def _on_uevent(self, client, action, device):
        if device.get_subsystem() == "power_supply":
            if action in ("add", "remove"):
                self.batteries = sensors.batteries()
                self.chargers = sensors.ac_supplies()
            if action in ("add", "remove") or device.get_name() in {os.path.basename(c) for c in self.chargers}:
                ac = sensors.on_ac(self.chargers)
                if ac != self.ac:
                    self.ac = ac
                    self.tick()  # record the plug/unplug right away
            return
        now = sensors.connectors()
        gpu_card = self.gpu and self.gpu.card
        for conn in now.keys() - self.connected.keys():
            port = " (dGPU port - keeps the dGPU awake)" if now[conn] == gpu_card else ""
            self.event("monitor", f"Monitor connected: {conn}{port}")
        for conn in self.connected.keys() - now.keys():
            self.event("monitor", f"Monitor disconnected: {conn}")
        self.connected = now

    def _on_config_changed(self, monitor, file, other, event_type):
        # editors either rewrite the file or rename a new one into place
        if event_type not in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            return
        try:
            new = config.load()
        except (ValueError, OSError) as e:  # includes TOML syntax errors
            self.event("config_error", f"Config not applied, keeping previous settings: {e}")
            return
        if new == self.cfg:
            return
        timing = new["sampling"] != self.cfg["sampling"]
        self.cfg = new
        self._make_ring()
        if timing:
            self._start_timers()
        self.event("config", "Config reloaded")

    # -------------------------------------------------------------------- D-Bus

    def state(self):
        bat = self.bat
        level, reasons = self._level()
        s = self.session
        awake_time = max(time.time() - s["start"] - self.sleep_s, 1)
        on_bat = bool(bat) and s["kind"] == "battery"

        def left(w):
            return bat["now_wh"] / w * 3600 if (on_bat and w) else None
        session_avg = self.energy_wh * 3600 / awake_time if awake_time >= 300 else None
        tip_cutoff = time.time() - self.cfg["plot"]["tooltip_minutes"] * 60
        return {
            "version": VERSION, "ts": time.time(), "interval": self.cfg["sampling"]["interval_seconds"],
            "has_battery": bool(bat), "on_ac": s["kind"] == "ac", "watts": self.watts,
            "percent": bat and bat["percent"], "status": bat and bat["status"],
            "health": bat and bat["health"], "cycles": bat and bat["cycles"],
            # main estimate (configurable window), current rate (like KDE), whole session average
            "time_left_s": left(self._avg_watts(self.cfg["estimate"]["time_left_minutes"] * 60)),
            "time_left_now_s": left(self._avg_watts(30)),
            "time_left_session_s": left(session_avg),
            "time_left_minutes": self.cfg["estimate"]["time_left_minutes"],
            "level": level, "level_reasons": reasons,
            "gpu": self.gpu and {
                "name": self.gpu_name, "pci": self.gpu.pci, "vendor": self.gpu.vendor,
                "status": self.gpu_status, "awake": self.gpu_awake, "busy": self.gpu_busy,
                "since": self.gpu_since, "util": self.gpu_info.get("util"),
                "power_w": self.gpu_info.get("power_w"), "last_wake_cause": self.last_wake_cause},
            "session": {"id": s["id"], "kind": s["kind"], "start": s["start"], "start_pct": s["start_pct"],
                        "energy_wh": self.energy_wh, "avg_w": self.energy_wh * 3600 / awake_time,
                        "dgpu_awake_s": self.dgpu_awake_s, "sleep_s": self.sleep_s},
            "recent": [[round(ts, 1), w and round(w, 2), g] for ts, w, g in self.ring if ts >= tip_cutoff],
            "events": list(self.events)[:5],
            "panel": self.cfg["panel"], "plot": self.cfg["plot"],
            "thresholds": {"yellow": self.cfg["levels"]["yellow_watts"], "red": self.cfg["levels"]["red_watts"]},
            "gpu_mode": self._gpu_mode(),
        }

    def _gpu_mode(self):
        now = time.time()
        if now - getattr(self, "_mode_ts", 0) > 10:
            self._mode, self._mode_ts = gpumode.status(), now
        return self._mode

    def _level(self):
        order = ["green", "yellow", "red"]
        lv = self.cfg["levels"]
        level, reasons = "green", []
        if self.watts is not None:
            if self.watts >= lv["red_watts"]:
                level = "red"
            elif self.watts >= lv["yellow_watts"]:
                level = "yellow"
            if level != "green":
                reasons.append(f"draw {self.watts:.1f} W")
        if self.gpu_awake:
            g = lv["dgpu_busy"] if self.gpu_busy else lv["dgpu_awake_idle"]
            if g != "green":
                reasons.append("dGPU busy" if self.gpu_busy else "dGPU awake")
            level = max(level, g, key=order.index)
        if self.session["kind"] == "ac" and level == "green":
            level = "grey"
        return level, reasons

    def gpu_details(self):
        d = {"gpu": self.state()["gpu"], "mode": self._gpu_mode()}
        if self.gpu:
            d["holders"] = {str(pid): name for pid, name in sensors.gpu_holders(self.gpu).items()}
            info = nvml.query(self.gpu) if self.gpu_awake else None
            if info:
                self.gpu_info = info
                d["nvml"] = {**info, "procs": [{"pid": pid, "name": sensors.comm(pid), "vram_mib": mem}
                                               for pid, mem in info["procs"].items()]}
        return d

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "GetXfcePanel":
                from .xfce_panel import panel_markup
                invocation.return_value(GLib.Variant("(s)", (panel_markup(self.state()),)))
                return
            if method == "GetState":
                out = self.state()
            elif method == "GetRecent":
                cutoff = time.time() - params.unpack()[0] * 60
                out = [[round(ts, 1), w and round(w, 2), g] for ts, w, g in self.ring if ts >= cutoff]
            elif method == "GetGpuDetails":
                out = self.gpu_details()
            else:
                raise ValueError(method)
            invocation.return_value(GLib.Variant("(s)", (json.dumps(out),)))
        except Exception as e:
            log.exception("D-Bus call %s failed", method)
            invocation.return_dbus_error(f"{DBUS_IFACE}.Error", str(e))

    # ---------------------------------------------------------------- run/stop

    def _start_timers(self):
        for t in self.timers:
            GLib.source_remove(t)
        s = self.cfg["sampling"]
        self.timers = [GLib.timeout_add_seconds(int(s["interval_seconds"]), self.tick),
                       GLib.timeout_add_seconds(int(s["flush_seconds"]), self._flush)]

    def _flush(self):
        self.store.update_session(self.session_id, energy_wh=self.energy_wh, dgpu_awake_s=self.dgpu_awake_s,
                                  sleep_s=self.sleep_s, last_ts=self.last_tick)
        self.store.flush()
        return True

    def stop(self):
        if not self.stopping:
            self.event("daemon_stop", "Daemon stopped (logout or service stop)")
        self._flush()
        self.store.close()
        self._release_inhibit()
        self.loop.quit()
        return False

    def run(self):
        self.system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._startup_records()

        node = Gio.DBusNodeInfo.new_for_xml(DBUS_XML)
        self.session_bus.register_object(DBUS_PATH, node.interfaces[0], self._on_call, None, None)

        def lost(*_):
            log.error("D-Bus name %s is taken - is another daemon running?", DBUS_NAME)
            self.loop.quit()
        Gio.bus_own_name_on_connection(self.session_bus, DBUS_NAME, Gio.BusNameOwnerFlags.NONE, None, lost)

        sub = self.system_bus.signal_subscribe
        sub(LOGIND[0], LOGIND[2], "PrepareForSleep", LOGIND[1], None, Gio.DBusSignalFlags.NONE,
            self._on_prepare_sleep)
        sub(LOGIND[0], LOGIND[2], "PrepareForShutdown", LOGIND[1], None, Gio.DBusSignalFlags.NONE,
            self._on_prepare_shutdown)
        self.session_bus.signal_subscribe(None, "org.freedesktop.ScreenSaver", "ActiveChanged", None, None,
                                          Gio.DBusSignalFlags.NONE, self._on_screensaver)
        self._inhibit()

        if GUdev:
            self.udev = GUdev.Client.new(["drm", "power_supply"])
            self.udev.connect("uevent", self._on_uevent)
        self.cfg_monitor = Gio.File.new_for_path(CONFIG_FILE).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.cfg_monitor.connect("changed", self._on_config_changed)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.stop)

        self._start_timers()
        self.tick()
        log.info("running: battery=%s dgpu=%s", bool(self.batteries), self.gpu and self.gpu.pci)
        self.loop.run()


def main(notify=True):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    Daemon(notify=notify).run()
