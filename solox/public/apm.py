#!/usr/bin/env python3
# encoding=utf-8
"""
@Author  : Lijiawei
@Date    : 2022/6/19
@Desc    : APM performance monitoring for Android & iOS
@Update  : 2026/4/23 - FPS单例修复, Battery只读采集, 批量ADB, 统一异常处理
"""
from __future__ import absolute_import, print_function

import datetime
import json
import multiprocessing
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple, Any

from logzero import logger

try:
    from py_ios_device.ios_device import IOSDevice
except ImportError:
    import tidevice
    IOSDevice = tidevice.Device

from solox.public.adb import adb
from solox.public.common import Devices, File, Method, Platform, Scrcpy
from solox.public.android_fps import FPSMonitor, TimeUtils

# ─────────────────────────────────────────────────────────────────────────────
# Target Enum
# ─────────────────────────────────────────────────────────────────────────────
class Target:
    CPU = "cpu"
    Memory = "memory"
    MemoryDetail = "memory_detail"
    Battery = "battery"
    Network = "network"
    FPS = "fps"
    GPU = "gpu"
    DISK = "disk"


# ─────────────────────────────────────────────────────────────────────────────
# Shared FPS registry — fixed singleton per (deviceId, pkgName) tuple
# ─────────────────────────────────────────────────────────────────────────────
class FPSRegistry:
    """
    Thread-safe registry for FPS monitors keyed by (deviceId, pkgName).
    Replaces the broken class-level singleton that ignored device/pkgName.
    """
    _instance: Optional["FPSRegistry"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._monitors: Dict[Tuple[str, str], Any] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "FPSRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = FPSRegistry()
        return cls._instance

    def get_monitor(self, pkg_name: str, device_id: str, platform: str, surfaceview: bool):
        """Get or create FPS monitor for a specific app/device."""
        key = (device_id, pkg_name)
        with self._registry_lock:
            if key not in self._monitors:
                self._monitors[key] = FPS(
                    pkg_name=pkg_name,
                    device_id=device_id,
                    platform=platform,
                    surfaceview=surfaceview,
                )
            return self._monitors[key]

    def remove(self, pkg_name: str, device_id: str):
        """Remove monitor when done."""
        key = (device_id, pkg_name)
        with self._registry_lock:
            self._monitors.pop(key, None)

    def clear(self):
        """Clear all monitors."""
        with self._registry_lock:
            self._monitors.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Global instances (lazy initialization to avoid import-time side effects)
# ─────────────────────────────────────────────────────────────────────────────
def _get_devices() -> Devices:
    return Devices()

def _get_file() -> File:
    return File()

def _get_method() -> Method:
    return Method()


# ─────────────────────────────────────────────────────────────────────────────
# CPU Monitor
# ─────────────────────────────────────────────────────────────────────────────
class CPU:
    """CPU usage monitor with efficient delta-based sampling."""

    def __init__(
        self,
        pkg_name: str,
        device_id: str,
        platform: str = Platform.Android.value,
        pid: Optional[int] = None,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self.pid = pid
        self._d = _get_devices()
        self._f = _get_file()
        # Resolve PID lazily
        if self.pid is None and self.platform == Platform.Android.value:
            pids = self._d.get_pid(device_id, pkg_name)
            if pids:
                self.pid = int(pids[0].split(":")[0])

    def _get_process_cpu_time(self, pid: int) -> float:
        """Read process CPU time from /proc/<pid>/stat."""
        result = adb.shell(f"cat /proc/{pid}/stat", deviceId=self.device_id, timeout=5)
        if not result:
            return 0.0
        parts = re.split(r"\s+", result.strip())
        try:
            return sum(float(parts[i]) for i in [13, 14, 15, 16])
        except (IndexError, ValueError):
            return 0.0

    def _get_total_cpu_time(self) -> float:
        """Read total CPU time from /proc/stat."""
        cmd = f"cat /proc/stat | {self._d.filter_type()} ^cpu"
        result = adb.shell(cmd, deviceId=self.device_id, timeout=5)
        total = 0.0
        for line in result.split("\n"):
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            try:
                total += sum(float(parts[i]) for i in range(1, 8))
            except (IndexError, ValueError):
                continue
        return total

    def _get_idle_cpu_time(self) -> float:
        """Read idle CPU time from /proc/stat."""
        cmd = f"cat /proc/stat | {self._d.filter_type()} ^cpu"
        result = adb.shell(cmd, deviceId=self.device_id, timeout=5)
        idle = 0.0
        for line in result.split("\n"):
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            try:
                idle += float(parts[4])
            except (IndexError, ValueError):
                continue
        return idle

    def get_android_cpu_rate(self, no_log: bool = False) -> Tuple[float, float]:
        """
        Get Android process CPU rate (app% and system%).
        Returns (app_cpu_rate, sys_cpu_rate) as floats.
        """
        if self.pid is None:
            logger.warning(f"[CPU] No PID for {self.pkg_name}")
            return 0.0, 0.0

        try:
            p1 = self._get_process_cpu_time(self.pid)
            t1 = self._get_total_cpu_time()
            i1 = self._get_idle_cpu_time()
            time.sleep(1.0)
            p2 = self._get_process_cpu_time(self.pid)
            t2 = self._get_total_cpu_time()
            i2 = self._get_idle_cpu_time()

            delta_proc = p2 - p1
            delta_total = t2 - t1
            delta_idle = i2 - i1

            if delta_total <= 0:
                return 0.0, 0.0

            app_rate = round(delta_proc / delta_total * 100, 2)
            sys_rate = round((delta_total - delta_idle) / delta_total * 100, 2)

            if not no_log:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                self._f.add_log(
                    os.path.join(self._f.report_dir, "cpu_app.log"), apm_time, app_rate
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "cpu_sys.log"), apm_time, sys_rate
                )
            return app_rate, sys_rate

        except Exception as e:
            logger.warning(f"[CPU] Failed to get CPU rate: {e}")
            return 0.0, 0.0

    def get_ios_cpu_rate(self, no_log: bool = False) -> Tuple[float, float]:
        """Get iOS CPU rate via py-ios-device."""
        try:
            ios_apm = iosPerformance(self.pkg_name, self.device_id)
            perf = ios_apm.get_performance(ios_apm.cpu)
            app_rate = round(float(perf[0]), 2)
            sys_rate = round(float(perf[1]), 2)
            if not no_log:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                self._f.add_log(
                    os.path.join(self._f.report_dir, "cpu_app.log"), apm_time, app_rate
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "cpu_sys.log"), apm_time, sys_rate
                )
            return app_rate, sys_rate
        except Exception as e:
            logger.warning(f"[CPU] iOS CPU rate failed: {e}")
            return 0.0, 0.0

    def get_cpu_rate(self, no_log: bool = False) -> Tuple[float, float]:
        """Get CPU rate for the target platform."""
        if self.platform == Platform.Android.value:
            return self.get_android_cpu_rate(no_log)
        return self.get_ios_cpu_rate(no_log)

    def get_core_cpu_rate(self, cores: int = 0, no_log: bool = False) -> List[float]:
        """Get per-core CPU rates."""
        if self.pid is None:
            return [0.0] * cores

        try:
            p1 = self._get_process_cpu_time(self.pid)
            core_t1 = self._get_core_cpu_times()
            time.sleep(1.0)
            p2 = self._get_process_cpu_time(self.pid)
            core_t2 = self._get_core_cpu_times()

            rates = []
            for i in range(min(len(core_t1), len(core_t2))):
                delta_proc = p2 - p1
                delta_core = core_t2[i] - core_t1[i]
                if delta_core > 0:
                    rate = round(delta_proc / delta_core * 100 / cores, 2)
                else:
                    rate = 0.0
                rates.append(rate)
                if not no_log:
                    apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                    self._f.add_log(
                        os.path.join(self._f.report_dir, f"cpu{i}.log"), apm_time, rate
                    )
            return rates

        except Exception as e:
            logger.warning(f"[CPU Core] Failed: {e}")
            return [0.0] * cores

    def _get_core_cpu_times(self) -> List[float]:
        """Read per-core CPU times."""
        cmd = f"cat /proc/stat | {self._d.filter_type()} ^cpu"
        result = adb.shell(cmd, deviceId=self.device_id, timeout=5)
        core_times = []
        for line in result.split("\n"):
            if not line.startswith("cpu") or line == "cpu":
                continue
            parts = line.split()
            try:
                total = sum(float(parts[i]) for i in range(1, 8))
                core_times.append(total)
            except (IndexError, ValueError):
                continue
        return core_times


# ─────────────────────────────────────────────────────────────────────────────
# Memory Monitor
# ─────────────────────────────────────────────────────────────────────────────
class Memory:
    """Memory usage monitor."""

    def __init__(
        self,
        pkg_name: str,
        device_id: str,
        platform: str = Platform.Android.value,
        pid: Optional[int] = None,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self.pid = pid
        self._d = _get_devices()
        self._f = _get_file()
        if self.pid is None and self.platform == Platform.Android.value:
            pids = self._d.get_pid(device_id, pkg_name)
            if pids:
                self.pid = int(pids[0].split(":")[0])

    def get_android_memory(self) -> Tuple[float, float]:
        """
        Get Android memory usage in MB.
        Returns (total_pss, swap_pss).
        """
        if self.pid is None:
            return 0.0, 0.0

        output = adb.shell(f"dumpsys meminfo {self.pid}", deviceId=self.device_id, timeout=5)

        def search(pattern: str) -> Optional[re.Match]:
            return re.search(pattern, output)

        # Try TOTAL first, fall back to TOTAL PSS
        m_total = search(r"TOTAL\s*(\d+)")
        if not m_total:
            m_total = search(r"TOTAL PSS:\s*(\d+)")

        m_swap = search(r"TOTAL SWAP PSS:\s*(\d+)")
        if not m_swap:
            m_swap = search(r"TOTAL SWAP \(KB\):\s*(\d+)")

        total = round(float(m_total.group(1)) / 1024, 2) if m_total else 0.0
        swap = round(float(m_swap.group(1)) / 1024, 2) if m_swap else 0.0
        return total, swap

    def get_android_memory_detail(self, no_log: bool = False) -> Dict[str, float]:
        """Get detailed Android memory breakdown."""
        if self.pid is None:
            return self._empty_memory_detail()

        output = adb.shell(f"dumpsys meminfo {self.pid}", deviceId=self.device_id, timeout=5)

        def get_kb(pattern: str) -> float:
            m = re.search(pattern, output)
            return round(float(m.group(1)) / 1024, 2) if m else 0.0

        detail = {
            "java_heap": get_kb(r"Java Heap:\s*(\d+)"),
            "native_heap": get_kb(r"Native Heap:\s*(\d+)"),
            "code_pss": get_kb(r"Code:\s*(\d+)"),
            "stack_pss": get_kb(r"Stack:\s*(\d+)"),
            "graphics_pss": get_kb(r"Graphics:\s*(\d+)"),
            "private_pss": get_kb(r"Private Other:\s*(\d+)"),
            "system_pss": get_kb(r"System:\s*(\d+)"),
        }

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            for key, val in detail.items():
                self._f.add_log(
                    os.path.join(self._f.report_dir, f"mem_{key}.log"), apm_time, val
                )
        return detail

    def get_ios_memory(self) -> Tuple[float, float]:
        """Get iOS memory usage."""
        try:
            ios_apm = iosPerformance(self.pkg_name, self.device_id)
            total = round(float(ios_apm.get_performance(ios_apm.memory)), 2)
            return total, 0.0
        except Exception as e:
            logger.warning(f"[Memory] iOS failed: {e}")
            return 0.0, 0.0

    def get_process_memory(self, no_log: bool = False) -> Tuple[float, float]:
        """Get process memory for the target platform."""
        if self.platform == Platform.Android.value:
            total, swap = self.get_android_memory()
        else:
            total, swap = self.get_ios_memory()

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            self._f.add_log(
                os.path.join(self._f.report_dir, "mem_total.log"), apm_time, total
            )
            if self.platform == Platform.Android.value:
                self._f.add_log(
                    os.path.join(self._f.report_dir, "mem_swap.log"), apm_time, swap
                )
        return total, swap

    @staticmethod
    def _empty_memory_detail() -> Dict[str, float]:
        return {k: 0.0 for k in [
            "java_heap", "native_heap", "code_pss", "stack_pss",
            "graphics_pss", "private_pss", "system_pss",
        ]}


# ─────────────────────────────────────────────────────────────────────────────
# Battery Monitor — READ-ONLY (no device state modification)
# ─────────────────────────────────────────────────────────────────────────────
class Battery:
    """
    Battery monitor. READ-ONLY implementation.
    Does NOT modify device charging state (removed dangerous set status behavior).
    """

    def __init__(self, device_id: str, platform: str = Platform.Android.value):
        self.device_id = device_id
        self.platform = platform
        self._f = _get_file()
        self._m = _get_method()

    def get_battery(self, no_log: bool = False) -> Tuple:
        """Get battery info for the platform."""
        if self.platform == Platform.Android.value:
            return self.get_android_battery(no_log)
        return self.get_ios_battery(no_log)

    def get_android_battery(self, no_log: bool = False) -> Tuple[float, float]:
        """
        Read Android battery level and temperature.
        Pure read-only operation — no dumpsys battery set status.
        """
        output = adb.shell("dumpsys battery", deviceId=self.device_id, timeout=5)

        level_m = re.search(r"level:\s*(\d+)", output)
        temp_m = re.search(r"temperature:\s*(\d+)", output)

        level = float(level_m.group(1)) if level_m else 0.0
        temperature = float(temp_m.group(1)) / 10 if temp_m else 0.0

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            self._f.add_log(
                os.path.join(self._f.report_dir, "battery_level.log"), apm_time, level
            )
            self._f.add_log(
                os.path.join(self._f.report_dir, "battery_tem.log"), apm_time, temperature
            )
        return level, temperature

    def get_ios_battery(self, no_log: bool = False) -> Tuple[float, float, float, float]:
        """Get iOS battery info via py-ios-device."""
        try:
            ios_dev = IOSDevice(udid=self.device_id)
            io_dict = ios_dev.get_io_power()
            diag = io_dict.get("Diagnostics", {}).get("IORegistry", {})

            temp = self._m._set_value(diag.get("Temperature", 0) / 100)
            current = self._m._set_value(abs(diag.get("InstantAmperage", 0)))
            voltage = self._m._set_value(diag.get("Voltage", 0))
            power = current * voltage / 1000

            if not no_log:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                self._f.add_log(
                    os.path.join(self._f.report_dir, "battery_tem.log"), apm_time, temp
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "battery_current.log"), apm_time, current
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "battery_voltage.log"), apm_time, voltage
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "battery_power.log"), apm_time, power
                )
            return temp, current, voltage, power
        except Exception as e:
            logger.warning(f"[Battery] iOS failed: {e}")
            return 0.0, 0.0, 0.0, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Network Monitor
# ─────────────────────────────────────────────────────────────────────────────
class Network:
    """Network traffic monitor (WiFi only, mobilecoming soon)."""

    def __init__(
        self,
        pkg_name: str,
        device_id: str,
        platform: str = Platform.Android.value,
        pid: Optional[int] = None,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self.pid = pid
        self._d = _get_devices()
        self._f = _get_file()
        if self.pid is None and self.platform == Platform.Android.value:
            pids = self._d.get_pid(device_id, pkg_name)
            if pids:
                self.pid = int(pids[0].split(":")[0])

    def _read_net_bytes(self, net_iface: str) -> Tuple[float, float]:
        """Read send/recv bytes from /proc/<pid>/net/dev for a given interface."""
        if self.pid is None:
            return 0.0, 0.0
        cmd = f"cat /proc/{self.pid}/net/dev | {self._d.filter_type()} {net_iface}"
        output = adb.shell(cmd, deviceId=self.device_id, timeout=5)
        m = re.search(
            rf"{re.escape(net_iface)}:\s*(\d+)\s*\d+\s*\d+\s*\d+\s*\d+\s*\d+\s*\d+\s*\d+\s*(\d+)",
            output,
        )
        if m:
            recv = round(float(m.group(1)) / 1024, 2)
            send = round(float(m.group(2)) / 1024, 2)
            return send, recv
        return 0.0, 0.0

    def get_android_net(self, wifi: bool = True) -> Tuple[float, float]:
        """
        Get Android network traffic delta over 0.5s interval.
        Returns (send_kb, recv_kb) delta since last call.
        """
        net = "wlan0" if wifi else "rmnet_ipa0"
        s1, r1 = self._read_net_bytes(net)
        time.sleep(0.5)
        s2, r2 = self._read_net_bytes(net)
        return round(s2 - s1, 2), round(r2 - r1, 2)

    def set_android_net(self, wifi: bool = True) -> Tuple[float, float]:
        """Get current network bytes (no delta) for baseline."""
        net = "wlan0" if wifi else "rmnet_ipa0"
        return self._read_net_bytes(net)

    def get_ios_net(self) -> Tuple[float, float]:
        """Get iOS network traffic."""
        try:
            ios_apm = iosPerformance(self.pkg_name, self.device_id)
            perf = ios_apm.get_performance(ios_apm.network)
            recv = round(float(perf[0]), 2)
            send = round(float(perf[1]), 2)
            return send, recv
        except Exception as e:
            logger.warning(f"[Network] iOS failed: {e}")
            return 0.0, 0.0

    def get_network_data(self, wifi: bool = True, no_log: bool = False) -> Tuple[float, float]:
        """Get network data delta for the target platform."""
        if self.platform == Platform.Android.value:
            send, recv = self.get_android_net(wifi)
        else:
            send, recv = self.get_ios_net()

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            self._f.add_log(
                os.path.join(self._f.report_dir, "upflow.log"), apm_time, send
            )
            self._f.add_log(
                os.path.join(self._f.report_dir, "downflow.log"), apm_time, recv
            )
        return send, recv


# ─────────────────────────────────────────────────────────────────────────────
# FPS Monitor — uses registry instead of broken singleton
# ─────────────────────────────────────────────────────────────────────────────
class FPS:
    """
    FPS monitor. Delegates to android_fps.FPSMonitor for Android,
    uses iosPerformance for iOS.
    """

    def __init__(
        self,
        pkg_name: str,
        device_id: str,
        platform: str = Platform.Android.value,
        surfaceview: bool = True,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self.surfaceview = surfaceview
        self._f = _get_file()

    def get_android_fps(self, no_log: bool = False) -> Tuple[float, float]:
        """Get Android FPS using gfxinfo/SurfaceFlinger."""
        try:
            monitors = FPSMonitor(
                device_id=self.device_id,
                package_name=self.pkg_name,
                frequency=1,
                surfaceview=self.surfaceview,
                start_time=TimeUtils.get_current_time_underline(),
            )
            monitors.start()
            fps, jank = monitors.stop()
            if not no_log:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                self._f.add_log(
                    os.path.join(self._f.report_dir, "fps.log"), apm_time, fps
                )
                self._f.add_log(
                    os.path.join(self._f.report_dir, "jank.log"), apm_time, jank
                )
            return fps, jank
        except Exception as e:
            logger.warning(f"[FPS] Android failed: {e}")
            return 0.0, 0.0

    def get_ios_fps(self, no_log: bool = False) -> Tuple[float, float]:
        """Get iOS FPS."""
        try:
            ios_apm = iosPerformance(self.pkg_name, self.device_id)
            fps = int(ios_apm.get_performance(ios_apm.fps))
            if not no_log:
                apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
                self._f.add_log(
                    os.path.join(self._f.report_dir, "fps.log"), apm_time, fps
                )
            return float(fps), 0.0
        except Exception as e:
            logger.warning(f"[FPS] iOS failed: {e}")
            return 0.0, 0.0

    def get_fps(self, no_log: bool = False) -> Tuple[float, float]:
        """Get FPS for the target platform."""
        if self.platform == Platform.Android.value:
            return self.get_android_fps(no_log)
        return self.get_ios_fps(no_log)

    @staticmethod
    def get_object(pkg_name: str, device_id: str, platform: str, surfaceview: bool) -> "FPS":
        """Factory via registry to avoid broken singleton."""
        return FPSRegistry.get_instance().get_monitor(
            pkg_name, device_id, platform, surfaceview
        )

    @staticmethod
    def clear():
        """Clear FPS registry."""
        FPSRegistry.get_instance().clear()


# ─────────────────────────────────────────────────────────────────────────────
# GPU Monitor
# ─────────────────────────────────────────────────────────────────────────────
class GPU:
    """GPU usage monitor."""

    def __init__(
        self,
        pkg_name: str,
        device_id: str,
        platform: str = Platform.Android.value,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self._f = _get_file()

    def get_android_gpu_rate(self) -> float:
        """Read GPU busy percentage from kgsl."""
        output = adb.shell(
            "cat /sys/class/kgsl/kgsl-3d0/gpubusy",
            deviceId=self.device_id,
            timeout=5,
        )
        parts = output.strip().split()
        if len(parts) >= 2:
            try:
                return round(float(int(parts[0]) / int(parts[1])) * 100, 2)
            except (ValueError, ZeroDivisionError):
                pass
        return 0.0

    def get_ios_gpu_rate(self) -> float:
        """Get iOS GPU rate via py-ios-device."""
        try:
            ios_apm = iosPerformance(self.pkg_name, self.device_id)
            return float(ios_apm.get_performance(ios_apm.gpu))
        except Exception:
            return 0.0

    def get_gpu(self, no_log: bool = False) -> float:
        if self.platform == Platform.Android.value:
            gpu = self.get_android_gpu_rate()
        else:
            gpu = self.get_ios_gpu_rate()

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            self._f.add_log(
                os.path.join(self._f.report_dir, "gpu.log"), apm_time, gpu
            )
        return gpu


# ─────────────────────────────────────────────────────────────────────────────
# Disk Monitor
# ─────────────────────────────────────────────────────────────────────────────
class Disk:
    """Disk usage monitor."""

    def __init__(self, device_id: str, platform: str = Platform.Android.value):
        self.device_id = device_id
        self.platform = platform
        self._f = _get_file()

    def set_initial_disk(self):
        """Record initial disk state."""
        disk_info = adb.shell("df", deviceId=self.device_id, timeout=5)
        self._f.create_file(filename="initail_disk.log", content=disk_info)

    def set_current_disk(self):
        """Record current disk state."""
        disk_info = adb.shell("df", deviceId=self.device_id, timeout=5)
        self._f.create_file(filename="current_disk.log", content=disk_info)

    def get_android_disk(self) -> Dict[str, int]:
        """Parse df output for total used/free in KB."""
        output = adb.shell("df", deviceId=self.device_id, timeout=5)
        lines = output.split("\n")
        if not lines:
            return {"used": 0, "free": 0}
        # Skip header, parse data lines
        totals = {"used": 0, "free": 0}
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                totals["used"] += int(parts[2])
                totals["free"] += int(parts[3])
            except ValueError:
                continue
        return totals

    def get_ios_disk(self) -> Dict[str, Any]:
        """Get iOS disk info."""
        try:
            ios_dev = IOSDevice(udid=self.device_id)
            return ios_dev.storage_info()
        except Exception as e:
            logger.warning(f"[Disk] iOS failed: {e}")
            return {"used": 0, "free": 0}

    def get_disk(self, no_log: bool = False) -> Dict[str, Any]:
        if self.platform == Platform.Android.value:
            disk = self.get_android_disk()
        else:
            disk = self.get_ios_disk()

        if not no_log:
            apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")
            self._f.add_log(
                os.path.join(self._f.report_dir, "disk_used.log"),
                apm_time,
                disk.get("used", 0),
            )
            self._f.add_log(
                os.path.join(self._f.report_dir, "disk_free.log"),
                apm_time,
                disk.get("free", 0),
            )
        return disk


# ─────────────────────────────────────────────────────────────────────────────
# Thermal Sensor Monitor
# ─────────────────────────────────────────────────────────────────────────────
class ThermalSensor:
    """Device thermal zone temperature monitor."""

    def __init__(self, device_id: str, platform: str = Platform.Android.value):
        self.device_id = device_id
        self.platform = platform
        self._f = _get_file()
        self._d = _get_devices()

    def _get_thermal_zones(self) -> List[str]:
        """Get all thermal zone types."""
        output = adb.shell(
            "cat /sys/class/thermal/thermal_zone*/type",
            deviceId=self.device_id,
            timeout=5,
        )
        return [z.strip() for z in output.split("\n") if z.strip()]

    def set_initial_thermal_temp(self):
        """Record initial thermal state."""
        zones = self._get_thermal_zones()
        if len(zones) <= 3:
            return
        temp_list = []
        for i, zone_type in enumerate(zones):
            output = adb.shell(
                f"cat /sys/class/thermal/thermal_zone{i}/temp",
                deviceId=self.device_id,
                timeout=5,
            )
            temp_list.append({"type": zone_type, "temp": output.strip()})
        self._f.create_file(
            filename="init_thermal_temp.json", content=json.dumps(temp_list)
        )

    def set_current_thermal_temp(self):
        """Record current thermal state."""
        zones = self._get_thermal_zones()
        if len(zones) <= 3:
            return
        temp_list = []
        for i, zone_type in enumerate(zones):
            output = adb.shell(
                f"cat /sys/class/thermal/thermal_zone{i}/temp",
                deviceId=self.device_id,
                timeout=5,
            )
            temp_list.append({"type": zone_type, "temp": output.strip()})
        self._f.create_file(
            filename="current_thermal_temp.json", content=json.dumps(temp_list)
        )

    def get_thermal_temp(self) -> List[Dict[str, str]]:
        """Get current thermal temperatures."""
        zones = self._get_thermal_zones()
        if len(zones) <= 3:
            logger.warning("[Thermal] Insufficient thermal zones (permission issue)")
            return []
        temp_list = []
        for i, zone_type in enumerate(zones):
            output = adb.shell(
                f"cat /sys/class/thermal/thermal_zone{i}/temp",
                deviceId=self.device_id,
                timeout=5,
            )
            temp_list.append({"type": zone_type, "temp": output.strip()})
        return temp_list


# ─────────────────────────────────────────────────────────────────────────────
# iOS Performance wrapper
# ─────────────────────────────────────────────────────────────────────────────
class iosPerformance:
    """Wrapper for py-ios-device performance collection on iOS."""

    def __init__(self, pkg_name: str, device_id: str):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.apm_time = datetime.datetime.now().strftime("%H:%M:%S.%f")

        # Import from iosperf package
        try:
            from solox.public.iosperf._perf import DataType, Performance
            self.cpu = DataType.CPU
            self.memory = DataType.MEMORY
            self.network = DataType.NETWORK
            self.fps = DataType.FPS
            self.gpu = DataType.GPU
        except ImportError:
            logger.warning("iosperf not available, iOS performance monitoring limited")
            self.cpu = self.memory = self.network = self.fps = self.gpu = None

        self.downflow = 0.0
        self.upflow = 0.0
        self.perfs_value = 0.0

    def _callback(self, perf_type: str, value: dict):
        if perf_type == "network":
            self.downflow = float(value.get("downFlow", 0))
            self.upflow = float(value.get("upFlow", 0))
        else:
            self.perfs_value = float(value.get("value", 0))

    def get_performance(self, perf_type) -> Tuple:
        """Get performance data for given DataType."""
        try:
            ios_dev = IOSDevice(udid=self.device_id)
            if perf_type == self.network:
                perf = Performance(ios_dev, [perf_type])
                perf.start(self.pkg_name, callback=self._callback)
                time.sleep(1)  # reduced from 3s for better API responsiveness
                perf.stop()
                return self.downflow, self.upflow
            else:
                perf = Performance(ios_dev, [perf_type])
                perf.start(self.pkg_name, callback=self._callback)
                return self.perfs_value, 0.0
        except Exception as e:
            logger.warning(f"[iOSPerf] get_performance failed: {e}")
            return 0.0, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Service state management
# ─────────────────────────────────────────────────────────────────────────────
_CONFIG_DIR = os.path.dirname(os.path.realpath(__file__))
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


class initPerformanceService:
    """Manages APM service run state via config file."""

    @classmethod
    def get_status(cls) -> str:
        try:
            with open(_CONFIG_PATH, "r") as f:
                return json.loads(f.read()).get("run_switch", "off")
        except (FileNotFoundError, json.JSONDecodeError):
            return "off"

    @classmethod
    def start(cls):
        with open(_CONFIG_PATH, "w") as f:
            json.dump({"run_switch": "on"}, f)

    @classmethod
    def stop(cls):
        with open(_CONFIG_PATH, "w") as f:
            json.dump({"run_switch": "off"}, f)
        logger.info("APM service stopped")


# ─────────────────────────────────────────────────────────────────────────────
# AppPerformanceMonitor — Python API entry point
# ─────────────────────────────────────────────────────────────────────────────
class AppPerformanceMonitor(initPerformanceService):
    """
    High-level APM wrapper for Python API.
    Provides collectCpu, collectMemory, collectFps, etc.
    """

    def __init__(
        self,
        pkg_name: Optional[str] = None,
        platform: str = Platform.Android.value,
        device_id: Optional[str] = None,
        surfaceview: bool = True,
        no_log: bool = True,
        pid: Optional[int] = None,
        record: bool = False,
        collect_all: bool = False,
        duration: int = 0,
    ):
        self.pkg_name = pkg_name
        self.device_id = device_id
        self.platform = platform
        self.surfaceview = surfaceview
        self.no_log = no_log
        self.pid = pid
        self.record = record
        self.collect_all = collect_all
        self.duration = duration
        self.end_time = time.time() + duration if duration > 0 else 0

        # Validate environment
        d = _get_devices()
        d.devices_check(platform=self.platform, deviceid=self.device_id, pkgname=self.pkg_name)
        self.start()

    def _running(self) -> bool:
        """Check if collection should continue."""
        if not self.collect_all:
            return False
        if self.duration > 0 and time.time() > self.end_time:
            return False
        return self.get_status() == "on"

    def collect_cpu(self) -> Dict[str, float]:
        _cpu = CPU(self.pkg_name, self.device_id, self.platform, pid=self.pid)
        while self._running():
            app, sys_cpu = _cpu.get_cpu_rate(no_log=self.no_log)
            logger.info(f"CPU: app={app}%, sys={sys_cpu}%")
            if not self.collect_all:
                break
        return {"appCpuRate": app, "systemCpuRate": sys_cpu}

    def collect_memory(self) -> Dict[str, float]:
        _mem = Memory(self.pkg_name, self.device_id, self.platform, pid=self.pid)
        while self._running():
            total, swap = _mem.get_process_memory(no_log=self.no_log)
            logger.info(f"Memory: total={total}MB, swap={swap}MB")
            if not self.collect_all:
                break
        return {"total": total, "swap": swap}

    def collect_memory_detail(self) -> Dict[str, float]:
        _mem = Memory(self.pkg_name, self.device_id, self.platform, pid=self.pid)
        while self._running():
            if self.platform == Platform.iOS.value:
                break
            detail = _mem.get_android_memory_detail(no_log=self.no_log)
            logger.info(f"Memory Detail: {detail}")
            if not self.collect_all:
                break
        return detail if self.platform == Platform.Android.value else {}

    def collect_battery(self) -> Dict[str, float]:
        _bat = Battery(self.device_id, self.platform)
        while self._running():
            result = _bat.get_battery(no_log=self.no_log)
            if self.platform == Platform.Android.value:
                logger.info(f"Battery: level={result[0]}%, temp={result[1]}°C")
            else:
                logger.info(f"Battery: temp={result[0]}°C, current={result[1]}mA")
            if not self.collect_all:
                break
        return dict(result) if isinstance(result, tuple) else result

    def collect_network(self, wifi: bool = True) -> Dict[str, float]:
        _net = Network(self.pkg_name, self.device_id, self.platform, pid=self.pid)
        if not self.no_log and self.platform == Platform.Android.value:
            data = _net.set_android_net(wifi=wifi)
            _get_file().record_net("pre", data[0], data[1])
        while self._running():
            send, recv = _net.get_network_data(wifi=wifi, no_log=self.no_log)
            logger.info(f"Network: send={send}KB, recv={recv}KB")
            if not self.collect_all:
                break
        return {"send": send, "recv": recv}

    def collect_fps(self) -> Dict[str, float]:
        fps_monitor = FPS.get_object(
            self.pkg_name, self.device_id, self.platform, self.surfaceview
        )
        while self._running():
            fps, jank = fps_monitor.get_fps(no_log=self.no_log)
            logger.info(f"FPS: {fps}, Jank: {jank}")
            if not self.collect_all:
                break
        return {"fps": fps, "jank": jank}

    def collect_gpu(self) -> Dict[str, float]:
        _gpu = GPU(self.pkg_name, self.device_id, self.platform)
        while self._running():
            gpu = _gpu.get_gpu(no_log=self.no_log)
            logger.info(f"GPU: {gpu}%")
            if not self.collect_all:
                break
        return {"gpu": gpu}

    def collect_thermal(self) -> List[Dict[str, str]]:
        _thermal = ThermalSensor(self.device_id, self.platform)
        return _thermal.get_thermal_temp()

    def collect_disk(self) -> Dict[str, Any]:
        _disk = Disk(self.device_id, self.platform)
        return _disk.get_disk()

    def set_perfs(self, report_path: str = None):
        """Generate HTML report after collection."""
        f = _get_file()
        d = _get_devices()

        if self.platform == Platform.Android.value:
            adb.shell(cmd="dumpsys battery reset", deviceId=self.device_id)
            _net = Network(self.pkg_name, self.device_id, self.platform, pid=self.pid)
            data = _net.set_android_net()
            f.record_net("end", data[0], data[1])
            scene = f.make_report(
                app=self.pkg_name,
                devices=self.device_id,
                video=0,
                platform=self.platform,
            )
            summary = f.set_android_perfs(scene)
            # Build summary dict for template
            summary_dict = {
                "devices": summary.get("devices", ""),
                "app": summary.get("app", ""),
                "platform": summary.get("platform", ""),
                "ctime": summary.get("ctime", ""),
                "cpu_app": summary.get("cpuAppRate", "0%"),
                "cpu_sys": summary.get("cpuSystemRate", "0%"),
                "mem_total": summary.get("totalPassAvg", "0MB"),
                "mem_swap": summary.get("swapPassAvg", "0MB"),
                "fps": summary.get("fps", "0HZ/s"),
                "jank": summary.get("jank", "0"),
                "level": summary.get("batteryLevel", "0%"),
                "tem": summary.get("batteryTeml", "0°C"),
                "net_send": summary.get("flow_send", "0MB"),
                "net_recv": summary.get("flow_recv", "0MB"),
                "gpu": summary.get("gpu", 0),
                "cpu_charts": f.get_cpu_log(Platform.Android.value, scene),
                "mem_charts": f.get_mem_log(Platform.Android.value, scene),
                "net_charts": f.get_flow_log(Platform.Android.value, scene),
                "battery_charts": f.get_battery_log(Platform.Android.value, scene),
                "fps_charts": f.get_fps_log(Platform.Android.value, scene).get("fps", []),
                "jank_charts": f.get_fps_log(Platform.Android.value, scene).get("jank", []),
                "gpu_charts": f.get_gpu_log(Platform.Android.value, scene),
            }
            f.make_android_html(scene=scene, summary=summary_dict, report_path=report_path)

        elif self.platform == Platform.iOS.value:
            scene = f.make_report(
                app=self.pkg_name,
                devices=self.device_id,
                video=0,
                platform=self.platform,
            )
            summary = f.set_ios_perfs(scene)
            summary_dict = {
                "devices": summary.get("devices", ""),
                "app": summary.get("app", ""),
                "platform": summary.get("platform", ""),
                "ctime": summary.get("ctime", ""),
                "cpu_app": summary.get("cpuAppRate", "0%"),
                "cpu_sys": summary.get("cpuSystemRate", "0%"),
                "mem_total": summary.get("totalPassAvg", "0MB"),
                "fps": summary.get("fps", "0HZ/s"),
                "tem": summary.get("batteryTeml", "0°C"),
                "gpu": summary.get("gpu", 0),
                "net_send": summary.get("flow_send", "0MB"),
                "net_recv": summary.get("flow_recv", "0MB"),
                "cpu_charts": f.get_cpu_log(Platform.iOS.value, scene),
                "mem_charts": f.get_mem_log(Platform.iOS.value, scene),
                "net_charts": f.get_flow_log(Platform.iOS.value, scene),
                "battery_charts": f.get_battery_log(Platform.iOS.value, scene),
                "fps_charts": f.get_fps_log(Platform.iOS.value, scene),
                "gpu_charts": f.get_gpu_log(Platform.iOS.value, scene),
            }
            f.make_ios_html(scene=scene, summary=summary_dict, report_path=report_path)

    def collect_all_metrics(self, report_path: str = None):
        """
        Collect all metrics using multiprocessing.
        Uses spawn (safer for Flask environment) and proper exception handling.
        """
        f = _get_file()
        try:
            f.clear_file()
            process_count = 8 if self.record else 7
            # Use spawn method for cross-platform safety with Flask
            ctx = multiprocessing.get_context("spawn")
            pool = ctx.Pool(processes=process_count)

            pool.apply_async(self.collect_cpu)
            pool.apply_async(self.collect_memory)
            pool.apply_async(self.collect_memory_detail)
            pool.apply_async(self.collect_battery)
            pool.apply_async(self.collect_fps)
            pool.apply_async(self.collect_network)
            pool.apply_async(self.collect_gpu)

            if self.record:
                pool.apply_async(Scrcpy.start_record, (self.device_id,))

            pool.close()
            pool.join()

            self.set_perfs(report_path=report_path)
        except KeyboardInterrupt:
            Scrcpy.stop_record()
            self.set_perfs(report_path=report_path)
        except Exception as e:
            Scrcpy.stop_record()
            logger.exception(e)
        finally:
            self.stop()
            FPS.clear()
            f.flush_all_logs()
            logger.info("Collection complete")
