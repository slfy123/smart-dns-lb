from __future__ import annotations

import math
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import Node, Settings
from .storage import Store
from .limits import (
    MIHOMO_STARTUP_CONCURRENCY,
    MIHOMO_VALIDATION_TIMEOUT,
    MIHOMO_READY_TIMEOUT,
)

logger = logging.getLogger(__name__)


@dataclass
class TrafficSnapshot:
    probe_server_id: str = ""
    probe_server_name: str = ""
    used_gib: float | None = None
    limit_gib: float | None = None
    rx_gib: float | None = None
    tx_gib: float | None = None
    rx_speed_bps: float | None = None
    tx_speed_bps: float | None = None
    reset_day: int = 0
    source_last_updated_ms: int = 0
    synced_at: str = ""
    age_seconds: float | None = None
    fresh: bool = False
    error: str = ""

    @property
    def remaining_gib(self) -> float | None:
        if not self.limit_gib or self.limit_gib < 0 or self.used_gib is None:
            return None
        return max(0.0, self.limit_gib - self.used_gib)

    @property
    def current_mbps(self) -> float | None:
        speeds = [v for v in (self.rx_speed_bps, self.tx_speed_bps) if v is not None]
        return max(speeds) * 8 / 1_000_000 if speeds else None


@dataclass
class NodeHealth:
    status: str = "UNKNOWN"
    last_active_status: str = "UNKNOWN"
    last_rtt_ms: float | None = None
    ewma_rtt_ms: float | None = None
    last_error: str = ""
    last_observation_mono: float | None = None
    last_success_mono: float | None = None
    certified_success_mono: float | None = None
    probe_startup_until_mono: float | None = None
    probe_startup_pending: bool = False
    last_success_at: str = ""
    recovery_started_mono: float | None = None
    failures: int = 0
    successes: int = 0
    slow_samples: int = 0
    fast_samples: int = 0
    samples: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    exit_verified: bool = False
    dns_answers: int = 0
    traffic: TrafficSnapshot = field(default_factory=TrafficSnapshot)
    effective_weight: float = 0.0
    quota_factor: float = 1.0
    capacity_factor: float = 1.0
    headroom_factor: float = 1.0
    budget_factor: float = 1.0
    quota_exhausted: bool = False

    def success_rate(self) -> float | None:
        return sum(self.samples) / len(self.samples) * 100 if self.samples else None


class RuntimeState:
    def __init__(
        self, settings: Settings, nodes: list[Node], store: Store, version: int = 0
    ):
        self.settings = settings
        self.nodes = nodes
        self.store = store
        self.version = version
        self.health: dict[str, NodeHealth] = {
            node.id: NodeHealth(effective_weight=float(node.base_weight))
            for node in nodes
        }
        self.dns_total = 0
        self.dns_answers = 0
        self.dns_errors = 0
        self.dns_running = False
        self.last_dns_request_at = ""
        self.probe_source_status = "未配置"
        self.probe_source_last_sync = ""
        self.probe_source_error = ""
        self.probe_executor_status = "未配置"
        self._event_cache = store.get_events(50)
        self.lowest_rtt_node_id = ""
        self.lowest_rtt_selected_mono: float | None = None
        self._last_weight_update: float | None = None
        self._event_times: dict[tuple[str, str, str], float] = {}
        self._weight_inputs: dict[str, tuple] = {}
        self._cache_probe_timeouts()

    @property
    def observation_timeout(self) -> float:
        return self._observation_timeout

    def _cache_probe_timeouts(self) -> None:
        active = sum(node.enabled and not node.maintenance for node in self.nodes)
        batches = math.ceil(active / self.settings.max_parallel_probes)
        self._observation_timeout = max(
            self.settings.stale_after_seconds,
            batches * self.settings.probe_timeout_seconds
            + self.settings.probe_interval_seconds
            + 1,
        )
        self._probe_startup_budget = (
            math.ceil(active / MIHOMO_STARTUP_CONCURRENCY)
            * (MIHOMO_VALIDATION_TIMEOUT + MIHOMO_READY_TIMEOUT)
            + self._observation_timeout
        )

    def add_event(
        self,
        message: str,
        level: str = "info",
        node_id: str = "",
        *,
        noise: bool = False,
    ) -> None:
        now = time.monotonic()
        key = (level, node_id, message[:500])
        if noise and now - self._event_times.get(key, -float("inf")) < 300:
            return
        try:
            self.store.append_event(message, level, node_id)
        except Exception as exc:
            logger.error("事件持久化失败：%s；已保留内存事件", type(exc).__name__)
        if noise:
            self._event_times = {
                key: stamp
                for key, stamp in self._event_times.items()
                if now - stamp < 300
            }
            self._event_times[key] = now
        self._event_cache.insert(
            0,
            {
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "level": level,
                "message": message,
                "node_id": node_id,
            },
        )
        del self._event_cache[100:]

    def replace_config(
        self, settings: Settings, nodes: list[Node], version: int
    ) -> None:
        old_nodes = {node.id: node for node in self.nodes}
        old_health = self.health
        now = time.monotonic()
        executor_changed = (
            settings.mihomo_path != self.settings.mihomo_path
            or settings.mihomo_config_dir != self.settings.mihomo_config_dir
        )
        startup_candidates = {
            node.id
            for node in self.nodes
            if executor_changed
            and self.effective_health_status(node, now) in {"UP", "DEGRADED"}
        }
        probe_fields = {
            "server",
            "port",
            "transport",
            "flow",
            "uuid",
            "encryption",
            "alpn",
            "tls_sni",
            "skip_cert_verify",
            "client_fingerprint",
            "ws_path",
            "ws_host",
            "grpc_service_name",
            "enabled",
            "maintenance",
        }
        source_changed = settings.probe_source != self.settings.probe_source
        new_health: dict[str, NodeHealth] = {}
        for node in nodes:
            previous = old_nodes.get(node.id)
            # 全局探测条件更新沿用尚未过期的观测；实际线路参数变化仍重新认证。
            if (
                previous
                and all(
                    getattr(previous, key) == getattr(node, key) for key in probe_fields
                )
                and node.id in old_health
            ):
                new_health[node.id] = old_health[node.id]
                if source_changed or any(
                    getattr(previous, key) != getattr(node, key)
                    for key in (
                        "probe_server_id",
                        "probe_server_name",
                        "traffic_counter",
                        "traffic_limit_gib_override",
                        "reset_day_override",
                    )
                ):
                    new_health[node.id].traffic = TrafficSnapshot()
                    new_health[node.id].quota_exhausted = False
            else:
                new_health[node.id] = NodeHealth(
                    effective_weight=float(node.base_weight)
                )
                if node.id in old_health:
                    new_health[node.id].dns_answers = old_health[node.id].dns_answers
                    prior = old_health[node.id]
                    new_health[node.id].last_active_status = (
                        prior.last_active_status
                        if prior.status == "DISABLED"
                        else prior.status
                    )
        self.settings = settings
        self.nodes = nodes
        self.health = new_health
        self.version = version
        self._cache_probe_timeouts()
        for node_id in startup_candidates:
            health = self.health.get(node_id)
            if health is old_health.get(node_id):
                health.probe_startup_until_mono = now + self._probe_startup_budget
                health.probe_startup_pending = True
        if source_changed:
            self.probe_source_status = (
                "等待同步" if settings.probe_source.base_url else "未配置"
            )
            self.probe_source_error = ""
            self.probe_source_last_sync = ""
        self._last_weight_update = None
        self._weight_inputs.clear()
        self.recompute_effective_weights()

    def complete_probe_startup(
        self, node_id: str, now_mono: float | None = None
    ) -> None:
        health = self.health.get(node_id)
        if health is None or not health.probe_startup_pending:
            return
        now = time.monotonic() if now_mono is None else now_mono
        health.probe_startup_pending = False
        if health.probe_startup_until_mono is not None:
            # 出口就绪后只再给一个完整观测周期；后续重试不会续期。
            health.probe_startup_until_mono = min(
                health.probe_startup_until_mono, now + self.observation_timeout
            )

    def _observation_expired(self, health: NodeHealth, now: float) -> bool:
        if (
            health.probe_startup_until_mono is not None
            and now <= health.probe_startup_until_mono
        ):
            return False
        return (
            health.last_observation_mono is not None
            and now - health.last_observation_mono > self.observation_timeout
        )

    def apply_probe_result(
        self,
        node_id: str,
        success: bool | None,
        rtt_ms: float | None = None,
        error: str = "",
        exit_verified: bool = False,
        now_mono: float | None = None,
        config_version: int | None = None,
    ) -> None:
        import time

        if config_version is not None and config_version != self.version:
            return
        now = time.monotonic() if now_mono is None else now_mono
        health = self.health.get(node_id)
        node = next((item for item in self.nodes if item.id == node_id), None)
        if health is None or node is None:
            return
        if node.maintenance or not node.enabled:
            if health.status != "DISABLED":
                health.last_active_status = health.status
            health.status = "DISABLED"
            health.last_error = "手动维护" if node.maintenance else "已停用"
            return
        if success is None:
            health.last_error = error or "本轮探测结果不确定"
            return
        health.probe_startup_until_mono = None
        health.probe_startup_pending = False

        previous_status = health.status
        health.last_observation_mono = now
        health.last_error = "" if success else (error or "端到端请求失败")
        health.samples.append(success)
        if success:
            health.failures = 0
            health.successes += 1
            health.last_success_mono = now
            health.last_success_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            health.exit_verified = exit_verified
            health.last_rtt_ms = rtt_ms
            if rtt_ms is not None:
                health.ewma_rtt_ms = (
                    rtt_ms
                    if health.ewma_rtt_ms is None
                    else 0.25 * rtt_ms + 0.75 * health.ewma_rtt_ms
                )
            threshold = node.max_rtt_ms or self.settings.max_rtt_ms
            if health.ewma_rtt_ms is not None and health.ewma_rtt_ms > threshold:
                health.slow_samples += 1
                health.fast_samples = 0
            elif (
                health.ewma_rtt_ms is not None
                and health.ewma_rtt_ms
                < threshold * self.settings.recovery_rtt_percent / 100
            ):
                health.fast_samples += 1
                health.slow_samples = 0

            if previous_status == "DOWN" or previous_status == "STALE":
                if health.successes >= self.settings.recover_threshold:
                    health.status = "RECOVERING"
                    if health.recovery_started_mono is None:
                        health.recovery_started_mono = now
            elif previous_status in {"UNKNOWN", "PROBE_UNAVAILABLE"}:
                if health.successes >= self.settings.recover_threshold:
                    health.status = "UP"
            elif previous_status == "RECOVERING":
                pass
            elif health.slow_samples >= 3:
                health.status = "DEGRADED"
            elif previous_status == "DEGRADED" and health.fast_samples >= 3:
                health.status = "UP"
            if health.status in {"UP", "DEGRADED"}:
                health.certified_success_mono = now
        else:
            health.successes = 0
            health.recovery_started_mono = None
            health.failures += 1
            health.exit_verified = False
            if (
                previous_status == "RECOVERING"
                or health.failures >= self.settings.fail_threshold
            ):
                health.status = "DOWN"
        if health.status != previous_status:
            self.add_event(
                f"{node.name} 状态：{previous_status} → {health.status}"
                + (f"（{health.last_error}）" if health.last_error else ""),
                "warning" if health.status in {"DOWN", "STALE"} else "info",
                node_id,
            )

    def refresh_time_dependent_state(self, now_mono: float | None = None) -> None:
        import time

        now = time.monotonic() if now_mono is None else now_mono
        for node in self.nodes:
            health = self.health.get(node.id)
            if health is None:
                continue
            if node.maintenance or not node.enabled:
                if health.status != "DISABLED" and health.status != "UNKNOWN":
                    health.last_active_status = health.status
                health.status = "DISABLED"
            elif self._observation_expired(health, now):
                if health.status not in {"STALE", "DOWN", "DISABLED"}:
                    health.status = "STALE"
                    health.successes = 0
                    health.recovery_started_mono = None
                    self.add_event(
                        f"{node.name} 探测数据过期，已退出调度",
                        "warning",
                        node.id,
                        noise=True,
                    )
            elif (
                health.status == "RECOVERING"
                and health.recovery_started_mono is not None
            ):
                if (
                    now - health.recovery_started_mono
                    >= self.settings.recovery_steady_seconds
                ):
                    old = health.status
                    health.status = "UP"
                    health.certified_success_mono = health.last_success_mono
                    health.recovery_started_mono = None
                    self.add_event(
                        f"{node.name} 恢复稳定，重新加入调度", "info", node.id
                    )
        self.recompute_effective_weights(now_mono=now)

    def apply_traffic_metrics(
        self, metrics: dict[str, dict[str, Any]], synced_at: str, now_epoch: float
    ) -> None:
        from .probe_source import metric_snapshot

        self.probe_source_last_sync = synced_at
        self.probe_source_error = ""
        self.probe_source_status = "已连接"
        for node in self.nodes:
            health = self.health.setdefault(node.id, NodeHealth())
            record = None
            if node.probe_server_id:
                record = metrics.get("id:" + node.probe_server_id)
            elif node.probe_server_name:
                record = metrics.get("name:" + node.probe_server_name)
            if record is None:
                health.traffic = TrafficSnapshot(error="未映射探针服务器")
                continue
            health.traffic = metric_snapshot(
                node,
                record,
                synced_at,
                now_epoch,
                self.settings.probe_source.max_age_seconds,
            )

    def traffic_sync_failed(self, message: str) -> None:
        self.probe_source_status = "连接异常"
        self.probe_source_error = message[:300]

    def note_dns(self, result: str, node_id: str = "") -> None:
        self.dns_total += 1
        self.last_dns_request_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        if result == "answer":
            self.dns_answers += 1
            if node_id in self.health:
                self.health[node_id].dns_answers += 1
        elif result == "error":
            self.dns_errors += 1

    def effective_health_status(self, node: Node, now_mono: float | None = None) -> str:
        import time

        if not node.enabled or node.maintenance:
            return "DISABLED"
        health = self.health.get(node.id)
        if health is None:
            return "UNKNOWN"
        now = time.monotonic() if now_mono is None else now_mono
        if self._observation_expired(health, now):
            return "STALE"
        return health.status

    def recompute_effective_weights(self, now_mono: float | None = None) -> None:
        import time

        explicit_clock = now_mono is not None
        now = time.monotonic() if now_mono is None else now_mono
        if (
            not explicit_clock
            and self._last_weight_update is not None
            and now - self._last_weight_update < 1
        ):
            return
        self._last_weight_update = now
        if self.settings.mode in {"TRAFFIC_REMAINING", "SMART_PEAK_REMAINING"}:
            self._recompute_remaining_traffic_weights(time.time())
            return
        capacities = [
            node.capacity_mbps
            for node in self.nodes
            if node.capacity_mbps and node.enabled and not node.maintenance
        ]
        median_capacity = statistics.median(capacities) if capacities else None
        now_epoch = time.time()
        budgets = [
            max(
                0.0,
                self.health[node.id].traffic.limit_gib
                - self.health[node.id].traffic.used_gib,
            )
            for node in self.nodes
            if node.enabled
            and not node.maintenance
            and (h := self.health.get(node.id)) is not None
            and h.traffic.fresh
            and h.traffic.limit_gib
            and h.traffic.used_gib is not None
            and now_epoch - h.traffic.source_last_updated_ms / 1000
            <= self.settings.probe_source.max_age_seconds
        ]
        median_budget = (
            statistics.median([b for b in budgets if b > 0])
            if any(b > 0 for b in budgets)
            else None
        )
        for node in self.nodes:
            health = self.health.setdefault(node.id, NodeHealth())
            old = health.effective_weight or float(node.base_weight)
            if node.base_weight <= 0:
                health.effective_weight = 0.0
                health.quota_factor = health.capacity_factor = (
                    health.headroom_factor
                ) = 1.0
                continue
            traffic = health.traffic
            age_seconds = (
                max(0.0, now_epoch - traffic.source_last_updated_ms / 1000)
                if traffic.source_last_updated_ms
                else None
            )
            fresh = (
                traffic.fresh
                and age_seconds is not None
                and age_seconds <= self.settings.probe_source.max_age_seconds
            )
            if not fresh:
                health.quota_exhausted = False
            quota_factor = 1.0
            capacity_factor = 1.0
            headroom_factor = 1.0
            budget_factor = 1.0
            if self.settings.mode == "TRAFFIC_AWARE" and fresh:
                limit = traffic.limit_gib
                used = traffic.used_gib
                if limit and limit > 0 and used is not None:
                    used_fraction = min(1.0, max(0.0, used / limit))
                    reset_day = traffic.reset_day
                    progress = _billing_cycle_progress(
                        reset_day, datetime.now(ZoneInfoSafe(self.settings.timezone))
                    )
                    if used_fraction >= self.settings.traffic_stop_at_percent / 100:
                        quota_factor = 0.0
                    elif progress is None:
                        quota_factor = _clamp(
                            0.5 + 1.5 * (1.0 - used_fraction), 0.25, 2.5
                        )
                    else:
                        pace = math.sqrt((progress + 0.05) / (used_fraction + 0.05))
                        quota_factor = _clamp(
                            pace * math.sqrt(max(0.0, 1.0 - used_fraction)), 0.25, 2.5
                        )
                    health.quota_exhausted = quota_factor == 0
                    if median_budget:
                        budget_factor = _clamp(
                            max(0.0, limit - used) / median_budget, 0.1, 10.0
                        )
                if node.capacity_mbps and median_capacity:
                    capacity_factor = _clamp(
                        math.sqrt(node.capacity_mbps / median_capacity), 0.35, 2.5
                    )
                    current = traffic.current_mbps
                    if current is not None:
                        utilization = current / node.capacity_mbps
                        headroom_factor = _clamp(
                            math.sqrt(max(0.02, 1.0 - utilization)), 0.15, 1.0
                        )
            if self.settings.mode == "TRAFFIC_AWARE" and health.quota_exhausted:
                quota_factor = 0.0
            raw = (
                node.base_weight
                * quota_factor
                * budget_factor
                * capacity_factor
                * headroom_factor
            )
            weight_input = (
                self.settings.mode,
                node.base_weight,
                node.capacity_mbps,
                traffic.source_last_updated_ms,
                traffic.used_gib,
                traffic.limit_gib,
                traffic.rx_speed_bps,
                traffic.tx_speed_bps,
                traffic.reset_day,
                fresh,
                median_budget,
                median_capacity,
            )
            if self.settings.mode == "TRAFFIC_AWARE":
                if not fresh or quota_factor == 0.0:
                    health.effective_weight = raw
                elif self._weight_inputs.get(node.id) != weight_input:
                    health.effective_weight = raw if old <= 0 else 0.7 * old + 0.3 * raw
            else:
                health.effective_weight = float(node.base_weight)
            health.quota_factor = quota_factor
            health.capacity_factor = capacity_factor
            health.headroom_factor = headroom_factor
            health.budget_factor = budget_factor
            self._weight_inputs[node.id] = weight_input

    def _recompute_remaining_traffic_weights(self, now_epoch: float) -> None:
        # Normalize by a common reference so valid quotas retain their exact ratios.
        # Missing or stale quota data uses the configured fallback base weight.
        remaining: dict[str, float] = {}
        for node in self.nodes:
            health = self.health.setdefault(node.id, NodeHealth())
            traffic = health.traffic
            age = (
                max(0.0, now_epoch - traffic.source_last_updated_ms / 1000)
                if traffic.source_last_updated_ms
                else None
            )
            valid = (
                traffic.fresh
                and age is not None
                and age <= self.settings.probe_source.max_age_seconds
                and traffic.limit_gib is not None
                and traffic.limit_gib > 0
                and traffic.used_gib is not None
            )
            health.quota_exhausted = bool(
                valid
                and traffic.used_gib / traffic.limit_gib
                >= self.settings.traffic_stop_at_percent / 100
            )
            if valid:
                remaining[node.id] = max(0.0, traffic.limit_gib - traffic.used_gib)
        budgets = [
            remaining[node.id]
            for node in self.nodes
            if node.id in remaining
            and remaining[node.id] > 0
            and node.enabled
            and not node.maintenance
            and node.base_weight > 0
            and not self.health[node.id].quota_exhausted
        ]
        reference = statistics.median(budgets) if budgets else 1.0
        for node in self.nodes:
            health = self.health[node.id]
            health.capacity_factor = health.headroom_factor = 1.0
            health.quota_factor = 0.0 if health.quota_exhausted else 1.0
            health.budget_factor = (
                remaining[node.id] / reference if node.id in remaining else 1.0
            )
            if node.base_weight <= 0 or health.quota_exhausted:
                health.effective_weight = 0.0
            elif node.id in remaining:
                health.effective_weight = 100.0 * health.budget_factor
            else:
                health.effective_weight = float(node.base_weight)

    def public_node(self, node: Node, now_mono: float | None = None) -> dict[str, Any]:
        import time

        now_epoch = time.time()
        health = self.health.get(node.id, NodeHealth())
        traffic = health.traffic
        age_seconds = (
            max(0.0, now_epoch - traffic.source_last_updated_ms / 1000)
            if traffic.source_last_updated_ms
            else None
        )
        fresh = (
            traffic.fresh
            and age_seconds is not None
            and age_seconds <= self.settings.probe_source.max_age_seconds
        )
        mapped = bool(node.probe_server_id or node.probe_server_name)
        return {
            "id": node.id,
            "name": node.name,
            "server": node.server,
            "port": node.port,
            "transport": node.transport,
            "flow": node.flow,
            "has_uuid": bool(node.uuid),
            "encryption": "none" if node.encryption == "none" else "",
            "has_encryption": node.encryption != "none",
            "alpn": list(node.alpn),
            "tls_sni": node.tls_sni,
            "skip_cert_verify": node.skip_cert_verify,
            "client_fingerprint": node.client_fingerprint,
            "ws_path": node.ws_path,
            "ws_host": node.ws_host,
            "grpc_service_name": node.grpc_service_name,
            "category": node.category,
            "enabled": node.enabled,
            "maintenance": node.maintenance,
            "base_weight": node.base_weight,
            "priority": node.priority,
            "max_rtt_ms": node.max_rtt_ms,
            "capacity_mbps": node.capacity_mbps,
            "probe_server_id": node.probe_server_id,
            "probe_server_name": node.probe_server_name,
            "traffic_counter": node.traffic_counter,
            "traffic_limit_gib_override": node.traffic_limit_gib_override,
            "reset_day_override": node.reset_day_override,
            "status": self.effective_health_status(node, now_mono),
            "health_status": (
                health.last_active_status
                if not node.enabled or node.maintenance
                else health.status
            ),
            "last_rtt_ms": health.last_rtt_ms,
            "ewma_rtt_ms": health.ewma_rtt_ms,
            "last_error": health.last_error,
            "last_success_at": health.last_success_at,
            "success_rate_20": health.success_rate(),
            "probe_sample_count": len(health.samples),
            "exit_verified": health.exit_verified,
            "dns_answers": health.dns_answers,
            "effective_weight": round(health.effective_weight, 2),
            "quota_factor": round(health.quota_factor, 3),
            "capacity_factor": round(health.capacity_factor, 3),
            "headroom_factor": round(health.headroom_factor, 3),
            "budget_factor": round(health.budget_factor, 3),
            "quota_exhausted": health.quota_exhausted,
            "probe_mapping_status": (
                "未配置"
                if not mapped
                else ("已映射" if fresh else (traffic.error or "数据过期"))
            ),
            "traffic_fresh": fresh,
            "traffic_used_gib": traffic.used_gib,
            "traffic_limit_gib": traffic.limit_gib,
            "traffic_remaining_gib": traffic.remaining_gib,
            "traffic_rx_gib": traffic.rx_gib,
            "traffic_tx_gib": traffic.tx_gib,
            "traffic_current_mbps": traffic.current_mbps,
            "traffic_source_updated_ms": traffic.source_last_updated_ms,
            "traffic_age_seconds": age_seconds,
            "traffic_reset_day": traffic.reset_day,
            "traffic_error": traffic.error,
        }

    def public_snapshot(self) -> dict[str, Any]:
        from datetime import datetime
        import time
        from .scheduler import _in_peak

        settings = self.settings.model_dump()
        settings["probe_source"]["bearer_token"] = ""
        settings["probe_source"]["has_bearer_token"] = bool(
            self.settings.probe_source.bearer_token
        )
        nodes = [self.public_node(node) for node in self.nodes]
        return {
            "version": self.version,
            "settings": settings,
            "is_peak": _in_peak(self.settings),
            "nodes": nodes,
            "dns": {
                "running": self.dns_running,
                "total_queries": self.dns_total,
                "answers": self.dns_answers,
                "service_a_answers": sum(h.dns_answers for h in self.health.values()),
                "errors": self.dns_errors,
                "last_request_at": self.last_dns_request_at,
            },
            "probe": {
                "executor_status": self.probe_executor_status,
                "source_status": self.probe_source_status,
                "source_last_sync": self.probe_source_last_sync,
                "source_error": self.probe_source_error,
            },
            "events": list(self._event_cache[:50]),
        }


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def ZoneInfoSafe(name: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _billing_cycle_progress(reset_day: int, now: datetime) -> float | None:
    if reset_day <= 0:
        return None
    from calendar import monthrange
    from datetime import datetime as dt

    day_this = min(reset_day, monthrange(now.year, now.month)[1])
    start = now.replace(day=day_this, hour=0, minute=0, second=0, microsecond=0)
    if now < start:
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1
        start = start.replace(
            year=year, month=month, day=min(reset_day, monthrange(year, month)[1])
        )
    if start.month == 12:
        next_year, next_month = start.year + 1, 1
    else:
        next_year, next_month = start.year, start.month + 1
    end = start.replace(
        year=next_year,
        month=next_month,
        day=min(reset_day, monthrange(next_year, next_month)[1]),
    )
    duration = (end - start).total_seconds()
    return _clamp(
        (now - start).total_seconds() / duration if duration else 0.0, 0.0, 1.0
    )
