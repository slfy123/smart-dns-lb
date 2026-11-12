import asyncio
import random
import socket
import struct
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import httpx
from dnslib import DNSRecord, RCODE, EDNS0

from app import load_runtime, make_lifespan
from smart_dns.api import create_app
from smart_dns.dns import AuthorityResolver, DNSServer, _UDPProtocol
from smart_dns.models import Node, Settings, validate_nodes
from smart_dns.probe_source import metric_snapshot
from smart_dns.prober import mihomo_node_config
from smart_dns.scheduler import select_node, _in_peak
from smart_dns.state import RuntimeState, _billing_cycle_progress
from smart_dns.storage import Store, VersionConflict


def node(name='a', **kwargs):
    return Node(id=name, name=name, server='192.0.2.'+str(ord(name[0])-96), port=20086, **kwargs)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.state = RuntimeState(Settings(mode='WEIGHTED'), [node('a'), node('b')], self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def up(self):
        for n in self.state.nodes:
            for _ in range(3): self.state.apply_probe_result(n.id, True, 100)

    def test_config_validation(self):
        for fields in [dict(service_name='outside.net'), dict(ns_name='node.example.com'), dict(peak_start='99:00'), dict(stale_after_seconds=5)]:
            with self.assertRaises(ValueError): Settings(**fields)
        with self.assertRaises(ValueError): node('../x')
        with self.assertRaises(ValueError): node(uuid='a'*33)
        with self.assertRaises(ValueError): validate_nodes([node(),node()])

    def test_failure_recovery_stale_and_old_version(self):
        s=self.state
        for t in (1,2,3): s.apply_probe_result('a', True, 100, now_mono=t)
        self.assertEqual(s.health['a'].status,'UP')
        s.apply_probe_result('a',False,now_mono=4);self.assertEqual(s.health['a'].status,'UP')
        for t in (5,6):s.apply_probe_result('a',False,now_mono=t)
        self.assertEqual(s.health['a'].status,'DOWN')
        for t in (7,8,9):s.apply_probe_result('a',True,100,now_mono=t)
        self.assertEqual(s.health['a'].status,'RECOVERING')
        s.apply_probe_result('a',False,now_mono=10)
        self.assertEqual(s.health['a'].status,'DOWN')
        for t in (11,12,13):s.apply_probe_result('a',True,100,now_mono=t)
        s.refresh_time_dependent_state(now_mono=50)
        self.assertEqual(s.health['a'].status,'STALE')
        s.apply_probe_result('a',True,100,config_version=99)
        self.assertEqual(s.health['a'].status,'STALE')

    def test_recovery_requires_continuing_success(self):
        s=self.state;s.health['a'].status='DOWN'
        for t in range(1,40,2):s.apply_probe_result('a',True,100,now_mono=t);s.refresh_time_dependent_state(now_mono=t)
        self.assertEqual(s.health['a'].status,'UP')

    def test_weight_edits_preserve_health_path_edits_reset(self):
        self.up();s=self.state
        s.replace_config(s.settings,[s.nodes[0].model_copy(update={'base_weight':10}),s.nodes[1]],1)
        self.assertEqual(s.health['a'].status,'UP')
        s.replace_config(s.settings,[s.nodes[0].model_copy(update={'port':1234}),s.nodes[1]],2)
        self.assertEqual(s.health['a'].status,'UNKNOWN')

    def test_degraded_recovery_rtt_percentage_and_node_threshold(self):
        from smart_dns.state import NodeHealth
        for percent, override, rtt, expected in [
            (80, None, 1300, 'DEGRADED'), (90, None, 1300, 'UP'),
            (100, None, 1499, 'UP'), (100, None, 1500, 'DEGRADED'),
            (50, None, 749, 'UP'), (90, 3000, 2600, 'UP'),
            (80, 3000, 2600, 'DEGRADED'),
        ]:
            with self.subTest(percent=percent, override=override, rtt=rtt):
                s = self.state
                s.settings.recovery_rtt_percent = percent
                s.nodes[0].max_rtt_ms = override
                s.health['a'] = NodeHealth(status='DEGRADED', ewma_rtt_ms=rtt)
                for t in (1, 2):
                    s.apply_probe_result('a', True, rtt, now_mono=t)
                    self.assertEqual(s.health['a'].status, 'DEGRADED')
                s.apply_probe_result('a', True, rtt, now_mono=3)
                self.assertEqual(s.health['a'].status, expected)

    def test_recovery_rtt_percentage_default_and_validation(self):
        self.assertEqual(Settings().recovery_rtt_percent, 80)
        for percent in (49, 101, float('nan'), float('inf')):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                Settings(recovery_rtt_percent=percent)

    def test_dns_semantics_and_no_disk_writes(self):
        self.up();r=AuthorityResolver(self.state)
        with patch.object(self.store,'append_event',side_effect=AssertionError('DNS disk access')):
            for name,kind,code,answers in [('node.example.com','A',0,1),('node.example.com','AAAA',0,0),('example.com','SOA',0,1),('example.com','NS',0,1),('absent.example.com','A',RCODE.NXDOMAIN,0),('evil-example.com','A',RCODE.REFUSED,0)]:
                response=r.resolve(DNSRecord.question(name,kind));self.assertEqual(response.header.rcode,code);self.assertEqual(len(response.rr),answers)
                if code==RCODE.REFUSED:self.assertEqual(response.header.aa,0)
        for h in self.state.health.values():h.status='DOWN'
        self.assertEqual(r.resolve(DNSRecord.question('node.example.com')).header.rcode,RCODE.SERVFAIL)
        self.assertEqual(DNSRecord.parse(DNSServer(self.state)._resolve_bytes(b'garbage')).header.rcode,RCODE.FORMERR)

    def test_weight_ratio_and_zero(self):
        self.state.nodes=[node('a',base_weight=60),node('b',base_weight=30),node('c',base_weight=10)]
        from smart_dns.state import NodeHealth
        self.state.health['c']=NodeHealth();self.up();random.seed(3)
        counts={x:0 for x in 'abc'}
        for _ in range(10000): counts[select_node(self.state).id]+=1
        self.assertTrue(5700<counts['a']<6300);self.assertTrue(2700<counts['b']<3300)
        for n in self.state.nodes:n.base_weight=0
        self.assertIsNone(select_node(self.state))

    def test_edns_truncation_and_response_packets_ignored(self):
        class Transport:
            def __init__(self):self.packets=[]
            def sendto(self,data,addr):self.packets.append(data)
        s=self.state
        s.settings.soa_mname='.'.join(['m'*60,'n'*60,'o'*60,'p'*60])+'.'
        s.settings.soa_rname='.'.join(['a'*60,'b'*60,'c'*60,'d'*60])+'.'
        protocol=_UDPProtocol(AuthorityResolver(s));transport=Transport();protocol.connection_made(transport)
        query=DNSRecord.question('example.com','SOA')
        protocol.datagram_received(query.pack(),('127.0.0.1',1))
        self.assertTrue(DNSRecord.parse(transport.packets[-1]).header.tc)
        query.add_ar(EDNS0(udp_len=1232));protocol.datagram_received(query.pack(),('127.0.0.1',1))
        self.assertFalse(DNSRecord.parse(transport.packets[-1]).header.tc)
        before=len(transport.packets);protocol.datagram_received(query.reply().pack(),('127.0.0.1',1))
        self.assertEqual(len(transport.packets),before)

    def test_failover_degraded_and_maintenance(self):
        self.up();self.state.settings.mode='FAILOVER';self.state.nodes[1].priority=2
        self.assertEqual(select_node(self.state).id,'a')
        self.state.health['a'].status='DEGRADED';self.assertEqual(select_node(self.state).id,'b')
        self.state.nodes[1].maintenance=True;self.assertEqual(select_node(self.state).id,'a')

    def test_last_known_bounded_and_disabled(self):
        self.up();self.state.settings.all_down_policy='LAST_KNOWN'
        for h in self.state.health.values():h.status='DOWN'
        self.assertIsNotNone(select_node(self.state))
        for h in self.state.health.values():h.certified_success_mono=time.monotonic()-100
        self.assertIsNone(select_node(self.state))

    def test_lowest_latency_hysteresis(self):
        self.up();s=self.state;s.settings.mode='LOWEST_RTT'
        s.health['b'].ewma_rtt_ms=110;self.assertEqual(select_node(s).id,'a')
        s.health['b'].ewma_rtt_ms=90;self.assertEqual(select_node(s).id,'a')
        s.lowest_rtt_selected_mono=time.monotonic()-60;s.health['b'].ewma_rtt_ms=50
        self.assertEqual(select_node(s).id,'b')

    def test_peak_cross_midnight_and_fallback(self):
        s=self.state;s.settings.peak_start='22:00';s.settings.peak_end='02:00';s.settings.mode='SMART_PEAK';self.up()
        self.assertTrue(_in_peak(s.settings,datetime(2026,10,26,1,tzinfo=ZoneInfo('Asia/Shanghai'))))
        s.nodes[1].category='PREMIUM'
        self.assertEqual(select_node(s,datetime(2026,10,26,23,tzinfo=ZoneInfo('Asia/Shanghai'))).id,'b')
        s.health['b'].status='DOWN';self.assertEqual(select_node(s,datetime(2026,10,26,23,tzinfo=ZoneInfo('Asia/Shanghai'))).id,'a')

    def test_billing_month_end(self):
        for date in [datetime(2024,2,29,12),datetime(2025,2,28),datetime(2026,1,1)]:
            self.assertTrue(0<=_billing_cycle_progress(31,date)<1)
        self.assertIsNone(_billing_cycle_progress(0,datetime.now()))

    def test_probe_units_modes_and_validation(self):
        now=time.time();record={'id':'one','traffic_limit':'500.0','net_rx_monthly':10*1024**3,'net_tx_monthly':20*1024**3,'last_updated':now*1000,'reset_day':1}
        for mode,expected in [('total',30),('ul',20),('dl',10),('max',20)]:
            snap=metric_snapshot(node(),{**record,'traffic_calc_type':mode},'',now,240)
            self.assertEqual(snap.used_gib,expected);self.assertEqual(snap.limit_gib,500);self.assertTrue(snap.fresh)
        for bad in [{'net_rx_monthly':-1},{'net_tx_monthly':'nan'},{'last_updated':(now+1000)*1000},{'last_updated':(now-1000)*1000},{'traffic_calc_type':'unknown'}]:
            self.assertFalse(metric_snapshot(node(),{**record,**bad},'',now,240).fresh)

    def test_traffic_quota_budget_stale_and_no_query_smoothing(self):
        s=self.state;self.up();s.settings.mode='TRAFFIC_AWARE'
        from smart_dns.state import TrafficSnapshot
        for n,limit in zip(s.nodes,[100,1000]):s.health[n.id].traffic=TrafficSnapshot(used_gib=10,limit_gib=limit,fresh=True,source_last_updated_ms=int(time.time()*1000))
        s.recompute_effective_weights(now_mono=1);self.assertGreater(s.health['b'].effective_weight,s.health['a'].effective_weight)
        before=s.health['b'].effective_weight
        for _ in range(100):select_node(s);s.public_snapshot()
        self.assertEqual(s.health['b'].effective_weight,before)
        s.health['a'].traffic.used_gib=99;s.recompute_effective_weights(now_mono=2)
        self.assertEqual(s.health['a'].effective_weight,0)
        s.health['a'].traffic.source_last_updated_ms=1;s.recompute_effective_weights(now_mono=3)
        self.assertEqual(s.health['a'].effective_weight,100)
        s.health['b'].traffic.source_last_updated_ms=1;s.recompute_effective_weights(now_mono=4)
        self.assertEqual(s.health['b'].effective_weight,100)

    def test_remaining_traffic_ratio_ignores_bandwidth_pace_and_positive_base_weights(self):
        from smart_dns.state import TrafficSnapshot
        s = self.state; self.up(); s.settings.mode = 'TRAFFIC_REMAINING'
        now = int(time.time() * 1000)
        s.nodes[0].capacity_mbps = 1; s.nodes[1].capacity_mbps = 1000000
        s.nodes[0].base_weight = 1; s.nodes[1].base_weight = 10000
        s.health['a'].traffic = TrafficSnapshot(used_gib=10, limit_gib=100, reset_day=1,
            rx_speed_bps=10**10, fresh=True, source_last_updated_ms=now)
        s.health['b'].traffic = TrafficSnapshot(used_gib=100, limit_gib=10000, reset_day=23,
            rx_speed_bps=0, fresh=True, source_last_updated_ms=now)
        s.recompute_effective_weights(now_mono=1)
        self.assertAlmostEqual(s.health['b'].effective_weight / s.health['a'].effective_weight, 110)
        before = [s.health[n.id].effective_weight for n in s.nodes]
        s.nodes[0].capacity_mbps = 1000000; s.nodes[1].capacity_mbps = 1
        s.health['a'].traffic.rx_speed_bps = 0; s.health['b'].traffic.rx_speed_bps = 10**10
        s.recompute_effective_weights(now_mono=2)
        self.assertEqual(before, [s.health[n.id].effective_weight for n in s.nodes])
        s.health['a'].traffic.used_gib = 20
        s.health['b'].traffic.limit_gib = 1000; s.health['b'].traffic.used_gib = 920
        s.recompute_effective_weights(now_mono=3)
        self.assertEqual(s.health['a'].effective_weight, s.health['b'].effective_weight)
        self.assertEqual(s.health['a'].capacity_factor, 1)
        self.assertEqual(s.health['b'].headroom_factor, 1)

    def test_remaining_traffic_exhaustion_failover_stale_and_reset(self):
        from smart_dns.state import TrafficSnapshot
        s = self.state; self.up(); s.settings.mode = 'TRAFFIC_REMAINING'
        now = int(time.time() * 1000)
        for n in s.nodes:
            s.health[n.id].traffic = TrafficSnapshot(used_gib=10, limit_gib=100,
                fresh=True, source_last_updated_ms=now)
        s.recompute_effective_weights(now_mono=1)
        s.health['a'].status = 'DOWN'
        self.assertEqual(select_node(s).id, 'b')
        s.health['b'].traffic.used_gib = 99
        s.recompute_effective_weights(now_mono=2)
        self.assertTrue(s.health['b'].quota_exhausted)
        self.assertEqual(s.health['b'].effective_weight, 0)
        self.assertIsNone(select_node(s))
        s.health['b'].traffic.source_last_updated_ms = 1
        s.recompute_effective_weights(now_mono=3)
        self.assertEqual(s.health['b'].effective_weight, s.nodes[1].base_weight)
        self.assertFalse(s.health['b'].quota_exhausted)
        s.health['b'].traffic.used_gib = 0; s.health['b'].traffic.source_last_updated_ms = now
        s.nodes[1].base_weight = 0
        s.recompute_effective_weights(now_mono=4)
        self.assertEqual(s.health['b'].effective_weight, 0)
        s.nodes[1].base_weight = 100; s.recompute_effective_weights(now_mono=5)
        self.assertGreater(s.health['b'].effective_weight, 0)
        self.assertEqual(select_node(s).id, 'b')

    def test_mihomo_transport_forced_route(self):
        for transport in ['tcp','ws','grpc']:
            cfg=mihomo_node_config(node(uuid='00000000-0000-4000-8000-000000000001',transport=transport),23000)
            self.assertEqual(cfg['rules'],['MATCH,FORCED_NODE']);self.assertNotIn('DIRECT',str(cfg));self.assertTrue(cfg['proxies'][0]['tls'])

    def test_vless_encryption_and_alpn_preserved_and_health_invalidated(self):
        encrypted = node(uuid='00000000-0000-4000-8000-000000000001', transport='ws',
                         encryption='mlkem768x25519plus.native.0rtt.example-public-key',
                         alpn=['h2', 'http/1.1'])
        proxy = mihomo_node_config(encrypted, 23000)['proxies'][0]
        self.assertEqual(proxy['encryption'], encrypted.encryption)
        self.assertEqual(proxy['alpn'], ['h2', 'http/1.1'])
        self.assertEqual(mihomo_node_config(node(uuid=encrypted.uuid), 23000)['proxies'][0]['encryption'], 'none')
        for field, value in [('encryption', 'none'), ('alpn', ['http/1.1'])]:
            state = RuntimeState(Settings(), [encrypted], self.store)
            previous = state.health[encrypted.id]
            changed = Node.model_validate({**encrypted.model_dump(), field: value})
            state.replace_config(state.settings, [changed], state.version + 1)
            self.assertIsNot(state.health[encrypted.id], previous)
        with self.assertRaises(ValueError):
            node(alpn=['h2\r\ninvalid'])
        with self.assertRaises(ValueError):
            node(encryption='none\ninvalid')

    def test_storage_conflict_and_persistence(self):
        self.store.save_config({'a':1},0)
        with self.assertRaises(VersionConflict):self.store.save_config({'a':2},0)
        self.assertEqual(self.store.get_config(),(1,{'a':1}))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ctx,self.dns=load_runtime(self.tmp.name)
        self.app=create_app(self.ctx,'<html>test</html>')
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test',headers={'Authorization':'Bearer '+self.ctx.admin_token})

    async def asyncTearDown(self):
        await self.client.aclose();await self.ctx.prober.stop();await self.ctx.poller.stop();await self.ctx.supervisor.stop();await self.dns.stop();self.ctx.store.close();self.tmp.cleanup()

    async def test_api_auth_crud_secret_preservation_and_conflicts(self):
        self.assertEqual((await self.client.get('/api/status',headers={'Authorization':''})).status_code,401)
        n=node(uuid='00000000-0000-4000-8000-000000000001')
        r=await self.client.post('/api/nodes',json=n.model_dump());self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn(n.uuid,r.text);version=r.json()['version']
        r=await self.client.put('/api/nodes/a',json={'base_weight':5,'uuid':''},headers={'If-Match':str(version)})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.ctx.state.nodes[0].uuid,n.uuid)
        r=await self.client.put('/api/nodes/a',json={'base_weight':20},headers={'If-Match':str(version)})
        self.assertEqual(r.status_code,409)
        r=await self.client.post('/api/nodes',json=n.model_dump());self.assertEqual(r.status_code,422)
        r=await self.client.post('/api/nodes',json={**n.model_dump(),'uuid':'secret-invalid'})
        self.assertEqual(r.status_code,422);self.assertNotIn('secret-invalid',r.text)
        self.assertEqual((await self.client.delete('/api/nodes/a')).status_code,200)

    async def test_api_add_node_can_inherit_connection_template(self):
        template = node(uuid='00000000-0000-4000-8000-000000000001', transport='ws',
                        encryption='mlkem768x25519plus.native.0rtt.example-public-key',
                        alpn=['h2', 'http/1.1'], tls_sni='node.example.com',
                        ws_path='/shared', ws_host='node.example.com')
        r = await self.client.post('/api/nodes', json=template.model_dump())
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.client.post('/api/nodes', json={
            'id': 'b', 'name': 'new line', 'server': '192.0.2.2', 'port': 24765,
            'inherit_connection': True, 'inherit_from': 'a', 'priority': 2,
        })
        self.assertEqual(r.status_code, 200, r.text)
        saved = next(item for item in self.ctx.state.nodes if item.id == 'b')
        self.assertEqual(saved.transport, 'ws')
        self.assertEqual(saved.encryption, template.encryption)
        self.assertEqual(saved.alpn, ['h2', 'http/1.1'])
        self.assertEqual(saved.ws_path, '/shared')
        self.assertNotIn(template.uuid, r.text)

    async def test_api_config_secrets_and_restart_flag(self):
        r=await self.client.put('/api/config',json={'settings':{'probe_source':{'bearer_token':'private-secret'}}})
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('private-secret',r.text)
        r=await self.client.put('/api/config',json={'settings':r.json()['settings'],'version':r.json()['version']})
        self.assertEqual(r.status_code,200);self.assertEqual(self.ctx.state.settings.probe_source.bearer_token,'private-secret')
        r=await self.client.put('/api/config',json={'settings':{'dns_port':15354},'clear_probe_token':True})
        self.assertTrue(r.json()['restart_required']);self.assertEqual(self.ctx.state.settings.probe_source.bearer_token,'')

    async def test_api_peak_remaining_mode_roundtrip_and_persistence(self):
        r = await self.client.put('/api/config', json={'settings': {
            'mode': 'SMART_PEAK_REMAINING', 'timezone': 'Asia/Shanghai',
            'peak_start': '19:30', 'peak_end': '00:00'}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['settings']['mode'], 'SMART_PEAK_REMAINING')
        self.assertEqual(r.json()['settings']['timezone'], 'Asia/Shanghai')
        version, saved = self.ctx.store.get_config()
        self.assertEqual(saved['settings']['mode'], 'SMART_PEAK_REMAINING')
        self.assertEqual(saved['settings']['peak_end'], '00:00')
        self.assertFalse(r.json()['restart_required'])
        restored, _ = load_runtime(self.tmp.name)
        try:
            self.assertEqual(restored.state.settings.mode, 'SMART_PEAK_REMAINING')
        finally:
            restored.store.close()

    async def test_api_encrypted_vless_roundtrip_keeps_protocol_parameters(self):
        n = node(uuid='00000000-0000-4000-8000-000000000001', transport='ws',
                 encryption='mlkem768x25519plus.native.0rtt.example-public-key',
                 alpn=['h2', 'http/1.1'])
        r = await self.client.post('/api/nodes', json=n.model_dump())
        self.assertEqual(r.status_code, 200, r.text)
        public = r.json()['nodes'][0]
        self.assertEqual(public['encryption'], '')
        self.assertTrue(public['has_encryption'])
        self.assertNotIn(n.encryption, r.text)
        self.assertEqual(public['alpn'], n.alpn)
        r = await self.client.put('/api/nodes/a', json={'base_weight': 42, 'uuid': ''})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.ctx.state.nodes[0].encryption, n.encryption)
        self.assertEqual(self.ctx.state.nodes[0].alpn, n.alpn)
        self.assertNotIn(n.uuid, r.text)

    async def test_api_recovery_rtt_percentage_persists_and_applies_live(self):
        self.assertEqual((await self.client.get('/api/status')).json()['settings']['recovery_rtt_percent'], 80)
        r = await self.client.put('/api/config', json={'settings': {'recovery_rtt_percent': 90}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['settings']['recovery_rtt_percent'], 90)
        self.assertEqual(self.ctx.state.settings.recovery_rtt_percent, 90)
        self.assertFalse(r.json()['restart_required'])
        version, saved = self.ctx.store.get_config()
        self.assertEqual(saved['settings']['recovery_rtt_percent'], 90)
        restored, _ = load_runtime(self.tmp.name)
        try:
            self.assertEqual(restored.state.settings.recovery_rtt_percent, 90)
        finally:
            restored.store.close()
        r = await self.client.put('/api/config', json={'settings': {'recovery_rtt_percent': 101}})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.ctx.state.settings.recovery_rtt_percent, 90)
        self.assertEqual(self.ctx.state.version, version)

    async def test_udp_tcp_real_sockets_and_clean_stop(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        self.ctx.state.settings.dns_host='127.0.0.1';self.ctx.state.settings.dns_port=port
        await self.dns.start();query=DNSRecord.question('example.com','SOA').pack()
        def udp_query():
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                udp.settimeout(2);udp.sendto(query,('127.0.0.1',port));return DNSRecord.parse(udp.recv(4096))
        self.assertEqual((await asyncio.to_thread(udp_query)).rr[0].rtype,6)
        reader,writer=await asyncio.open_connection('127.0.0.1',port);writer.write(struct.pack('!H',len(query))+query);await writer.drain()
        size=struct.unpack('!H',await reader.readexactly(2))[0];reply=DNSRecord.parse(await reader.readexactly(size))
        self.assertEqual(reply.rr[0].rtype,6);writer.close();await writer.wait_closed();await self.dns.stop()

    async def test_complete_lifespan_and_persisted_restart(self):
        # Independent context since the lifespan owns and closes its database.
        with tempfile.TemporaryDirectory() as directory:
            ctx,dns=load_runtime(directory)
            ctx.state.nodes=[node('a',maintenance=True)]
            ctx.store.save_config({'settings':ctx.state.settings.model_dump(),'nodes':[ctx.state.nodes[0].model_dump()]})
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            ctx.state.settings.dns_host='127.0.0.1';ctx.state.settings.dns_port=port
            app=create_app(ctx,'test')
            async with make_lifespan(ctx,dns)(app):
                self.assertTrue(ctx.state.dns_running)
                self.assertIsNotNone(ctx.prober._task)
            self.assertFalse(ctx.state.dns_running)
            self.assertIsNone(ctx.prober._task);self.assertIsNone(ctx.poller._task)
            with socket.socket() as check:check.bind(('127.0.0.1',port))
            restarted,_=load_runtime(directory)
            try:self.assertTrue(restarted.state.nodes[0].maintenance)
            finally:restarted.store.close()


if __name__=='__main__':unittest.main()
