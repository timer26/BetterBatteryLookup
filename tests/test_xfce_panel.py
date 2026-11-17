import unittest
from xml.etree import ElementTree as ET

from bbl.xfce_panel import COLORS, panel_markup


class XfcePanelTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "ts": 600, "on_ac": False, "watts": 12.25, "has_battery": True,
            "percent": 42, "status": "Discharging", "time_left_s": 7200,
            "time_left_minutes": 5, "time_left_now_s": None, "time_left_session_s": None,
            "gpu": None, "level": "green", "level_reasons": [],
            "panel": {"show_lamp": True, "show_watts": True},
            "plot": {"tooltip_minutes": 10}, "thresholds": {"red": 30}, "recent": [],
        }

    def widget(self):
        return ET.fromstring("<widget>" + panel_markup(self.state) + "</widget>")

    def test_lamp_and_watts_preferences(self):
        for lamp, watts, expected in [(True, True, "● 12.2 W"), (False, True, "12.2 W"),
                                      (True, False, "●"), (False, False, "●")]:
            with self.subTest(lamp=lamp, watts=watts):
                self.state["panel"].update(show_lamp=lamp, show_watts=watts)
                self.assertEqual("".join(self.widget().find("txt").itertext()), expected)

    def test_missing_draw_is_distinct_from_ac(self):
        self.state.update(watts=None, has_battery=False, percent=None, time_left_s=None)
        self.assertIn("n/a", "".join(self.widget().find("txt").itertext()))
        self.state.update(on_ac=True, level="grey")
        self.assertEqual("".join(self.widget().find("txt").itertext()), "● AC")
        self.assertEqual(self.widget().find("txt/span").get("foreground"), COLORS["grey"])

    def test_time_buckets_keep_gaps_and_gpu_activity_without_draw(self):
        self.state["recent"] = [[-1, 90, 2], [0, 0, 0], [575, None, 1], [600, 30, 2]]
        chart = self.widget().find("tool/tt")
        self.assertEqual("".join(chart.itertext()), "▁" + " " * 30 + "█")
        self.assertEqual([s.get("background") for s in chart], [COLORS["yellow"], COLORS["red"]])

    def test_daemon_text_cannot_inject_genmon_commands(self):
        message = "draw < 10 W & </tool><txtclick>touch /tmp/example</txtclick>"
        self.state["level_reasons"] = [message]
        widget = self.widget()
        self.assertEqual([tag.tag for tag in widget], ["txt", "tool"])
        self.assertIn(message, "".join(widget.find("tool").itertext()))
