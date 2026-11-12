from __future__ import annotations

import asyncio
import struct

from dnslib import A, DNSHeader, DNSLabel, DNSRecord, EDNS0, NS, QTYPE, RR, SOA, RCODE

from .scheduler import select_node
from .state import RuntimeState

# 大型递归解析器会复用多个 TCP 连接；保留每 IP 防滥用上限并给并行查询留出余量。
TCP_PER_IP_LIMIT = 32


class AuthorityResolver:
    def __init__(self, state: RuntimeState):
        self.state = state

    def resolve(self, request: DNSRecord) -> DNSRecord:
        reply = _new_reply(request)
        opts = [rr for rr in request.ar if rr.rtype == QTYPE.OPT]
        if len(opts) > 1:
            reply.header.rcode = RCODE.FORMERR
            self.state.note_dns("error")
            return reply
        if opts:
            opt = opts[0]
            bad_version = opt.edns_ver != 0
            reply.add_ar(
                EDNS0(
                    udp_len=max(512, min(1232, opt.rclass)), ext_rcode=int(bad_version)
                )
            )
            if bad_version:
                # BADVERS = 16：低四位为零，高位放在 OPT 的扩展 RCODE。
                self.state.note_dns("error")
                return reply
        if request.header.opcode != 0:
            reply.header.rcode = RCODE.NOTIMP
            self.state.note_dns("error")
            return reply
        if len(request.questions) != 1:
            reply.header.rcode = RCODE.FORMERR
            self.state.note_dns("error")
            return reply
        question = request.questions[0]
        qname = str(question.qname).lower()
        qtype = QTYPE.get(question.qtype, "UNKNOWN")
        zone = self.state.settings.zone.lower()
        service = self.state.settings.service_name.lower()
        apex = zone
        if question.qclass != 1 or not (
            qname == apex or qname.endswith("." + zone.rstrip(".") + ".")
        ):
            reply.header.aa = 0
            reply.header.rcode = RCODE.REFUSED
            self.state.note_dns("error")
            return reply
        if qname == apex:
            self._answer_apex(reply, qtype)
            self.state.note_dns("answer")
            return reply
        if qname == self.state.settings.ns_name.lower():
            if qtype in {"A", "ANY"}:
                reply.add_answer(
                    RR(
                        rname=question.qname,
                        rtype=QTYPE.A,
                        rclass=1,
                        ttl=self.state.settings.ttl,
                        rdata=A(self.state.settings.ns_ipv4),
                    )
                )
            else:
                self._add_soa(reply)
            self.state.note_dns("answer")
            return reply
        if qname == service:
            if qtype in {"A", "ANY"}:
                node = select_node(self.state)
                if node is None:
                    reply.header.rcode = RCODE.SERVFAIL
                    self.state.note_dns("error")
                else:
                    reply.add_answer(
                        RR(
                            rname=question.qname,
                            rtype=QTYPE.A,
                            rclass=1,
                            ttl=self.state.settings.ttl,
                            rdata=A(node.server),
                        )
                    )
                    self.state.note_dns("answer", node.id)
                return reply
            if qtype == "AAAA":
                self._add_soa(reply)
                self.state.note_dns("answer")
                return reply
            self._add_soa(reply)
            self.state.note_dns("answer")
            return reply
        # Names that are configured or are parents of configured names exist as empty non-terminals.
        known_names = [self.state.settings.ns_name.lower(), service]
        if qname in known_names or any(
            name.endswith("." + qname) for name in known_names
        ):
            self._add_soa(reply)
            self.state.note_dns("answer")
            return reply
        reply.header.rcode = RCODE.NXDOMAIN
        self._add_soa(reply)
        self.state.note_dns("answer")
        return reply

    def _answer_apex(self, reply: DNSRecord, qtype: str) -> None:
        if qtype in {"SOA", "ANY"}:
            self._add_soa_answer(reply)
        if qtype in {"NS", "ANY"}:
            reply.add_answer(
                RR(
                    rname=reply.q.qname,
                    rtype=QTYPE.NS,
                    rclass=1,
                    ttl=self.state.settings.ttl,
                    rdata=NS(self.state.settings.ns_name),
                )
            )
        if qtype not in {"SOA", "NS", "ANY"}:
            self._add_soa(reply)

    def _add_soa(self, reply: DNSRecord) -> None:
        settings = self.state.settings
        reply.add_auth(
            RR(
                rname=DNSLabel(settings.zone),
                rtype=QTYPE.SOA,
                rclass=1,
                ttl=settings.negative_ttl,
                rdata=SOA(
                    mname=DNSLabel(settings.soa_mname),
                    rname=DNSLabel(settings.soa_rname),
                    times=(
                        max(1, self.state.version) & 0xFFFFFFFF,
                        settings.soa_refresh,
                        settings.soa_retry,
                        settings.soa_expire,
                        settings.negative_ttl,
                    ),
                ),
            )
        )

    def _add_soa_answer(self, reply: DNSRecord) -> None:
        settings = self.state.settings
        reply.add_answer(
            RR(
                rname=DNSLabel(settings.zone),
                rtype=QTYPE.SOA,
                rclass=1,
                ttl=settings.ttl,
                rdata=SOA(
                    mname=DNSLabel(settings.soa_mname),
                    rname=DNSLabel(settings.soa_rname),
                    times=(
                        max(1, self.state.version) & 0xFFFFFFFF,
                        settings.soa_refresh,
                        settings.soa_retry,
                        settings.soa_expire,
                        settings.negative_ttl,
                    ),
                ),
            )
        )


class DNSServer:
    def __init__(self, state: RuntimeState):
        self.state = state
        self.resolver = AuthorityResolver(state)
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.udp_protocol: asyncio.DatagramProtocol | None = None
        self.tcp_server: asyncio.AbstractServer | None = None
        self._tcp_tasks: set[asyncio.Task] = set()
        self._tcp_writers: set[asyncio.StreamWriter] = set()
        self._tcp_per_ip: dict[str, int] = {}
        self.bind_host: str | None = None
        self.bind_port: int | None = None
        self.tcp_lifetime_seconds = 30.0
        self.tcp_per_ip_limit = TCP_PER_IP_LIMIT

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        host = (
            self.bind_host
            if self.bind_host is not None
            else self.state.settings.dns_host
        )
        port = (
            self.bind_port
            if self.bind_port is not None
            else self.state.settings.dns_port
        )
        self.udp_transport, self.udp_protocol = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self.resolver), local_addr=(host, port)
        )
        try:
            self.tcp_server = await asyncio.start_server(self._handle_tcp, host, port)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.udp_transport:
            self.udp_transport.close()
            self.udp_transport = None
        if self.tcp_server:
            self.tcp_server.close()
        for writer in list(self._tcp_writers):
            writer.close()
        tasks = list(self._tcp_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tcp_server:
            await self.tcp_server.wait_closed()
            self.tcp_server = None

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        peer = writer.get_extra_info("peername")
        ip = str(peer[0]) if peer else "unknown"
        if (
            len(self._tcp_tasks) >= 128
            or self._tcp_per_ip.get(ip, 0) >= self.tcp_per_ip_limit
        ):
            writer.close()
            return
        self._tcp_tasks.add(task)
        self._tcp_writers.add(writer)
        self._tcp_per_ip[ip] = self._tcp_per_ip.get(ip, 0) + 1
        try:
            await asyncio.wait_for(
                self._serve_tcp(reader, writer), timeout=self.tcp_lifetime_seconds
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._tcp_tasks.discard(task)
            self._tcp_writers.discard(writer)
            self._tcp_per_ip[ip] -= 1
            if not self._tcp_per_ip[ip]:
                del self._tcp_per_ip[ip]
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except Exception:
                pass

    async def _serve_tcp(self, reader, writer):
        for _ in range(100):
            length = await asyncio.wait_for(reader.readexactly(2), timeout=3)
            size = struct.unpack("!H", length)[0]
            if size <= 0 or size > 4096:
                return
            payload = await asyncio.wait_for(reader.readexactly(size), timeout=3)
            response = self._resolve_bytes(payload, tcp=True)
            if not response:
                return
            writer.write(struct.pack("!H", len(response)) + response)
            await asyncio.wait_for(writer.drain(), timeout=3)

    def _resolve_bytes(self, payload: bytes, tcp: bool = False) -> bytes:
        try:
            request = DNSRecord.parse(payload)
            if request.header.qr:
                return b""
            if len(payload) > 4096:
                return DNSRecord(
                    DNSHeader(
                        id=request.header.id, qr=1, ra=0, aa=1, rcode=RCODE.FORMERR
                    )
                ).pack()
            return self.resolver.resolve(request).pack()
        except Exception:
            header = DNSHeader(
                id=struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0,
                qr=1,
                aa=1,
                ra=0,
                rcode=RCODE.FORMERR,
            )
            return DNSRecord(header=header).pack()


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, resolver: AuthorityResolver):
        self.resolver = resolver
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) > 4096:
            return
        try:
            request = DNSRecord.parse(data)
            if request.header.qr:
                return
            response = self.resolver.resolve(request).pack()
        except Exception:
            response = _bad_response(data)
        limit = 512
        try:
            for rr in request.ar:
                if rr.rtype == QTYPE.OPT:
                    limit = max(512, min(1232, rr.rclass))
        except Exception:
            pass
        if self.transport and len(response) <= limit:
            self.transport.sendto(response, addr)
        elif self.transport:
            # Responses in this service are intentionally small; reject oversized packets rather than send malformed truncation.
            try:
                request = DNSRecord.parse(data)
                full_reply = DNSRecord.parse(response)
                truncated = _new_reply(request)
                truncated.header.rcode = full_reply.header.rcode
                truncated.header.aa = full_reply.header.aa
                truncated.ar = [rr for rr in full_reply.ar if rr.rtype == QTYPE.OPT]
                truncated.header.tc = 1
                self.transport.sendto(truncated.pack(), addr)
            except Exception:
                pass


def _bad_response(data: bytes) -> bytes:
    header = DNSHeader(
        id=struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 0,
        qr=1,
        aa=1,
        ra=0,
        rcode=RCODE.FORMERR,
    )
    return DNSRecord(header=header).pack()


def _new_reply(request: DNSRecord) -> DNSRecord:
    return DNSRecord(
        header=DNSHeader(
            id=request.header.id,
            qr=1,
            aa=1,
            ra=0,
            opcode=request.header.opcode,
            rd=request.header.rd,
            cd=request.header.cd,
        ),
        questions=list(request.questions),
    )
