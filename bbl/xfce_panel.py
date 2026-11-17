"""Format the daemon's cached state for the Xfce Generic Monitor."""
from html import escape

from . import fmt


COLORS = {"green": "#4e9a06", "yellow": "#e5a50a", "red": "#e34b4b", "grey": "#888a85"}


def power_text(state):
    if state["on_ac"]:
        return "AC"
    return fmt.watts(state["watts"])


def summary(state):
    lines = ["On AC power" if state["on_ac"] else f"Battery draw: {fmt.watts(state['watts'])}"]
    if state["has_battery"]:
        lines.append(f"Battery {fmt.pct(state['percent'])} · {state['status']}")
    if state["time_left_s"] is not None:
        lines.append(f"{fmt.dur(state['time_left_s'])} left ({state['time_left_minutes']} min average)")
        estimates = [f"{label}: {fmt.dur(state[key])}" for label, key in
                     (("Now", "time_left_now_s"), ("Session", "time_left_session_s"))
                     if state[key] is not None]
        if estimates:
            lines.append(" · ".join(estimates))
    gpu = state["gpu"]
    if gpu:
        activity = "busy" if gpu["busy"] else "awake" if gpu["awake"] else "asleep"
        lines.append(f"dGPU {activity} for {fmt.dur(state['ts'] - gpu['since'])}")
    if state["level_reasons"]:
        lines.append(f"Lamp {state['level']} · " + ", ".join(state["level_reasons"]))
    return "\n".join(lines)


def history(state):
    """A fixed time axis keeps gaps in the samples visible after suspend or on AC."""
    columns = 32
    span = 60 * state["plot"]["tooltip_minutes"]
    start = state["ts"] - span
    values = [[] for _ in range(columns)]
    activity = [0] * columns
    for ts, watts, gpu in state["recent"]:
        if start <= ts <= state["ts"]:
            index = min(columns - 1, int((ts - start) * columns / span))
            if watts is not None:
                values[index].append(watts)
            activity[index] = max(activity[index], gpu)

    if not any(values) and not any(activity):
        return "No recent battery draw readings"
    averages = [sum(v) / len(v) if v else None for v in values]
    top = max([1, state["thresholds"]["red"]] + [v for v in averages if v is not None])
    blocks = "▁▂▃▄▅▆▇█"
    bars = []
    for watts, gpu in zip(averages, activity):
        bar = " " if watts is None else blocks[round(7 * watts / top)]
        if gpu in (1, 2):
            color = COLORS["red" if gpu == 2 else "yellow"]
            bar = f'<span background="{color}" foreground="#202020">{bar}</span>'
        bars.append(bar)
    return (f"<tt>{''.join(bars)}</tt>\nLast {state['plot']['tooltip_minutes']} min · 0–{top:g} W"
            + ("\nShading: dGPU awake (yellow) or busy (red)" if any(activity) else ""))


def panel_markup(state):
    panel = state["panel"]
    parts = []
    if panel["show_lamp"] or not panel["show_watts"]:
        color = COLORS.get(state["level"], COLORS["grey"])
        parts.append(f'<span foreground="{color}">●</span>')
    if panel["show_watts"]:
        parts.append(power_text(state))
    tip = escape(summary(state), quote=False) + "\n\n" + history(state)
    return f"<txt>{' '.join(parts)}</txt>\n<tool>{tip}</tool>"
