"""Minimal ctypes NVML client. Opening NVML wakes a sleeping NVIDIA GPU and keeps it
awake while initialized, so query() checks the GPU is already awake, reads once and
shuts NVML down again."""
import ctypes

_lib = None


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Proc(ctypes.Structure):  # nvmlProcessInfo_v2_t (used by the _v3 calls)
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]


def _load():
    global _lib
    if _lib is None:
        try:
            _lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError:
            _lib = False
    return _lib


def available():
    return bool(_load())


def _procs(lib, handle, fn_name):
    fn = getattr(lib, fn_name, None)
    if fn is None:
        return []
    count = ctypes.c_uint(64)
    buf = (_Proc * 64)()
    if fn(handle, ctypes.byref(count), buf) != 0:
        return []
    return [(buf[i].pid, buf[i].usedGpuMemory) for i in range(count.value)]


def query(gpu):
    """Read power/utilization/processes from an NVIDIA GPU that is already awake.
    Returns None if the GPU is asleep or NVML is unavailable."""
    lib = _load()
    if not lib or not gpu.is_nvidia or gpu.runtime_status() != "active":
        return None
    if lib.nvmlInit_v2() != 0:
        return None
    try:
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByPciBusId_v2(gpu.pci.encode(), ctypes.byref(handle)) != 0:
            return None
        out = {}
        mw = ctypes.c_uint()
        if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(mw)) == 0:
            out["power_w"] = mw.value / 1000
        util = _Util()
        if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
            out["util"] = util.gpu
        temp = ctypes.c_uint()
        if lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp)) == 0:
            out["temp_c"] = temp.value
        pstate = ctypes.c_int()
        if lib.nvmlDeviceGetPerformanceState(handle, ctypes.byref(pstate)) == 0:
            out["pstate"] = f"P{pstate.value}"
        procs = {}
        for fn in ("nvmlDeviceGetGraphicsRunningProcesses_v3", "nvmlDeviceGetComputeRunningProcesses_v3"):
            for pid, mem in _procs(lib, handle, fn):
                procs[pid] = max(procs.get(pid, 0), mem)
        # NVML reports unknown memory as ULLONG_MAX
        out["procs"] = {pid: (mem // 2**20 if mem < 2**63 else None) for pid, mem in procs.items()}
        return out
    finally:
        lib.nvmlShutdown()
