"""将分享链接转换为节点字段；域名入口由异步入口解析。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from .models import Node, normalize_name

SUPPORTED_PARAMETERS = {
    "type",
    "security",
    "sni",
    "path",
    "host",
    "serviceName",
    "alpn",
    "encryption",
    "flow",
    "fp",
    "allowInsecure",
    "insecure",
    "headerType",
}
IGNORED_PARAMETERS = {"spx", "remark", "remarks", "name"}


def _link_parts(link: str):
    try:
        parsed = urlsplit(link.strip())
        if parsed.scheme != "vless" or not parsed.username or not parsed.hostname:
            raise ValueError("链接缺少 UUID 或入口地址")
        if parsed.password is not None or parsed.path not in ("", "/"):
            raise ValueError("VLESS 链接含多余密码或地址路径")
        if not unquote(parsed.username).strip():
            raise ValueError("链接缺少 UUID")
        if not parsed.port:
            raise ValueError("链接缺少有效端口（1–65535）")
    except ValueError as exc:
        if str(exc).startswith(("Port ", "Invalid ")):
            raise ValueError("链接地址或端口格式错误") from None
        raise

    params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("链接含重复参数，请保留每项的单一值")
    params = {key: values[0] for key, values in params.items()}
    if params.get("security", "tls") != "tls":
        raise ValueError(
            "当前支持 TLS；Reality 等安全协议尚未支持，请使用 security=tls"
        )
    if any(key in params for key in ("pbk", "sid", "password")):
        raise ValueError("链接含 Reality 公钥/短 ID 或额外认证参数，当前探测器尚未支持")
    if params.get("type", "tcp") not in {"tcp", "ws", "grpc"}:
        raise ValueError(
            "当前支持 TCP、WebSocket 和 gRPC；xhttp/httpupgrade/kcp 等传输尚未支持"
        )
    if params.get("headerType", "none") not in {"", "none"}:
        raise ValueError("当前尚未支持 headerType HTTP 伪装，请使用 headerType=none")
    if params.get("mode", "gun") not in {"", "gun"}:
        raise ValueError("当前尚未支持该 gRPC 传输模式")
    insecure_values = [
        params[key].lower() for key in ("allowInsecure", "insecure") if key in params
    ]
    if any(value not in {"0", "1", "false", "true"} for value in insecure_values):
        raise ValueError("allowInsecure/insecure 应为 0 或 1")
    booleans = {value in {"1", "true"} for value in insecure_values}
    if len(booleans) > 1:
        raise ValueError("allowInsecure 与 insecure 设置冲突")
    return parsed, params, True in booleans


def parse_vless_link(link: str, resolved_ipv4: str | None = None) -> dict:
    parsed, params, insecure = _link_parts(link)
    domain = ""
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        domain = normalize_name(parsed.hostname).rstrip(".")
        if resolved_ipv4 is None:
            raise ValueError("域名入口需异步解析 IPv4") from None
        address = ipaddress.ip_address(resolved_ipv4)
    if address.version != 4:
        raise ValueError("当前 DNS 调度仅支持 IPv4 入口")
    try:
        node = Node(
            id=str(uuid.uuid4()),
            name=unquote(parsed.fragment) or parsed.hostname,
            uuid=unquote(parsed.username),
            server=str(address),
            port=parsed.port,
            transport=params.get("type", "tcp"),
            tls_sni=params.get("sni") or domain,
            ws_path=params.get("path", "/"),
            ws_host=params.get("host", ""),
            grpc_service_name=params.get("serviceName", ""),
            alpn=params["alpn"].split(",") if params.get("alpn") else [],
            encryption=params.get("encryption", "none"),
            flow=params.get("flow", ""),
            client_fingerprint=params.get("fp", "chrome"),
            skip_cert_verify=insecure,
        )
    except ValidationError as exc:
        raise ValueError(
            "; ".join(error["msg"] for error in exc.errors(include_input=False))
        ) from None
    return node.model_dump()


def _lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > 64 or len(text) > 262144:
        raise ValueError("请粘贴 1–64 条 VLESS 链接，总长度限 256 KiB")
    return lines


def parse_vless_links(text: str) -> list[dict]:
    """供本地调用的同步解析入口，仅处理 IP 入口。"""
    result = []
    for number, line in enumerate(_lines(text), 1):
        try:
            result.append(parse_vless_link(line))
        except ValueError as exc:
            raise ValueError(f"第 {number} 条：{exc}") from None
    return result


async def resolve_ipv4(hostname: str) -> str:
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM
            ),
            timeout=3,
        )
        if not addresses:
            raise ValueError("域名没有 IPv4 记录")
        return str(ipaddress.IPv4Address(addresses[0][4][0]))
    except asyncio.TimeoutError:
        raise ValueError("域名 IPv4 解析超时（3 秒），请重试或手动填写 IPv4") from None
    except (OSError, ValueError):
        raise ValueError("域名 IPv4 解析失败，请检查域名的 A 记录") from None


async def parse_vless_links_async(text: str) -> dict:
    async def parse_one(number, line):
        try:
            parsed, params, _ = _link_parts(line)
            warnings = []
            resolved = None
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                domain = normalize_name(parsed.hostname).rstrip(".")
                resolved = await resolve_ipv4(domain)
                warnings.append(
                    f"{domain}：已将域名解析为 {resolved}；入口 IP 变更后请重新导入"
                )
            node = parse_vless_link(line, resolved)
            unknown = set(params) - SUPPORTED_PARAMETERS - IGNORED_PARAMETERS - {"mode"}
            if unknown:
                warnings.append("已忽略附加参数：" + ", ".join(sorted(unknown)))
            return node, warnings
        except ValueError as exc:
            raise ValueError(f"第 {number} 条：{exc}") from None

    # 每个域名有独立的三秒上限，整批并行解析避免逐条等待。
    items = await asyncio.gather(
        *(parse_one(number, line) for number, line in enumerate(_lines(text), 1))
    )
    return {
        "nodes": [node for node, _ in items],
        "warnings": [warning for _, warnings in items for warning in warnings],
    }
