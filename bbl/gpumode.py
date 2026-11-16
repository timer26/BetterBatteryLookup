"""GPU mode status. Switching is done by the root helper (helper/bbl-gpumode)."""
import os
import shutil
import subprocess

from . import sensors
from .paths import GPUMODE_HELPER

# Must match helper/bbl-gpumode
INTEGRATED_MARKER = "/etc/modprobe.d/betterbatterylookup-integrated.conf"


def status():
    configured = "integrated" if os.path.exists(INTEGRATED_MARKER) else "hybrid"
    gpu = sensors.find_dgpu()
    running = "hybrid" if gpu is not None else "integrated"
    return {
        "configured": configured,
        "running": running,
        "pending_reboot": configured != running,
        "helper_installed": os.access(GPUMODE_HELPER, os.X_OK),
    }


def switch(mode):
    """Run the root helper through pkexec. Returns (ok, output)."""
    if not os.access(GPUMODE_HELPER, os.X_OK):
        return False, f"helper not installed ({GPUMODE_HELPER}); run ./install.sh --system"
    if not shutil.which("pkexec"):
        return False, "pkexec not found"
    p = subprocess.run(["pkexec", GPUMODE_HELPER, mode], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()
