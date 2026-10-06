"""Cancellable terminal input; no executor thread remains blocked after a timeout."""

import asyncio
import getpass
import os
import sys

from .models import AnswerInput, Request


def prompt_text(request: Request, *, multiline=True) -> str:
    parts = [f"[{request.kind.upper()} · {request.urgency}] {request.question}"]
    if request.context:
        parts.append(request.context)
    if request.options:
        parts.append("\n".join(f"{i}. {option}" for i, option in enumerate(request.options, 1)))
    if request.kind != "notification":
        parts.append(
            "Reply with an option number or exact label."
            if request.options
            else "Reply with your answer."
        )
        if request.options and multiline:
            parts.append("Optional explanation: add it on subsequent lines.")
    parts.append(f"Request: {request.id}")
    return "".join(c for c in "\n\n".join(parts) if c.isprintable() or c in "\n\t")


def parse_reply(request: Request, text: str, respondent: str) -> AnswerInput:
    text = text.strip()
    if not text:
        raise ValueError("An answer is required")
    if not request.options:
        return AnswerInput(answer=text, respondent=respondent)
    first, _, explanation = text.partition("\n")
    first = first.strip()
    selected = None
    if first.isascii() and first.isdecimal() and len(first) <= 2:
        index = int(first) - 1
        if 0 <= index < len(request.options):
            selected = request.options[index]
    if selected is None and first in request.options:
        selected = first
    if (
        selected is None
        and request.kind == "approval"
        and first.casefold() in ("approve", "reject")
    ):
        selected = "Approve" if first.casefold() == "approve" else "Reject"
    if selected is None:
        raise ValueError(
            "Choose an option number or exact label; approval requires Approve or Reject"
        )
    return AnswerInput(
        answer=explanation.strip() or selected,
        selected_option=selected,
        respondent=respondent,
        approved=(selected == "Approve") if request.kind == "approval" else None,
    )


async def read_line() -> str:
    if os.name == "nt":
        import msvcrt

        characters = []
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.05)
                continue
            character = msvcrt.getwch()
            if character == "\x03":
                raise asyncio.CancelledError
            if character == "\x1a":
                raise EOFError
            if character in ("\r", "\n"):
                print(file=sys.stderr)
                return "".join(characters)
            if character in ("\x00", "\xe0"):
                msvcrt.getwch()
            elif character == "\b":
                if characters:
                    characters.pop()
                    print("\b \b", end="", file=sys.stderr, flush=True)
            else:
                characters.append(character)
                print(character, end="", file=sys.stderr, flush=True)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = sys.stdin.fileno()
    buffer = bytearray()

    def ready():
        try:
            if future.done():
                loop.remove_reader(fd)
                return
            character = os.read(fd, 1)
            if not character:
                loop.remove_reader(fd)
                future.set_exception(EOFError())
            elif character in (b"\r", b"\n"):
                loop.remove_reader(fd)
                future.set_result(buffer.decode(sys.stdin.encoding or "utf-8", errors="replace"))
            else:
                buffer.extend(character)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)

    loop.add_reader(fd, ready)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


class Terminal:
    def __init__(self, interactive: bool):
        self.interactive = interactive
        self.lock = asyncio.Lock()

    @property
    def available(self):
        return self.interactive and sys.stdin is not None and sys.stdin.isatty()

    def notify(self, request: Request):
        if self.interactive:
            print(prompt_text(request, multiline=False), file=sys.stderr, flush=True)

    async def answer(self, request: Request) -> AnswerInput:
        print("\n" + prompt_text(request, multiline=False), file=sys.stderr, flush=True)
        while True:
            print("Your answer: ", end="", file=sys.stderr, flush=True)
            try:
                return parse_reply(request, await read_line(), getpass.getuser())
            except ValueError as exc:
                print(str(exc), file=sys.stderr, flush=True)
