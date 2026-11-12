from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from .models import Node, ProbeTarget, Settings
from .state import RuntimeState
from .tls import verified_context
from .tasks import supervised_loop
from .limits import (
    MIHOMO_STARTUP_CONCURRENCY,
    MIHOMO_VALIDATION_TIMEOUT,
    MIHOMO_READY_TIMEOUT,
)


def mihomo_node_config(node: Node, socks_port: int) -> dict[str, Any]:
    if not node.uuid:
        raise ValueError("缺少 VLESS UUID")
    proxy: dict[str, Any] = {
        "name": "probe-vless",
        "type": "vless",
        "server": node.server,
        "port": node.port,
        "uuid": node.uuid,
        "encryption": node.encryption,
        "network": node.transport,
        "tls": True,
        "skip-cert-verify": node.skip_cert_verify,
        "client-fingerprint": node.client_fingerprint,
    }
    if node.tls_sni:
        proxy["servername"] = node.tls_sni
    if node.alpn:
        proxy["alpn"] = node.alpn
    if node.flow:
        proxy["flow"] = node.flow
    if node.transport == "ws":
        proxy["ws-opts"] = {
            "path": node.ws_path,
            "headers": ({"Host": node.ws_host} if node.ws_host else {}),
        }
    elif node.transport == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": node.grpc_service_name}
    return {
        "mixed-port": socks_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "proxies": [proxy],
        "proxy-groups": [
            {"name": "FORCED_NODE", "type": "select", "proxies": ["probe-vless"]}
        ],
        "rules": ["MATCH,FORCED_NODE"],
    }


@dataclass
class ManagedNodeProcess:
    node_id: str
    port: int
    process: asyncio.subprocess.Process
    config_path: Path
    ready: bool = False


class MihomoSupervisor:
    """Run one isolated Mihomo instance per node so probes cannot fall back to another route."""

    def __init__(self, binary: str, config_dir: str | Path):
        self.binary = binary
        self.config_dir = Path(config_dir).resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.config_dir.chmod(0o700)
        self.processes: dict[str, ManagedNodeProcess] = {}
        self.retry_after: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._started_at: dict[str, float] = {}
        self._reservations: dict[str, socket.socket] = {}
        self._clock = time.monotonic
        self.status = "未配置"
        self.last_error = ""

    async def validate_nodes(self, nodes: list[Node]) -> None:
        if not self.binary:
            return
        semaphore = asyncio.Semaphore(MIHOMO_STARTUP_CONCURRENCY)

        async def validate(idx, node):
            if not node.enabled or node.maintenance or not node.uuid:
                return
            node_dir = self.config_dir / node.id
            node_dir.mkdir(exist_ok=True)
            path = node_dir / f"validate-{secrets.token_hex(8)}.yaml"
            path.write_text(
                yaml.safe_dump(
                    mihomo_node_config(node, 23000 + idx),
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            _chmod_private(path)
            try:
                async with semaphore:
                    if not await _validate_binary(self.binary, path, node_dir):
                        raise ValueError(
                            f"节点 {node.id} 的 Mihomo 配置检查失败，请核对传输参数及内核版本"
                        )
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass

        results = await asyncio.gather(
            *(validate(idx, node) for idx, node in enumerate(nodes)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def _backoff(self, node_id: str) -> None:
        failures = self._failures.get(node_id, 0) + 1
        self._failures[node_id] = failures
        self.retry_after[node_id] = self._clock() + min(300, 2 ** min(failures, 9))

    async def reconcile(self, nodes: list[Node]) -> None:
        desired = {
            node.id: node
            for node in nodes
            if node.enabled and not node.maintenance and node.uuid
        }
        for mapping in (self.retry_after, self._failures, self._started_at):
            for key in set(mapping) - set(desired):
                mapping.pop(key, None)
        if not self.binary:
            await asyncio.gather(
                *(self._stop_node(node_id) for node_id in list(self.processes)),
                return_exceptions=True,
            )
            self.status = "未配置 Mihomo 路径"
            return
        for node_id in list(self.processes):
            if node_id not in desired:
                await self._stop_node(node_id)
        used_ports = {
            item.port
            for key, item in self.processes.items()
            if key in desired and item.process.returncode is None
        }
        semaphore = asyncio.Semaphore(MIHOMO_STARTUP_CONCURRENCY)

        async def ensure(node_id, node):
            current = self.processes.get(node_id)
            if current and current.process.returncode is not None:
                await self._stop_node(node_id)
                self._backoff(node_id)
                current = None
            node_dir = self.config_dir / node_id
            node_dir.mkdir(exist_ok=True)
            if os.name != "nt":
                node_dir.chmod(0o700)
            config_path = node_dir / "config.yaml"
            if current and current.process.returncode is None:
                try:
                    if current.ready and yaml.safe_load(
                        config_path.read_text(encoding="utf-8")
                    ) == mihomo_node_config(node, current.port):
                        if (
                            self._clock() - self._started_at.get(node_id, self._clock())
                            > 60
                        ):
                            self._failures.pop(node_id, None)
                        return
                except Exception:
                    pass
                await self._stop_node(node_id)
                used_ports.discard(current.port)
            if self._clock() < self.retry_after.get(node_id, 0):
                return
            async with semaphore:
                port, reservation = _reserve_port(node_id, used_ports)
                used_ports.add(port)
                self._reservations[node_id] = reservation
                try:
                    await self._start_node(node, port, config_path)
                finally:
                    self._reservations.pop(node_id, None)
                    reservation.close()

        results = await asyncio.gather(
            *(ensure(node_id, node) for node_id, node in desired.items()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        if not self.binary:
            self.status = "未配置 Mihomo 路径"
        elif not desired:
            self.status = "等待已配置 VLESS 节点"
        elif len(self.processes) == len(desired):
            self.status = f"运行中（{len(self.processes)} 个探测出口）"
        else:
            self.status = f"部分探测器不可用（{len(self.processes)}/{len(desired)}）"

    async def _start_node(self, node: Node, port: int, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            config_path.parent.chmod(0o700)
        config = mihomo_node_config(node, port)
        text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
        config_path.write_text(text, encoding="utf-8")
        _chmod_private(config_path)
        try:
            if not await _validate_binary(self.binary, config_path, config_path.parent):
                self.last_error = "Mihomo 配置检查失败，请核对传输参数及内核版本"
                self.status = "Mihomo 配置检查失败"
                self._backoff(node.id)
                return
            # Mihomo 不接收继承的监听 fd；将保留窗口压缩到创建进程前。
            reservation = self._reservations.get(node.id)
            if reservation:
                reservation.close()
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "-f",
                str(config_path),
                "-d",
                str(config_path.parent),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            managed = ManagedNodeProcess(node.id, port, process, config_path)
            self.processes[node.id] = managed
            ready = await _wait_port("127.0.0.1", port, process, MIHOMO_READY_TIMEOUT)
            if not ready:
                self.last_error = "Mihomo 本地 SOCKS 入口启动超时"
                self.status = "Mihomo 启动失败"
                await self._stop_node(node.id)
                self._backoff(node.id)
                return
            self._started_at[node.id] = self._clock()
            managed.ready = True
        except asyncio.CancelledError:
            await self._stop_node(node.id)
            raise
        except FileNotFoundError:
            self.last_error = "找不到 Mihomo 可执行文件"
            self.status = "未配置 Mihomo 路径"
        except asyncio.TimeoutError:
            self.last_error = "Mihomo 配置检查超时"
            self.status = "Mihomo 配置检查失败"
        except Exception as exc:
            self.last_error = _safe_error(exc)
            self.status = "Mihomo 启动失败"
            await self._stop_node(node.id)
        finally:
            if node.id not in self.processes:
                try:
                    config_path.unlink(missing_ok=True)
                    config_path.parent.rmdir()
                except OSError:
                    pass
        if node.id not in self.processes:
            self._backoff(node.id)

    async def _stop_node(self, node_id: str) -> None:
        managed = self.processes.get(node_id)
        if not managed:
            return
        # 终止期间退出就绪状态，但等待完成前继续跟踪，取消后仍可再次回收。
        managed.ready = False
        if managed.process.returncode is None:
            try:
                managed.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(managed.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    managed.process.kill()
                except ProcessLookupError:
                    pass
                await managed.process.wait()
        if self.processes.get(node_id) is managed:
            self.processes.pop(node_id)
        try:
            managed.config_path.unlink(missing_ok=True)
            managed.config_path.parent.rmdir()
        except OSError:
            pass

    async def stop(self) -> None:
        await asyncio.gather(
            *(self._stop_node(node_id) for node_id in list(self.processes)),
            return_exceptions=True,
        )
        for reservation in self._reservations.values():
            reservation.close()
        self._reservations.clear()

    def port_for(self, node_id: str) -> int | None:
        managed = self.processes.get(node_id)
        if managed and managed.ready and managed.process.returncode is None:
            return managed.port
        return None


async def _wait_port(
    host: str, port: int, process: asyncio.subprocess.Process, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.returncode is not None:
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=0.25
            )
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.15)
            return process.returncode is None
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.1)
    return False


def _chmod_private(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _stable_port(node_id: str, occupied: set[int]) -> int:
    port, reservation = _reserve_port(node_id, occupied)
    reservation.close()
    return port


def _reserve_port(node_id: str, occupied: set[int]) -> tuple[int, socket.socket]:
    count = 12768
    start = (
        int.from_bytes(hashlib.sha256(node_id.encode("utf-8")).digest()[:4], "big")
        % count
    )
    for offset in range(count):
        port = 20000 + ((start + offset) % count)
        if port not in occupied:
            candidate = socket.socket()
            try:
                if os.name == "nt":
                    candidate.setsockopt(
                        socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                    )
                candidate.bind(("127.0.0.1", port))
                return port, candidate
            except OSError:
                candidate.close()
    raise RuntimeError("没有可分配的 Mihomo 本地端口")


async def _validate_binary(binary: str, path: Path, directory: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-t",
        "-f",
        str(path),
        "-d",
        str(directory),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=MIHOMO_VALIDATION_TIMEOUT)
        return proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class EndToEndProber:
    def __init__(self, state: RuntimeState, supervisor: MihomoSupervisor):
        self.state = state
        self.supervisor = supervisor
        self._task: asyncio.Task | None = None
        self._helper_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(state.settings.max_parallel_probes)
        self._node_locks: dict[str, asyncio.Lock] = {}

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._semaphore = asyncio.Semaphore(self.state.settings.max_parallel_probes)
            self._task = asyncio.create_task(
                self._run(), name="vless-end-to-end-prober"
            )
            self._helper_task = asyncio.create_task(
                self._run_helpers(), name="mihomo-supervisor"
            )

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._task, self._helper_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._helper_task = None
        current = {node.id for node in self.state.nodes}
        self._node_locks = {
            key: lock
            for key, lock in self._node_locks.items()
            if key in current or lock.locked()
        }

    async def probe_once(
        self, node: Node, port: int | None = None
    ) -> tuple[bool | None, float | None, str, bool]:
        lock = self._node_locks.setdefault(node.id, asyncio.Lock())

        async def probe():
            async with lock:
                return await self._probe_path(node, port)

        try:
            return await asyncio.wait_for(
                probe(), timeout=self.state.settings.probe_timeout_seconds
            )
        except asyncio.TimeoutError:
            return None, None, "探测总时限已到，等待下一轮观测", False

    def _prune_locks(self):
        current = {node.id for node in self.state.nodes}
        self._node_locks = {
            key: lock
            for key, lock in self._node_locks.items()
            if key in current or lock.locked()
        }

    async def _probe_path(
        self, node: Node, port: int | None = None
    ) -> tuple[bool | None, float | None, str, bool]:
        managed = self.supervisor.processes.get(node.id)
        if port is None and managed:
            try:
                if yaml.safe_load(
                    managed.config_path.read_text(encoding="utf-8")
                ) != mihomo_node_config(node, managed.port):
                    return None, None, "节点配置正在更新，等待新的探测出口", False
            except (OSError, ValueError, yaml.YAMLError):
                return None, None, "探测出口配置暂未就绪", False
        port = port if port is not None else self.supervisor.port_for(node.id)
        if port is None:
            return None, None, "Mihomo 探测出口不可用", False
        self.state.complete_probe_startup(node.id)
        proxy_url = f"socks5://127.0.0.1:{port}"
        timeout = httpx.Timeout(max(0.2, self.state.settings.probe_timeout_seconds / 5))
        target_results: list[bool] = []
        total_started = time.monotonic()
        for target in self.state.settings.probe_targets:
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    verify=verified_context(),
                ) as client:
                    if not await _check_response(client, target):
                        target_results.append(False)
                        continue
                    verified = False
                    if self.state.settings.expected_exit_ip:
                        async with client.stream(
                            "GET",
                            self.state.settings.exit_check_url,
                            headers={"Cache-Control": "no-cache"},
                        ) as response:
                            body = (
                                (await _bounded_body(response, 256))
                                .decode("utf-8", "replace")
                                .strip()
                            )
                            if (
                                response.status_code != 200
                                or body != self.state.settings.expected_exit_ip
                            ):
                                return False, None, "落地出口 IP 校验不符", False
                        verified = True
                    elapsed = (time.monotonic() - total_started) * 1000
                    return True, elapsed, "", verified
            except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
                target_results.append(False)
                last_error = _safe_error(exc)
            except Exception as exc:
                target_results.append(False)
                last_error = _safe_error(exc)
        if not target_results:
            return None, None, "未配置有效 HTTPS 探测目标", False

        # Distinguish a broken relay/VLESS path from a shared probe-target outage.
        for target in self.state.settings.probe_targets:
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    verify=verified_context(),
                ) as direct:
                    if await _check_response(direct, target):
                        return (
                            False,
                            None,
                            locals().get("last_error", "VLESS 路径探测失败"),
                            False,
                        )
            except Exception:
                continue
        return None, None, "探测目标与线路外控制检查均失败，观测不确定", False

    async def _run(self) -> None:
        async def cycle():
            self._prune_locks()
            nodes = [
                node
                for node in self.state.nodes
                if node.enabled and not node.maintenance
            ]
            results = await asyncio.gather(
                *(self._probe_guarded(node) for node in nodes), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result

        await supervised_loop(
            "线路探测",
            cycle,
            self._stop,
            lambda: self.state.settings.probe_interval_seconds,
            lambda exc: self.state.add_event(
                "线路探测循环异常，将自动重试", "error", noise=True
            ),
            retry_max=lambda: self.state.settings.probe_interval_seconds,
        )

    async def _run_helpers(self):
        async def cycle():
            crashed = [
                node_id
                for node_id, managed in self.supervisor.processes.items()
                if managed.process.returncode is not None
            ]
            await self.supervisor.reconcile(self.state.nodes)
            self.state.probe_executor_status = self.supervisor.status
            for node_id in crashed:
                self.state.add_event(
                    "Mihomo 子进程退出，将退避重启", "warning", node_id
                )

        await supervised_loop(
            "Mihomo 进程",
            cycle,
            self._stop,
            lambda: 1,
            lambda exc: self.state.add_event(
                "Mihomo 管理循环异常，将自动重试", "error", noise=True
            ),
        )

    async def _probe_guarded(self, node: Node) -> None:
        version = self.state.version
        async with self._semaphore:
            try:
                success, rtt, error, verified = await self.probe_once(node)
            except Exception as exc:
                success, rtt, error, verified = None, None, _safe_error(exc), False
            self.state.apply_probe_result(
                node.id, success, rtt, error, verified, config_version=version
            )


def _safe_error(exc: Exception) -> str:
    return exc.__class__.__name__ + "：请检查本地网络与服务配置"


async def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise ValueError("探测响应超过大小限制")
    return bytes(chunks)


async def _check_response(client: httpx.AsyncClient, target: ProbeTarget) -> bool:
    async with client.stream(
        "GET", target.url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
    ) as response:
        if response.status_code != target.expected_status:
            return False
        content = (await _bounded_body(response, 4096)).decode("utf-8", "replace")
        if target.expected_text and target.expected_text not in content:
            return False
        if target.expected_body and target.expected_body not in content:
            return False
        return True
