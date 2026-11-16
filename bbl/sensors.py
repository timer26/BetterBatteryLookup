"""Hardware readings from sysfs/procfs only. Nothing here touches the dGPU itself,
so reading never wakes it."""
import glob
import os
import time
from collections import deque

PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
CLK_TCK = os.sysconf("SC_CLK_TCK")


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- power supply

def _supplies(kind):
    for p in sorted(glob.glob("/sys/class/power_supply/*")):
        if read(f"{p}/type") == kind and read(f"{p}/scope") != "Device":  # skip mice etc.
            yield p


def batteries():
    return list(_supplies("Battery"))


def ac_supplies():
    """Charger supply paths. USB-C chargers come and go as devices, so callers
    re-run this on power_supply udev events."""
    return list(_supplies("Mains")) + list(_supplies("USB"))


def on_ac(supplies=None):
    return any(read(f"{p}/online") == "1" for p in (ac_supplies() if supplies is None else supplies))


def read_battery(paths):
    """Sum over all batteries. Energies in Wh, watts = current discharge rate."""
    if not paths:
        return None
    total = {"now_wh": 0.0, "full_wh": 0.0, "design_wh": 0.0, "watts": 0.0}
    statuses, cycles = [], []
    for p in paths:
        volt = read_int(f"{p}/voltage_now") or 0
        vmin = read_int(f"{p}/voltage_min_design") or volt
        if read(f"{p}/energy_now") is not None:
            now, full, design = (read_int(f"{p}/{k}") or 0 for k in ("energy_now", "energy_full", "energy_full_design"))
            total["now_wh"] += now / 1e6
            total["full_wh"] += full / 1e6
            total["design_wh"] += design / 1e6
        else:  # charge-based batteries report µAh
            now, full, design = (read_int(f"{p}/{k}") or 0 for k in ("charge_now", "charge_full", "charge_full_design"))
            total["now_wh"] += now * vmin / 1e12
            total["full_wh"] += full * vmin / 1e12
            total["design_wh"] += design * vmin / 1e12
        pw = read_int(f"{p}/power_now")
        if pw:
            total["watts"] += abs(pw) / 1e6
        else:
            cur = read_int(f"{p}/current_now") or 0
            total["watts"] += abs(cur) * volt / 1e12
        statuses.append(read(f"{p}/status", "Unknown"))
        c = read_int(f"{p}/cycle_count")
        if c:
            cycles.append(c)
    total["percent"] = 100.0 * total["now_wh"] / total["full_wh"] if total["full_wh"] else None
    total["status"] = statuses[0] if len(set(statuses)) == 1 else "/".join(statuses)
    total["cycles"] = max(cycles) if cycles else None
    total["health"] = 100.0 * total["full_wh"] / total["design_wh"] if total["design_wh"] else None
    return total


# ------------------------------------------------------------------------ GPUs

class Gpu:
    def __init__(self, path):
        self.path = path
        self.pci = os.path.basename(path)
        self.vendor_id = read(f"{path}/vendor")
        self.vendor = PCI_VENDORS.get(self.vendor_id, self.vendor_id)
        self.nodes = {f"/dev/dri/{os.path.basename(d)}" for d in glob.glob(f"{path}/drm/*")}
        self.card = next((os.path.basename(d) for d in glob.glob(f"{path}/drm/card*")), None)

    @property
    def is_nvidia(self):
        return self.vendor_id == "0x10de"

    def runtime_status(self):
        """active | suspended | resuming | suspending | unsupported | gone"""
        return read(f"{self.path}/power/runtime_status", "gone")

    def name(self):
        return _pci_name(self.vendor_id, read(f"{self.path}/device")) or f"{self.vendor} GPU {self.pci}"


def _pci_name(vendor, device):
    """Device name from the hwdata pci.ids database, e.g. 'GA104M [GeForce RTX 3070 Mobile / Max-Q]'."""
    if not vendor or not device:
        return None
    v, d = vendor[2:].lower(), device[2:].lower()
    for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if line.startswith(v + " "):
                        in_vendor = True
                    elif in_vendor:
                        if line.startswith("\t" + d + " "):
                            return line.split("  ", 1)[1].strip()
                        if line[:1] not in ("\t", "#", "\n"):
                            return None
        except OSError:
            continue
    return None


def find_dgpu():
    """The discrete GPU: a display-class PCI device that is not the boot VGA device."""
    gpus = [p for p in glob.glob("/sys/bus/pci/devices/*") if read(f"{p}/class", "").startswith("0x03")]
    if len(gpus) < 2:
        # single GPU: only interesting if it is a dGPU that can runtime-suspend
        gpus = [p for p in gpus if read(f"{p}/vendor") != "0x8086"]
    for p in gpus:
        if read(f"{p}/boot_vga") != "1":
            return Gpu(p)
    return Gpu(gpus[0]) if gpus else None


def connectors():
    """{connector name: card} for connected outputs. Reads cached status (no probing)."""
    out = {}
    for s in glob.glob("/sys/class/drm/card*-*/status"):
        if read(s) == "connected":
            name = os.path.basename(os.path.dirname(s))
            card, _, conn = name.partition("-")
            out[conn] = card
    return out


# ---------------------------------------------------------------------- system

LID_STATES = glob.glob("/proc/acpi/button/lid/*/state")


def lid_closed():
    for s in LID_STATES:
        v = read(s, "")
        if "closed" in v:
            return True
        if "open" in v:
            return False
    return None


def suspended_seconds():
    """Total time spent in system sleep since boot (BOOTTIME keeps counting, MONOTONIC stops)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def boot_id():
    return read("/proc/sys/kernel/random/boot_id", "")


def boot_time():
    return time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)


# ------------------------------------------------------------------- processes

def comm(pid):
    return read(f"/proc/{pid}/comm", "?")


class ProcTracker:
    """Notices newly started user-space processes with one listdir of /proc per update."""

    def __init__(self, keep_seconds=30):
        self.keep = keep_seconds
        self.known = set(self._pids())
        self.recent = deque()  # (ts, pid, comm)

    @staticmethod
    def _pids():
        return [int(p) for p in os.listdir("/proc") if p.isdigit()]

    def update(self, now):
        pids = set(self._pids())
        for pid in pids - self.known:
            if read(f"/proc/{pid}/cmdline"):  # kernel threads have no cmdline
                self.recent.append((now, pid, comm(pid)))
        self.known = pids
        while self.recent and self.recent[0][0] < now - self.keep:
            self.recent.popleft()

    def started_between(self, t0, t1):
        return [(ts, pid, name) for ts, pid, name in self.recent if t0 <= ts <= t1]


def cpu_times():
    """{pid: (comm, utime+stime ticks)} for all readable processes."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            continue
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            out[int(pid)] = (name, int(fields[11]) + int(fields[12]))
        except (IndexError, ValueError):
            pass
    return out


def top_cpu(before, after, seconds, n=3):
    """Top processes by CPU% between two cpu_times() snapshots (100% = one core)."""
    usage = {}
    for pid, (name, ticks) in after.items():
        prev = before.get(pid)
        if prev:
            usage[name] = usage.get(name, 0) + (ticks - prev[1])
    ranked = sorted(usage.items(), key=lambda kv: -kv[1])[:n]
    return [(name, 100.0 * t / CLK_TCK / seconds) for name, t in ranked if t > 0]


def gpu_holders(gpu):
    """Processes with the dGPU open, best effort: privileged processes (kwin,
    btop with capabilities) hide their fds from normal users."""
    found = {}
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target in gpu.nodes or (gpu.is_nvidia and target.startswith("/dev/nvidia")):
                    pid = int(fd_dir.split("/")[2])
                    found[pid] = comm(pid)
                    break
        except OSError:
            continue
    return found
