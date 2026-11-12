from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import socket
import struct
from pathlib import Path

import httpx
from dnslib import DNSError, DNSRecord, QTYPE, RCODE

from .models import Settings, normalize_name
from .tls import verified_context

PUBLIC_IP_SOURCES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)
RESOLVERS = ("1.1.1.1", "223.5.5.5")


def derive_dns_settings(service_name: str) -> dict[str, str]:
    service = normalize_name(service_name)
    labels = service.rstrip(".").split(".")
    common_suffixes = {
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "ac.cn",
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "co.jp",
        "ne.jp",
        "co.kr",
        "com.hk",
        "com.tw",
        "com.sg",
        "com.br",
        "co.nz",
        "co.in",
        "co.za",
        "com.mx",
        "com.tr",
    }
    suffix_length = 2 if ".".join(labels[-2:]) in common_suffixes else 1
    if len(labels) < suffix_length + 3:
        raise ValueError("请使用独立子区，例如 node.lb.example.com")
    zone = ".".join(labels[1:]) + "."
    ns = "ns1." + zone
    return {
        "service_name": service,
        "zone": zone,
        "ns_name": ns,
        "soa_mname": ns,
        "soa_rname": "hostmaster." + zone,
    }


async def detect_public_ipv4() -> dict:
    async def fetch(client, url):
        try:
            r = await client.get(url)
            r.raise_for_status()
            address = ipaddress.ip_address(r.text.strip())
            if address.version == 4 and address.is_global:
                return {"ip": str(address), "source": url}
        except (httpx.HTTPError, ValueError):
            pass
        return None

    async with httpx.AsyncClient(
        timeout=2, follow_redirects=False, trust_env=False, verify=verified_context()
    ) as client:
        items = [
            x
            for x in await asyncio.gather(
                *(fetch(client, u) for u in PUBLIC_IP_SOURCES)
            )
            if x
        ]
    if items:
        item = max(items, key=lambda x: sum(y["ip"] == x["ip"] for y in items))
        return {
            **item,
            "detected": True,
            "message": "请确认该地址可接收公网 UDP/TCP 53 请求",
        }
    return {
        "ip": "",
        "source": "",
        "detected": False,
        "message": "公网 IPv4 探测失败，请手动填写 NS IPv4",
    }


def detect_mihomo(data_dir: str | Path, configured: str = "") -> dict:
    data_dir = Path(data_dir)
    candidates = [
        configured,
        shutil.which("mihomo"),
        "/usr/local/bin/mihomo",
        str(data_dir / "mihomo"),
        str(data_dir / "mihomo.exe"),
        str(data_dir / "bin" / "mihomo"),
        str(data_dir / "bin" / "mihomo.exe"),
    ]
    checked = []
    for candidate in candidates:
        if not candidate or candidate in checked:
            continue
        checked.append(candidate)
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return {
                "available": True,
                "path": str(path.resolve()),
                "checked": checked,
                "message": "已找到可执行文件",
            }
    return {
        "available": False,
        "path": "",
        "checked": checked,
        "message": "请安装 Mihomo，或填写可执行文件的绝对路径",
    }


def mihomo_config_directory(data_dir: str | Path, configured: str = "") -> str:
    if configured:
        return configured
    default = Path(data_dir) / "mihomo"
    # 数据目录本身允许直接放 mihomo 可执行文件，运行配置应使用另一目录。
    return str(default.with_name("mihomo-config") if default.is_file() else default)


def setup_status(settings: Settings, nodes, data_dir) -> dict:
    mihomo = detect_mihomo(data_dir, settings.mihomo_path)
    example = settings.zone.endswith(".example.com.") or settings.zone == "example.com."
    address = ipaddress.ip_address(settings.ns_ipv4)
    checks = {
        "zone": {
            "ok": not example,
            "message": "请填写自己的区域名称" if example else "区域已配置",
        },
        "service_name": {
            "ok": not example,
            "message": "请填写服务域名" if example else "服务域名已配置",
        },
        "ns_ipv4": {
            "ok": address.is_global,
            "message": "NS IPv4 已配置" if address.is_global else "请填写本机公网 IPv4",
        },
        "nodes": {
            "ok": bool(nodes),
            "message": "已有节点" if nodes else "请至少添加一个节点",
        },
        "mihomo": {"ok": mihomo["available"], "message": mihomo["message"]},
    }
    return {
        "complete": all(v["ok"] for v in checks.values()),
        "checks": checks,
        "missing": [k for k, v in checks.items() if not v["ok"]],
        "mihomo": mihomo,
        "records": [
            {"name": settings.zone, "type": "NS", "value": settings.ns_name},
            {"name": settings.ns_name, "type": "A", "value": settings.ns_ipv4},
        ],
    }


def dns_query(server: str, name: str, kind: str, recursive=True) -> DNSRecord:
    ipaddress.IPv4Address(server)
    query = DNSRecord.question(name, kind)
    query.header.rd = int(recursive)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect((server, 53))
        sock.send(query.pack())
        raw = sock.recv(65535)
    reply = DNSRecord.parse(raw)
    if reply.header.tc:

        def receive(sock, size):
            chunks = bytearray()
            while len(chunks) < size:
                chunk = sock.recv(size - len(chunks))
                if not chunk:
                    raise ValueError("DNS TCP 响应提前结束")
                chunks.extend(chunk)
            return bytes(chunks)

        with socket.create_connection((server, 53), timeout=2) as sock:
            wire = query.pack()
            sock.sendall(struct.pack("!H", len(wire)) + wire)
            reply = DNSRecord.parse(
                receive(sock, struct.unpack("!H", receive(sock, 2))[0])
            )
    if (
        reply.header.id != query.header.id
        or not reply.header.qr
        or reply.header.tc
        or len(reply.questions) != 1
        or reply.q.qname != query.q.qname
        or reply.q.qtype != query.q.qtype
        or reply.q.qclass != query.q.qclass
    ):
        raise ValueError("DNS 响应与查询不匹配")
    return reply


def _records(reply, name, kind):
    return [
        rr
        for rr in (*reply.rr, *reply.auth)
        if rr.rtype == kind and str(rr.rname).lower() == name.lower()
    ]


def _parent_authorities(settings):
    """从公共递归获取最近父区 SOA/NS，再直接联系父区权威。"""
    parent = settings.zone.split(".", 1)[1] or "."
    for resolver in RESOLVERS:
        try:
            reply = dns_query(resolver, parent, "SOA")
            zones = [
                str(rr.rname).lower()
                for rr in (*reply.rr, *reply.auth)
                if rr.rtype == QTYPE.SOA
                and (
                    parent == str(rr.rname).lower()
                    or parent.endswith("." + str(rr.rname).lower())
                    or str(rr.rname) == "."
                )
            ]
            if not zones:
                continue
            parent_zone = max(zones, key=len)
            reply = dns_query(resolver, parent_zone, "NS")
            names = sorted(
                {str(rr.rdata).lower() for rr in _records(reply, parent_zone, QTYPE.NS)}
            )
            servers = []
            for name in names[:4]:
                addresses = dns_query(resolver, name, "A")
                for rr in _records(addresses, name, QTYPE.A)[:2]:
                    servers.append((parent_zone, name, str(rr.rdata)))
            if servers:
                return servers[:4]
        except (OSError, ValueError, DNSError):
            continue
    return []


def check_delegation(settings: Settings) -> dict:
    results = []
    authorities = []
    for parent_zone, hostname, address in _parent_authorities(settings):
        item = {
            "zone": parent_zone,
            "hostname": hostname,
            "server": address,
            "delegated": False,
        }
        try:
            reply = dns_query(address, settings.service_name, "A", recursive=False)
            names = [
                str(rr.rdata).lower() for rr in _records(reply, settings.zone, QTYPE.NS)
            ]
            glue = [
                str(rr.rdata)
                for rr in reply.ar
                if rr.rtype == QTYPE.A and str(rr.rname).lower() == settings.ns_name
            ]
            item.update(
                delegated=reply.header.rcode == RCODE.NOERROR
                and settings.ns_name in names,
                nameservers=names,
                glue=glue,
                glue_ok=settings.ns_ipv4 in glue,
                rcode=RCODE[reply.header.rcode],
            )
        except (OSError, ValueError, DNSError):
            item["error"] = "父区权威查询超时或响应格式错误"
        authorities.append(item)
    for resolver in RESOLVERS:
        try:
            reply = dns_query(resolver, settings.zone, "NS")
            names = [
                str(rr.rdata).lower() for rr in _records(reply, settings.zone, QTYPE.NS)
            ]
            service = dns_query(resolver, settings.service_name, "A")
            answers = [
                str(rr.rdata)
                for rr in _records(service, settings.service_name, QTYPE.A)
            ]
            results.append(
                {
                    "server": resolver,
                    "delegated": reply.header.rcode == RCODE.NOERROR
                    and settings.ns_name in names,
                    "service_ok": service.header.rcode == RCODE.NOERROR
                    and bool(answers),
                    "rcode": RCODE[service.header.rcode],
                    "answers": answers,
                }
            )
        except (OSError, ValueError, DNSError):
            results.append(
                {
                    "server": resolver,
                    "delegated": False,
                    "service_ok": False,
                    "error": "查询超时或响应格式错误",
                }
            )
    parent_ok = bool(authorities) and all(
        item.get("delegated") and item.get("glue_ok") for item in authorities
    )
    recursive_ok = all(item.get("delegated") for item in results)
    effective = parent_ok and recursive_ok
    suggestions = []
    if not parent_ok:
        suggestions.append(
            f"在父区添加 {settings.zone} NS {settings.ns_name}，并为 {settings.ns_name} 添加 A/glue {settings.ns_ipv4}（仅 DNS）"
        )
    if not recursive_ok:
        suggestions.append("等待父区 TTL 缓存到期后重试，并检查公网 UDP/TCP 53")
    if not all(item.get("service_ok") for item in results):
        suggestions.append(
            "服务 A 记录尚未正常返回；检查节点健康、区域配置和公网 UDP/TCP 53。空节点池返回 SERVFAIL 不等于委派缺失"
        )
    return {
        "effective": effective,
        "parent": authorities,
        "recursive": results,
        "message": "委派已生效" if effective else "委派尚未确认生效",
        "suggestions": suggestions,
    }


async def delegation_diagnostics(settings: Settings) -> dict:
    return await asyncio.to_thread(check_delegation, settings)
