import asyncio
import json
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from smart_dns.models import Node, ProbeSource, Settings
from smart_dns.probe_source import fetch_probe_servers
from smart_dns.prober import EndToEndProber, MihomoSupervisor
from smart_dns.state import RuntimeState
from smart_dns.storage import Store
from smart_dns.tls import verified_context


class ProbingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.node = Node(id='relay',name='test',server='192.0.2.1',port=20086,uuid='00000000-0000-4000-8000-000000000001')
        self.state = RuntimeState(Settings(),[self.node],self.store)
        self.supervisor = MihomoSupervisor('',Path(self.tmp.name)/'helpers')
        self.prober = EndToEndProber(self.state,self.supervisor)

    async def asyncTearDown(self):
        await self.prober.stop();await self.supervisor.stop();self.store.close();self.tmp.cleanup()

    def factory(self, handler):
        original = httpx.AsyncClient
        def create(**kwargs):
            proxied = kwargs.pop('proxy',None) is not None
            kwargs['transport']=httpx.MockTransport(lambda request:handler(proxied,request))
            return original(**kwargs)
        return create

    async def test_real_request_success_and_exit_check(self):
        self.state.settings.expected_exit_ip='203.0.113.10'
        def handler(proxied,request):
            self.assertTrue(proxied)
            return httpx.Response(200,text='203.0.113.10') if request.url.host=='api.ipify.org' else httpx.Response(204)
        with patch('httpx.AsyncClient',side_effect=self.factory(handler)):
            result=await self.prober.probe_once(self.node,23000)
        self.assertTrue(result[0]);self.assertTrue(result[3])

    async def test_proxy_failure_with_healthy_control_is_down(self):
        def handler(proxied,request):
            if proxied:raise httpx.ConnectError('relay backend rejected',request=request)
            return httpx.Response(204)
        with patch('httpx.AsyncClient',side_effect=self.factory(handler)):
            self.assertFalse((await self.prober.probe_once(self.node,23000))[0])

    async def test_shared_target_outage_is_inconclusive(self):
        with patch('httpx.AsyncClient',side_effect=self.factory(lambda *args:httpx.Response(503))):
            self.assertIsNone((await self.prober.probe_once(self.node,23000))[0])

    async def test_wrong_exit_never_marks_healthy(self):
        self.state.settings.expected_exit_ip='203.0.113.10'
        with patch('httpx.AsyncClient',side_effect=self.factory(lambda p,r:httpx.Response(200,text='203.0.113.11') if r.url.host=='api.ipify.org' else httpx.Response(204))):
            result=await self.prober.probe_once(self.node,23000)
        self.assertFalse(result[0]);self.assertFalse(result[3])

    async def test_body_size_and_invalid_target_response(self):
        def handler(proxied,request):
            return httpx.Response(200,text='x'*5000) if proxied else httpx.Response(204)
        with patch('httpx.AsyncClient',side_effect=self.factory(handler)):
            self.assertFalse((await self.prober.probe_once(self.node,23000))[0])

    async def test_missing_helper_is_not_tcp_health(self):
        self.assertIsNone((await self.prober.probe_once(self.node))[0])

    async def test_tls_trust_store_shared_across_probes_control_and_monitor(self):
        original = httpx.AsyncClient
        contexts = []
        def create(**kwargs):
            contexts.append(kwargs.get('verify'))
            proxied = kwargs.pop('proxy', None) is not None
            def respond(request):
                if request.url.host == 'monitor.invalid':
                    return httpx.Response(200, json={'servers': []})
                if proxied:
                    raise httpx.ConnectError('relay failed', request=request)
                return httpx.Response(204)
            kwargs['transport'] = httpx.MockTransport(respond)
            return original(**kwargs)
        with patch('httpx.AsyncClient', side_effect=create):
            for _ in range(3):
                self.assertFalse((await self.prober.probe_once(self.node, 23000))[0])
            await fetch_probe_servers(ProbeSource(base_url='https://monitor.invalid'))
        self.assertGreaterEqual(len(contexts), 7)
        context = verified_context()
        self.assertTrue(all(item is context for item in contexts))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    async def test_monitor_actual_http_fixture_and_duplicate_names(self):
        payload={'servers':[{'id':'one','name':'same'},{'id':'two','name':'same'}]}
        async def handle(reader,writer):
            await reader.readuntil(b'\r\n\r\n')
            body=json.dumps(payload).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
            await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        try:
            port=server.sockets[0].getsockname()[1]
            records,_=await fetch_probe_servers(ProbeSource(base_url=f'http://127.0.0.1:{port}'))
            self.assertIn('id:one',records);self.assertIn('id:two',records);self.assertNotIn('name:same',records)
        finally:server.close();await server.wait_closed()

    async def test_monitor_redirect_and_non_json_rejected(self):
        for status,headers in [(302,{'location':'https://other.invalid'}),(200,{'content-type':'text/html'})]:
            with patch('httpx.AsyncClient',side_effect=self.factory(lambda p,r:httpx.Response(status,headers=headers,text='login page'))):
                with self.assertRaises(Exception):await fetch_probe_servers(ProbeSource(base_url='https://monitor.invalid'))

    async def test_helper_process_lifecycle_and_credentials_cleanup(self):
        # Small local process fixture exercises supervision, not the VLESS protocol.
        fixture=Path(self.tmp.name)/'helper.py'
        fixture.write_text("""import sys,socket,yaml
from pathlib import Path
path=Path(sys.argv[sys.argv.index('-f')+1]); config=yaml.safe_load(path.read_text())
if '-t' in sys.argv: raise SystemExit(0)
sock=socket.socket();sock.bind(('127.0.0.1',config['mixed-port']));sock.listen()
while True:
 connection,_=sock.accept();connection.close()
""",encoding='utf-8')
        original=asyncio.create_subprocess_exec
        async def launch(binary,*args,**kwargs):return await original(sys.executable,str(fixture),*args,**kwargs)
        self.supervisor.binary='local-fixture'
        with patch('asyncio.create_subprocess_exec',side_effect=launch):
            await self.supervisor.validate_nodes([self.node]);await self.supervisor.reconcile([self.node])
            self.assertIsNotNone(self.supervisor.port_for(self.node.id))
            managed=self.supervisor.processes[self.node.id]
            self.assertTrue(managed.config_path.exists())
            self.node.port=20087
            await self.supervisor.reconcile([self.node])
            self.assertIsNotNone(managed.process.returncode)
            managed=self.supervisor.processes[self.node.id]
            self.assertIn('20087',managed.config_path.read_text())
            await self.supervisor.stop()
            self.assertIsNotNone(managed.process.returncode);self.assertFalse(managed.config_path.exists())


if __name__=='__main__':unittest.main()
