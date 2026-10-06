import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .client import AskHuman, AskHumanError, HumanCancelled, HumanTimeout, selected_mode
from .config import Channel, RoutingConfig
from .embedded import config_path
from .models import AnswerInput
from .settings import Settings, data_directory
from .store import Conflict, Store


def main():
    parser = argparse.ArgumentParser(prog="askhuman", description="Give your agent a human.")
    parser.add_argument("--mode", choices=["embedded", "remote"], help="Default: embedded")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Configure direct embedded channels")
    configure.add_argument("--channel", choices=["terminal", "telegram", "slack"])
    configure.add_argument(
        "--from-file", type=Path, help="Import and replace the full configuration"
    )
    configure.add_argument("--output", type=Path, default=config_path(data_directory()))
    init = commands.add_parser("init", help="Initialize the optional web server")
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
    ask.add_argument(
        "--wait",
        type=float,
        default=None,
        help="Wait budget in seconds; 0 creates only, default waits for the answer",
    )
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
        if args.command == "configure":
            configure_embedded(args)
        elif args.command == "init":
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
            if selected_mode(args.mode) == "embedded":
                store = Store(
                    data_directory() / "embedded.sqlite3", default_config=RoutingConfig.embedded()
                )
                if args.command == "inbox":
                    print(json.dumps([r.model_dump(mode="json") for r in store.list()], indent=2))
                else:
                    print(
                        store.answer(
                            args.request_id, answer_body(args), "terminal"
                        ).model_dump_json(indent=2)
                    )
                return
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
                    body = answer_body(args)
                    result = client.post(
                        f"/api/admin/requests/{args.request_id}/answer", json=body.model_dump()
                    )
                result.raise_for_status()
                print(json.dumps(result.json(), indent=2))
        else:
            asyncio.run(agent_command(args))
    except (ValueError, OSError, AskHumanError, Conflict, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


async def agent_command(args):
    async with AskHuman(mode=args.mode) as human:
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
            if args.wait != 0 and result.status == "pending":
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


def answer_body(args):
    return AnswerInput(
        answer=args.text,
        selected_option=args.option,
        respondent=args.name,
        approved=True if args.approve else False if args.reject else None,
    )


def configure_embedded(args):
    path = args.output.expanduser()
    if args.from_file:
        config = RoutingConfig.model_validate_json(args.from_file.read_text()).require_embedded()
    else:
        if path.exists():
            raise FileExistsError(
                f"Configuration exists at {path}. Edit it or import with --from-file."
            )
        choice = args.channel
        if choice is None:
            if not sys.stdin.isatty():
                raise ValueError("Choose --channel terminal, telegram, or slack")
            choice = input("Channel [terminal/telegram/slack] (terminal): ").strip() or "terminal"
        kind = {
            "terminal": "terminal",
            "telegram": "telegram_polling",
            "slack": "slack_socket",
        }.get(choice)
        if kind is None:
            raise ValueError("Choose terminal, telegram, or slack")
        values = {}
        if choice != "terminal":
            if not sys.stdin.isatty():
                raise ValueError(
                    "Use an interactive terminal or --from-file for channel credentials"
                )
            print("Use env:VARIABLE_NAME to reference credentials from your environment.")
            values["bot_token"] = getpass.getpass("Bot token (or env reference): ").strip()
            if choice == "slack":
                values["app_token"] = getpass.getpass("App token with connections:write: ").strip()
                values["channel_id"] = input("Slack channel or DM ID: ").strip()
            else:
                values["chat_id"] = input("Telegram chat ID: ").strip()
            values["allowed_user_ids"] = input("Allowed human user IDs, comma-separated: ").strip()
        config = RoutingConfig(
            channels=[Channel(id=choice, type=kind, settings=values)], default_channels=[choice]
        ).require_embedded()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        file.write(config.model_dump_json(indent=2))
        temporary = Path(file.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Saved {path.resolve()}. New AskHuman instances will use these channels.")


if __name__ == "__main__":
    main()
