"""The on-demand GTK details window for the Xfce panel."""
import json
import os
import sys
from importlib.util import find_spec

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import APP_NAME, DBUS_IFACE, DBUS_NAME, DBUS_PATH, fmt
from .xfce_panel import summary


def gpu_text(state, details):
    gpu = state["gpu"]
    if not gpu:
        return ""
    lines = [gpu["name"]]
    if gpu["last_wake_cause"]:
        lines.append("Last wake: " + gpu["last_wake_cause"])
    snapshot = details.get("gpu") or {}
    if (snapshot.get("pci"), snapshot.get("since")) == (gpu["pci"], gpu["since"]):
        stats = details.get("nvml", {})
        readings = [str(stats[key]) + unit for key, unit in
                    (("pstate", ""), ("power_w", " W"), ("util", "%"), ("temp_c", " °C"))
                    if key in stats]
        if readings:
            lines.append(" · ".join(readings))
        procs = [p["name"] + (f" ({p['vram_mib']} MiB)" if p["vram_mib"] is not None else "")
                 for p in stats.get("procs", [])]
        if procs:
            lines.append("Rendering: " + ", ".join(procs))
        holders = details.get("holders", {}).values()
        if holders:
            lines.append("Has GPU open: " + ", ".join(holders))
    return "\n".join(lines)


class DetailsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(420, 460)
        self.set_icon_name("battery")
        self.bus = app.get_dbus_connection()
        self.cancel = Gio.Cancellable()
        self.timer = 0
        self.reading = False
        self.switching = False
        self.state = None
        self.details = {}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9, margin=12)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(box)
        outer.pack_start(scroll, True, True, 0)

        def label():
            widget = Gtk.Label(xalign=0, selectable=True, wrap=True)
            box.pack_start(widget, False, False, 0)
            return widget

        self.status = label()
        self.gpu = label()
        self.events = label()
        self.mode = label()
        self.mode_buttons = {}
        modes = Gtk.Box(spacing=6)
        box.pack_start(modes, False, False, 0)
        for mode, title in (("hybrid", "Hybrid"), ("integrated", "Integrated only")):
            button = Gtk.Button(label=title)
            button.set_sensitive(False)
            button.connect("clicked", self.run_command, "gpu-mode", mode)
            modes.add(button)
            self.mode_buttons[mode] = button
        self.mode_buttons["integrated"].set_tooltip_text(
            "Requires a reboot. Display ports wired to the dGPU will stop working.")
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        outer.pack_end(footer, False, False, 0)
        self.feedback = Gtk.Label(xalign=0, wrap=True, selectable=True)
        footer.add(self.feedback)
        actions = Gtk.Box(spacing=6)
        footer.add(actions)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.connect("clicked", lambda _: self.refresh(read_gpu=True))
        actions.add(self.refresh_button)
        for title, command in (("Edit config", "config"), ("Open TUI", "open-tui")):
            button = Gtk.Button(label=title)
            button.connect("clicked", self.run_command, command)
            actions.add(button)
        self.connect("destroy", self.on_destroy)
        self.show_all()
        self.status.set_text("Reading battery status…")
        self.refresh(read_gpu=True)

    def on_destroy(self, *_):
        self.cancel.cancel()
        if self.timer:
            GLib.source_remove(self.timer)

    def call(self, method, callback):
        def done(bus, result):
            try:
                value = json.loads(bus.call_finish(result).unpack()[0])
            except GLib.Error:
                value = None
            if not self.cancel.is_cancelled():
                callback(value)

        self.bus.call(DBUS_NAME, DBUS_PATH, DBUS_IFACE, method, None, GLib.VariantType("(s)"),
                      Gio.DBusCallFlags.NONE, 3000, self.cancel, done)

    def refresh(self, read_gpu=False):
        if self.reading:
            return
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.reading = True
        self.refresh_button.set_sensitive(False)

        def finish():
            self.reading = False
            self.refresh_button.set_sensitive(True)
            self.render()
            interval = self.state["interval"] if self.state else 5
            self.timer = GLib.timeout_add(max(1000, round(interval * 1000)), self.poll)

        def got_details(details):
            self.details = details or {}
            finish()

        def got_state(state):
            first_state = self.state is None
            self.state = state
            if state and state["gpu"] and (read_gpu or first_state):
                self.call("GetGpuDetails", got_details)
            else:
                if not state or not state["gpu"]:
                    self.details = {}
                finish()

        self.call("GetState", got_state)

    def poll(self):
        self.timer = 0
        self.refresh()
        return GLib.SOURCE_REMOVE

    def render(self):
        st = self.state
        self.status.set_text(summary(st) if st else
                             "Daemon unavailable. Run systemctl --user start betterbatterylookup.")
        self.gpu.set_text(gpu_text(st, self.details) if st else "")
        events = [f"{fmt.clock(e['ts'])}  {e['message']}" for e in st["events"]] if st else []
        self.events.set_text("Recent events\n" + "\n".join(events) if events else "")
        mode = st["gpu_mode"] if st else {}
        gpu = (st["gpu"] or {}) if st else {}
        can_switch = gpu.get("vendor") == "NVIDIA" or mode.get("configured") == "integrated"
        mode_text = ""
        if can_switch:
            mode_text = f"GPU mode: {mode['running']} running · {mode['configured']} configured"
            if mode["pending_reboot"]:
                mode_text += "\nReboot to apply the configured mode."
            if not mode["helper_installed"]:
                mode_text += "\nInstall the GPU mode helper with ./install.sh --system."
        self.mode.set_text(mode_text)
        for target, button in self.mode_buttons.items():
            button.set_visible(can_switch)
            button.set_sensitive(can_switch and mode["helper_installed"]
                                 and target != mode["configured"] and not self.switching)
        for label in (self.gpu, self.events, self.mode, self.feedback):
            label.set_visible(bool(label.get_text()))

    def run_command(self, button, *args):
        if args[0] == "open-tui" and find_spec("textual") is None:
            self.feedback.set_text("Install Textual to open the terminal interface.")
            self.feedback.show()
            return
        switch = args[0] == "gpu-mode"
        if switch:
            self.switching = True
        self.feedback.set_text("")

        def finished(message):
            if not self.cancel.is_cancelled():
                if switch:
                    self.switching = False
                self.feedback.set_text(message)
                self.render()

        def done(proc, result):
            try:
                _, output, _ = proc.communicate_utf8_finish(result)
                message = (output or "").strip()
                if not proc.get_successful() and not message:
                    message = f"Could not run bbl {args[0]}."
            except GLib.Error as error:
                message = error.message
            finished(message)

        try:
            proc = Gio.Subprocess.new([os.path.realpath(sys.argv[0]), *args],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as error:
            finished(error.message)
            return
        self.render()
        proc.communicate_utf8_async(None, None, done)


def main():
    app = Gtk.Application(application_id="org.betterbatterylookup.XfceDetails")

    def activate(app):
        window = app.get_active_window() or DetailsWindow(app)
        window.present()

    app.connect("activate", activate)
    return app.run([])
