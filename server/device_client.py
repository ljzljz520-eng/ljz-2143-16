"""device_client.py — C 设备端 (device/device) 的进程内客户端。

stdin/stdout 行协议（与 device.c 对应）：
    请求:  CMD [TAB key=value ...]
    响应:  RES ok=0|1 at=... [TAB k=v ...]，EVENTS 可伴随多行 `EVT ...`
所有命令串行化；超时或管道错误抛出 DeviceTransportError（上层用于"确认丢失"处理）。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any


class DeviceTransportError(RuntimeError):
    """命令已发出，但结果不确定（超时 / 管道断裂 / 响应残缺）。
    这不代表设备一定没应用 —— 必须走 reconcile，禁止盲目重发提交。"""


class DeviceClient:
    def __init__(self, bin_path: str, seed_path: str, seed_sha: str,
                 seed_title: str, workdir: str):
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            [bin_path,
             "--seed", seed_path,
             "--seed-sha256", seed_sha,
             "--seed-title", seed_title],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(os.path.join(workdir, "device.stderr.log"), "ab"),
            cwd=workdir, bufsize=0)
        self._rbuf = b""

    def close(self) -> None:
        try:
            self._call("SHUTDOWN", timeout=3.0)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3.0)
        except Exception:
            self.proc.kill()

    @staticmethod
    def _parse_kv(line: str) -> dict[str, str]:
        out: dict[str, str] = {}
        # RES 行里键值既可能用 TAB 分隔，也可能在同一段内用空格分隔
        # （如 "ok=1 at=... live=1 live_w=1276 ..."），全部展平处理。
        # 注意：引号包裹的 value（如标题）内部空格不能拆，逐字符扫描。
        i, n = 0, len(line)
        while i < n:
            while i < n and line[i] in "\t ":
                i += 1
            start = i
            while i < n and line[i] != "=":
                i += 1
            if i >= n:
                break
            key = line[start:i]
            i += 1  # skip '='
            val = ""
            if i < n and line[i] == '"':
                i += 1
                j0 = i
                while i < n and line[i] != '"':
                    i += 1
                val = line[j0:i]
                if i < n:
                    i += 1
            else:
                j0 = i
                while i < n and line[i] not in "\t ":
                    i += 1
                val = line[j0:i]
            if key:
                out[key] = val
        return out

    def _readline(self, timeout: float):
        """从原始 fd 读取一行（自带字节缓冲，避免与文本层缓冲混用）。"""
        import selectors
        fd = self.proc.stdout.fileno()
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while b"\n" not in self._rbuf:
                ev = sel.select(timeout)
                if not ev:
                    return None
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                self._rbuf += chunk
        finally:
            sel.close()
        line, _, rest = self._rbuf.partition(b"\n")
        self._rbuf = rest
        return line.decode("utf-8", errors="replace")

    def _call(self, cmd: str, timeout: float = 10.0,
              swallow_response: bool = False) -> dict[str, Any]:
        with self._lock:
            try:
                assert self.proc.stdin
                self.proc.stdin.write((cmd + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise DeviceTransportError(f"pipe broken: {e}") from e

            events: list[str] = []
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceTransportError(
                        f"timeout after {timeout}s: {cmd.split(chr(9))[0]}")
                line = self._readline(remaining)
                if line is None:
                    raise DeviceTransportError(
                        f"timeout waiting RES: {cmd.split(chr(9))[0]}")
                line = line.rstrip("\n")
                if line.startswith("EVT "):
                    events.append(line[4:])
                    continue
                if line.startswith("RES "):
                    res = self._parse_kv(line[4:])
                    res["_events"] = events
                    if swallow_response:
                        # 故障注入：设备已应答，但应答在客户端侧"丢失"
                        raise DeviceTransportError(
                            "SIMULATED response loss (fault injection)")
                    return res
                # 忽略空行等

    # ---- 语义化封装 ----

    def hello(self) -> dict[str, str]:
        return self._call("HELLO", timeout=5.0)

    def prepare(self, plan_id: str, rev: int, path: str, sha256: str,
                title: str, zoom: int, darken: int) -> dict[str, Any]:
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", " ").replace("\n", " ")
        return self._call(
            f"PREPARE\tplan={plan_id}\trev={rev}\tpath={path}\tsha256={sha256}"
            f"\ttitle={esc(title)}\tzoom={zoom}\tdarken={darken}", timeout=15.0)

    def activate(self, plan_id: str, rev: int, at: str,
                 lose_response: bool = False) -> dict[str, Any]:
        return self._call(f"ACTIVATE\tplan={plan_id}\trev={rev}\tat={at}",
                          timeout=10.0, swallow_response=lose_response)

    def discard(self, plan_id: str) -> dict[str, Any]:
        return self._call(f"DISCARD\tplan={plan_id}", timeout=5.0)

    def status(self) -> dict[str, Any]:
        return self._call("STATUS", timeout=5.0)

    def events(self) -> list[str]:
        return self._call("EVENTS", timeout=5.0).get("_events", [])

    def preview_begin(self, session: str, path: str, sha256: str,
                      delay_ms: int = 0) -> dict[str, Any]:
        return self._call(
            f"PREVIEW_BEGIN\tsession={session}\tpath={path}\tsha256={sha256}"
            f"\tdelay_ms={delay_ms}", timeout=5.0)

    def preview_cancel(self, session: str) -> dict[str, Any]:
        return self._call(f"PREVIEW_CANCEL\tsession={session}", timeout=5.0)
