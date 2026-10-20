"""Command-line entry points for the local demo and separate deployment services."""

import argparse
import asyncio
import os
from contextlib import nullcontext

import uvicorn

from gateway.app import create_app
from gateway.config import load_config
from gateway.mock_provider import create_mock_app
from gateway.runtime import run_async


async def demo(host, port):
    os.environ.setdefault("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    os.environ.setdefault("DEMO_TEAM_KEY", "demo-team-key-change-me")
    os.environ.setdefault("ANALYST_TEAM_KEY", "demo-analyst-key-change-me")
    os.environ.setdefault("GATEWAY_STATE_BACKEND", "memory")
    mock = uvicorn.Server(
        uvicorn.Config(create_mock_app(), host=host, port=9001, log_level="warning")
    )
    mock.capture_signals = nullcontext
    mock_task = asyncio.create_task(mock.serve())
    gateway = uvicorn.Server(
        uvicorn.Config(create_app("config/demo.yaml"), host=host, port=port, access_log=False)
    )
    print(f"Local demo: http://{host}:{port}/docs ; mock provider: http://{host}:9001")
    try:
        await gateway.serve()
    finally:
        mock.should_exit = True
        await mock_task


def main():
    parser = argparse.ArgumentParser(description="Provider-neutral LLM gateway")
    parser.add_argument("command", choices=["demo", "serve", "mock", "check-config"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    if args.command == "demo":
        if args.host != "127.0.0.1":
            parser.error("demo credentials are public; demo must bind to 127.0.0.1")
        run_async(demo(args.host, args.port or 8000))
    elif args.command == "check-config":
        cfg = load_config(args.config or "config/demo.yaml")
        print(
            f"Valid configuration: {len(cfg.providers)} providers, "
            f"{len(cfg.routes)} routes, {len(cfg.teams)} teams"
        )
    else:
        app = create_mock_app() if args.command == "mock" else create_app(args.config)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port or (9001 if args.command == "mock" else 8000),
            loop="gateway.runtime:loop_factory",
        )


if __name__ == "__main__":
    main()
