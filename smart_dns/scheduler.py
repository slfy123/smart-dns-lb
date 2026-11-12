from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Node, Settings
from .state import RuntimeState

TRAFFIC_MODES = {"TRAFFIC_AWARE", "TRAFFIC_REMAINING", "SMART_PEAK_REMAINING"}


def _in_peak(settings: Settings, now: datetime | None = None) -> bool:
    local = (now or datetime.now(ZoneInfo(settings.timezone))).astimezone(
        ZoneInfo(settings.timezone)
    )
    current = local.hour * 60 + local.minute
    start_h, start_m = map(int, settings.peak_start.split(":"))
    end_h, end_m = map(int, settings.peak_end.split(":"))
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    if start == end:
        return True  # 相同时刻表示全天高峰，包括 00:00–24:00 归一化后的情况。
    return start <= current < end if start < end else current >= start or current < end


def _weighted(
    nodes: list[Node], state: RuntimeState, allow_zero_fallback: bool = False
) -> Node | None:
    if not nodes:
        return None
    weights = [
        max(
            0.0,
            (
                state.health[node.id].effective_weight
                if state.settings.mode in TRAFFIC_MODES
                else node.base_weight
            ),
        )
        for node in nodes
    ]
    if sum(weights) <= 0:
        return (
            sorted(nodes, key=lambda item: (item.priority, item.id))[0]
            if allow_zero_fallback
            else None
        )
    return random.choices(nodes, weights=weights, k=1)[0]


def select_node(state: RuntimeState, now: datetime | None = None) -> Node | None:
    import time

    current = time.monotonic()
    candidates = [
        node
        for node in state.nodes
        if state.effective_health_status(node) in {"UP", "DEGRADED"}
    ]
    if not candidates and state.settings.all_down_policy == "LAST_KNOWN":
        import time

        current = time.monotonic()
        candidates = [
            node
            for node in state.nodes
            if node.enabled
            and not node.maintenance
            and state.health.get(node.id)
            and state.health[node.id].certified_success_mono is not None
            and current - state.health[node.id].certified_success_mono
            <= state.settings.last_known_grace_seconds
        ]
    if not candidates:
        return None
    candidates = [
        node
        for node in candidates
        if node.base_weight > 0
        and (
            state.settings.mode not in TRAFFIC_MODES
            or state.health[node.id].effective_weight > 0
        )
    ]
    if not candidates:
        return None

    mode = state.settings.mode
    if mode == "SMART_PEAK_REMAINING":
        # Time group and explicit priority precede latency preferences. A slow
        # but available primary must not be displaced by another group's UP node.
        category = "PREMIUM" if _in_peak(state.settings, now) else "NORMAL"
        primary = [node for node in candidates if node.category == category]
        group = primary or candidates
        best_priority = min(node.priority for node in group)
        tier = [node for node in group if node.priority == best_priority]
        preferred = [
            node for node in tier if state.effective_health_status(node) == "UP"
        ]
        return _weighted(preferred or tier, state)

    if mode == "SMART_PEAK":
        category = "PREMIUM" if _in_peak(state.settings, now) else "NORMAL"
        primary = [node for node in candidates if node.category == category]
        group = primary or candidates
        preferred = [
            node for node in group if state.effective_health_status(node) == "UP"
        ]
        return _weighted(preferred or group, state)

    preferred = [
        node for node in candidates if state.effective_health_status(node) == "UP"
    ]
    candidates = preferred or candidates

    if mode == "FAILOVER":
        best_priority = min(node.priority for node in candidates)
        return _weighted(
            [node for node in candidates if node.priority == best_priority],
            state,
            allow_zero_fallback=True,
        )
    if mode == "LOWEST_RTT":
        best = min(
            candidates,
            key=lambda node: (
                state.health[node.id].ewma_rtt_ms or float("inf"),
                node.id,
            ),
        )
        previous = next(
            (n for n in candidates if n.id == state.lowest_rtt_node_id), None
        )
        if previous and previous.id != best.id:
            held = current - (state.lowest_rtt_selected_mono or 0)
            old_rtt = state.health[previous.id].ewma_rtt_ms or float("inf")
            new_rtt = state.health[best.id].ewma_rtt_ms or float("inf")
            if held < state.settings.lowest_rtt_hold_seconds or new_rtt >= old_rtt * (
                1 - state.settings.lowest_rtt_improvement_percent / 100
            ):
                return previous
        if state.lowest_rtt_node_id != best.id:
            state.lowest_rtt_node_id = best.id
            state.lowest_rtt_selected_mono = current
        return best
    return _weighted(candidates, state)
