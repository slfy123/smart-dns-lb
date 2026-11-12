"""R1–R8 验收回归；所有外部服务和子进程均使用 mock。"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import httpx
from dnslib import DNSRecord

from app import load_runtime
from smart_dns.api import create_app
from smart_dns.dns import AuthorityResolver, DNSServer
from smart_dns.models import Node, Settings
from smart_dns.prober import EndToEndProber, ManagedNodeProcess, MihomoSupervisor
from smart_dns.scheduler import _in_peak, select_node
from smart_dns.state import RuntimeState
from smart_dns.storage import Store


def node(identifier="a", **kwargs):
    return Node(id=identifier, name=identifier, server="192.0.2.1", port=443, **kwargs)


class StateRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.state = RuntimeState(
            Settings(mode="WEIGHTED"), [node(), node("b")], self.store
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def healthy(self, observed=100):
        for health in self.state.health.values():
            health.status = "UP"
            health.last_observation_mono = observed
            health.certified_success_mono = observed

    def test_r1_64_node_selection_visits_nodes_linearly(self):
        class CountingNodes(list):
            visits = 0

            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item

        nodes = CountingNodes(node(str(index)) for index in range(64))
        self.state = RuntimeState(Settings(mode="WEIGHTED"), nodes, self.store)
        self.healthy()
        nodes.visits = 0
        with patch("smart_dns.state.time.monotonic", return_value=105):
            for _ in range(10):
                self.assertIsNotNone(select_node(self.state))
        self.assertLessEqual(nodes.visits, 10 * 64 * 3)

    def test_r1_cached_timeout_updates_with_config(self):
        nodes = [node(str(index)) for index in range(64)]
        self.state.replace_config(self.state.settings, nodes, 1)
        self.assertEqual(self.state.observation_timeout, 70)
        self.state.replace_config(self.state.settings, nodes[:32], 2)
        self.assertEqual(self.state.observation_timeout, 38)
        settings = Settings(mode="WEIGHTED", max_parallel_probes=1)
        self.state.replace_config(settings, nodes[:32], 3)
        self.assertEqual(self.state.observation_timeout, 134)
        disabled = [item.model_copy(update={"enabled": False}) for item in nodes]
        self.state.replace_config(settings, disabled, 4)
        self.assertEqual(self.state.observation_timeout, 19)

    def test_r2_event_storage_failure_keeps_all_transitions_and_weights(self):
        self.healthy()
        with patch.object(
            self.store, "append_event", side_effect=OSError("fixture")
        ), patch.object(
            self.state, "recompute_effective_weights"
        ) as recompute, self.assertLogs(
            "smart_dns.state", "ERROR"
        ):
            self.state.refresh_time_dependent_state(now_mono=140)
        self.assertEqual(
            [health.status for health in self.state.health.values()], ["STALE", "STALE"]
        )
        recompute.assert_called_once_with(now_mono=140)
        self.assertEqual(len(self.state._event_cache), 2)

    def test_r3_state_transitions_and_config_events_are_never_deduplicated(self):
        self.state.settings.fail_threshold = 1
        self.state.settings.recover_threshold = 1
        self.state.settings.recovery_steady_seconds = 0
        self.healthy()
        self.state.apply_probe_result("a", False, now_mono=101)
        self.state.apply_probe_result("a", True, 100, now_mono=102)
        self.state.refresh_time_dependent_state(now_mono=102)
        self.state.apply_probe_result("a", False, now_mono=103)
        self.state.add_event("配置已保存并重新加载")
        self.state.add_event("配置已保存并重新加载")
        events = self.store.get_events(100)
        self.assertEqual(sum("→ DOWN" in event["message"] for event in events), 2)
        self.assertEqual(
            sum(event["message"] == "配置已保存并重新加载" for event in events), 2
        )
        self.assertIn("→ DOWN", events[2]["message"])

    def test_r3_known_stale_noise_is_still_rate_limited(self):
        for _ in range(3):
            self.healthy()
            self.state.refresh_time_dependent_state(now_mono=140)
        self.assertEqual(len(self.store.get_events(100)), 2)

    def test_r4_cd_flag_is_copied(self):
        resolver = AuthorityResolver(self.state)
        for cd in (0, 1):
            request = DNSRecord.question("example.com", "SOA")
            request.header.cd = cd
            reply = DNSRecord.parse(resolver.resolve(request).pack())
            self.assertEqual(reply.header.cd, cd)

    def restart_executor(self, now=105):
        settings = self.state.settings.model_copy(update={"mihomo_path": "fixture-new"})
        with patch("smart_dns.state.time.monotonic", return_value=now):
            self.state.replace_config(settings, self.state.nodes, 1)

    def test_r5_executor_restart_has_bounded_startup_grace(self):
        self.state = RuntimeState(
            Settings(mode="WEIGHTED"),
            [node()] + [node(str(index)) for index in range(63)],
            self.store,
        )
        self.healthy()
        self.restart_executor()
        self.state.refresh_time_dependent_state(now_mono=250)
        self.assertTrue(
            all(health.status == "UP" for health in self.state.health.values())
        )
        with patch("smart_dns.state.time.monotonic", return_value=250):
            self.assertIsNotNone(select_node(self.state))
        deadline = self.state.health["a"].probe_startup_until_mono
        self.assertIsNotNone(deadline)
        self.state.refresh_time_dependent_state(now_mono=deadline + 1)
        self.assertEqual(self.state.health["a"].status, "STALE")

    def test_r5_ready_exit_shortens_grace_and_real_observation_ends_it(self):
        self.healthy()
        self.restart_executor()
        self.state.complete_probe_startup("a", now_mono=110)
        deadline = self.state.health["a"].probe_startup_until_mono
        self.assertEqual(deadline, 110 + self.state.observation_timeout)
        self.state.complete_probe_startup("a", now_mono=120)
        self.assertEqual(self.state.health["a"].probe_startup_until_mono, deadline)
        self.state.settings.fail_threshold = 1
        self.state.apply_probe_result("a", False, now_mono=111)
        self.assertEqual(self.state.health["a"].status, "DOWN")
        self.assertIsNone(self.state.health["a"].probe_startup_until_mono)

    def test_r5_restart_does_not_revive_an_already_stale_node(self):
        self.healthy()
        self.restart_executor(now=140)
        self.assertEqual(
            self.state.effective_health_status(self.state.nodes[0], 140), "STALE"
        )

    def test_r7_equal_times_mean_all_day_peak(self):
        for fields in (
            {"peak_start": "00:00"},
            {"peak_start": "00:00", "peak_end": "24:00"},
            {"peak_start": "21:00", "peak_end": "21:00"},
        ):
            settings = Settings(**fields)
            for hour in range(24):
                now = datetime(2026, 11, 1, hour, tzinfo=ZoneInfo("Asia/Shanghai"))
                self.assertTrue(_in_peak(settings, now))


class AsyncRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx, self.dns = load_runtime(self.tmp.name)
        self.ctx.state.replace_config(
            self.ctx.state.settings, [node()], self.ctx.state.version
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(self.ctx, "fixture"), raise_app_exceptions=False
            ),
            base_url="http://fixture",
            headers={"Authorization": "Bearer " + self.ctx.admin_token},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.ctx.prober.stop()
        await self.ctx.poller.stop()
        await self.ctx.supervisor.stop()
        await self.dns.stop()
        self.ctx.store.close()
        self.tmp.cleanup()

    async def test_r2_manual_probe_survives_event_storage_failure(self):
        self.ctx.state.settings.recover_threshold = 1
        with patch.object(
            self.ctx.prober,
            "probe_once",
            new=AsyncMock(return_value=(True, 100, "", False)),
        ), patch.object(
            self.ctx.store, "append_event", side_effect=OSError("fixture")
        ), self.assertLogs(
            "smart_dns.state", "ERROR"
        ):
            response = await self.client.post("/api/nodes/a/probe")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node"]["status"], "UP")
        self.assertTrue(self.ctx.state._event_cache)

    async def test_r2_probe_loop_retry_is_capped_at_probe_interval(self):
        prober = self.ctx.prober
        delays = []
        calls = 0

        async def failed(*args):
            nonlocal calls
            calls += 1
            if calls == 8:
                prober._stop.set()
            raise OSError("fixture")

        async def wait(awaitable, timeout):
            awaitable.close()
            delays.append(timeout)

        with patch.object(prober, "_probe_guarded", side_effect=failed), patch(
            "smart_dns.tasks.asyncio.wait_for", side_effect=wait
        ), self.assertLogs("smart_dns.tasks", "ERROR"):
            await prober._run()
        self.assertEqual(len(delays), 8)
        self.assertLessEqual(
            max(delays), self.ctx.state.settings.probe_interval_seconds
        )

    async def test_r5_unready_helper_does_not_extend_startup_deadline(self):
        state = self.ctx.state
        health = state.health["a"]
        health.status = "UP"
        health.last_observation_mono = health.certified_success_mono = 100
        with patch("smart_dns.state.time.monotonic", return_value=105):
            state.replace_config(
                state.settings.model_copy(update={"mihomo_path": "fixture-new"}),
                state.nodes,
                state.version + 1,
            )
        deadline = health.probe_startup_until_mono
        for _ in range(3):
            result = await self.ctx.prober.probe_once(state.nodes[0])
            self.assertIsNone(result[0])
        self.assertEqual(health.probe_startup_until_mono, deadline)

    async def test_r6_cancelled_stop_keeps_process_tracked_until_exit(self):
        supervisor = MihomoSupervisor("fixture", Path(self.tmp.name) / "cancel")
        config = supervisor.config_dir / "a" / "config.yaml"
        config.parent.mkdir()
        config.write_text("fixture\n", encoding="utf-8")
        waiting = asyncio.Event()

        async def wait():
            waiting.set()
            await asyncio.Event().wait()

        process = Mock(returncode=None, wait=AsyncMock(side_effect=wait))
        managed = ManagedNodeProcess("a", 21000, process, config, ready=True)
        supervisor.processes["a"] = managed
        task = asyncio.create_task(supervisor._stop_node("a"))
        await asyncio.wait_for(waiting.wait(), 0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(supervisor.processes.get("a"), managed)
        self.assertIsNone(supervisor.port_for("a"))
        process.wait = AsyncMock(side_effect=[asyncio.TimeoutError, 0])
        await supervisor.stop()
        process.kill.assert_called_once()
        self.assertFalse(supervisor.processes)
        self.assertFalse(config.exists())

    async def test_r7_json_import_accepts_default_all_day_peak(self):
        with patch.object(self.ctx.prober, "start"), patch.object(
            self.ctx.poller, "start"
        ):
            response = await self.client.post(
                "/api/config/import",
                json={"settings": {"peak_start": "00:00"}, "nodes": []},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_peak"])

    async def test_r8_tcp_accepts_32_per_ip_and_rejects_the_33rd(self):
        self.assertEqual(self.dns.tcp_per_ip_limit, 32)
        writer = Mock(
            get_extra_info=Mock(return_value=("192.0.2.1", 1000)),
            wait_closed=AsyncMock(),
        )
        self.dns._tcp_per_ip["192.0.2.1"] = 31
        with patch.object(self.dns, "_serve_tcp", new=AsyncMock()) as serve:
            await self.dns._handle_tcp(Mock(), writer)
            serve.assert_awaited_once()
        self.dns._tcp_per_ip["192.0.2.1"] = 32
        with patch.object(self.dns, "_serve_tcp", new=AsyncMock()) as serve:
            await self.dns._handle_tcp(Mock(), writer)
            serve.assert_not_awaited()
        self.dns._tcp_per_ip.clear()


if __name__ == "__main__":
    unittest.main()
