"""阶段 3：故障注入、协议边界和调度回归；外部服务全部替身化。"""

import asyncio
import socket
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import httpx
from dnslib import DNSRecord, DNSHeader, EDNS0, QTYPE, RCODE

from app import load_runtime
from smart_dns.api import create_app
from smart_dns.dns import AuthorityResolver, DNSServer
from smart_dns.models import Node, Settings
from smart_dns.prober import MihomoSupervisor, _stable_port
from smart_dns.scheduler import select_node, _in_peak
from smart_dns.state import RuntimeState, TrafficSnapshot
from smart_dns.storage import Store


def node(identifier="a", **kwargs):
    return Node(id=identifier, name=identifier, server="192.0.2.1", port=443, **kwargs)


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx, self.dns = load_runtime(self.tmp.name)
        self.app = create_app(self.ctx, "fixture")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer " + self.ctx.admin_token},
        )
        self.starts = [
            patch.object(self.ctx.prober, "start"),
            patch.object(self.ctx.poller, "start"),
        ]
        for start in self.starts:
            start.start()

    async def asyncTearDown(self):
        for start in self.starts:
            start.stop()
        await self.client.aclose()
        await self.ctx.prober.stop()
        await self.ctx.poller.stop()
        await self.ctx.supervisor.stop()
        await self.dns.stop()
        self.ctx.store.close()
        self.tmp.cleanup()

    async def test_01_supervised_loop_survives_operation_and_event_failure(self):
        from smart_dns.tasks import supervised_loop

        stop = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise OSError("fixture")
            stop.set()

        with self.assertLogs("smart_dns.tasks", "ERROR"):
            await asyncio.wait_for(
                supervised_loop(
                    "fixture",
                    operation,
                    stop,
                    lambda: 0.001,
                    on_error=Mock(side_effect=OSError("event store")),
                    retry_base=0.001,
                ),
                0.5,
            )
        self.assertEqual(calls, 3)

    async def test_02_helper_startup_is_concurrent(self):
        supervisor = MihomoSupervisor("fixture", Path(self.tmp.name) / "helpers")
        entered = set()
        both = asyncio.Event()

        async def start(item, *args):
            entered.add(item.id)
            if len(entered) == 2:
                both.set()
            await both.wait()

        nodes = [
            node(key, uuid="00000000-0000-4000-8000-000000000001") for key in ("a", "b")
        ]
        with patch.object(supervisor, "_start_node", side_effect=start):
            await asyncio.wait_for(supervisor.reconcile(nodes), 0.5)
        self.assertEqual(entered, {"a", "b"})

    async def test_01_poller_recovers_when_error_callback_also_throws(self):
        from smart_dns.models import ProbeSource
        from smart_dns.probe_source import ProbePoller

        source = ProbeSource(base_url="https://fixture.invalid").model_copy(
            update={"poll_interval_seconds": 0.001}
        )

        def observed(*args):
            poller._stop.set()

        poller = ProbePoller(source, observed, Mock(side_effect=OSError("disk")))
        with patch(
            "smart_dns.probe_source.fetch_probe_servers",
            new=AsyncMock(side_effect=[OSError("network"), ({}, "fixture")]),
        ), self.assertLogs("smart_dns.tasks", "ERROR"):
            await asyncio.wait_for(poller._run(), 0.5)
        self.assertTrue(poller._stop.is_set())

    async def test_01_lifespan_timer_recovers_from_state_and_event_failure(self):
        from app import make_lifespan

        observed = asyncio.Event()
        attempts = 0

        def refresh():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("state write")
            observed.set()

        with patch.object(self.dns, "start", new=AsyncMock()), patch.object(
            self.dns, "stop", new=AsyncMock()
        ), patch.object(
            self.ctx.state, "refresh_time_dependent_state", side_effect=refresh
        ):
            async with make_lifespan(self.ctx, self.dns)(self.app):
                with patch.object(
                    self.ctx.state, "add_event", side_effect=OSError("events write")
                ), self.assertLogs("smart_dns.tasks", "ERROR"):
                    await asyncio.wait_for(observed.wait(), 2)
        self.assertGreaterEqual(attempts, 2)
        self.assertFalse(self.ctx.dns_started)

    async def test_02_full_probe_cycle_does_not_make_waiting_nodes_stale(self):
        nodes = [node(str(index)) for index in range(64)]
        state = RuntimeState(Settings(), nodes, self.ctx.store)
        health = state.health["63"]
        health.status = "UP"
        health.last_observation_mono = 1
        state.refresh_time_dependent_state(now_mono=60)
        self.assertEqual(state.effective_health_status(nodes[-1], 60), "UP")

    async def test_03_probe_settings_reload_preserves_certified_health(self):
        state = self.ctx.state
        state.replace_config(state.settings, [node()], state.version)
        for _ in range(3):
            state.apply_probe_result("a", True, 100)
        health = state.health["a"]
        for field, value in [
            ("expected_exit_ip", "203.0.113.10"),
            ("exit_check_url", "https://check.example.com"),
            ("mihomo_path", "fixture"),
        ]:
            settings = Settings.model_validate(
                {**state.settings.model_dump(), field: value}
            )
            state.replace_config(settings, state.nodes, state.version + 1)
            self.assertIs(state.health["a"], health)
            self.assertIsNotNone(select_node(state))

    async def test_02_blocked_startup_does_not_block_observations(self):
        from smart_dns.prober import EndToEndProber

        self.ctx.state.replace_config(
            self.ctx.state.settings, [node()], self.ctx.state.version
        )
        prober = EndToEndProber(self.ctx.state, self.ctx.supervisor)
        observed = asyncio.Event()

        async def observation(*args):
            observed.set()
            return None, None, "fixture", False

        async def blocked(*args):
            await asyncio.Event().wait()

        with patch.object(
            self.ctx.supervisor, "reconcile", side_effect=blocked
        ), patch.object(prober, "probe_once", side_effect=observation):
            prober.start()
            try:
                await asyncio.wait_for(observed.wait(), 0.5)
            finally:
                await prober.stop()
        self.assertIsNone(prober._helper_task)

    async def test_04_runtime_bind_override_is_not_persisted(self):
        from app import apply_runtime_overrides

        original = self.ctx.state.settings.model_dump()
        apply_runtime_overrides(
            self.ctx, self.dns, "127.0.0.1", 15353, "127.0.0.1", 18080
        )
        response = await self.client.put("/api/config", json={"settings": {"ttl": 60}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["restart_required"])
        persisted = self.ctx.store.get_config()[1]["settings"]
        for field in ("dns_host", "dns_port", "api_host", "api_port"):
            self.assertEqual(persisted[field], original[field])
        self.assertEqual((self.dns.bind_host, self.dns.bind_port), ("127.0.0.1", 15353))

    async def test_05_unauthenticated_request_never_waits_for_config_lock(self):
        async with self.ctx.config_lock:
            response = await asyncio.wait_for(
                self.client.put(
                    "/api/config", content=b"{", headers={"Authorization": ""}
                ),
                0.5,
            )
        self.assertEqual(response.status_code, 401)

    async def test_05_request_body_size_is_bounded(self):
        response = await self.client.post(
            "/api/nodes/parse-link", json={"links": "x" * 1_048_577}
        )
        self.assertEqual(response.status_code, 413)

    async def test_06_tcp_clients_close_before_server_wait_closed(self):
        released = asyncio.Event()

        async def client():
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

        task = asyncio.create_task(client())
        await asyncio.sleep(0)
        self.dns._tcp_tasks.add(task)
        self.dns.tcp_server = Mock(
            close=Mock(), wait_closed=AsyncMock(side_effect=released.wait)
        )
        try:
            await asyncio.wait_for(self.dns.stop(), 0.5)
        finally:
            self.dns.tcp_server = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_07_port_range_avoids_default_linux_ephemeral_ports(self):
        for index in range(100):
            self.assertTrue(20000 <= _stable_port(str(index), set()) < 32768)

    async def test_07_port_is_reserved_during_validation(self):
        from smart_dns.prober import _reserve_port

        port, reservation = _reserve_port("fixture", set())
        try:
            with socket.socket() as contender, self.assertRaises(OSError):
                contender.bind(("127.0.0.1", port))
        finally:
            reservation.close()

    async def test_18_clear_uuid_false_is_accepted(self):
        await self.client.post("/api/nodes", json=node().model_dump())
        response = await self.client.put(
            "/api/nodes/a", json={"clear_uuid": False, "name": "edited"}
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_12_tcp_per_ip_limit_and_total_lifetime(self):
        reader = Mock()
        writer = Mock(
            get_extra_info=Mock(return_value=("192.0.2.1", 1000)),
            wait_closed=AsyncMock(),
        )
        self.dns._tcp_per_ip["192.0.2.1"] = self.dns.tcp_per_ip_limit
        await self.dns._handle_tcp(reader, writer)
        writer.close.assert_called_once()
        self.dns._tcp_per_ip.clear()
        writer.reset_mock()
        self.dns.tcp_lifetime_seconds = 0.01

        async def blocked(*args):
            await asyncio.Event().wait()

        with patch.object(self.dns, "_serve_tcp", side_effect=blocked):
            await asyncio.wait_for(self.dns._handle_tcp(reader, writer), 0.5)
        writer.close.assert_called_once()
        self.assertFalse(self.dns._tcp_writers)
        self.assertFalse(self.dns._tcp_per_ip)
        self.assertFalse(self.dns._tcp_tasks)

    async def test_14_deleted_node_locks_and_retry_entries_are_pruned(self):
        self.ctx.prober._node_locks["deleted"] = asyncio.Lock()
        self.ctx.prober._prune_locks()
        self.assertNotIn("deleted", self.ctx.prober._node_locks)
        self.ctx.supervisor.retry_after["deleted"] = time.monotonic() + 100
        await self.ctx.supervisor.reconcile([])
        self.assertFalse(self.ctx.supervisor.retry_after)

    async def test_crashed_helper_restarts_with_exponential_backoff(self):
        from smart_dns.prober import ManagedNodeProcess

        supervisor = MihomoSupervisor("fixture", Path(self.tmp.name) / "crash")
        item = node(uuid="00000000-0000-4000-8000-000000000001")
        config = supervisor.config_dir / item.id / "config.yaml"
        supervisor.processes[item.id] = ManagedNodeProcess(
            item.id, 21000, Mock(returncode=1), config
        )
        with patch.object(supervisor, "_clock", return_value=100), patch.object(
            supervisor, "_start_node", new=AsyncMock()
        ) as start:
            await supervisor.reconcile([item])
            self.assertEqual(supervisor.retry_after[item.id], 102)
            start.assert_not_awaited()
        with patch.object(supervisor, "_clock", return_value=103), patch.object(
            supervisor, "_start_node", new=AsyncMock()
        ) as start:
            await supervisor.reconcile([item])
            start.assert_awaited_once()
            supervisor._backoff(item.id)
            self.assertEqual(supervisor.retry_after[item.id], 107)
        self.assertFalse(supervisor._reservations)

    async def test_startup_cancellation_cleans_process_and_credentials(self):
        supervisor = MihomoSupervisor("fixture", Path(self.tmp.name) / "cancel")
        item = node(uuid="00000000-0000-4000-8000-000000000001")
        process = Mock(returncode=None, wait=AsyncMock())
        ready = asyncio.Event()

        async def waiting(*args):
            ready.set()
            await asyncio.Event().wait()

        with patch(
            "smart_dns.prober._validate_binary", new=AsyncMock(return_value=True)
        ), patch(
            "smart_dns.prober.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ), patch(
            "smart_dns.prober._wait_port", side_effect=waiting
        ):
            task = asyncio.create_task(supervisor.reconcile([item]))
            await asyncio.wait_for(ready.wait(), 0.5)
            self.assertIsNone(supervisor.port_for(item.id))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        process.terminate.assert_called_once()
        self.assertFalse(supervisor.processes)
        self.assertFalse(list(supervisor.config_dir.rglob("*.yaml")))
        self.assertFalse(supervisor._reservations)

    async def test_storage_concurrent_writers_keep_versions_and_events(self):
        def write(index):
            self.ctx.store.append_event("thread " + str(index))
            return self.ctx.store.save_config({"thread": index})

        initial = self.ctx.store.get_config()[0]
        versions = await asyncio.gather(
            *(asyncio.to_thread(write, index) for index in range(20))
        )
        self.assertEqual(sorted(versions), list(range(initial + 1, initial + 21)))
        self.assertEqual(len(self.ctx.store.get_events(100)), 21)

    async def test_healthz_is_minimal(self):
        self.assertEqual(
            (await self.client.get("/healthz")).json(), {"ok": False, "dns": False}
        )

    async def test_s1_encryption_is_redacted_and_preserved(self):
        item = node(encryption="fixture-client-key")
        await self.client.post("/api/nodes", json=item.model_dump())
        snapshot = (await self.client.get("/api/status")).json()
        self.assertNotIn("fixture-client-key", str(snapshot))
        self.assertTrue(snapshot["nodes"][0]["has_encryption"])
        response = await self.client.put(
            "/api/config", json={"nodes": snapshot["nodes"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ctx.state.nodes[0].encryption, "fixture-client-key")
        backup = (await self.client.get("/api/config/export")).json()
        self.assertNotIn("fixture-client-key", str(backup))
        response = await self.client.post("/api/config/import", json=backup)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ctx.state.nodes[0].encryption, "fixture-client-key")
        backup["nodes"][0]["id"] = "other"
        self.assertEqual(
            (await self.client.post("/api/config/import", json=backup)).status_code, 422
        )


class ProtocolAndSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.state = RuntimeState(Settings(mode="WEIGHTED"), [node()], self.store)
        self.resolver = AuthorityResolver(self.state)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_08_header_flags_are_fresh(self):
        request = DNSRecord.question("example.com", "SOA")
        request.header.tc = request.header.cd = request.header.z = 1
        request.header.rcode = 3
        reply = self.resolver.resolve(request)
        self.assertEqual(
            (reply.header.tc, reply.header.cd, reply.header.z, reply.header.rcode),
            (0, 1, 0, 0),
        )

    def test_09_edns_opt_and_badvers(self):
        for version in (0, 1):
            query = DNSRecord.question("example.com", "SOA")
            query.add_ar(EDNS0(version=version, udp_len=4096))
            reply = DNSRecord.parse(self.resolver.resolve(query).pack())
            opt = [rr for rr in reply.ar if rr.rtype == QTYPE.OPT]
            self.assertEqual(len(opt), 1)
            self.assertEqual(opt[0].rclass, 1232)
            self.assertEqual(opt[0].edns_ver, 0)
            self.assertEqual(opt[0].edns_rcode, int(version > 0))
            if version:
                self.assertEqual(reply.rr, [])

    def test_10_zero_questions_and_malformed_have_no_fake_question(self):
        request = DNSRecord(header=DNSHeader(q=0), questions=[])
        reply = self.resolver.resolve(request)
        self.assertEqual(reply.header.rcode, RCODE.FORMERR)
        self.assertEqual(DNSRecord.parse(reply.pack()).questions, [])
        self.assertEqual(
            DNSRecord.parse(DNSServer(self.state)._resolve_bytes(b"bad")).questions, []
        )

    def test_11_nodata_is_not_an_error(self):
        for name in ("example.com", "ns1.example.com"):
            self.resolver.resolve(DNSRecord.question(name, "AAAA"))
        self.assertEqual(self.state.dns_errors, 0)
        self.assertEqual(self.state.dns_answers, 2)

    def test_13_smart_peak_group_precedes_health_preference(self):
        self.state.settings.mode = "SMART_PEAK"
        self.state.nodes.append(node("premium", category="PREMIUM"))
        from smart_dns.state import NodeHealth

        self.state.health["a"].status = "UP"
        self.state.health["premium"] = NodeHealth(status="DEGRADED")
        now = datetime(2026, 11, 1, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(select_node(self.state, now).id, "premium")

    def test_15_ewma_only_advances_for_a_new_snapshot(self):
        self.state.settings.mode = "TRAFFIC_AWARE"
        self.state.health["a"].traffic = TrafficSnapshot(
            fresh=True,
            used_gib=10,
            limit_gib=100,
            source_last_updated_ms=int(time.time() * 1000),
        )
        self.state.recompute_effective_weights(now_mono=1)
        first = self.state.health["a"].effective_weight
        for now in range(2, 60):
            self.state.recompute_effective_weights(now_mono=now)
        self.assertEqual(self.state.health["a"].effective_weight, first)
        self.state.health["a"].traffic.used_gib = 20
        self.state.recompute_effective_weights(now_mono=61)
        self.assertNotEqual(self.state.health["a"].effective_weight, first)

    def test_16_disabled_nodes_do_not_change_budget_reference(self):
        from smart_dns.state import NodeHealth

        self.state.settings.mode = "TRAFFIC_AWARE"
        self.state.nodes.append(node("disabled", enabled=False))
        stamp = int(time.time() * 1000)
        self.state.health["a"].traffic = TrafficSnapshot(
            fresh=True, used_gib=10, limit_gib=100, source_last_updated_ms=stamp
        )
        self.state.health["disabled"] = NodeHealth(
            traffic=TrafficSnapshot(
                fresh=True, used_gib=10, limit_gib=10000, source_last_updated_ms=stamp
            )
        )
        self.state.recompute_effective_weights(now_mono=1)
        self.assertEqual(self.state.health["a"].budget_factor, 1)

    def test_17_events_are_rate_limited_and_table_bounded(self):
        for _ in range(100):
            self.state.add_event("same outage", "warning", "a", noise=True)
        self.assertEqual(len(self.store.get_events(200)), 1)
        for index in range(1200):
            self.store.append_event(str(index))
        count = self.store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertLessEqual(count, 1100)
        self.assertEqual(
            self.store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )

    def test_20_end_of_day_includes_last_minute(self):
        settings = Settings(peak_end="24:00")
        now = datetime(2026, 11, 1, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(_in_peak(settings, now))
        self.assertTrue(_in_peak(Settings(), now))


if __name__ == "__main__":
    unittest.main()
