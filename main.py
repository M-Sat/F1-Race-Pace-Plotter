import sys
import math
import pathlib
import tempfile
from dataclasses import dataclass

import fastf1
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QUrl
import plotly.graph_objects as go


def _enable_fastf1_cache() -> None:
    cache_dir = pathlib.Path.home() / ".cache" / "fastf1"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))


def _normalize_team_hex(raw: object) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return "#" + s.upper()


def _lighten_hex(hex_color: str, amount: float = 0.18) -> str:
    h = hex_color.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def _format_lap_time(seconds: float | None) -> str:
    if seconds is None or seconds != seconds:
        return "N/A"
    m = int(seconds // 60)
    s = seconds - (m * 60)
    return f"{m}:{s:06.3f}"


@dataclass(frozen=True)
class DriverSeries:
    driver: str
    laps: list[int]
    lap_times_s: list[float]
    positions: list[int | None]
    compounds: list[str | None]
    pit_notes: list[str]
    team: str | None = None
    color: str | None = None


COMPOUND_COLORS: dict[str, str] = {
    "SOFT": "#FF3333",
    "MEDIUM": "#FFD700",
    "HARD": "#E8E8E8",
    "INTERMEDIATE": "#39B54A",
    "WET": "#00AEEF",
    "UNKNOWN": "#888888",
}


class LoadRaceWorker(QtCore.QThread):
    loaded = QtCore.pyqtSignal(int, str, object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, year: int, race_name: str, parent=None):
        super().__init__(parent)
        self._year = year
        self._race_name = race_name

    def run(self) -> None:
        try:
            session = fastf1.get_session(self._year, self._race_name, "R")
            session.load(telemetry=False, weather=False, messages=False)

            laps = session.laps.copy()
            if laps.empty:
                self.loaded.emit(self._year, self._race_name, {"series": [], "status_periods": []})
                return

            # Detect SC/VSC periods from consecutive lap status groups
            status_periods: list[dict] = []
            if "TrackStatus" in laps.columns and "LapNumber" in laps.columns:
                ts = laps[["LapNumber", "TrackStatus"]].dropna(subset=["LapNumber"]).copy()
                ts = ts.sort_values("LapNumber")
                ts["_sg"] = (ts["TrackStatus"] != ts["TrackStatus"].shift()).cumsum()
                for (_, track_status), grp in ts.groupby(["_sg", "TrackStatus"]):
                    s = str(track_status)
                    is_sc = "4" in s
                    is_vsc = "6" in s
                    if not (is_sc or is_vsc):
                        continue
                    status_periods.append({
                        "kind": "SC" if is_sc else "VSC",
                        "start": int(grp["LapNumber"].min()),
                        "end": int(grp["LapNumber"].max()),
                    })

            laps["LapTimeSec"] = laps["LapTime"].dt.total_seconds()
            laps = laps[
                laps["LapTimeSec"].notna()
                & (laps["LapTimeSec"] > 45)
                & (laps["LapTimeSec"] < 600)
            ].copy()

            driver_meta: dict[str, dict] = {}
            team_info: dict[str, dict] = {}
            for drv_num in session.drivers:
                try:
                    info = session.get_driver(drv_num)
                    abbr = info.get("Abbreviation")
                    if not abbr:
                        continue
                    team = info.get("TeamName")
                    base = _normalize_team_hex(info.get("TeamColor") or info.get("TeamColour"))
                    if team and base:
                        team_info.setdefault(team, {"color": base, "drivers": []})
                        team_info[team]["drivers"].append(abbr)
                    driver_meta[abbr] = {"team": team, "color": base}
                except Exception:
                    continue

            # Lighten the second teammate's colour so they're visually distinct
            for info in team_info.values():
                drvs = info.get("drivers") or []
                if len(drvs) >= 2 and info.get("color"):
                    second = drvs[1]
                    if second in driver_meta:
                        driver_meta[second]["color"] = _lighten_hex(info["color"], 0.5)

            out: list[DriverSeries] = []
            for drv in sorted(laps["Driver"].unique()):
                d = laps[laps["Driver"] == drv].sort_values("LapNumber")
                lap_numbers = [int(x) for x in d["LapNumber"].tolist()]
                lap_times = [float(x) for x in d["LapTimeSec"].tolist()]

                positions = (
                    [(int(p) if p == p and p is not None else None) for p in d["Position"].tolist()]
                    if "Position" in d.columns
                    else [None] * len(lap_numbers)
                )
                compounds = (
                    [(str(c).upper() if c == c and c is not None else None) for c in d["Compound"].tolist()]
                    if "Compound" in d.columns
                    else [None] * len(lap_numbers)
                )

                pit_notes: list[str] = []
                has_pit_in = "PitInTime" in d.columns
                has_pit_out = "PitOutTime" in d.columns
                if has_pit_in or has_pit_out:
                    pit_in_vals = d["PitInTime"].tolist() if has_pit_in else [None] * len(lap_numbers)
                    pit_out_vals = d["PitOutTime"].tolist() if has_pit_out else [None] * len(lap_numbers)
                    for pin, pout in zip(pit_in_vals, pit_out_vals):
                        if pin == pin and pin is not None:
                            pit_notes.append("<br>Pit: in-lap")
                        elif pout == pout and pout is not None:
                            pit_notes.append("<br>Pit: out-lap")
                        else:
                            pit_notes.append("")
                else:
                    pit_notes = [""] * len(lap_numbers)

                meta = driver_meta.get(drv, {})
                out.append(DriverSeries(
                    driver=drv,
                    laps=lap_numbers,
                    lap_times_s=lap_times,
                    positions=positions,
                    compounds=compounds,
                    pit_notes=pit_notes,
                    team=meta.get("team"),
                    color=meta.get("color"),
                ))

            self.loaded.emit(self._year, self._race_name, {"series": out, "status_periods": status_periods})
        except Exception as e:
            self.failed.emit(str(e))


class PacePlotterWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("F1 Race Pace Plotter")
        self.resize(1250, 760)

        self._driver_trace_idx: dict[str, int] = {}
        self._loading_worker: LoadRaceWorker | None = None
        self._plot_js_ready = False

        _enable_fastf1_cache()
        self._build_ui()
        self._populate_years()
        self._update_race_list()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        self.setStyleSheet("""
            QGroupBox {
                color: #E0E0E0;
                font-weight: bold;
                font-size: 11px;
                text-transform: uppercase;
                border: 1px solid #333;
                border-radius: 4px;
                margin-top: 10px;
                background-color: #1A1A1A;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px;
            }
            QLabel {
                color: #AAAAAA;
                font-size: 10px;
                font-weight: bold;
                text-transform: uppercase;
                margin-bottom: -2px;
            }
            QComboBox {
                background-color: #2D2D2D;
                border: 1px solid #444;
                border-radius: 3px;
                padding: 4px;
                color: white;
            }
            QComboBox:hover { border: 1px solid #FF1801; }
            QPushButton {
                background-color: #3D3D3D;
                color: white;
                border: none;
                border-radius: 3px;
                font-weight: bold;
                padding: 6px;
            }
            QPushButton:hover { background-color: #FF1801; }
            QPushButton:pressed { background-color: #CC1400; }
        """)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        controls_box = QtWidgets.QGroupBox()
        hbox_main = QtWidgets.QHBoxLayout(controls_box)
        hbox_main.setContentsMargins(10, 8, 10, 8)
        hbox_main.setSpacing(20)

        left_half = QtWidgets.QHBoxLayout()
        left_half.setSpacing(10)

        year_vbox = QtWidgets.QVBoxLayout()
        year_vbox.addWidget(QtWidgets.QLabel("Year"))
        self.year_box = QtWidgets.QComboBox()
        self.year_box.currentIndexChanged.connect(self._update_race_list)
        year_vbox.addWidget(self.year_box)
        left_half.addLayout(year_vbox, 2)

        race_vbox = QtWidgets.QVBoxLayout()
        race_vbox.addWidget(QtWidgets.QLabel("Grand Prix"))
        self.race_box = QtWidgets.QComboBox()
        self.race_box.setMinimumWidth(260)
        race_vbox.addWidget(self.race_box)
        left_half.addLayout(race_vbox, 2)

        right_half = QtWidgets.QVBoxLayout()
        right_half.addWidget(QtWidgets.QLabel(" "))  # spacer to bottom-align the button with dropdowns
        self.load_btn = QtWidgets.QPushButton("LOAD RACE DATA")
        self.load_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.load_btn.clicked.connect(self._load_clicked)
        right_half.addWidget(self.load_btn)

        hbox_main.addLayout(left_half, 1)
        hbox_main.addLayout(right_half, 1)
        root.addWidget(controls_box, 0)

        self.plot_container = QtWidgets.QFrame()
        self.plot_container.setStyleSheet("""
            QFrame {
                border: 1px solid #333;
                border-radius: 8px;
                background-color: #111;
            }
        """)
        plot_layout = QtWidgets.QVBoxLayout(self.plot_container)
        plot_layout.setContentsMargins(1, 1, 1, 1)

        self.browser = QWebEngineView()
        self.browser.loadFinished.connect(self._on_plot_load_finished)
        plot_layout.addWidget(self.browser)
        root.addWidget(self.plot_container, 1)

        self._plot_empty("Select a year + race, then click Load race.")

    def _on_plot_load_finished(self, ok: bool) -> None:
        if not ok:
            self._plot_js_ready = False
            return
        self.browser.page().runJavaScript(
            "typeof setDriverVisible === 'function' && typeof setAllVisible === 'function';",
            self._set_plot_ready,
        )

    def _set_plot_ready(self, ready: bool) -> None:
        self._plot_js_ready = bool(ready)

    def _run_plot_js(self, js: str) -> None:
        if self._plot_js_ready:
            self.browser.page().runJavaScript(js)

    def _populate_years(self) -> None:
        this_year = QtCore.QDate.currentDate().year()
        years = list(range(this_year, 2019, -1))
        self.year_box.clear()
        self.year_box.addItems([str(y) for y in years])

    def _update_race_list(self) -> None:
        year = int(self.year_box.currentText()) if self.year_box.count() else 2020
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
            races = [str(x) for x in schedule["EventName"].tolist()]
            self.race_box.clear()
            self.race_box.addItems(races)
        except Exception:
            self.race_box.clear()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _load_clicked(self) -> None:
        if self._loading_worker and self._loading_worker.isRunning():
            return

        year = int(self.year_box.currentText())
        race = self.race_box.currentText().strip()

        self._plot_empty(f"Loading {year} {race} race laps…")
        self.load_btn.setEnabled(False)
        self.year_box.setEnabled(False)
        self.race_box.setEnabled(False)

        self._loading_worker = LoadRaceWorker(year, race, self)
        self._loading_worker.loaded.connect(self._on_loaded)
        self._loading_worker.failed.connect(self._on_failed)
        self._loading_worker.start()

    def _on_failed(self, msg: str) -> None:
        self.load_btn.setEnabled(True)
        self.year_box.setEnabled(True)
        self.race_box.setEnabled(True)
        self._plot_empty("Load failed. Try another race/year.")

    def _on_loaded(self, year: int, race: str, payload: object) -> None:
        self.load_btn.setEnabled(True)
        self.year_box.setEnabled(True)
        self.race_box.setEnabled(True)

        if not isinstance(payload, dict):
            self._plot_empty("Load failed. Try another race/year.")
            return

        series_list = payload.get("series") or []
        status_periods = payload.get("status_periods") or []

        if not series_list:
            self._plot_empty("No lap data available.")
            return

        self._plot_series(series_list, status_periods)

    def _plot_empty(self, msg: str) -> None:
        self._plot_js_ready = False
        html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;background:#111;color:#ddd;font-family:Segoe UI, sans-serif;">
  <div style="padding:18px;">
    <h3 style="margin:0 0 10px 0;font-weight:600;">F1 Race Pace Plotter</h3>
    <div style="color:#aaa;">{msg}</div>
  </div>
  <script>
    function setDriverVisible(driver, visibleState) {{}}
    function setAllVisible(visibleState) {{}}
  </script>
</body>
</html>"""
        self._load_html_in_webview(html)

    def _plot_series(self, series_list: list[DriverSeries], status_periods: list[dict]) -> None:
        self._plot_js_ready = False
        self._driver_trace_idx.clear()

        fig = go.Figure()

        for p in status_periods or []:
            try:
                kind = str(p.get("kind"))
                start = int(p.get("start"))
                end = int(p.get("end"))
            except Exception:
                continue
            if end < start:
                continue
            fill = "rgba(255, 230, 0, 0.16)" if kind == "SC" else "rgba(255, 230, 0, 0.10)"
            fig.add_vrect(
                x0=start - 0.5, x1=end + 0.5,
                fillcolor=fill, line_width=0, layer="below",
                annotation_text=kind, annotation_position="top left",
                annotation=dict(font=dict(color="#FFEA00", size=10, family="Segoe UI")),
            )

        max_lap = 0
        all_times: list[float] = []
        for ds in series_list:
            self._driver_trace_idx[ds.driver] = len(fig.data)
            cmpd_strs = [(c if c else "UNKNOWN") for c in (ds.compounds or [None] * len(ds.laps))]
            pit_notes = ds.pit_notes or [""] * len(ds.laps)
            custom = list(zip(
                ds.positions or [None] * len(ds.laps),
                [_format_lap_time(t) for t in ds.lap_times_s],
                cmpd_strs,
                pit_notes,
            ))
            if ds.laps:
                max_lap = max(max_lap, max(ds.laps))
            all_times.extend(ds.lap_times_s)
            marker_colors = [COMPOUND_COLORS.get(c, COMPOUND_COLORS["UNKNOWN"]) for c in cmpd_strs]
            fig.add_trace(go.Scatter(
                x=ds.laps,
                y=ds.lap_times_s,
                customdata=custom,
                mode="lines+markers",
                name=ds.driver,
                line=dict(color=ds.color or "#AAAAAA", width=2),
                marker=dict(
                    size=7,
                    color=marker_colors,
                    opacity=0.95,
                    line=dict(color=ds.color or "#AAAAAA", width=1),
                ),
                hovertemplate=(
                    f"<b>{ds.driver}</b>"
                    + (f" <span style='color:#888'>({ds.team})</span>" if ds.team else "")
                    + "<br>Lap: %{x}<br>Lap time: %{customdata[1]}<br>"
                    + "Position: %{customdata[0]}<br>Compound: %{customdata[2]}"
                    + "%{customdata[3]}<extra></extra>"
                ),
            ))

        # Build Y-axis ticks: fastest lap + whole-second boundaries up to the 90th percentile
        tickvals: list[float] = []
        ticktext: list[str] = []
        if all_times:
            times_sorted = sorted(all_times)
            fastest = times_sorted[0]
            p90 = times_sorted[int(0.90 * (len(times_sorted) - 1))]

            lower = float(math.floor(fastest))
            upper = float(math.ceil(p90))

            tickvals = [lower]
            if abs(fastest - lower) > 1e-9:
                tickvals.append(round(float(fastest), 3))

            v = float(math.ceil(fastest))
            if abs(v - fastest) < 1e-9:
                v += 1.0
            while v <= upper + 1e-9 and len(tickvals) < 80:
                tickvals.append(float(v))
                v += 1.0

            tickvals = sorted(set(tickvals))
            ticktext = [_format_lap_time(v) for v in tickvals]

        fig.update_layout(
            template="plotly_dark",
            title=dict(text=f"<b>{self.year_box.currentText()} {self.race_box.currentText()}</b>", x=0.5),
            xaxis=dict(title="Lap", range=[0, max_lap + 1]),
            yaxis=dict(
                title="Lap time",
                tickmode="array",
                tickvals=tickvals,
                ticktext=ticktext,
                gridcolor="rgba(255,255,255,0.07)",
            ),
            hovermode="closest",
            height=860,
            margin=dict(l=75, r=30, t=60, b=55),
            paper_bgcolor="#111111",
            plot_bgcolor="#111111",
        )
        fig.update_yaxes(autorange="reversed")

        driver_to_idx = {k: int(v) for k, v in self._driver_trace_idx.items()}

        # Embed Plotly inline to avoid QtWebEngine CDN blocking; shim insertRule to
        # suppress parse errors on CSS selectors (e.g. :focus-visible) in some Qt builds.
        plot_div = fig.to_html(
            full_html=False,
            include_plotlyjs=True,
            div_id="raceplot",
            config={"displayModeBar": False, "responsive": True},
        )
        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script>
    (function() {{
      if (!('CSSStyleSheet' in window) || !CSSStyleSheet.prototype) return;
      const orig = CSSStyleSheet.prototype.insertRule;
      if (typeof orig !== 'function') return;
      CSSStyleSheet.prototype.insertRule = function(rule, index) {{
        try {{ return orig.call(this, rule, index); }} catch (e) {{ return 0; }}
      }};
    }})();
  </script>
</head>
<body style="margin:0;background:#111;">
{plot_div}
<script>
  const DRIVER_TO_IDX = {driver_to_idx};
  const GD = document.getElementById('raceplot');

  function setDriverVisible(driver, visibleState) {{
    const idx = DRIVER_TO_IDX[driver];
    if (idx === undefined) return;
    Plotly.restyle(GD, {{visible: [visibleState]}}, [idx]);
  }}

  function setAllVisible(visibleState) {{
    const idxs = Object.values(DRIVER_TO_IDX);
    Plotly.restyle(GD, {{visible: idxs.map(() => visibleState)}}, idxs);
  }}
</script>
</body>
</html>"""
        self._load_html_in_webview(html)

    def _load_html_in_webview(self, html: str) -> None:
        tmp_dir = pathlib.Path(tempfile.gettempdir()) / "f1_pace_plotter"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / "raceplot.html"
        path.write_text(html, encoding="utf-8")
        self.browser.setUrl(QUrl.fromLocalFile(str(path)))


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    w = PacePlotterWindow()
    w.showMaximized()
    w.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())