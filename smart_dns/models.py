from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_name(value: str) -> str:
    value = value.strip().rstrip(".").lower()
    if not value:
        raise ValueError("DNS 名称不能为空")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("DNS 名称不是有效的 IDNA 名称") from exc
    labels = value.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("DNS 名称格式无效")
    if len(value) > 253:
        raise ValueError("DNS 名称过长")
    return value + "."


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, allow_inf_nan=False
    )


class ProbeTarget(StrictModel):
    url: str = "https://www.gstatic.com/generate_204"
    expected_status: int = Field(default=204, ge=100, le=599)
    expected_text: str = ""
    expected_body: str = ""

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("探测目标必须是无凭证的 HTTPS URL")
        if len(value) > 2048:
            raise ValueError("探测 URL 过长")
        return value.strip()


class ProbeSource(StrictModel):
    base_url: str = ""
    bearer_token: str = ""
    poll_interval_seconds: int = Field(default=45, ge=10, le=3600)
    max_age_seconds: int = Field(default=240, ge=30, le=86400)
    request_timeout_seconds: float = Field(default=8.0, gt=0.2, le=60)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("探针地址必须是有效的 HTTP(S) 地址")
        if parsed.query or parsed.fragment:
            raise ValueError("探针基础地址不能包含 query 或 fragment")
        return value


class Settings(StrictModel):
    zone: str = "example.com."
    service_name: str = "node.example.com."
    ns_name: str = "ns1.example.com."
    ns_ipv4: str = "127.0.0.1"
    dns_host: str = "0.0.0.0"
    dns_port: int = Field(default=53, ge=1, le=65535)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    ttl: int = Field(default=30, ge=5, le=86400)
    negative_ttl: int = Field(default=30, ge=1, le=86400)
    soa_mname: str = "ns1.example.com."
    soa_rname: str = "hostmaster.example.com."
    soa_refresh: int = Field(default=3600, ge=60, le=86400)
    soa_retry: int = Field(default=600, ge=30, le=86400)
    soa_expire: int = Field(default=1209600, ge=3600, le=2419200)
    mode: Literal[
        "WEIGHTED",
        "LOWEST_RTT",
        "FAILOVER",
        "SMART_PEAK",
        "TRAFFIC_AWARE",
        "TRAFFIC_REMAINING",
        "SMART_PEAK_REMAINING",
    ] = "TRAFFIC_AWARE"
    all_down_policy: Literal["SERVFAIL", "LAST_KNOWN"] = "SERVFAIL"
    last_known_grace_seconds: int = Field(default=60, ge=0, le=3600)
    probe_interval_seconds: int = Field(default=5, ge=2, le=300)
    probe_timeout_seconds: float = Field(default=4.0, gt=0.2, le=60)
    fail_threshold: int = Field(default=3, ge=1, le=20)
    recover_threshold: int = Field(default=3, ge=1, le=20)
    recovery_steady_seconds: int = Field(default=30, ge=0, le=3600)
    stale_after_seconds: int = Field(default=19, ge=5, le=3600)
    max_parallel_probes: int = Field(default=4, ge=1, le=64)
    max_rtt_ms: float = Field(default=1500, gt=1, le=60000)
    recovery_rtt_percent: float = Field(default=80.0, ge=50.0, le=100.0)
    traffic_stop_at_percent: float = Field(default=99.0, gt=50, le=100)
    lowest_rtt_hold_seconds: int = Field(default=30, ge=0, le=3600)
    lowest_rtt_improvement_percent: float = Field(default=15, ge=0, le=100)
    peak_start: str = "19:30"
    peak_end: str = "00:00"
    timezone: str = "Asia/Shanghai"
    expected_exit_ip: str = ""
    exit_check_url: str = "https://api.ipify.org"
    mihomo_path: str = ""
    mihomo_config_dir: str = ""
    probe_targets: list[ProbeTarget] = Field(
        default_factory=lambda: [
            ProbeTarget(
                url="https://www.gstatic.com/generate_204", expected_status=204
            ),
            ProbeTarget(
                url="https://cp.cloudflare.com/generate_204", expected_status=204
            ),
        ]
    )
    probe_source: ProbeSource = Field(default_factory=ProbeSource)

    @field_validator("zone", "service_name", "ns_name", "soa_mname", "soa_rname")
    @classmethod
    def valid_dns_names(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("ns_ipv4")
    @classmethod
    def valid_ns_ipv4(cls, value: str) -> str:
        ip = ipaddress.ip_address(value.strip())
        if ip.version != 4:
            raise ValueError("第一版 NS 地址请填写 IPv4")
        return str(ip)

    @field_validator("dns_host", "api_host")
    @classmethod
    def valid_bind_host(cls, value: str) -> str:
        value = value.strip()
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("监听地址必须是本机 IP 地址") from exc
        return value

    @field_validator("expected_exit_ip")
    @classmethod
    def valid_exit_ip(cls, value: str) -> str:
        value = value.strip()
        return str(ipaddress.ip_address(value)) if value else ""

    @field_validator("exit_check_url")
    @classmethod
    def valid_exit_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("出口 IP 检查地址必须是无凭证的 HTTPS URL")
        return value.strip()

    @field_validator("peak_start", "peak_end")
    @classmethod
    def valid_time(cls, value: str, info) -> str:
        if value == "24:00" and info.field_name == "peak_end":
            return "00:00"
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间格式应为 HH:MM")
        return value

    @model_validator(mode="after")
    def validate_zone(self) -> "Settings":
        zone = self.zone.lower()
        if self.service_name == zone or not self.service_name.endswith(
            "." + zone.rstrip(".") + "."
        ):
            raise ValueError("服务域名必须是区域内的非 apex 名称")
        if self.ns_name == self.service_name:
            raise ValueError("服务域名不能与权威 NS 主机名相同")
        if not self.ns_name.endswith("." + zone.rstrip(".") + "."):
            raise ValueError("权威 NS 主机名必须位于当前区域内")
        if self.soa_mname == self.service_name:
            raise ValueError("SOA 主机名不能与服务域名相同")
        if not 1 <= len(self.probe_targets) <= 8:
            raise ValueError("探测目标数量应为 1 到 8 个")
        if (
            self.stale_after_seconds
            <= self.probe_timeout_seconds + self.probe_interval_seconds
        ):
            raise ValueError("探测过期时间应大于单轮超时与探测间隔之和")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("时区无效") from exc
        return self


class Node(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=80)
    server: str
    port: int = Field(ge=1, le=65535)
    transport: Literal["tcp", "ws", "grpc"] = "tcp"
    flow: Literal["", "xtls-rprx-vision"] = ""
    uuid: str = ""
    encryption: str = Field(default="none", min_length=1, max_length=2048)
    alpn: list[str] = Field(default_factory=list, max_length=8)
    tls_sni: str = ""
    skip_cert_verify: bool = False
    client_fingerprint: str = "chrome"
    ws_path: str = "/"
    ws_host: str = ""
    grpc_service_name: str = ""
    category: Literal["NORMAL", "PREMIUM"] = "NORMAL"
    enabled: bool = True
    maintenance: bool = False
    base_weight: int = Field(default=100, ge=0, le=10000)
    priority: int = Field(default=1, ge=1, le=10000)
    max_rtt_ms: float | None = Field(default=None, gt=1, le=60000)
    capacity_mbps: float | None = Field(default=None, gt=0, le=1000000)
    probe_server_id: str = ""
    probe_server_name: str = ""
    traffic_counter: Literal["auto", "total", "upload", "download", "max"] = "auto"
    traffic_limit_gib_override: float | None = Field(default=None, ge=0, le=100000000)
    reset_day_override: int | None = Field(default=None, ge=0, le=31)

    @field_validator("server")
    @classmethod
    def valid_ipv4(cls, value: str) -> str:
        ip = ipaddress.ip_address(value.strip())
        if ip.version != 4:
            raise ValueError("线路入口必须填写 IPv4")
        return str(ip)

    @field_validator("uuid")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        from uuid import UUID

        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("VLESS UUID 格式无效") from exc

    @field_validator("encryption")
    @classmethod
    def valid_encryption(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or not value.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
        ):
            raise ValueError("VLESS Encryption 应为 none 或有效的加密参数串")
        return value

    @field_validator("alpn")
    @classmethod
    def valid_alpn(cls, values: list[str]) -> list[str]:
        result = [value.strip() for value in values]
        if any(
            not value
            or len(value) > 255
            or not value.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
            for value in result
        ):
            raise ValueError("ALPN 应为有效的协议名称列表")
        return result

    @field_validator(
        "tls_sni",
        "ws_host",
        "grpc_service_name",
        "probe_server_id",
        "probe_server_name",
    )
    @classmethod
    def trim_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("ws_path")
    @classmethod
    def valid_ws_path(cls, value: str) -> str:
        value = value.strip() or "/"
        if not value.startswith("/") or "\r" in value or "\n" in value:
            raise ValueError("WebSocket path 必须以 / 开头")
        return value

    @model_validator(mode="after")
    def validate_flow(self):
        if self.flow and self.transport != "tcp":
            raise ValueError("Vision flow 仅适用于 TCP 传输")
        return self


def validate_nodes(nodes: list[Node]) -> None:
    if len(nodes) > 64:
        raise ValueError("最多配置 64 台线路机")
    if len({node.id for node in nodes}) != len(nodes):
        raise ValueError("线路 ID 重复")
