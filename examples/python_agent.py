"""Run with `python examples/python_agent.py` after starting the service."""

import asyncio

from askhuman import AskHuman


async def main():
    async with AskHuman() as human:
        request = await human.create(
            "Which customer definition should this report use?",
            kind="decision",
            options=["Active subscription", "Any customer with revenue"],
            context="Finance and sales currently use different definitions for the board report.",
        )
        print(f"Waiting for a human. Saved request: {request.id}")
        answer = await human.wait(request.id)
        print(f"Continuing with: {answer.selected_option} (answered by {answer.respondent})")


if __name__ == "__main__":
    asyncio.run(main())
