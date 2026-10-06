import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from .client import AskHuman, AskHumanError, HumanCancelled, HumanTimeout
from .models import AnswerInput
from .settings import Settings, data_directory


def main():
    parser = argparse.ArgumentParser(prog="askhuman", description="Give your agent a human.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create local credentials and storage")
    init.add_argument("--data-dir", type=Path, default=data_directory())
    init.add_argument("--base-url", default="http://127.0.0.1:8765")
    serve = commands.add_parser("serve", help="Run the API, human inbox, and delivery worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    token = commands.add_parser("token", help="Print a credential for local setup")
    token.add_argument("--admin", action="store_true")
    ask = commands.add_parser("ask", help="Create a human request and optionally wait")
    ask.add_argument("question")
    ask.add_argument("--kind", default="ask")
    ask.add_argument("--context", default="")
    ask.add_argument("--option", action="append", default=[])
    ask.add_argument("--recipient")
    ask.add_argument("--timeout", type=int, default=86400)
    ask.add_argument("--wait", type=float, default=0)
    ask.add_argument("--idempotency-key")
    get = commands.add_parser("get", help="Get a saved request")
    get.add_argument("request_id")
    wait = commands.add_parser("wait", help="Wait for a saved request")
    wait.add_argument("request_id")
    wait.add_argument("--seconds", type=float, default=25)
    cancel = commands.add_parser("cancel", help="Cancel a pending request")
    cancel.add_argument("request_id")
    commands.add_parser("inbox", help="List requests as the local human operator")
    answer = commands.add_parser("answer", help="Answer a request as the local human operator")
    answer.add_argument("request_id")
    answer.add_argument("--text", default="")
    answer.add_argument("--option")
    choice = answer.add_mutually_exclusive_group()
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--reject", action="store_true")
    answer.add_argument("--name", required=True)
    install = commands.add_parser(
        "install-skill", help="Copy the bundled skill into an agent directory"
    )
    install.add_argument(
        "destination", type=Path, help="Skills parent directory, e.g. .agents/skills"
    )
    commands.add_parser("mcp", help="Run the MCP stdio server")
    args = parser.parse_args()
    try:
        if args.command == "init":
            settings = Settings.initialize(args.data_dir, args.base_url)
            print(
                f"AskHuman initialized in {settings.data_dir.resolve()}\n"
                f"Run: askhuman serve\nHuman inbox: {settings.base_url}\n"
                "Get the inbox key: askhuman token --admin\n"
                "Get the agent key: askhuman token"
            )
        elif args.command == "serve":
            import uvicorn

            from .server import create_app

            uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)
        elif args.command == "token":
            settings = Settings.load()
            print(settings.admin_key if args.admin else settings.api_key)
        elif args.command == "install-skill":
            destination = args.destination / "askhuman"
            shutil.copytree(Path(__file__).parent / "skill", destination)
            print(f"Installed skill: {destination.resolve()}")
        elif args.command == "mcp":
            from .mcp import main as mcp_main

            mcp_main()
        elif args.command in ("inbox", "answer"):
            # This command deliberately requires local operator credentials, not the agent key.
            import httpx

            settings = Settings.load()
            with httpx.Client(
                base_url=settings.base_url,
                timeout=30,
                trust_env=False,
                headers={"Authorization": f"Bearer {settings.admin_key}"},
            ) as client:
                if args.command == "inbox":
                    result = client.get("/api/admin/requests")
                else:
                    body = AnswerInput(
                        answer=args.text,
                        selected_option=args.option,
                        respondent=args.name,
                        approved=True if args.approve else False if args.reject else None,
                    )
                    result = client.post(
                        f"/api/admin/requests/{args.request_id}/answer", json=body.model_dump()
                    )
                result.raise_for_status()
                print(json.dumps(result.json(), indent=2))
        else:
            asyncio.run(agent_command(args))
    except (ValueError, OSError, AskHumanError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


async def agent_command(args):
    async with AskHuman() as human:
        if args.command == "ask":
            result = await human.create(
                args.question,
                kind=args.kind,
                context=args.context,
                options=args.option,
                recipient=args.recipient,
                timeout_seconds=args.timeout,
                idempotency_key=args.idempotency_key,
            )
            if args.wait and result.status == "pending":
                try:
                    await human.wait(result.id, wait_timeout=args.wait)
                except (HumanTimeout, HumanCancelled):
                    pass
                result = await human.get(result.id)
        elif args.command == "wait":
            try:
                await human.wait(args.request_id, wait_timeout=args.seconds)
            except (HumanTimeout, HumanCancelled):
                pass
            result = await human.get(args.request_id)
        elif args.command == "cancel":
            result = await human.cancel(args.request_id)
        else:
            result = await human.get(args.request_id)
        print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
