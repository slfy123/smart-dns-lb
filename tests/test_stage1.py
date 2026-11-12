import asyncio
import contextlib
import copy
import io
import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app import load_runtime, main
from dnslib import A, NS, SOA, DNSRecord, QTYPE, RCODE, RR
from smart_dns.api import create_app
from smart_dns.links import (
    parse_vless_link,
    parse_vless_links,
    parse_vless_links_async,
    resolve_ipv4,
)
from smart_dns.models import Node, Settings
from smart_dns.setup import (
    RESOLVERS,
    check_delegation,
    derive_dns_settings,
    detect_mihomo,
    detect_public_ipv4,
    dns_query,
    setup_status,
)
from smart_dns.storage import Store

LINK = "vless://00000000-0000-4000-8000-000000000001@203.0.113.10:443?security=tls"


class Stage1Tests(unittest.TestCase):
    def test_domain_derivation_requires_independent_subzone(self):
        for name in (
            "dns.example.com",
            "example.com",
            "dns.example.com.cn",
            "dns.example.co.uk",
            "dns.example.com.au",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "请使用独立子区"
            ):
                derive_dns_settings(name)
        for name, zone in [
            ("node.lb.example.com", "lb.example.com."),
            ("node.lb.example.com.cn", "lb.example.com.cn."),
            ("node.lb.example.co.uk", "lb.example.co.uk."),
        ]:
            with self.subTest(name=name):
                self.assertEqual(derive_dns_settings(name)["zone"], zone)

    def test_first_start_detects_usr_local_mihomo_and_docker_defaults(self):
        binary = "/usr/local/bin/mihomo"
        with tempfile.TemporaryDirectory() as directory:
            original_is_file = Path.is_file

            def is_file(path):
                return str(path).replace("\\", "/").endswith(
                    binary
                ) or original_is_file(path)

            with patch("smart_dns.setup.shutil.which", return_value=None), patch(
                "smart_dns.setup.Path.is_file", is_file
            ), patch("smart_dns.setup.os.access", return_value=True):
                ctx, _ = load_runtime(directory)
            try:
                self.assertTrue(
                    ctx.state.settings.mihomo_path.replace("\\", "/").endswith(binary)
                )
                self.assertEqual(
                    ctx.store.get_config()[1]["settings"]["mihomo_path"],
                    ctx.state.settings.mihomo_path,
                )
            finally:
                ctx.store.close()
        root = Path(__file__).resolve().parent.parent
        self.assertIn(
            "ARG INSTALL_MIHOMO=1", (root / "Dockerfile").read_text(encoding="utf-8")
        )
        self.assertIn(
            "${INSTALL_MIHOMO:-1}",
            (root / "docker-compose.yml").read_text(encoding="utf-8"),
        )

    def test_vless_link_decoding_and_fields(self):
        link = "vless://00000000-0000-4000-8000-000000000001@203.0.113.10:443?type=ws&security=tls&sni=node.example.com&path=%2Fedge%3Fx%3D1&host=cdn.example.com&alpn=h2%2Chttp%2F1.1#线路%20甲"
        node = parse_vless_link(link)
        self.assertEqual(node["server"], "203.0.113.10")
        self.assertEqual(node["ws_path"], "/edge?x=1")
        self.assertEqual(node["name"], "线路 甲")
        self.assertEqual(node["alpn"], ["h2", "http/1.1"])

    def test_vless_batch_and_invalid_host(self):
        good = (
            "vless://00000000-0000-4000-8000-000000000001@203.0.113.10:443?security=tls"
        )
        self.assertEqual(len(parse_vless_links(good + "\n" + good)), 2)
        for bad in (
            good.replace("203.0.113.10", "node.example.com"),
            good.replace(":443", ""),
            "http://x",
        ):
            with self.assertRaises(ValueError):
                parse_vless_link(bad)

    def test_setup_helpers(self):
        self.assertEqual(
            derive_dns_settings("node.lb.example.com")["zone"], "lb.example.com."
        )
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "mihomo"
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            self.assertTrue(detect_mihomo(directory, str(binary))["available"])

    def test_vless_errors_and_protocol_fields(self):
        for old, new, message in (
            ("203.0.113.10", "[2001:db8::1]", "IPv4"),
            ("203.0.113.10", "node.example.com", "域名"),
            (":443", "", "端口"),
            (":443", ":65536", "端口"),
            ("00000000-0000-4000-8000-000000000001", "", "UUID"),
            ("00000000-0000-4000-8000-000000000001", "%20", "UUID"),
            ("00000000-0000-4000-8000-000000000001", "bad-secret", "UUID"),
            ("security=tls", "security=none", "TLS"),
            ("security=tls", "type=ws&type=tcp", "重复"),
            ("security=tls", "type=xhttp", "尚未支持"),
        ):
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, message):
                parse_vless_link(LINK.replace(old, new))
        with self.assertRaisesRegex(ValueError, "第 2 条"):
            parse_vless_links(LINK + "\ninvalid")
        with self.assertRaisesRegex(ValueError, "1–64"):
            parse_vless_links("\n".join([LINK] * 65))
        with self.assertRaises(ValueError) as error:
            parse_vless_link(
                LINK.replace("00000000-0000-4000-8000-000000000001", "bad-secret")
            )
        self.assertNotIn("bad-secret", str(error.exception))
        grpc = parse_vless_link(
            LINK + "&type=grpc&serviceName=edge%2Fgrpc&encryption=none"
        )
        self.assertEqual(grpc["grpc_service_name"], "edge/grpc")
        vision = parse_vless_link(LINK + "&type=tcp&flow=xtls-rprx-vision")
        self.assertEqual(vision["flow"], "xtls-rprx-vision")

    def test_setup_missing_and_complete(self):
        with patch(
            "smart_dns.setup.detect_mihomo",
            return_value={"available": False, "message": "未安装"},
        ):
            status = setup_status(Settings(), [], ".")
        self.assertFalse(status["complete"])
        self.assertEqual(
            set(status["missing"]),
            {"zone", "service_name", "ns_ipv4", "nodes", "mihomo"},
        )
        settings = Settings(
            **derive_dns_settings("node.lb.example.net"), ns_ipv4="1.1.1.1"
        )
        with patch(
            "smart_dns.setup.detect_mihomo",
            return_value={"available": True, "message": "已找到"},
        ):
            self.assertTrue(
                setup_status(
                    settings, [Node.model_validate(parse_vless_link(LINK))], "."
                )["complete"]
            )

    def test_mihomo_search_path_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "bin" / "mihomo"
            binary.parent.mkdir()
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            with patch("smart_dns.setup.shutil.which", return_value=None):
                self.assertEqual(
                    detect_mihomo(directory)["path"], str(binary.resolve())
                )
            with patch("smart_dns.setup.shutil.which", return_value=str(binary)):
                self.assertTrue(detect_mihomo("missing")["available"])
            with patch("smart_dns.setup.shutil.which", return_value=None), patch(
                "pathlib.Path.is_file", return_value=False
            ):
                self.assertFalse(detect_mihomo(directory)["available"])

    def test_token_cli_show_reset_without_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            old, _ = store.get_or_create_admin_token()
            store.save_config({"settings": {"invalid": True}})
            store.close()
            for command in ("--show-token", "--reset-token", "--show-token"):
                output = io.StringIO()
                with patch(
                    "sys.argv", ["app.py", "--data-dir", directory, command]
                ), patch("app.load_runtime") as runtime, contextlib.redirect_stdout(
                    output
                ):
                    main()
                runtime.assert_not_called()
                token = output.getvalue().strip()
                if command == "--reset-token":
                    self.assertNotEqual(token, old)
                    old = token
                else:
                    self.assertEqual(token, old)
                self.assertEqual(
                    (Path(directory) / "admin.token").read_bytes(),
                    (token + "\n").encode(),
                )
                if os.name != "nt":
                    self.assertEqual(
                        (Path(directory) / "admin.token").stat().st_mode & 0o777, 0o600
                    )

    def test_runtime_detects_data_directory_binary_without_path_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "mihomo"
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            ctx, _ = load_runtime(directory)
            try:
                self.assertEqual(ctx.state.settings.mihomo_path, str(binary.resolve()))
                self.assertEqual(
                    ctx.supervisor.config_dir,
                    Path(directory).resolve() / "mihomo-config",
                )
                self.assertEqual(
                    ctx.store.get_config()[1]["settings"]["mihomo_path"],
                    str(binary.resolve()),
                )
            finally:
                ctx.store.close()


class PublicIPTests(unittest.IsolatedAsyncioTestCase):
    async def test_typical_3xui_ws_and_tcp_domain_exports(self):
        ws = (
            LINK.replace("203.0.113.10", "edge.example.com")
            + "&type=ws&path=%2Fedge&host=cdn.example.com&headerType=none&allowInsecure=0&insecure=0&spx=%2F#WS"
        )
        tcp = (
            LINK.replace("203.0.113.10", "tcp.example.com")
            + "&type=tcp&sni=origin.example.com&headerType=none&allowInsecure=0&fp=chrome#TCP"
        )
        with patch(
            "smart_dns.links.resolve_ipv4", new=AsyncMock(return_value="203.0.113.20")
        ) as resolver:
            result = await parse_vless_links_async(ws + "\n" + tcp)
        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(result["nodes"][0]["server"], "203.0.113.20")
        self.assertEqual(result["nodes"][0]["tls_sni"], "edge.example.com")
        self.assertEqual(result["nodes"][0]["ws_path"], "/edge")
        self.assertFalse(result["nodes"][0]["skip_cert_verify"])
        self.assertEqual(result["nodes"][1]["tls_sni"], "origin.example.com")
        self.assertIn("已将域名解析为 203.0.113.20", result["warnings"][0])

    async def test_unsupported_behavior_and_resolution_failure(self):
        for query in (
            "security=reality",
            "security=tls&pbk=key",
            "security=tls&sid=abc",
            "security=tls&type=httpupgrade",
            "security=tls&type=kcp",
            "security=tls&headerType=http",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                await parse_vless_links_async(LINK.split("?")[0] + "?" + query)
        with patch(
            "smart_dns.links.resolve_ipv4",
            new=AsyncMock(side_effect=ValueError("解析超时")),
        ), self.assertRaisesRegex(ValueError, "第 1 条.*超时"):
            await parse_vless_links_async(
                LINK.replace("203.0.113.10", "edge.example.com")
            )
        self.assertTrue(parse_vless_link(LINK + "&allowInsecure=1")["skip_cert_verify"])

    async def test_dns_resolution_three_second_timeout(self):
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            new=AsyncMock(
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", 0))
                ]
            ),
        ):
            self.assertEqual(await resolve_ipv4("edge.example.com"), "203.0.113.20")

        async def timeout(awaitable, timeout):
            self.assertEqual(timeout, 3)
            awaitable.close()
            raise asyncio.TimeoutError

        with patch(
            "smart_dns.links.asyncio.wait_for", side_effect=timeout
        ), self.assertRaisesRegex(ValueError, "3 秒"):
            await resolve_ipv4("edge.example.com")

    async def test_sources_failure_fallback_and_consensus(self):
        original = httpx.AsyncClient

        def client(**kwargs):
            self.assertEqual(kwargs["timeout"], 2)
            self.assertFalse(kwargs["trust_env"])

            def handle(request):
                if request.url.host == "api.ipify.org":
                    raise httpx.ConnectError("fixture", request=request)
                return httpx.Response(200, text="1.1.1.1\n")

            return original(**kwargs, transport=httpx.MockTransport(handle))

        with patch("smart_dns.setup.httpx.AsyncClient", side_effect=client):
            result = await detect_public_ipv4()
        self.assertTrue(result["detected"])
        self.assertEqual(result["ip"], "1.1.1.1")
        with patch(
            "smart_dns.setup.httpx.AsyncClient",
            side_effect=lambda **kw: original(
                **kw,
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, text="127.0.0.1")
                )
            ),
        ):
            self.assertFalse((await detect_public_ipv4())["detected"])


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            **derive_dns_settings("node.lb.example.com"), ns_ipv4="203.0.113.10"
        )

    def answer(self, server, name, kind, recursive=True):
        reply = DNSRecord.question(name, kind).reply()
        if name == "example.com." and kind == "SOA":
            reply.add_answer(
                RR(
                    name,
                    QTYPE.SOA,
                    rdata=SOA(
                        "ns.example.com.", "hostmaster.example.com.", (1, 2, 3, 4, 5)
                    ),
                )
            )
        elif name == "example.com." and kind == "NS":
            reply.add_answer(RR(name, QTYPE.NS, rdata=NS("ns.example.com.")))
        elif name == "ns.example.com.":
            reply.add_answer(RR(name, QTYPE.A, rdata=A("198.51.100.53")))
        elif not recursive:
            self.assertEqual(server, "198.51.100.53")
            reply.add_auth(
                RR(self.settings.zone, QTYPE.NS, rdata=NS(self.settings.ns_name))
            )
            reply.add_ar(
                RR(self.settings.ns_name, QTYPE.A, rdata=A(self.settings.ns_ipv4))
            )
        elif kind == "NS":
            reply.add_answer(RR(name, QTYPE.NS, rdata=NS(self.settings.ns_name)))
        else:
            reply.add_answer(RR(name, QTYPE.A, rdata=A("203.0.113.21")))
        return reply

    def test_parent_authority_and_both_recursive_resolvers(self):
        with patch("smart_dns.setup.dns_query", side_effect=self.answer) as query:
            result = check_delegation(self.settings)
        self.assertTrue(result["effective"])
        self.assertTrue(result["parent"][0]["glue_ok"])
        self.assertEqual(len(result["recursive"]), 2)
        query.assert_any_call(
            "198.51.100.53", self.settings.service_name, "A", recursive=False
        )
        for resolver in RESOLVERS:
            query.assert_any_call(resolver, self.settings.service_name, "A")

    def test_missing_glue_and_timeout_diagnostics(self):
        def missing_glue(*args, **kwargs):
            reply = self.answer(*args, **kwargs)
            if kwargs.get("recursive") is False:
                reply.ar.clear()
            return reply

        with patch("smart_dns.setup.dns_query", side_effect=missing_glue):
            result = check_delegation(self.settings)
        self.assertFalse(result["effective"])
        self.assertIn("glue", result["suggestions"][0])
        with patch("smart_dns.setup.dns_query", side_effect=socket.timeout):
            result = check_delegation(self.settings)
        self.assertFalse(result["effective"])
        self.assertEqual(result["parent"], [])
        self.assertTrue(all("error" in item for item in result["recursive"]))

    def test_service_servfail_does_not_hide_valid_delegation(self):
        def unhealthy(server, name, kind, recursive=True):
            reply = self.answer(server, name, kind, recursive)
            if recursive and name == self.settings.service_name:
                reply.rr.clear()
                reply.header.rcode = RCODE.SERVFAIL
            return reply

        with patch("smart_dns.setup.dns_query", side_effect=unhealthy):
            result = check_delegation(self.settings)
        self.assertTrue(result["effective"])
        self.assertTrue(result["suggestions"])

    def test_dns_packet_question_validation_and_tcp_fallback(self):
        query = DNSRecord.question("lb.example.com", "NS")
        udp = MagicMock()
        tcp = MagicMock()
        reply = query.reply()
        reply.header.tc = 1
        udp.recv.return_value = reply.pack()
        final = query.reply().pack()
        mismatch = DNSRecord.question("other.example.com", "NS").reply()
        mismatch.header.id = query.header.id
        # 模拟短 TCP 读取，验证长度帧完整接收。
        tcp.recv.side_effect = [struct.pack("!H", len(final)), final[:4], final[4:]]
        with patch("smart_dns.setup.DNSRecord.question", return_value=query), patch(
            "smart_dns.setup.socket.socket"
        ) as sock, patch("smart_dns.setup.socket.create_connection") as connect:
            sock.return_value.__enter__.return_value = udp
            connect.return_value.__enter__.return_value = tcp
            self.assertEqual(
                dns_query("198.51.100.53", "lb.example.com", "NS").q.qtype, QTYPE.NS
            )
            udp.recv.return_value = mismatch.pack()
            with self.assertRaisesRegex(ValueError, "不匹配"):
                dns_query("198.51.100.53", "lb.example.com", "NS")


class Stage1APITests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_link_api_returns_resolution_warning(self):
        with patch(
            "smart_dns.links.resolve_ipv4", new=AsyncMock(return_value="203.0.113.20")
        ):
            response = await self.client.post(
                "/api/nodes/parse-link",
                json={
                    "links": LINK.replace("203.0.113.10", "edge.example.com")
                    + "&headerType=none&insecure=0"
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["nodes"][0]["server"], "203.0.113.20")
        self.assertEqual(response.json()["nodes"][0]["tls_sni"], "edge.example.com")
        self.assertIn("已将域名解析为", response.json()["warnings"][0])

    async def test_root_zone_derivation_failure_leaves_config_unchanged(self):
        before = self.ctx.store.get_config()
        for route, method, body in [
            ("/api/config", "PUT", {"settings": {"service_domain": "dns.example.com"}}),
            (
                "/api/setup/check-delegation",
                "POST",
                {"service_name": "dns.example.co.uk"},
            ),
        ]:
            response = await self.client.request(method, route, json=body)
            self.assertEqual(response.status_code, 422)
            self.assertIn("独立子区", response.json()["detail"])
        self.assertEqual(self.ctx.store.get_config(), before)

    async def test_concurrent_setup_requests_share_one_ip_probe(self):
        with patch(
            "smart_dns.api.detect_public_ipv4",
            new=AsyncMock(return_value={"detected": True, "ip": "203.0.113.10"}),
        ) as detect:
            results = await asyncio.gather(
                *(self.client.get("/api/setup/status") for _ in range(3))
            )
        self.assertTrue(all(response.status_code == 200 for response in results))
        self.assertEqual(detect.await_count, 1)

    async def test_public_ip_ten_minute_cache_refresh_and_expiry(self):
        with patch(
            "smart_dns.api.detect_public_ipv4",
            new=AsyncMock(return_value={"detected": False, "ip": ""}),
        ) as detect:
            first = await self.client.get("/api/setup/status")
            second = await self.client.get("/api/setup/status")
            self.assertEqual(first.json()["public_ip"], second.json()["public_ip"])
            self.assertEqual(detect.await_count, 1)
            await self.client.get("/api/setup/status?refresh_ip=1")
            self.assertEqual(detect.await_count, 2)
            self.ctx.public_ip_cache = (
                self.ctx.public_ip_cache[0] - 601,
                self.ctx.public_ip_cache[1],
            )
            await self.client.get("/api/setup/status")
            self.assertEqual(detect.await_count, 3)

    async def test_asset_whitelist_and_traversal(self):
        for name, content_type in [
            ("app.js", "text/javascript"),
            ("app.css", "text/css"),
        ]:
            response = await self.client.get("/assets/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertIn(content_type, response.headers["content-type"])
        for name in (
            "index.html",
            "api.py",
            "%2e%2e/app.py",
            "..%2fapp.py",
            "%2e%2e%5capp.py",
            "nested/app.js",
        ):
            response = await self.client.get("/assets/" + name)
            self.assertEqual(response.status_code, 404, name)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx, self.dns = load_runtime(self.tmp.name)
        # 配置保存会重新启动后台任务；这些测试只验证 API，禁止真实网络探测。
        self.prober_start = patch.object(self.ctx.prober, "start").start()
        self.poller_start = patch.object(self.ctx.poller, "start").start()
        self.addCleanup(patch.stopall)
        self.app = create_app(self.ctx, "test")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
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

    async def test_parse_export_import_and_setup_status(self):
        good = (
            "vless://00000000-0000-4000-8000-000000000001@203.0.113.10:443?security=tls"
        )
        response = await self.client.post("/api/nodes/parse-link", json={"links": good})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["nodes"][0]["port"], 443)
        with patch(
            "smart_dns.api.detect_public_ipv4",
            new=AsyncMock(return_value={"detected": False, "ip": ""}),
        ):
            response = await self.client.get("/api/setup/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("missing", response.json())
        exported = await self.client.get("/api/config/export")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            exported.json()["settings"]["probe_source"]["bearer_token"], ""
        )
        imported = await self.client.post("/api/config/import", json=exported.json())
        self.assertEqual(imported.status_code, 200, imported.text)

    async def test_delegation_endpoint_uses_mocked_diagnostics(self):
        with patch(
            "smart_dns.api.delegation_diagnostics",
            new=AsyncMock(return_value={"effective": True, "suggestions": []}),
        ) as check:
            response = await self.client.post("/api/setup/check-delegation", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["effective"])
        check.assert_awaited_once()

    async def test_all_stage1_routes_require_auth(self):
        for method, route in [
            ("GET", "/api/setup/status"),
            ("POST", "/api/setup/check-delegation"),
            ("POST", "/api/nodes/parse-link"),
            ("GET", "/api/config/export"),
            ("POST", "/api/config/import"),
        ]:
            response = await self.client.request(
                method, route, headers={"Authorization": ""}
            )
            self.assertEqual(response.status_code, 401, route)

    async def test_link_parse_errors_are_atomic(self):
        version = self.ctx.state.version
        response = await self.client.post(
            "/api/nodes/parse-link", json={"links": LINK + "\ninvalid"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.ctx.state.version, version)
        self.assertEqual(self.ctx.state.nodes, [])

    async def test_single_domain_and_public_ip_configuration(self):
        with patch(
            "smart_dns.api.detect_public_ipv4",
            new=AsyncMock(return_value={"detected": True, "ip": "203.0.113.10"}),
        ):
            response = await self.client.put(
                "/api/config",
                json={"settings": {"service_domain": "node.lb.example.com"}},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ctx.state.settings.ns_name, "ns1.lb.example.com.")
        self.assertEqual(self.ctx.state.settings.ns_ipv4, "203.0.113.10")
        with patch(
            "smart_dns.api.delegation_diagnostics",
            new=AsyncMock(return_value={"effective": False}),
        ) as diagnostic:
            response = await self.client.post(
                "/api/setup/check-delegation", json={"ns_ipv4": "198.51.100.10"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(diagnostic.call_args.args[0].ns_ipv4, "198.51.100.10")
        self.assertEqual(self.ctx.state.settings.ns_ipv4, "203.0.113.10")
        response = await self.client.post(
            "/api/setup/check-delegation", json={"service_name": "bad"}
        )
        self.assertEqual(response.status_code, 422)

    async def seed_config(self):
        payload = {
            "settings": {"ttl": 90, "probe_source": {"bearer_token": "fixture-token"}},
            "nodes": [parse_vless_link(LINK)],
            "version": 999,
        }
        response = await self.client.post("/api/config/import", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return payload

    async def test_export_redaction_and_secret_restoration(self):
        await self.seed_config()
        backup = (await self.client.get("/api/config/export")).json()
        self.assertTrue(backup["redacted"])
        self.assertNotIn("fixture-token", str(backup))
        self.assertEqual(backup["nodes"][0]["uuid"], "")
        complete = (
            await self.client.get("/api/config/export?include_secrets=1")
        ).json()
        self.assertFalse(complete["redacted"])
        self.assertEqual(
            complete["settings"]["probe_source"]["bearer_token"], "fixture-token"
        )
        self.assertTrue(complete["nodes"][0]["uuid"])
        backup["settings"]["ttl"] = 60
        response = await self.client.post("/api/config/import", json=backup)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.ctx.state.settings.probe_source.bearer_token, "fixture-token"
        )
        self.assertTrue(self.ctx.state.nodes[0].uuid)
        backup["nodes"][0]["id"] = "unmatched"
        before = self.ctx.store.get_config()
        response = await self.client.post("/api/config/import", json=backup)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.ctx.store.get_config(), before)

    async def test_import_replaces_defaults_and_clears_explicit_secrets(self):
        await self.seed_config()
        response = await self.client.post(
            "/api/config/import", json={"settings": {}, "nodes": []}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ctx.state.settings.ttl, Settings().ttl)
        self.assertEqual(self.ctx.state.settings.probe_source.bearer_token, "")
        self.assertEqual(self.ctx.state.nodes, [])

    async def test_import_failures_preserve_database_and_runtime(self):
        payload = await self.seed_config()
        before = self.ctx.store.get_config()
        settings = self.ctx.state.settings
        variants = [
            {},
            {"settings": {}, "nodes": None},
            {"settings": {"unknown": True}, "nodes": []},
        ]
        invalid = copy.deepcopy(payload)
        invalid["nodes"] *= 2
        variants.append(invalid)
        invalid = copy.deepcopy(payload)
        invalid["settings"]["ttl"] = 0
        variants.append(invalid)
        for invalid in variants:
            response = await self.client.post("/api/config/import", json=invalid)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.ctx.store.get_config(), before)
            self.assertIs(self.ctx.state.settings, settings)
        with patch.object(
            self.ctx.supervisor,
            "validate_nodes",
            new=AsyncMock(side_effect=ValueError("fixture validation failed")),
        ):
            response = await self.client.post("/api/config/import", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.ctx.store.get_config(), before)
        with patch.object(
            self.ctx.store, "save_config", side_effect=OSError("fixture write failed")
        ):
            response = await self.client.post("/api/config/import", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.ctx.store.get_config(), before)
        self.assertIs(self.ctx.state.settings, settings)

    async def test_import_conflicts_and_new_mihomo_supervisor(self):
        payload = await self.seed_config()
        response = await self.client.post(
            "/api/config/import", json={**payload, "expected_version": 0}
        )
        self.assertEqual(response.status_code, 409)
        response = await self.client.post(
            "/api/config/import", json=payload, headers={"If-Match": "0"}
        )
        self.assertEqual(response.status_code, 409)
        old_supervisor = self.ctx.supervisor
        payload["settings"]["mihomo_path"] = "/fixture/mihomo"
        payload["settings"]["dns_port"] = 15353
        with patch(
            "smart_dns.prober.MihomoSupervisor.validate_nodes", new=AsyncMock()
        ) as validate, patch.object(old_supervisor, "stop", new=AsyncMock()) as stop:
            response = await self.client.post("/api/config/import", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["restart_required"])
        self.assertEqual(self.ctx.supervisor.binary, "/fixture/mihomo")
        self.assertIs(self.ctx.prober.supervisor, self.ctx.supervisor)
        validate.assert_awaited_once()
        stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
