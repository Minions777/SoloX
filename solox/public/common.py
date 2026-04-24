#!/usr/bin/env python3
# encoding=utf-8
"""
@Author  : Lijiawei
@Date    : 2022/6/19
@Desc    : Common utilities for SoloX
@Update  : 2026/4/23 - subprocess替代popen, BufferedLogWriter, 统一异常处理
"""
from __future__ import absolute_import, print_function

import json
import os
import platform
import re
import shutil
import subprocess
import time
import signal
import socket
import ssl
from functools import wraps
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

import requests
import cv2
import psutil
from logzero import logger
from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader

try:
    from py_ios_device.ios_device import Device
    from py_ios_device.usbmux import Usbmux
except ImportError:
    from tidevice._device import Device
    from tidevice import Usbmux

from solox.public.adb import adb


# ─────────────────────────────────────────────────────────────────────────────
# Platform Enum
# ─────────────────────────────────────────────────────────────────────────────
class Platform(Enum):
    Android = "Android"
    iOS = "iOS"
    Mac = "MacOS"
    Windows = "Windows"

    @classmethod
    def from_string(cls, value: str) -> "Platform":
        """Safe platform from string, case-insensitive."""
        value = value.strip().lower()
        for p in cls:
            if p.value.lower() == value:
                return p
        raise ValueError(f"Unknown platform: {value}")


# ─────────────────────────────────────────────────────────────────────────────
# Buffered Log Writer — reduces disk IO from N opens per second to 1 per batch
# ─────────────────────────────────────────────────────────────────────────────
class BufferedLogWriter:
    """
    Accumulates log entries in memory and flushes to disk in batches.
    Dramatically reduces disk IO for high-frequency APM collection.
    """

    def __init__(self, path: str, flush_interval: int = 10, max_buffer: int = 100):
        self.path = path
        self._buffer: List[str] = []
        self._flush_interval = flush_interval  # seconds
        self._max_buffer = max_buffer  # entries
        self._last_flush = time.time()
        self._lock = __import__("threading").Lock()
        self._ensure_dir()

    def _ensure_dir(self):
        dir_path = os.path.dirname(self.path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

    def write(self, line: str):
        """Thread-safe write with auto-flush."""
        with self._lock:
            self._buffer.append(line)
            should_flush = (
                len(self._buffer) >= self._max_buffer
                or time.time() - self._last_flush >= self._flush_interval
            )
            if should_flush:
                self._flush()

    def _flush(self):
        if not self._buffer:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.writelines(self._buffer)
            self._buffer.clear()
            self._last_flush = time.time()
        except Exception as e:
            logger.warning(f"Log flush failed: {e}")

    def flush(self):
        """Force flush."""
        with self._lock:
            self._flush()


class File:
    """
    File operations with buffered logging and improved resource management.
    """

    # Global log writer registry: path -> BufferedLogWriter
    _log_writers: Dict[str, BufferedLogWriter] = {}
    _writers_lock = __import__("threading").Lock()

    def __init__(self, fileroot: str = "."):
        self.fileroot = fileroot
        self.report_dir = self.get_report_dir()

    def _get_log_writer(self, path: str) -> BufferedLogWriter:
        """Get or create a buffered log writer for a path."""
        with self._writers_lock:
            if path not in self._log_writers:
                self._log_writers[path] = BufferedLogWriter(path)
            return self._log_writers[path]

    def clear_file(self):
        """Clean up useless files from report directory."""
        logger.info("Cleaning up report files ...")
        if not os.path.exists(self.report_dir):
            return
        for f in os.listdir(self.report_dir):
            filepath = os.path.join(self.report_dir, f)
            try:
                if os.path.isfile(filepath) and f.split(".")[-1] in ["log", "json", "mkv"]:
                    os.remove(filepath)
            except Exception as e:
                logger.warning(f"Failed to remove {filepath}: {e}")
        # Clear log writers
        with self._writers_lock:
            self._log_writers.clear()
        logger.info("Report cleanup complete")

    def add_log(self, path: str, log_time: str, value: Any):
        """Write a log entry using buffered I/O."""
        if value < 0:
            return
        writer = self._get_log_writer(path)
        writer.write(f"{log_time}={value}\n")

    def flush_all_logs(self):
        """Force flush all buffered log writers."""
        with self._writers_lock:
            for writer in self._log_writers.values():
                writer.flush()

    def export_excel(self, platform: str, scene: str) -> str:
        """Export log data to Excel file."""
        import xlwt

        logger.info("Exporting Excel ...")
        android_log_file_list = [
            "cpu_app", "cpu_sys", "mem_total", "mem_swap",
            "battery_level", "battery_tem", "upflow", "downflow", "fps", "gpu",
        ]
        ios_log_file_list = [
            "cpu_app", "cpu_sys", "mem_total",
            "battery_tem", "battery_current", "battery_voltage", "battery_power",
            "upflow", "downflow", "fps", "gpu",
        ]
        log_file_list = android_log_file_list if platform == Platform.Android.value else ios_log_file_list

        wb = xlwt.Workbook(encoding="utf-8")
        for name in log_file_list:
            ws = wb.add_sheet(name)
            ws.write(0, 0, "Time")
            ws.write(0, 1, "Value")
            log_path = os.path.join(self.report_dir, scene, f"{name}.log")
            if not os.path.exists(log_path):
                continue
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    for row, line in enumerate(f, start=1):
                        parts = line.strip().split("=", 1)
                        if len(parts) == 2:
                            ws.write(row, 0, parts[0])
                            ws.write(row, 1, parts[1])
            except Exception as e:
                logger.warning(f"Failed to read {log_path}: {e}")

        xls_path = os.path.join(self.report_dir, scene, f"{scene}.xls")
        wb.save(xls_path)
        logger.info(f"Excel export complete: {xls_path}")
        return xls_path

    def make_android_html(self, scene: str, summary: Dict, report_path: str = None) -> str:
        """Generate Android HTML report."""
        logger.info("Generating Android HTML report ...")
        STATICPATH = os.path.dirname(os.path.realpath(__file__))
        env = Environment(loader=FileSystemLoader(os.path.join(STATICPATH, "report_template")))
        template = env.get_template("android.html")
        html_path = report_path or os.path.join(self.report_dir, scene, "report.html")
        os.makedirs(os.path.dirname(html_path), exist_ok=True)
        try:
            html_content = template.render(**summary)
            with open(html_path, "w", encoding="utf-8") as fout:
                fout.write(html_content)
            logger.info(f"Android HTML report generated: {html_path}")
        except Exception as e:
            logger.exception(e)
        return html_path

    def make_ios_html(self, scene: str, summary: Dict, report_path: str = None) -> str:
        """Generate iOS HTML report."""
        logger.info("Generating iOS HTML report ...")
        STATICPATH = os.path.dirname(os.path.realpath(__file__))
        env = Environment(loader=FileSystemLoader(os.path.join(STATICPATH, "report_template")))
        template = env.get_template("ios.html")
        html_path = report_path or os.path.join(self.report_dir, scene, "report.html")
        os.makedirs(os.path.dirname(html_path), exist_ok=True)
        try:
            html_content = template.render(**summary)
            with open(html_path, "w", encoding="utf-8") as fout:
                fout.write(html_content)
            logger.info(f"iOS HTML report generated: {html_path}")
        except Exception as e:
            logger.exception(e)
        return html_path

    def filter_scene(self, scene: str) -> List[str]:
        """Return report directories sorted by modification time, excluding current."""
        dirs = os.listdir(self.report_dir)
        dir_list = sorted(dirs, key=lambda x: os.path.getmtime(os.path.join(self.report_dir, x)), reverse=True)
        if scene in dir_list:
            dir_list.remove(scene)
        return dir_list

    def get_report_dir(self) -> str:
        """Get or create the report directory."""
        report_dir = os.path.join(os.getcwd(), "report")
        if not os.path.exists(report_dir):
            os.makedirs(report_dir, exist_ok=True)
        return report_dir

    def create_file(self, filename: str, content: str = ""):
        """Create a file with optional content."""
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir, exist_ok=True)
        filepath = os.path.join(self.report_dir, filename)
        with open(filepath, "a+", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def record_net(self, net_type: str, send: float, recv: float):
        """Record network data to JSON file."""
        net_dict = {"send": send, "recv": recv}
        content = json.dumps(net_dict)
        filename = f"{net_type}_net.json"
        self.create_file(filename=filename, content=content)

    def make_report(self, app: str, devices: str, video: str, platform: str = "Android", model: str = "normal", cores: int = 0) -> str:
        """Generate test report and organize log files."""
        logger.info("Generating test results ...")
        current_time = time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())
        result_dict = {
            "app": app,
            "icon": "",
            "platform": platform,
            "model": model,
            "devices": devices,
            "ctime": current_time,
            "video": video,
            "cores": cores,
        }
        self.create_file(filename="result.json", content=json.dumps(result_dict))

        report_new_dir = os.path.join(self.report_dir, f"apm_{current_time}")
        if not os.path.exists(report_new_dir):
            os.makedirs(report_new_dir, exist_ok=True)

        for f in os.listdir(self.report_dir):
            filepath = os.path.join(self.report_dir, f)
            if os.path.isfile(filepath) and f.split(".")[-1] in ["log", "json", "mkv"]:
                shutil.move(filepath, report_new_dir)

        logger.info(f"Test results generated: {report_new_dir}")
        # Flush all buffered logs
        self.flush_all_logs()
        return f"apm_{current_time}"

    def open_file(self, path: str, mode: str = "r"):
        """Generator that yields lines from a file."""
        if not os.path.exists(path):
            return
        with open(path, mode, encoding="utf-8") as f:
            for line in f:
                yield line

    def read_json(self, scene: str) -> Dict:
        """Read result.json for a scene."""
        path = os.path.join(self.report_dir, scene, "result.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")
            return {}

    def read_log(self, scene: str, filename: str) -> Tuple[List[Dict], List[float]]:
        """
        Read APM log file and return (point_list, value_list).
        point_list: [{'x': time_str, 'y': value}, ...]
        value_list: [v1, v2, ...]
        """
        log_data_list = []
        target_data_list = []
        path = os.path.join(self.report_dir, scene, filename)
        if not os.path.exists(path):
            return log_data_list, target_data_list

        for line in self.open_file(path, "r"):
            if "=" not in line:
                continue
            parts = line.strip().split("=", 1)
            if len(parts) != 2:
                continue
            time_str, val_str = parts
            try:
                value = float(val_str) if "." in val_str else int(val_str)
                log_data_list.append({"x": time_str.strip(), "y": value})
                target_data_list.append(value)
            except ValueError:
                continue
        return log_data_list, target_data_list

    # ── Log aggregation helpers ─────────────────────────────────────────────

    def get_cpu_log(self, platform: str, scene: str) -> Dict:
        app_data = self.read_log(scene=scene, filename="cpu_app.log")[0]
        sys_data = self.read_log(scene=scene, filename="cpu_sys.log")[0]
        return {"status": 1, "cpuAppData": app_data, "cpuSysData": sys_data}

    def get_mem_log(self, platform: str, scene: str) -> Dict:
        result = {"status": 1, "memTotalData": self.read_log(scene=scene, filename="mem_total.log")[0]}
        if platform == Platform.Android.value:
            result["memSwapData"] = self.read_log(scene=scene, filename="mem_swap.log")[0]
        return result

    def get_fps_log(self, platform: str, scene: str) -> Dict:
        result = {"status": 1, "fps": self.read_log(scene=scene, filename="fps.log")[0]}
        if platform == Platform.Android.value:
            result["jank"] = self.read_log(scene=scene, filename="jank.log")[0]
        return result

    def approximate_size(self, size: int, binary: bool = True) -> str:
        """Convert bytes to human-readable string."""
        if size < 0:
            raise ValueError("size must be non-negative")
        multiple = 1024 if binary else 1000
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        for unit in units[:-1]:
            size /= multiple
            if size < multiple:
                return f"{size:.2f} {unit}"
        return f"{size:.2f} {units[-1]}"

    def set_android_perfs(self, scene: str) -> Dict:
        """Aggregate Android APM data."""
        info = self.read_json(scene)
        cpu_app = self.read_log(scene, "cpu_app.log")[1]
        cpu_sys = self.read_log(scene, "cpu_sys.log")[1]
        mem_total = self.read_log(scene, "mem_total.log")[1]
        mem_swap = self.read_log(scene, "mem_swap.log")[1]
        fps = self.read_log(scene, "fps.log")[1]
        jank = self.read_log(scene, "jank.log")[1]
        battery_level = self.read_log(scene, "battery_level.log")[1]
        battery_tem = self.read_log(scene, "battery_tem.log")[1]
        gpu = self.read_log(scene, "gpu.log")[1]

        # Network delta
        pre_net_path = os.path.join(self.report_dir, scene, "pre_net.json")
        end_net_path = os.path.join(self.report_dir, scene, "end_net.json")
        send = recv = 0
        if os.path.exists(pre_net_path) and os.path.exists(end_net_path):
            try:
                with open(pre_net_path) as f:
                    pre = json.loads(f.read())
                with open(end_net_path) as f:
                    end = json.loads(f.read())
                send = end["send"] - pre["send"]
                recv = end["recv"] - pre["recv"]
            except Exception:
                pass

        def avg(data): return round(sum(data) / len(data), 2) if data else 0

        return {
            "app": info.get("app"),
            "devices": info.get("devices"),
            "platform": info.get("platform"),
            "ctime": info.get("ctime"),
            "cpuAppRate": f"{avg(cpu_app)}%",
            "cpuSystemRate": f"{avg(cpu_sys)}%",
            "totalPassAvg": f"{avg(mem_total)}MB",
            "swapPassAvg": f"{avg(mem_swap)}MB",
            "fps": f"{int(avg(fps))}HZ/s" if fps else "0HZ/s",
            "jank": str(int(sum(jank))) if jank else "0",
            "flow_send": f"{round(send / 1024, 2)}MB",
            "flow_recv": f"{round(recv / 1024, 2)}MB",
            "batteryLevel": f"{battery_level[-1] if battery_level else 0}%",
            "batteryTeml": f"{battery_tem[-1] if battery_tem else 0}°C",
            "gpu": avg(gpu),
        }

    def set_ios_perfs(self, scene: str) -> Dict:
        """Aggregate iOS APM data."""
        info = self.read_json(scene)
        cpu_app = self.read_log(scene, "cpu_app.log")[1]
        mem_total = self.read_log(scene, "mem_total.log")[1]
        fps = self.read_log(scene, "fps.log")[1]

        def avg(data): return round(sum(data) / len(data), 2) if data else 0

        return {
            "app": info.get("app"),
            "devices": info.get("devices"),
            "platform": info.get("platform"),
            "ctime": info.get("ctime"),
            "cpuAppRate": f"{avg(cpu_app)}%",
            "cpuSystemRate": "0%",
            "totalPassAvg": f"{avg(mem_total)}MB",
            "fps": f"{int(avg(fps))}HZ/s" if fps else "0HZ/s",
            "gpu": 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Devices — improved with subprocess, better error handling
# ─────────────────────────────────────────────────────────────────────────────
class Devices:

    def __init__(self, platform: str = Platform.Android.value):
        self.platform = platform
        self.adb_path = adb.adb_path

    @staticmethod
    def exec_cmd(cmd: List[str], timeout: int = 15) -> str:
        """
        Execute a shell command and return stdout.
        Replaces os.popen with safer subprocess.run.
        """
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.replace("\x1b[0m", "").strip()
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out: {' '.join(cmd)}")
            return ""
        except Exception as e:
            logger.warning(f"exec_cmd failed: {e}")
            return ""

    def filter_type(self) -> str:
        """Return appropriate grep command for the OS."""
        return "findstr" if platform.system() == Platform.Windows.value else "grep"

    def get_device_ids(self) -> List[str]:
        """Get all connected device IDs."""
        output = self.exec_cmd([self.adb_path, "devices"])
        lines = [l.strip() for l in output.split("\n") if l.strip()]
        device_ids = []
        for line in lines[1:]:  # Skip first line (header)
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                device_ids.append(parts[0])
        return device_ids

    # Backward-compatible public aliases (legacy camelCase API)
    def getDeviceIds(self) -> List[str]:
        return self.get_device_ids()

    def filterType(self) -> str:
        return self.filter_type()

    def getIdbyDevice(self, device: str, platform: str) -> str:
        return self.get_id_by_device(device, platform)

    def getPkgname(self, deviceId: str) -> List[str]:
        return self.get_pkg_names(deviceId)

    def getPkgnameByiOS(self, udid: str) -> List[str]:
        return self.get_pkg_names_by_ios(udid)

    def get_devices_name(self, device_id: str) -> str:
        """Get device model name for a given device ID."""
        output = adb.shell(f"getprop ro.product.model", deviceId=device_id, timeout=5)
        return output.strip() or "Unknown"

    def get_devices(self) -> List[str]:
        """Get list of connected devices with names."""
        device_ids = self.get_device_ids()
        devices = [f"{did}({self.get_devices_name(did)})" for did in device_ids]
        logger.info(f"Connected devices: {devices}")
        return devices

    def get_id_by_device(self, device_info: str, platform_val: str) -> str:
        """Extract device ID from device info string."""
        if platform_val == Platform.Android.value:
            device_id = re.sub(r"\(.*?\)|\{.*?\}|\[.*?\]", "", device_info).strip()
            if device_id not in self.get_device_ids():
                raise Exception("Device not found")
            return device_id
        return device_info

    def get_sdk_version(self, device_id: str) -> str:
        """Get Android SDK version."""
        return adb.shell("getprop ro.build.version.sdk", deviceId=device_id, timeout=5).strip()

    def get_sdk_version_int(self, device_id: str) -> int:
        """Get Android SDK version as integer."""
        try:
            return int(self.get_sdk_version(device_id))
        except (ValueError, TypeError):
            return 0

    def get_android_version_release(self, device_id: str) -> str:
        """Get Android release version (e.g., '14', '15')."""
        return adb.shell("getprop ro.build.version.release", deviceId=device_id, timeout=5).strip()

    def is_android_version_above(self, device_id: str, version: int) -> bool:
        """Check if Android SDK version >= specified version."""
        return self.get_sdk_version_int(device_id) >= version

    def get_cpu_cores(self, device_id: str) -> int:
        """Get number of CPU cores on device."""
        output = adb.shell("cat /sys/devices/system/cpu/online", deviceId=device_id, timeout=5)
        try:
            return int(output.split("-")[1]) + 1
        except (IndexError, ValueError):
            return 1

    def get_pid(self, device_id: str, pkg_name: str) -> List[str]:
        """
        Get PIDs for a package name on Android.
        Returns list of '{pid}:{packagename}' strings.
        """
        try:
            sdk_version = self.get_sdk_version(device_id)
            sdk_int = int(sdk_version) if sdk_version else 0
            ft = self.filter_type()

            if sdk_int < 26:
                # Older ps format: PID PPID NAME
                output = self.exec_cmd(
                    [self.adb_path, "-s", device_id, "shell", f"ps | {ft}", pkg_name]
                )
                lines = [l for l in output.split("\n") if pkg_name in l]
                process_list = []
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 9:
                        process_list.append(f"{parts[1]}:{parts[-1]}")
            else:
                # Newer ps -ef format: UID PID PPID NAME
                output = self.exec_cmd(
                    [self.adb_path, "-s", device_id, "shell", f"ps -ef | {ft}", pkg_name]
                )
                lines = [l for l in output.split("\n") if pkg_name in l]
                process_list = []
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 8:
                        process_list.append(f"{parts[1]}:{parts[-1]}")

            # Move exact match to front if found
            for i, p in enumerate(process_list):
                parts = p.split(":")
                if len(parts) == 2 and parts[1] == pkg_name:
                    process_list.insert(0, process_list.pop(i))
                    break

            if not process_list:
                logger.warning(f"No PID found for {pkg_name}")
        except Exception as e:
            process_list = []
            logger.exception(e)
        return process_list

    def getPid(self, deviceId: str = None, pkgName: str = None, **kwargs) -> List[str]:
        """Backward-compatible wrapper for legacy kwargs/callers."""
        device_id = deviceId or kwargs.get("device_id")
        pkg_name = pkgName or kwargs.get("pkg_name")
        if device_id is None or pkg_name is None:
            raise ValueError("deviceId and pkgName are required")
        return self.get_pid(device_id, pkg_name)

    def check_pkgname(self, pkgname: str) -> bool:
        """Check if package name passes validation."""
        blocked_prefixes = ["com.google"]
        return not any(pkgname.startswith(p) for p in blocked_prefixes)

    def get_pkg_names(self, device_id: str) -> List[str]:
        """Get all package names on Android device."""
        output = self.exec_cmd(
            [self.adb_path, "-s", device_id, "shell", "pm list packages --user 0"]
        )
        pkglist = [p.lstrip("package:").strip() for p in output.split("\n") if p.startswith("package:")]
        if not pkglist:
            output = self.exec_cmd(
                [self.adb_path, "-s", device_id, "shell", "pm list packages"]
            )
            pkglist = [p.lstrip("package:").strip() for p in output.split("\n") if p.startswith("package:")]
        return pkglist

    def get_device_info_by_ios(self) -> List[str]:
        """Get list of connected iOS device UDIDs."""
        try:
            udids = Usbmux().device_udid_list()
            logger.info(f"Connected iOS devices: {udids}")
            return udids
        except Exception as e:
            logger.warning(f"Failed to get iOS devices: {e}")
            return []

    def get_pkg_names_by_ios(self, udid: str) -> List[str]:
        """Get installed package names on iOS device."""
        try:
            device = Device(udid)
            return [i.get("CFBundleIdentifier") for i in device.installation.iter_installed(app_type="User")]
        except Exception as e:
            logger.warning(f"Failed to get iOS packages: {e}")
            return []

    def get_pc_ip(self) -> str:
        """Get local PC IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            logger.error("Failed to get local IP")
            return "127.0.0.1"

    def get_device_ip(self, device_id: str) -> Optional[str]:
        """Get device WiFi IP address."""
        output = self.exec_cmd([self.adb_path, "-s", device_id, "shell", "ip addr show wlan0"])
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def devices_check(self, platform_val: str, deviceid: str = None, pkgname: str = None):
        """Validate device environment is ready."""
        if platform_val == Platform.Android.value:
            if not self.get_device_ids():
                raise Exception("No Android devices found")
            if deviceid and pkgname and not self.get_pid(deviceid, pkgname):
                raise Exception(f"No process found for {pkgname}")
        elif platform_val == Platform.iOS.value:
            if not self.get_device_info_by_ios():
                raise Exception("No iOS devices found")
        else:
            raise Exception("Platform must be Android or iOS")

    def get_device_detail(self, device_id: str, platform_val: str) -> Dict:
        """Get detailed device information."""
        result = {}
        if platform_val == Platform.Android.value:
            props = adb.get_props(
                [
                    "ro.product.brand",
                    "ro.product.model",
                    "ro.build.version.release",
                    "ro.serialno",
                ],
                device_id,
            )
            result = {
                "brand": props.get("ro.product.brand", "").strip(),
                "name": props.get("ro.product.model", "").strip(),
                "version": props.get("ro.build.version.release", "").strip(),
                "serialno": props.get("ro.serialno", "").strip(),
                "cpu_cores": self.get_cpu_cores(device_id),
                "physical_size": adb.shell("wm size", deviceId=device_id, timeout=5)
                .replace("Physical size:", "")
                .strip(),
            }
            # WiFi MAC
            mac_output = adb.shell(
                f"ip addr show wlan0 | {self.filter_type()} link/ether", deviceId=device_id, timeout=5
            )
            result["wifiadr"] = mac_output.split()[1] if len(mac_output.split()) > 1 else ""
        elif platform_val == Platform.iOS.value:
            ios_dev = Device(udid=device_id)
            result = {
                "brand": ios_dev.get_value("DeviceClass", no_session=True) or "",
                "name": ios_dev.get_value("DeviceName", no_session=True) or "",
                "version": ios_dev.get_value("ProductVersion", no_session=True) or "",
                "serialno": device_id,
                "wifiadr": ios_dev.get_value("WiFiAddress", no_session=True) or "",
                "cpu_cores": 0,
                "physical_size": self._get_ios_screen_size(device_id),
            }
        else:
            raise Exception(f"Undefined platform: {platform_val}")
        return result

    def _get_ios_screen_size(self, device_id: str) -> str:
        """Get iOS device screen size."""
        try:
            ios_dev = Device(udid=device_id)
            info = ios_dev.screen_info()
            return f"{info.get('width', 0)}x{info.get('height', 0)}"
        except Exception as e:
            logger.warning(f"Failed to get iOS screen size: {e}")
            return ""

    def get_current_activity(self, device_id: str) -> str:
        """Get current foreground activity name."""
        ft = self.filter_type()
        result = adb.shell(f"dumpsys window | {ft} mCurrentFocus", deviceId=device_id, timeout=5)
        if "mCurrentFocus" in result:
            return result.split(" ")[-1].replace("}", "").strip()
        raise Exception("No current activity found")

    def get_startup_time_by_android(self, activity: str, device_id: str) -> str:
        """Measure app startup time."""
        return adb.shell(f"am start -W {activity}", deviceId=device_id, timeout=15)

    def get_startup_time_by_ios(self, pkg_name: str) -> str:
        """Measure iOS app startup time."""
        try:
            import pyidevice
        except ImportError:
            logger.error("py-ios-devices not found. Install with: pip install py-ios-devices")
            return ""
        result = self.exec_cmd(f"pyidevice instruments app_lifecycle -b {pkg_name}".split())
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Method — HTTP request helpers
# ─────────────────────────────────────────────────────────────────────────────
class Method:

    @classmethod
    def _request(cls, request, key: str) -> Any:
        """Extract value from Flask request (form or query args)."""
        if request.method == "POST":
            return request.form.get(key, "")
        if request.method == "GET":
            return request.args.get(key, "")
        raise Exception("Unsupported HTTP method")

    @classmethod
    def _set_value(cls, value: Any, default: Any = 0) -> Any:
        """Safe numeric conversion with default on error."""
        try:
            return float(value)
        except (ZeroDivisionError, ValueError, TypeError):
            return default

    @classmethod
    def _index(cls, target: List, index: int, default: Any = None) -> Any:
        """Safe list index access."""
        try:
            return target[index]
        except IndexError:
            return default


# ─────────────────────────────────────────────────────────────────────────────
# Install — APK/IPA installation utilities
# ─────────────────────────────────────────────────────────────────────────────
class Install:

    def upload_file(self, file_path: str, file_obj):
        """Save uploaded file."""
        try:
            file_obj.save(file_path)
            return True
        except Exception as e:
            logger.exception(e)
            return False

    def download_link(self, file_link: str = None, path: str = None, name: str = None) -> bool:
        """Download file with progress bar."""
        try:
            logger.info(f"Downloading: {file_link}")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            response = requests.get(file_link, stream=True, timeout=30)
            response.raise_for_status()
            total_size = int(response.headers.get("Content-Length", 0))
            pbar = tqdm(total=total_size, unit="B", unit_scale=True, desc=name or file_link.split("/")[-1])
            with open(os.path.join(path, name), "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
            pbar.close()
            return True
        except Exception as e:
            logger.exception(e)
            return False

    def install_apk(self, path: str) -> Tuple[bool, int]:
        """Install APK via ADB."""
        result = adb.shell_no_device(cmd=f"install -r {path}")
        if result == 0 and os.path.exists(path):
            os.remove(path)
        return result == 0, result

    def install_ipa(self, path: str) -> Tuple[bool, int]:
        """Install IPA via tidevice."""
        try:
            result = subprocess.run(
                ["tidevice", "install", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            if result.returncode == 0 and os.path.exists(path):
                os.remove(path)
            return result.returncode == 0, result.returncode
        except subprocess.TimeoutExpired:
            return False, -1

    # Backward-compatible public aliases (legacy camelCase API)
    def downloadLink(self, filelink: str = None, path: str = None, name: str = None) -> bool:
        return self.download_link(file_link=filelink, path=path, name=name)

    def installAPK(self, path: str) -> Tuple[bool, int]:
        return self.install_apk(path)

    def installIPA(self, path: str) -> Tuple[bool, int]:
        return self.install_ipa(path)


# ─────────────────────────────────────────────────────────────────────────────
# Scrcpy — screen mirroring/recording
# ─────────────────────────────────────────────────────────────────────────────
class Scrcpy:

    STATICPATH = os.path.dirname(os.path.realpath(__file__))
    DEFAULT_SCRCPY_PATH = {
        "64": os.path.join(STATICPATH, "scrcpy", "scrcpy-win64-v2.4", "scrcpy.exe"),
        "32": os.path.join(STATICPATH, "scrcpy", "scrcpy-win32-v2.4", "scrcpy.exe"),
        "default": "scrcpy",
    }

    @classmethod
    def _scrcpy_path(cls) -> str:
        """Get platform-appropriate scrcpy path."""
        if platform.system() == Platform.Windows.value:
            bit = platform.architecture()[0]
            if "64" in bit:
                return cls.DEFAULT_SCRCPY_PATH["64"]
            return cls.DEFAULT_SCRCPY_PATH["32"]
        return cls.DEFAULT_SCRCPY_PATH["default"]

    @classmethod
    def start_record(cls, device: str) -> int:
        """Start screen recording via scrcpy."""
        logger.info("Starting screen recording")
        f = File()
        video_path = os.path.join(f.report_dir, "record.mkv")
        scrcpy_path = cls._scrcpy_path()
        record_cmd = f'{scrcpy_path} -s {device} --no-playback --record={video_path}'

        if platform.system() == Platform.Windows.value:
            result = subprocess.run(f"start /b {record_cmd}", shell=True)
        else:
            result = subprocess.run(f"nohup {record_cmd} &", shell=True)

        if result.returncode == 0:
            logger.info(f"Screen recording started: {video_path}")
        else:
            logger.error("scrcpy not compatible. Install: brew install scrcpy (macOS) or choco install scrcpy (Windows)")
        return result.returncode

    @classmethod
    def stop_record(cls):
        """Stop scrcpy recording process."""
        logger.info("Stopping scrcpy recording")
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("scrcpy" in str(c).lower() for c in cmdline):
                    proc.send_signal(signal.SIGABRT)
                    logger.info(f"Stopped scrcpy PID: {proc.pid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    @classmethod
    def cast_screen(cls, device: str) -> int:
        """Start screen casting via scrcpy."""
        logger.info("Starting screen cast")
        scrcpy_path = cls._scrcpy_path()
        cast_cmd = f'{scrcpy_path} -s {device} --stay-awake'

        if platform.system() == Platform.Windows.value:
            result = subprocess.run(f"start /i {cast_cmd}", shell=True)
        else:
            result = subprocess.run(f"nohup {cast_cmd} &", shell=True)

        if result.returncode == 0:
            logger.info("Screen cast started")
        else:
            logger.error("scrcpy not compatible. Install: brew install scrcpy")
        return result.returncode

    @classmethod
    def play_video(cls, video: str):
        """Play back recorded video."""
        logger.info(f"Playing video: {video}")
        cap = cv2.VideoCapture(video)
        if not cap.isOpened():
            logger.error(f"Cannot open video: {video}")
            return
        cv2.namedWindow("frame", 0)
        cv2.resizeWindow("frame", 430, 900)
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            cv2.imshow("frame", gray)
            if cv2.waitKey(25) & 0xFF == ord("q"):
                break
        cap.release()
        cv2.destroyAllWindows()
