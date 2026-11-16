import time


def dur(seconds):
    """Human duration: '42 s', '3 m 10 s', '2 h 05 m', '1 d 3 h'."""
    if seconds is None:
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} m {s:02d} s" if m < 10 else f"{m} m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} m"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def watts(w):
    return "n/a" if w is None else f"{w:.1f} W"


def pct(p):
    return "?%" if p is None else f"{p:.0f}%"


def clock(ts, with_date=False):
    if ts is None:
        return "--:--"
    return time.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S", time.localtime(ts))
