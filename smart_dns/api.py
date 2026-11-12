from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .links import parse_vless_links_async
from .models import Node, ProbeSource, Settings, validate_nodes
from .probe_source import fetch_probe_servers
from .setup import (
    setup_status,
    detect_public_ipv4,
    derive_dns_settings,
    delegation_diagnostics,
    mihomo_config_directory,
)
from .state import RuntimeState
from .storage import Store, VersionConflict


class Context:
    def __init__(self, state: RuntimeState, store: Store, supervisor, prober, poller):
        self.state = state
        self.store = store
        self.supervisor = supervisor
        self.prober = prober
        self.poller = poller
        self.admin_token, self.generated_token = store.get_or_create_admin_token()
        self.dns_started = False
        self.config_lock = asyncio.Lock()
        self.public_ip_lock = asyncio.Lock()
        self.public_ip_cache = None
        self.configured_binds = {
            key: getattr(state.settings, key)
            for key in ("dns_host", "dns_port", "api_host", "api_port")
        }
        self.bind_overrides = {}

    async def public_ip(self, refresh: bool = False) -> dict:
        async with self.public_ip_lock:
            if (
                not refresh
                and self.public_ip_cache is not None
                and time.monotonic() - self.public_ip_cache[0] < 600
            ):
                return dict(self.public_ip_cache[1])
            result = await detect_public_ipv4()
            self.public_ip_cache = (time.monotonic(), dict(result))
            return result

    async def sync_probe(self) -> None:
        records, synced_at = await fetch_probe_servers(self.state.settings.probe_source)
        self.state.apply_traffic_metrics(records, synced_at, time.time())


def _auth_dependency(ctx: Context):
    async def require_auth(request: Request) -> None:
        value = request.headers.get("authorization", "")
        token = value[7:].strip() if value.lower().startswith("bearer ") else ""
        if (
            not token
            or not token.isascii()
            or not hmac.compare_digest(token, ctx.admin_token)
        ):
            raise HTTPException(status_code=401, detail="需要管理令牌")

    return require_auth


def create_app(ctx: Context, index_html: str, lifespan=None) -> FastAPI:
    app = FastAPI(
        title="Smart DNS LB",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    require_auth = _auth_dependency(ctx)

    @app.middleware("http")
    async def serialize_changes(request, call_next):
        if request.method in {"POST", "PUT", "DELETE"}:
            from fastapi.responses import JSONResponse

            try:
                await require_auth(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

            # 先读取有界请求体，再进入配置锁；慢客户端不会占住保存通道。
            async def bounded_body():
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        return None
                return bytes(body)

            try:
                body = await asyncio.wait_for(bounded_body(), timeout=5)
            except asyncio.TimeoutError:
                return JSONResponse(
                    {"detail": "请求体接收超时，请重试"}, status_code=408
                )
            if body is None:
                return JSONResponse(
                    {"detail": "请求体超过 1 MiB，请减少导入内容"}, status_code=413
                )
            request._body = body
            async with ctx.config_lock:
                match = request.headers.get("if-match")
                if match is not None and match != str(ctx.state.version):
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        {"detail": "配置已更新，请刷新后重新编辑"}, status_code=409
                    )
                return await call_next(request)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": _validation_message(exc)}, status_code=422)

    @app.exception_handler(VersionConflict)
    async def version_conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html)

    @app.get("/assets/{filename:path}")
    async def frontend_asset(filename: str):
        from fastapi.responses import FileResponse

        allowed = {"app.js": "text/javascript", "app.css": "text/css"}
        if filename not in allowed:
            raise HTTPException(status_code=404, detail="静态资源不存在")
        return FileResponse(
            Path(__file__).resolve().parent.parent / "templates" / filename,
            media_type=allowed[filename],
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": ctx.dns_started,
            "dns": ctx.dns_started,
        }

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    async def status() -> dict[str, Any]:
        return ctx.state.public_snapshot()

    @app.get("/api/setup/status", dependencies=[Depends(require_auth)])
    async def setup_status_endpoint(refresh_ip: bool = False) -> dict[str, Any]:
        result = setup_status(ctx.state.settings, ctx.state.nodes, ctx.store.data_dir)
        result["public_ip"] = await ctx.public_ip(refresh_ip)
        result["settings"] = {
            "zone": ctx.state.settings.zone,
            "service_name": ctx.state.settings.service_name,
            "ns_name": ctx.state.settings.ns_name,
            "ns_ipv4": ctx.state.settings.ns_ipv4,
        }
        return result

    @app.post("/api/setup/check-delegation", dependencies=[Depends(require_auth)])
    async def setup_check_delegation(
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = body or {}
        settings = ctx.state.settings
        if body.get("service_name"):
            derived = derive_dns_settings(str(body["service_name"]))
            settings = Settings.model_validate(
                {
                    **settings.model_dump(),
                    **derived,
                    "ns_ipv4": body.get("ns_ipv4", settings.ns_ipv4),
                }
            )
        elif "ns_ipv4" in body:
            settings = Settings.model_validate(
                {**settings.model_dump(), "ns_ipv4": body["ns_ipv4"]}
            )
        return await delegation_diagnostics(settings)

    @app.post("/api/nodes/parse-link", dependencies=[Depends(require_auth)])
    async def parse_node_links(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await parse_vless_links_async(
                str(body.get("links", body.get("text", "")))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/api/config/export", dependencies=[Depends(require_auth)])
    async def export_config(include_secrets: bool = False) -> dict[str, Any]:
        payload = {
            "settings": ctx.state.settings.model_dump(),
            "nodes": [node.model_dump() for node in ctx.state.nodes],
            "version": ctx.state.version,
            "redacted": not include_secrets,
        }
        if not include_secrets:
            payload["settings"]["probe_source"]["bearer_token"] = ""
            payload["settings"]["probe_source"]["has_bearer_token"] = bool(
                ctx.state.settings.probe_source.bearer_token
            )
            for node in payload["nodes"]:
                node["has_uuid"] = bool(node["uuid"])
                node["uuid"] = ""
                node["has_encryption"] = node["encryption"] != "none"
                if node["has_encryption"]:
                    node["encryption"] = ""
        return payload

    @app.post("/api/config/import", dependencies=[Depends(require_auth)])
    async def import_config(body: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(body.get("settings"), dict) or not isinstance(
                body.get("nodes"), list
            ):
                raise ValueError("备份必须包含 settings 对象和 nodes 数组")
            raw_settings = dict(body["settings"])
            source = dict(raw_settings.get("probe_source") or {})
            redacted = body.get("redacted") is True
            has_token = source.pop("has_bearer_token", False)
            if redacted and has_token and not source.get("bearer_token"):
                source["bearer_token"] = ctx.state.settings.probe_source.bearer_token
                if not source["bearer_token"]:
                    raise ValueError("备份已脱敏，请补填探针令牌或导入含密钥的备份")
            raw_settings["probe_source"] = source
            settings = Settings.model_validate(raw_settings)
            incoming_nodes = []
            current_nodes = {node.id: node for node in ctx.state.nodes}
            for raw in body["nodes"]:
                raw = dict(raw)
                previous = current_nodes.get(raw.get("id"))
                has_uuid = raw.pop("has_uuid", False)
                has_encryption = raw.pop("has_encryption", False)
                if redacted and has_encryption and not raw.get("encryption"):
                    if previous and previous.encryption != "none":
                        raw["encryption"] = previous.encryption
                    else:
                        raise ValueError(
                            "备份已脱敏，请补填 VLESS Encryption 或导入含密钥的备份"
                        )
                if redacted and has_uuid and not raw.get("uuid"):
                    if previous and previous.uuid:
                        raw["uuid"] = previous.uuid
                    else:
                        raise ValueError(
                            "备份已脱敏，请补填节点 UUID 或导入含密钥的备份"
                        )
                incoming_nodes.append(Node.model_validate(raw))
            nodes = incoming_nodes
            validate_nodes(nodes)
            # 备份 version 是来源实例的版本；并发控制使用 If-Match 或 expected_version。
            return await _replace_config(
                ctx, settings, nodes, body.get("expected_version", ctx.state.version)
            )
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail=_validation_message(exc)
            ) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="导入失败，请检查本地文件权限和 Mihomo 配置"
            ) from None

    @app.get("/api/events", dependencies=[Depends(require_auth)])
    async def events() -> dict[str, Any]:
        return {"events": ctx.store.get_events(100)}

    @app.put("/api/config", dependencies=[Depends(require_auth)])
    async def update_config(body: dict[str, Any]) -> dict[str, Any]:
        try:
            allowed_settings = set(Settings.model_fields)
            new_settings_data = {
                key: value
                for key, value in (body.get("settings") or {}).items()
                if key in allowed_settings
            }
            service_domain = (body.get("settings") or {}).get("service_domain")
            if service_domain:
                new_settings_data.update(derive_dns_settings(str(service_domain)))
                if "ns_ipv4" not in new_settings_data:
                    public_ip = await ctx.public_ip()
                    if public_ip["detected"]:
                        new_settings_data["ns_ipv4"] = public_ip["ip"]
            old_settings = ctx.state.settings.model_dump()
            old_source = old_settings.get("probe_source", {})
            incoming_source = {
                key: value
                for key, value in (new_settings_data.get("probe_source") or {}).items()
                if key in ProbeSource.model_fields
            }
            if "bearer_token" not in incoming_source or not incoming_source.get(
                "bearer_token"
            ):
                incoming_source["bearer_token"] = old_source.get("bearer_token", "")
            if body.get("clear_probe_token"):
                incoming_source["bearer_token"] = ""
            new_settings_data["probe_source"] = {**old_source, **incoming_source}
            settings = Settings.model_validate({**old_settings, **new_settings_data})
            incoming_nodes = body.get("nodes")
            if incoming_nodes is None:
                nodes = ctx.state.nodes
            else:
                nodes = []
                for raw in incoming_nodes:
                    raw = {
                        key: value
                        for key, value in dict(raw).items()
                        if key in Node.model_fields
                    }
                    current = next(
                        (node for node in ctx.state.nodes if node.id == raw.get("id")),
                        None,
                    )
                    if not raw.get("uuid") and current:
                        raw["uuid"] = current.uuid
                    if not raw.get("encryption"):
                        raw["encryption"] = current.encryption if current else "none"
                    nodes.append(Node.model_validate(raw))
            if settings.ns_name == settings.service_name:
                raise ValueError("服务域名不能与 NS 主机名相同")
            validate_nodes(nodes)
            return await _replace_config(ctx, settings, nodes, body.get("version"))
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="保存失败，请检查本地文件权限、Mihomo 路径和配置",
            )

    @app.post("/api/nodes", dependencies=[Depends(require_auth)])
    async def add_node(body: dict[str, Any]) -> dict[str, Any]:
        payload = dict(body)
        inherit_from = str(payload.pop("inherit_from", "") or "")
        inherit_connection = bool(payload.pop("inherit_connection", False))
        if inherit_connection and ctx.state.nodes:
            template = next(
                (item for item in ctx.state.nodes if item.id == inherit_from),
                ctx.state.nodes[0],
            )
            connection_fields = {
                "transport",
                "flow",
                "uuid",
                "encryption",
                "alpn",
                "tls_sni",
                "skip_cert_verify",
                "client_fingerprint",
                "ws_path",
                "ws_host",
                "grpc_service_name",
            }
            for key in connection_fields:
                payload[key] = getattr(template, key)
        payload.setdefault("id", str(uuid.uuid4()))
        try:
            node = Node.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc))
        await _save_nodes(ctx, ctx.state.nodes + [node])
        return ctx.state.public_snapshot()

    @app.put("/api/nodes/{node_id}", dependencies=[Depends(require_auth)])
    async def edit_node(node_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = next((node for node in ctx.state.nodes if node.id == node_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="线路不存在")
        payload = {**current.model_dump(), **body, "id": node_id}
        if not body.get("uuid"):
            payload["uuid"] = current.uuid
        if payload.pop("clear_uuid", False):
            payload["uuid"] = ""
        payload.pop("has_encryption", None)
        if not payload.get("encryption"):
            payload["encryption"] = current.encryption
        try:
            node = Node.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc))
        await _save_nodes(
            ctx, [node if item.id == node_id else item for item in ctx.state.nodes]
        )
        return ctx.state.public_snapshot()

    @app.delete("/api/nodes/{node_id}", dependencies=[Depends(require_auth)])
    async def delete_node(
        node_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not any(node.id == node_id for node in ctx.state.nodes):
            raise HTTPException(status_code=404, detail="线路不存在")
        await _save_nodes(ctx, [node for node in ctx.state.nodes if node.id != node_id])
        return ctx.state.public_snapshot()

    @app.post("/api/nodes/{node_id}/probe", dependencies=[Depends(require_auth)])
    async def probe_node(node_id: str) -> dict[str, Any]:
        node = next((node for node in ctx.state.nodes if node.id == node_id), None)
        if node is None:
            raise HTTPException(status_code=404, detail="线路不存在")
        version = ctx.state.version
        success, rtt, error, verified = await ctx.prober.probe_once(node)
        ctx.state.apply_probe_result(
            node.id, success, rtt, error, verified, config_version=version
        )
        return {
            "success": success,
            "rtt_ms": rtt,
            "error": error,
            "exit_verified": verified,
            "node": ctx.state.public_node(node),
        }

    @app.post("/api/probe-source/sync", dependencies=[Depends(require_auth)])
    async def sync_probe() -> dict[str, Any]:
        try:
            await ctx.sync_probe()
            return ctx.state.public_snapshot()
        except Exception as exc:
            ctx.state.traffic_sync_failed("探针同步失败，请检查地址、只读令牌和网络")
            raise HTTPException(
                status_code=502, detail="探针同步失败，请检查地址、只读令牌和网络"
            )

    @app.post("/api/probe-source/test", dependencies=[Depends(require_auth)])
    async def test_probe_source() -> dict[str, Any]:
        try:
            records, synced_at = await fetch_probe_servers(
                ctx.state.settings.probe_source
            )
            servers = [
                {"id": str(item.get("id", "")), "name": str(item.get("name", ""))}
                for key, item in records.items()
                if key.startswith("id:")
            ]
            return {
                "ok": True,
                "count": len(servers),
                "servers": servers,
                "synced_at": synced_at,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="探针连接失败，请检查地址、只读令牌和网络"
            )

    return app


async def _replace_config(
    ctx: Context, settings: Settings, nodes: list[Node], expected_version
) -> dict:
    from pathlib import Path
    from .prober import MihomoSupervisor

    candidate = ctx.supervisor
    config_dir = mihomo_config_directory(ctx.store.data_dir, settings.mihomo_config_dir)
    if (
        settings.mihomo_path != candidate.binary
        or Path(config_dir).resolve() != candidate.config_dir
    ):
        candidate = MihomoSupervisor(settings.mihomo_path, config_dir)
    await candidate.validate_nodes(nodes)
    await ctx.prober.stop()
    try:
        await ctx.poller.stop()
        if candidate is not ctx.supervisor:
            await ctx.supervisor.stop()
        version = ctx.store.save_config(
            {
                "settings": settings.model_dump(),
                "nodes": [node.model_dump() for node in nodes],
            },
            expected_version,
        )
    except Exception:
        ctx.prober.start()
        ctx.poller.start()
        raise
    ctx.state.replace_config(settings, nodes, version)
    ctx.supervisor = candidate
    ctx.prober.supervisor = candidate
    ctx.poller.source = settings.probe_source
    ctx.prober.start()
    ctx.poller.start()
    ctx.state.add_event("配置已保存并重新加载")
    snapshot = ctx.state.public_snapshot()
    binds = ("dns_host", "dns_port", "api_host", "api_port")
    configured_binds = getattr(ctx, "configured_binds", {})
    runtime_overrides = getattr(ctx, "bind_overrides", {})
    snapshot["restart_required"] = any(
        key not in runtime_overrides
        and configured_binds.get(key, getattr(settings, key)) != getattr(settings, key)
        for key in binds
    )
    return snapshot


async def _save_nodes(ctx: Context, nodes: list[Node]) -> None:
    validate_nodes(nodes)
    settings = ctx.state.settings
    await ctx.supervisor.validate_nodes(nodes)
    version = ctx.store.save_config(
        {
            "settings": settings.model_dump(),
            "nodes": [node.model_dump() for node in nodes],
        },
        ctx.state.version,
    )
    ctx.state.replace_config(settings, nodes, version)
    await ctx.prober.stop()
    ctx.prober.start()
    ctx.state.add_event("线路配置已保存")


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            ".".join(map(str, error["loc"])) + ": " + error["msg"]
            for error in exc.errors(include_input=False, include_context=False)
        )[:600]
    return str(exc)[:300]
