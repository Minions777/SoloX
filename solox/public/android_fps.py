# -*- coding: utf-8 -*-
"""
Android FPS monitoring via SurfaceFlinger / gfxinfo.
Refactored: thread-safe state, proper exception handling, removed bare excepts.
"""
from __future__ import absolute_import, print_function

import datetime
import queue
import re
import threading
import time
import traceback
from typing import Dict, List, Optional, Tuple, Any

from logzero import logger
from solox.public.adb import adb
from solox.public.common import Devices

d = Devices()

# Module-level defaults (read-only after init)
collect_fps = 0
collect_jank = 0
_state_lock = threading.Lock()


def _update_fps_jank(fps: int, jank: int):
    global collect_fps, collect_jank
    with _state_lock:
        collect_fps = fps
        collect_jank = jank


# ─────────────────────────────────────────────────────────────────────────────
# TimeUtils
# ─────────────────────────────────────────────────────────────────────────────
class TimeUtils:
    UnderLineFormatter = "%Y_%m_%d_%H_%M_%S"
    NormalFormatter = "%Y-%m-%d %H-%M-%S"
    ColonFormatter = "%Y-%m-%d %H:%M:%S"

    @staticmethod
    def get_current_time_underline() -> str:
        return time.strftime(TimeUtils.UnderLineFormatter, time.localtime())

    @staticmethod
    def get_current_time_normal() -> str:
        return time.strftime(TimeUtils.NormalFormatter, time.localtime())


# ─────────────────────────────────────────────────────────────────────────────
# SurfaceStatsCollector
# ─────────────────────────────────────────────────────────────────────────────
class SurfaceStatsCollector:
    """
    Collects and computes Android FPS using SurfaceFlinger (pre-Android 12)
    or gfxinfo framestats (Android 12+).
    """

    NANOSECONDS_PER_SECOND = 1e9
    PENDING_FENCE_TIMESTAMP = (1 << 63) - 1

    def __init__(
        self,
        device: str,
        frequency: float,
        package_name: str,
        fps_queue,
        jank_threshold: float,
        surfaceview: bool = True,
        use_legacy: bool = False,
    ):
        self.device = device
        self.frequency = frequency
        self.package_name = package_name
        self.fps_queue = fps_queue
        self.jank_threshold = jank_threshold / 1000.0
        self.surfaceview = surfaceview
        self.use_legacy_method = use_legacy
        self.surface_before: Dict[str, Any] = {}
        self.last_timestamp = 0.0
        self.data_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.focus_window: Optional[str] = None
        # Cached SDK version
        self._sdk_version: Optional[int] = None

    # ── SDK version caching ──────────────────────────────────────────────────

    def _get_sdk_version(self) -> int:
        """Cache SDK version to avoid repeated ADB calls."""
        if self._sdk_version is None:
            try:
                result = adb.shell("getprop ro.build.version.sdk", deviceId=self.device, timeout=5)
                self._sdk_version = int(result.strip())
            except (ValueError, TypeError):
                self._sdk_version = 0
        return self._sdk_version

    # ── Focus window detection ───────────────────────────────────────────────

    def _get_focus_activity(self) -> Optional[str]:
        """Get currently focused activity name via dumpsys window."""
        try:
            output = adb.shell("dumpsys window windows", deviceId=self.device, timeout=5)
            for line in output.split("\n"):
                if "mCurrentFocus" in line:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        # Handle u0 prefix: "u0" "com.example/.MainActivity" "{...}"
                        idx = 1 if parts[1] == "u0" else 1
                        activity = parts[idx].rstrip("}")
                        if "/" in activity:
                            return activity
                    elif len(parts) >= 2:
                        return parts[1].rstrip("}")
        except Exception as e:
            logger.warning(f"Failed to get focus activity: {e}")
        return None

    def _get_surfaceview_activity(self) -> Optional[str]:
        """Find SurfaceView activity name for the package."""
        try:
            output = adb.shell(
                f"dumpsys SurfaceFlinger --list | {d.filter_type()} {self.package_name}",
                deviceId=self.device,
                timeout=5,
            )
            lines = [l.strip() for l in output.split("\n") if l.strip()]

            # Priority: SurfaceView lines that contain the package name
            for line in lines:
                if line.startswith("SurfaceView") and self.package_name in line:
                    # Format: "SurfaceView[...] com.example/com.example.X activity"
                    parts = line.split()
                    if len(parts) >= 3:
                        return parts[2]
                    # Try parsing without spaces
                    cleaned = line.replace("SurfaceView", "").replace("[", "").replace("]", "").replace("-", " ").strip()
                    tokens = cleaned.split()
                    for tok in tokens:
                        if "/" in tok:
                            return tok

            # Fallback: last line
            if lines:
                last = lines[-1]
                if self.package_name in last:
                    return last.split()[-1] if " " in last else last
        except Exception as e:
            logger.warning(f"Failed to get surfaceview activity: {e}")
        return None

    def _get_focus_window(self) -> Optional[str]:
        """Get the focus window name for SurfaceFlinger latency."""
        if self.use_legacy_method:
            return None

        try:
            activity = self._get_focus_activity()
            if activity:
                # Escape $ in activity names for shell
                return activity.replace("$", "\\$")
        except Exception as e:
            logger.warning(f"Failed to get focus window: {e}")

        # Fallback to surfaceview
        sv = self._get_surfaceview_activity()
        if sv:
            return sv.replace("$", "\\$")
        return None

    # ── SurfaceFlinger latency data ─────────────────────────────────────────

    def _clear_surfaceflinger_latency(self):
        """Clear SurfaceFlinger latency data. Returns True if supported."""
        window = self.focus_window or ""
        cmd = f"dumpsys SurfaceFlinger --latency-clear {window}".strip()
        result = adb.shell(cmd, deviceId=self.device, timeout=5)
        return len(result) == 0

    def _get_surface_stats_legacy(self) -> Optional[Dict[str, Any]]:
        """Legacy method using service call SurfaceFlinger 1013."""
        try:
            result = adb.shell(
                "service call SurfaceFlinger 1013", deviceId=self.device, timeout=5
            )
            m = re.search(r"Result: Parcel\((\w+)", result)
            if m:
                return {
                    "page_flip_count": int(m.group(1), 16),
                    "timestamp": datetime.datetime.now(),
                }
        except Exception as e:
            logger.debug(f"Legacy surface stats failed: {e}")
        return None

    # ── gfxinfo framestats (Android 12+) ────────────────────────────────────

    def _get_gfxinfo_framestats_data(self) -> Tuple[Optional[float], List[List[float]]]:
        """
        Android 12+ (SDK 31+) FPS via gfxinfo framestats.
        Returns (refresh_period_seconds, timestamps[[intended_vsync, vsync, frame_completed], ...]).
        """
        try:
            # Reset and wait for fresh data
            adb.shell(
                f"dumpsys gfxinfo {self.package_name} reset",
                deviceId=self.device,
                timeout=5,
            )
            time.sleep(1.0)

            output = adb.shell(
                f"dumpsys gfxinfo {self.package_name} framestats",
                deviceId=self.device,
                timeout=10,
            )
            lines = output.replace("\r\n", "\n").splitlines()
            if not lines:
                return None, []

            # Find window section
            activity = self.focus_window.split("#")[0] if self.focus_window and "#" in self.focus_window else (self.focus_window or "")

            in_window = False
            timestamps: List[List[float]] = []

            for line in lines:
                if not in_window:
                    if "Window" in line and activity in line:
                        in_window = True
                    continue

                if "PROFILEDATA" in line:
                    continue

                fields = line.split(",")
                if len(fields) < 14:
                    continue

                try:
                    intended_vsync = int(fields[0])
                    vsync = int(fields[1])
                    frame_completed = int(fields[13])

                    if intended_vsync == 0 or vsync == 0:
                        continue

                    timestamps.append([
                        intended_vsync / self.NANOSECONDS_PER_SECOND,
                        vsync / self.NANOSECONDS_PER_SECOND,
                        frame_completed / self.NANOSECONDS_PER_SECOND,
                    ])
                except (ValueError, IndexError):
                    continue

            return 0.016667, timestamps  # ~60fps

        except Exception as e:
            logger.debug(f"gfxinfo framestats failed: {e}")
            return None, []

    # ── SurfaceFlinger --latency (pre-Android 12) ─────────────────────────

    def _get_surfaceflinger_frame_data(self) -> Tuple[Optional[float], List[List[float]]]:
        """
        Main entry point for frame timing data.
        Dispatches to gfxinfo (Android 12+) or --latency (older).
        """
        sdk = self._get_sdk_version()

        # Android 12+: use gfxinfo framestats
        if sdk >= 31:
            return self._get_gfxinfo_framestats_data()

        # Pre-Android 12: use --latency
        return self._get_surfaceflinger_latency_data()

    def _get_surfaceflinger_latency_data(self) -> Tuple[Optional[float], List[List[float]]]:
        """SurfaceFlinger --latency method for Android < 12."""
        if not self.surfaceview:
            return self._get_gfxinfo_latency_fallback()

        # Get window name
        focus = self._get_surfaceview_activity()
        if not focus:
            return None, []

        window = focus.replace("$", "\\$")
        cmd = f'dumpsys SurfaceFlinger --latency "{window}"'
        output = adb.shell(cmd, deviceId=self.device, timeout=5)
        lines = output.replace("\r\n", "\n").splitlines()

        if len(lines) <= 1:
            # Try get_surfaceview_activity fallback
            alt = self._get_surfaceview_activity()
            if alt:
                window = alt.replace("$", "\\$")
                cmd = f'dumpsys SurfaceFlinger --latency "{window}"'
                output = adb.shell(cmd, deviceId=self.device, timeout=5)
                lines = output.replace("\r\n", "\n").splitlines()

        if not lines or not lines[0].isdigit():
            return None, []

        try:
            refresh_period = int(lines[0]) / self.NANOSECONDS_PER_SECOND
        except (ValueError, IndexError):
            return None, []

        timestamps: List[List[float]] = []
        for line in lines[2:]:
            fields = line.split()
            if len(fields) != 3:
                continue
            try:
                ts0, ts1, ts2 = int(fields[0]), int(fields[1]), int(fields[2])
                if ts1 == self.PENDING_FENCE_TIMESTAMP:
                    continue
                timestamps.append([
                    ts0 / self.NANOSECONDS_PER_SECOND,
                    ts1 / self.NANOSECONDS_PER_SECOND,
                    ts2 / self.NANOSECONDS_PER_SECOND,
                ])
            except ValueError:
                continue

        return refresh_period, timestamps

    def _get_gfxinfo_latency_fallback(self) -> Tuple[Optional[float], List[List[float]]]:
        """gfxinfo framestats fallback when surfaceview=False."""
        output = adb.shell(
            f"dumpsys gfxinfo {self.package_name} framestats",
            deviceId=self.device,
            timeout=10,
        )
        lines = output.replace("\r\n", "\n").splitlines()
        if not lines:
            return None, []

        activity = (self.focus_window or "").split("#")[0]
        in_window = False
        timestamps: List[List[float]] = []
        profile_line_count = 0

        for line in lines:
            if not in_window:
                if "Window" in line and activity in line:
                    in_window = True
                continue

            if "PROFILEDATA" in line:
                profile_line_count += 1
                continue

            fields = line.split(",")
            if len(fields) < 14:
                continue

            try:
                if fields[0].strip() == "0":
                    ts = [int(fields[i]) for i in [1, 2, 13]]
                    if ts[1] == self.PENDING_FENCE_TIMESTAMP:
                        continue
                    timestamps.append([t / self.NANOSECONDS_PER_SECOND for t in ts])
            except (ValueError, IndexError):
                continue

            if profile_line_count >= 2:
                break

        return 0.016667, timestamps

    # ── FPS/Jank calculation ────────────────────────────────────────────────

    @staticmethod
    def _calculate_jank_simple(timestamps: List[List[float]], threshold: float) -> int:
        """Simple per-frame jank detection."""
        jank = 0
        prev_ts = 0.0
        for ts in timestamps:
            if prev_ts == 0.0:
                prev_ts = ts[1]
                continue
            if ts[1] - prev_ts > threshold:
                jank += 1
            prev_ts = ts[1]
        return jank

    @staticmethod
    def _calculate_jank_advanced(timestamps: List[List[float]], threshold: float) -> int:
        """
        Advanced jank detection using frame time variance.
        Detects frames that exceed expected frame time by more than threshold.
        """
        jank = 0
        if len(timestamps) < 5:
            return SurfaceStatsCollector._calculate_jank_simple(timestamps, threshold)

        for i in range(4, len(timestamps)):
            # Calculate expected frame time from last 3 frames
            dt1 = timestamps[i - 3][1] - timestamps[i - 4][1]
            dt2 = timestamps[i - 2][1] - timestamps[i - 3][1]
            dt3 = timestamps[i - 1][1] - timestamps[i - 2][1]
            expected = (dt1 + dt2 + dt3) / 3 * 2

            actual = timestamps[i][1] - timestamps[i - 1][1]
            if actual > expected and actual > threshold:
                jank += 1

        return jank

    def _compute_fps_jank(
        self, refresh_period: Optional[float], timestamps: List[List[float]]
    ) -> Tuple[int, int]:
        """Compute FPS and jank from frame timestamps."""
        count = len(timestamps)
        if count == 0:
            return 0, 0
        if count == 1:
            return 1, 0

        # Use last timestamp - first timestamp for duration
        duration = timestamps[-1][1] - timestamps[0][1]
        if duration <= 0:
            return 1, 0

        fps = int(round((count - 1) / duration))

        if count <= 4:
            jank = self._calculate_jank_simple(timestamps, self.jank_threshold)
        else:
            jank = self._calculate_jank_advanced(timestamps, self.jank_threshold)

        return min(fps, 120), jank  # Cap at reasonable max

    # ── Collection threads ──────────────────────────────────────────────────

    def _collector_thread(self):
        """Thread that continuously collects frame data."""
        is_first = True
        while not self.stop_event.is_set():
            try:
                before = time.time()
                if self.use_legacy_method:
                    state = self._get_surface_stats_legacy()
                    if state:
                        self.data_queue.put(state)
                else:
                    refresh_period, new_ts = self._get_surfaceflinger_frame_data()
                    if refresh_period is None or not new_ts:
                        cur = self._get_focus_window()
                        if cur and cur != self.focus_window:
                            self.focus_window = cur
                        time.sleep(0.1)
                        continue

                    # Filter timestamps newer than last seen
                    new_ts = [ts for ts in new_ts if ts[1] > self.last_timestamp]
                    if new_ts:
                        if not is_first:
                            new_ts.insert(0, [0, self.last_timestamp, 0])
                        else:
                            is_first = False
                        self.last_timestamp = new_ts[-1][1]
                        self.data_queue.put((refresh_period, new_ts, time.time()))
                    else:
                        is_first = True

                elapsed = time.time() - before
                sleep_time = self.frequency - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.warning(f"[_collector_thread] Error: {e}")
                time.sleep(0.5)

        self.data_queue.put("Stop")

    def _calculator_thread(self, start_time):
        """Thread that computes FPS/jank from collected data."""
        while True:
            try:
                data = self.data_queue.get(timeout=2)
                if isinstance(data, str) and data == "Stop":
                    break

                before = time.time()
                if self.use_legacy_method:
                    td = data["timestamp"] - self.surface_before.get("timestamp", data["timestamp"])
                    seconds = td.seconds + td.microseconds / 1e6
                    frame_count = data["page_flip_count"] - self.surface_before.get("page_flip_count", 0)
                    if seconds > 0 and frame_count > 0:
                        fps = min(int(round(frame_count / seconds)), 60)
                    else:
                        fps = 0
                    jank = 0
                    self.surface_before = data
                else:
                    refresh_period, timestamps, _ = data
                    fps, jank = self._compute_fps_jank(refresh_period, timestamps)

                _update_fps_jank(fps, jank)

                elapsed = time.time() - before
                sleep_time = self.frequency - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except queue.Empty:
                continue
            except Exception as e:
                logger.warning(f"[_calculator_thread] Error: {e}")
                time.sleep(0.5)

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, start_time: str):
        """Start collection and calculation threads."""
        if not self.use_legacy_method:
            self.focus_window = self._get_focus_window()
            if self.focus_window is None:
                logger.debug("No focus window found, using legacy method")
                self.use_legacy_method = True
                self.surface_before = self._get_surface_stats_legacy() or {}

        t_collect = threading.Thread(target=self._collector_thread, daemon=True)
        t_calc = threading.Thread(target=self._calculator_thread, args=(start_time,), daemon=True)
        t_collect.start()
        t_calc.start()

    def stop(self) -> Tuple[int, int]:
        """Stop threads and return final FPS/jank."""
        global collect_fps, collect_jank
        self.stop_event.set()
        # Give threads time to finish
        time.sleep(0.5)
        with _state_lock:
            return collect_fps, collect_jank


# ─────────────────────────────────────────────────────────────────────────────
# Monitor base class
# ─────────────────────────────────────────────────────────────────────────────
class Monitor:
    """Base monitor class."""

    def __init__(self, **kwargs):
        self.config = kwargs
        self.matched_data: Dict = {}

    def start(self):
        logger.warning(f"start() not implemented in {type(self).__name__}")

    def clear(self):
        self.matched_data = {}

    def stop(self):
        logger.warning(f"stop() not implemented in {type(self).__name__}")

    def save(self):
        logger.warning(f"save() not implemented in {type(self).__name__}")


# ─────────────────────────────────────────────────────────────────────────────
# FPSMonitor
# ─────────────────────────────────────────────────────────────────────────────
class FPSMonitor(Monitor):
    """
    High-level FPS monitor.
    Usage:
        monitor = FPSMonitor(device_id='xxx', package_name='com.example.app')
        monitor.start()
        time.sleep(5)
        fps, jank = monitor.stop()
    """

    def __init__(
        self,
        device_id: str,
        package_name: Optional[str] = None,
        frequency: float = 1.0,
        timeout: float = 24 * 3600,
        fps_queue=None,
        jank_threshold: float = 166,
        use_legacy: bool = False,
        surfaceview: bool = True,
        start_time: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.start_time = start_time
        self.use_legacy = use_legacy
        self.frequency = frequency
        self.jank_threshold = jank_threshold
        self.device = device_id
        self.timeout = timeout
        self.surfaceview = surfaceview
        self.package = package_name
        self.fpscollector = SurfaceStatsCollector(
            device=device_id,
            frequency=frequency,
            package_name=package_name,
            fps_queue=fps_queue,
            jank_threshold=jank_threshold,
            surfaceview=surfaceview,
            use_legacy=use_legacy,
        )

    def start(self):
        self.fpscollector.start(self.start_time)

    def stop(self) -> Tuple[int, int]:
        return self.fpscollector.stop()

    def save(self):
        pass

    def parse(self, file_path: str):
        pass

    def get_fps_collector(self):
        return self.fpscollector
