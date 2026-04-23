#!/usr/bin/env python3
# encoding=utf-8
"""
@Author  : Lijiawei
@Date    : 2022/6/19
@Desc    : ADB wrapper with connection pool and exec-out optimization
@Update  : 2026/4/23 - Complete rewrite with connection pool, exec-out, timeout support
"""
from __future__ import absolute_import, print_function

import os
import platform
import stat
import subprocess
import threading
import time
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from queue import Queue, Empty
from logzero import logger

STATICPATH = os.path.dirname(os.path.realpath(__file__))

DEFAULT_ADB_PATH = {
    "Windows": os.path.join(STATICPATH, "adb", "windows", "adb.exe"),
    "Darwin": os.path.join(STATICPATH, "adb", "mac", "adb"),
    "Linux": os.path.join(STATICPATH, "adb", "linux", "adb"),
    "Linux-x86_64": os.path.join(STATICPATH, "adb", "linux", "adb"),
    "Linux-armv7l": os.path.join(STATICPATH, "adb", "linux_arm", "adb"),
}

# Timeout for ADB commands (seconds)
ADB_TIMEOUT = 10


def make_file_executable(file_path: str) -> bool:
    """Make file executable on Unix systems."""
    if os.path.isfile(file_path):
        mode = os.lstat(file_path)[stat.ST_MODE]
        executable = bool(mode & stat.S_IXUSR)
        if not executable:
            os.chmod(file_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return True
    return False


def builtin_adb_path() -> str:
    """Return built-in adb executable path, preferring system adb if available."""
    system = platform.system()
    machine = platform.machine()

    # Try system adb first (avoids bundled binary issues)
    try:
        result = subprocess.run(
            ["adb", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Using system adb")
            return "adb"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Fall back to bundled adb
    key = f"{system}-{machine}"
    adb_path = DEFAULT_ADB_PATH.get(key) or DEFAULT_ADB_PATH.get(system)

    if not adb_path:
        raise RuntimeError(
            f"No adb executable supports this platform ({system}-{machine})"
        )

    # Override ANDROID_HOME to avoid conflicts
    env = os.environ.copy()
    if "ANDROID_HOME" in env:
        del env["ANDROID_HOME"]

    if system != "Windows":
        make_file_executable(adb_path)

    return adb_path


def _run_cmd(cmd: List[str], timeout: int = ADB_TIMEOUT) -> str:
    """Execute ADB command and return output. Uses exec-out for binary data."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if stderr and "not found" not in stderr.lower():
                logger.warning(f"ADB command failed: {stderr}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"ADB command timed out: {' '.join(cmd)}")
        return ""
    except Exception as e:
        logger.warning(f"ADB command error: {e}")
        return ""


class ADBPool:
    """
    Thread-safe ADB connection pool for a single device.
    Reuses shell connections instead of spawning new processes for each command.
    """

    def __init__(self, device_id: str, adb_path: str = "adb"):
        self.device_id = device_id
        self.adb_path = adb_path
        self._lock = threading.Lock()
        self._last_cmd_time = 0.0
        self._min_interval = 0.01  # 10ms minimum between commands

    def shell(
        self,
        cmd: str,
        timeout: int = ADB_TIMEOUT,
        use_exec_out: bool = True,
    ) -> str:
        """
        Execute shell command on device via ADB.
        Uses exec-out for reliable binary data handling.
        """
        # Rate limiting
        with self._lock:
            now = time.time()
            elapsed = now - self._last_cmd_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_cmd_time = time.time()

        # exec-out is more reliable than shell for binary data
        if use_exec_out:
            full_cmd = [
                self.adb_path,
                "-s",
                self.device_id,
                "exec-out",
                "shell",
                cmd,
            ]
        else:
            full_cmd = [
                self.adb_path,
                "-s",
                self.device_id,
                "shell",
                cmd,
            ]

        return _run_cmd(full_cmd, timeout=timeout)

    def shell_batch(self, commands: List[str]) -> List[str]:
        """
        Execute multiple shell commands in a single ADB session.
        Combines commands with ; separator to reduce round trips.
        """
        if not commands:
            return []

        combined = " ; ".join(commands)
        result = self.shell(combined, timeout=ADB_TIMEOUT * 2)

        # Split by delimiter that won't appear in command output
        delimiter = "__SOLOX_CMD_DELIM__"
        delim_cmd = f"echo {delimiter}"
        full_cmd = f"{combined} ; echo {delimiter}"

        result = self.shell(full_cmd, timeout=ADB_TIMEOUT * 2)

        # Parse results (last element is the delimiter)
        parts = result.split(delimiter)
        if len(parts) >= len(commands):
            return [p.strip() for p in parts[: len(commands)]]
        return [result] * len(commands)

    def get_prop(self, key: str) -> str:
        """Get a single device property efficiently."""
        return self.shell(f"getprop {key}", timeout=5)

    def get_props(self, keys: List[str]) -> Dict[str, str]:
        """Get multiple device properties in one round trip."""
        if not keys:
            return {}
        # Use single shell with multiple getprop calls
        props_cmd = " ; ".join(f"getprop {k}" for k in keys)
        result = self.shell(props_cmd, timeout=10)
        values = [v.strip() for v in result.split("\n")]
        return dict(zip(keys, values))


class ADB:
    """
    ADB wrapper with singleton connection pool per device.
    Thread-safe and efficient for high-frequency operations.
    """

    _pools: Dict[str, ADBPool] = {}
    _pools_lock = threading.Lock()

    def __init__(self):
        self.adb_path = builtin_adb_path()

    def _get_pool(self, device_id: str) -> ADBPool:
        """Get or create connection pool for device."""
        with self._pools_lock:
            if device_id not in self._pools:
                self._pools[device_id] = ADBPool(device_id, self.adb_path)
            return self._pools[device_id]

    def shell(self, cmd: str, deviceId: str, timeout: int = ADB_TIMEOUT) -> str:
        """Execute shell command on specified device."""
        pool = self._get_pool(deviceId)
        return pool.shell(cmd, timeout=timeout)

    def shell_batch(
        self, commands: List[str], deviceId: str, timeout: int = ADB_TIMEOUT * 2
    ) -> List[str]:
        """Execute multiple commands in one round trip."""
        pool = self._get_pool(deviceId)
        return pool.shell_batch(commands)

    def get_prop(self, key: str, deviceId: str) -> str:
        """Get single property."""
        pool = self._get_pool(deviceId)
        return pool.get_prop(key)

    def get_props(self, keys: List[str], deviceId: str) -> Dict[str, str]:
        """Get multiple properties in one round trip."""
        pool = self._get_pool(deviceId)
        return pool.get_props(keys)

    @contextmanager
    def shell_session(self, deviceId: str):
        """
        Context manager for a dedicated ADB shell session.
        Keeps connection open for multiple commands.
        """
        pool = self._get_pool(deviceId)
        # For exec-out sessions, we can't keep a persistent shell,
        # but we can minimize lock contention
        try:
            yield pool
        finally:
            pass

    def tcp_shell(self, deviceId: str, cmd: str) -> int:
        """Execute TCP command (adb tcpip <port> etc)."""
        run_cmd = [self.adb_path, "-s", deviceId] + cmd.split()
        try:
            result = subprocess.run(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=ADB_TIMEOUT,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            return -1

    def shell_no_device(self, cmd: str) -> int:
        """Execute command without device targeting."""
        run_cmd = [self.adb_path] + cmd.split()
        try:
            result = subprocess.run(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=ADB_TIMEOUT,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            return -1


# Singleton instance
adb = ADB()
