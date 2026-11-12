from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from smart_dns.api import Context, create_app
from smart_dns.dns import DNSServer
from smart_dns.models import Node, Settings, validate_nodes
from smart_dns.probe_source import ProbePoller
from smart_dns.prober import EndToEndProber, MihomoSupervisor
from smart_dns.state import RuntimeState
from smart_dns.storage import Store
from smart_dns.setup import detect_mihomo, mihomo_config_directory
from smart_dns.tasks import supervised_loop

ROOT = Path(__file__).resolve().parent


def load_runtime(
    data_dir: str, init_config: str | None = None
) -> tuple[Context, DNSServer]:
    store = Store(data_dir)
    existing = store.get_config()
    if existing:
        version, payload = existing
        try:
            settings = Settings.model_validate(payload.get("settings") or {})
            nodes = [Node.model_validate(raw) for raw in payload.get("nodes", [])]
            validate_nodes(nodes)
        except Exception as exc:
            store.close()
            raise RuntimeError("配置数据库校验失败，请检查配置或恢复备份") from None
    else:
        version = 0
        settings = Settings()
        nodes = []
        if init_config:
            try:
                payload = json.loads(Path(init_config).read_text(encoding="utf-8-sig"))
                settings = Settings.model_validate(payload.get("settings") or {})
                nodes = [Node.model_validate(raw) for raw in payload.get("nodes", [])]
                validate_nodes(nodes)
            except Exception:
                store.close()
                raise RuntimeError(
                    "首次导入配置校验失败，请检查 JSON、域名和节点参数"
                ) from None
        version = store.save_config(
            {
                "settings": settings.model_dump(),
                "nodes": [n.model_dump() for n in nodes],
            }
        )
        store.append_event("首次启动：请在面板填写区域、节点、探针和 Mihomo 配置")
    if not settings.mihomo_path:
        detected = detect_mihomo(data_dir)
        if detected["available"]:
            settings.mihomo_path = detected["path"]
            version = store.save_config(
                {
                    "settings": settings.model_dump(),
                    "nodes": [n.model_dump() for n in nodes],
                },
                version,
            )
    state = RuntimeState(settings, nodes, store, version)
    supervisor = MihomoSupervisor(
        settings.mihomo_path,
        mihomo_config_directory(data_dir, settings.mihomo_config_dir),
    )
    prober = EndToEndProber(state, supervisor)
    poller = ProbePoller(
        settings.probe_source,
        lambda records, synced_at: state.apply_traffic_metrics(
            records, synced_at, time.time()
        ),
        lambda message: _async_event(state, message),
    )
    ctx = Context(state, store, supervisor, prober, poller)
    dns_server = DNSServer(state)
    ctx.dns_server = dns_server
    return ctx, dns_server


async def _async_event(state: RuntimeState, message: str) -> None:
    state.traffic_sync_failed(message)
    state.add_event(f"探针同步失败：{message}", "warning", noise=True)


def make_lifespan(ctx: Context, dns_server: DNSServer):
    async def state_timer():
        async def refresh():
            ctx.state.refresh_time_dependent_state()

        await supervised_loop(
            "状态刷新",
            refresh,
            asyncio.Event(),
            lambda: 1,
            lambda exc: ctx.state.add_event("状态刷新异常，将自动重试", "error"),
        )

    @asynccontextmanager
    async def lifespan(app):
        timer = None
        try:
            await dns_server.start()
            ctx.dns_started = True
            ctx.state.dns_running = True
            ctx.state.add_event("权威 DNS 已启动")
            ctx.prober.start()
            ctx.poller.start()
            timer = asyncio.create_task(state_timer(), name="state-timer")
            yield
        finally:
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            # 每个清理阶段独立，单项失败仍继续回收其余资源。
            try:
                for stop in (
                    ctx.poller.stop,
                    ctx.prober.stop,
                    ctx.supervisor.stop,
                    dns_server.stop,
                ):
                    try:
                        await stop()
                    except Exception as exc:
                        logging.error("退出清理失败：%s", type(exc).__name__)
            finally:
                ctx.dns_started = False
                ctx.state.dns_running = False
                ctx.store.close()

    return lifespan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smart DNS LB")
    parser.add_argument(
        "--data-dir", default=os.environ.get("SMART_DNS_DATA_DIR", str(ROOT / "data"))
    )
    parser.add_argument(
        "--host", default=None, help="覆盖面板监听地址（只影响本次启动）"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="覆盖面板监听端口（只影响本次启动）"
    )
    parser.add_argument("--check-config", action="store_true", help="只校验配置并退出")
    parser.add_argument("--init-config", help="仅空数据库首次导入配置 JSON")
    parser.add_argument(
        "--dns-host",
        default=os.environ.get("SMART_DNS_BIND_HOST"),
        help="覆盖本次 DNS 监听地址",
    )
    parser.add_argument("--dns-port", type=int, help="覆盖本次 DNS 监听端口")
    tokens = parser.add_mutually_exclusive_group()
    tokens.add_argument(
        "--reset-token",
        action="store_true",
        help="重新生成管理令牌并退出，运行中的服务需重启",
    )
    tokens.add_argument("--show-token", action="store_true", help="打印管理令牌并退出")
    return parser.parse_args()


def apply_runtime_overrides(
    ctx, dns_server, dns_host=None, dns_port=None, api_host=None, api_port=None
):
    overrides = {
        key: value
        for key, value in {
            "dns_host": dns_host,
            "dns_port": dns_port,
            "api_host": api_host,
            "api_port": api_port,
        }.items()
        if value is not None
    }
    validated = Settings.model_validate(
        {**ctx.state.settings.model_dump(), **overrides}
    )
    ctx.bind_overrides = overrides
    dns_server.bind_host = validated.dns_host
    dns_server.bind_port = validated.dns_port


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    if args.reset_token or args.show_token:
        store = Store(args.data_dir)
        try:
            print(
                store.reset_admin_token()
                if args.reset_token
                else store.get_or_create_admin_token()[0]
            )
        finally:
            store.close()
        return
    ctx, dns_server = load_runtime(args.data_dir, args.init_config)
    apply_runtime_overrides(
        ctx, dns_server, args.dns_host, args.dns_port, args.host, args.port
    )
    if args.check_config:
        print(
            json.dumps(
                {
                    "version": ctx.state.version,
                    "settings": ctx.state.public_snapshot()["settings"],
                    "nodes": len(ctx.state.nodes),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        ctx.store.close()
        return
    if ctx.generated_token:
        print(f"首次管理令牌已保存到 {ctx.store.data_dir / 'admin.token'}", flush=True)
    index_path = ROOT / "templates" / "index.html"
    index_html = index_path.read_text(encoding="utf-8")
    app = create_app(ctx, index_html, make_lifespan(ctx, dns_server))
    host = args.host or ctx.state.settings.api_host
    port = args.port or ctx.state.settings.api_port
    uvicorn.run(app, host=host, port=port, workers=1, reload=False, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
