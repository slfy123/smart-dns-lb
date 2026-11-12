import random
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dnslib import DNSRecord, RCODE

from smart_dns.dns import AuthorityResolver
from smart_dns.models import Node, Settings
from smart_dns.scheduler import select_node
from smart_dns.state import NodeHealth, RuntimeState, TrafficSnapshot
from smart_dns.storage import Store


class PeakRemainingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.state = self.make_state()
        self.day = self.at(12)
        self.peak = self.at(20)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def at(hour, minute=0):
        return datetime(2026, 10, 26, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai'))

    def make_state(self):
        nodes = [Node(id=name, name=name, server=f'192.0.2.{i + 1}', port=24765,
                      category='NORMAL' if name.startswith('n') else 'PREMIUM',
                      priority=2 if name.endswith('3') else 1)
                 for i, name in enumerate(['n1', 'n2', 'n3', 'p1', 'p2', 'p3'])]
        s = RuntimeState(Settings(mode='SMART_PEAK_REMAINING', peak_start='19:30',
                                  peak_end='00:00', timezone='Asia/Shanghai'), nodes, self.store)
        for i, n in enumerate(nodes):
            s.health[n.id] = NodeHealth(status='UP', last_observation_mono=time.monotonic(),
                certified_success_mono=time.monotonic(), traffic=TrafficSnapshot(
                    used_gib=100, limit_gib=200 if i % 3 == 0 else 400,
                    fresh=True, source_last_updated_ms=int(time.time() * 1000)))
        s.recompute_effective_weights(now_mono=1)
        return s

    def test_beijing_boundaries_and_utc_input(self):
        for when, group in [(self.at(0), 'n'), (self.at(19, 29), 'n'),
                            (self.at(19, 30), 'p'), (self.at(23, 59), 'p'),
                            (datetime(2026, 10, 26, 11, 30, tzinfo=timezone.utc), 'p')]:
            with self.subTest(when=when):
                for _ in range(50):
                    self.assertIn(select_node(self.state, when).id, [group+'1', group+'2'])
        self.state.settings.peak_start = '22:00'
        self.state.settings.peak_end = '02:00'
        for when, group in [(self.at(21, 59), 'n'), (self.at(22), 'p'),
                            (self.at(1, 59), 'p'), (self.at(2), 'n')]:
            with self.subTest(overnight=when):
                self.assertTrue(select_node(self.state, when).id.startswith(group))

    def test_same_tier_traffic_ratio_ignores_bandwidth_and_base_weight(self):
        s = self.state
        s.nodes[0].capacity_mbps = 1
        s.nodes[1].capacity_mbps = 1000000
        s.nodes[0].base_weight = 10000
        s.nodes[1].base_weight = 1
        s.health['n1'].traffic.rx_speed_bps = 10**10
        s.recompute_effective_weights(now_mono=2)
        rng = random.Random(42)
        with patch('smart_dns.scheduler.random.choices', side_effect=rng.choices):
            counts = Counter(select_node(s, self.day).id for _ in range(12000))
        self.assertEqual(set(counts), {'n1', 'n2'})
        self.assertAlmostEqual(counts['n1'] / 12000, 0.25, delta=0.02)

    def test_priority_within_group_before_cross_group_fallback(self):
        s = self.state
        s.nodes[0].priority = 2
        s.nodes[1].priority = 3
        s.nodes[2].priority = 4
        for _ in range(30):
            self.assertEqual(select_node(s, self.day).id, 'n1')
        s.health['n1'].status = 'DOWN'
        self.assertEqual(select_node(s, self.day).id, 'n2')
        s.health['n2'].status = 'DOWN'
        self.assertEqual(select_node(s, self.day).id, 'n3')
        s.health['n3'].status = 'DOWN'
        self.assertIn(select_node(s, self.day).id, {'p1', 'p2'})
        s.health['n1'].status = 'UP'
        self.assertEqual(select_node(s, self.day).id, 'n1')

    def test_quota_exhaustion_advances_priority_then_group_and_reset_restores(self):
        s = self.state
        for name in ['p1', 'p2']:
            s.health[name].traffic.used_gib = s.health[name].traffic.limit_gib * 0.99
        s.recompute_effective_weights(now_mono=2)
        self.assertEqual(select_node(s, self.peak).id, 'p3')
        s.health['p3'].traffic.used_gib = 10000
        s.recompute_effective_weights(now_mono=3)
        self.assertIn(select_node(s, self.peak).id, {'n1', 'n2'})
        s.health['p1'].traffic.used_gib = 0
        s.recompute_effective_weights(now_mono=4)
        self.assertEqual(select_node(s, self.peak).id, 'p1')

    def test_ineligible_primary_is_excluded_and_falls_back(self):
        for reason in ['disabled', 'maintenance', 'zero', 'DOWN', 'UNKNOWN', 'RECOVERING', 'STALE']:
            with self.subTest(reason=reason):
                s = self.make_state()
                for n in s.nodes[:3]:
                    if reason == 'disabled':
                        n.enabled = False
                    elif reason == 'maintenance':
                        n.maintenance = True
                    elif reason == 'zero':
                        n.base_weight = 0
                    elif reason == 'STALE':
                        s.health[n.id].last_observation_mono = time.monotonic() - 100
                    else:
                        s.health[n.id].status = reason
                s.recompute_effective_weights(now_mono=2)
                self.assertIn(select_node(s, self.day).id, {'p1', 'p2'})

    def test_degraded_primary_preserves_time_group_and_explicit_priority(self):
        s = self.state
        s.health['n1'].status = 'DEGRADED'
        s.health['n2'].status = 'DOWN'
        self.assertEqual(select_node(s, self.day).id, 'n1')
        # UP is preferred among peers, without skipping the group or priority.
        s.health['n2'].status = 'UP'
        self.assertEqual(select_node(s, self.day).id, 'n2')
        s.nodes[1].priority = 2
        self.assertEqual(select_node(s, self.day).id, 'n1')

    def test_missing_and_stale_quota_use_base_weight_but_keep_group_and_priority(self):
        s = self.state
        s.health['n1'].traffic.source_last_updated_ms = 1
        s.health['n2'].traffic = TrafficSnapshot()
        s.nodes[0].base_weight = 10
        s.nodes[1].base_weight = 30
        s.recompute_effective_weights(now_mono=2)
        self.assertEqual(s.health['n1'].effective_weight, 10)
        self.assertEqual(s.health['n2'].effective_weight, 30)
        rng = random.Random(12)
        with patch('smart_dns.scheduler.random.choices', side_effect=rng.choices):
            counts = Counter(select_node(s, self.day).id for _ in range(8000))
        self.assertEqual(set(counts), {'n1', 'n2'})
        self.assertAlmostEqual(counts['n1'] / 8000, 0.25, delta=0.02)

    def test_all_down_policy_and_dns_fail_closed(self):
        s = self.state
        for health in s.health.values():
            health.status = 'DOWN'
        self.assertIsNone(select_node(s, self.day))
        r = AuthorityResolver(s).resolve(DNSRecord.question(s.settings.service_name))
        self.assertEqual(r.header.rcode, RCODE.SERVFAIL)
        s.settings.all_down_policy = 'LAST_KNOWN'
        self.assertIn(select_node(s, self.day).id, {'n1', 'n2'})
        for health in s.health.values():
            health.certified_success_mono = time.monotonic() - 100
        self.assertIsNone(select_node(s, self.day))
        for health in s.health.values():
            health.status = 'UP'
            health.last_observation_mono = time.monotonic()
            health.traffic.used_gib = 10000
        s.recompute_effective_weights(now_mono=2)
        self.assertIsNone(select_node(s, self.day))

    def test_dns_answers_follow_time_group_without_disk_writes(self):
        s = self.state
        r = AuthorityResolver(s)
        with patch.object(self.store, 'append_event', side_effect=AssertionError('DNS disk write')):
            for peak, ids in [(False, {'n1', 'n2'}), (True, {'p1', 'p2'})]:
                ips = {n.server for n in s.nodes if n.id in ids}
                with patch('smart_dns.scheduler._in_peak', return_value=peak):
                    for _ in range(50):
                        reply = r.resolve(DNSRecord.question(s.settings.service_name))
                        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
                        self.assertEqual(len(reply.rr), 1)
                        self.assertIn(str(reply.rr[0].rdata), ips)


if __name__ == '__main__':
    unittest.main()
