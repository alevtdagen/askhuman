import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from askhuman import AskHuman, AskHumanError, HumanCancelled, HumanTimeout
from askhuman.config import RoutingConfig
from askhuman.models import AnswerInput, Question
from askhuman.settings import Settings
from askhuman.store import Store
from askhuman.terminal import Terminal, parse_reply


@pytest.fixture(autouse=True)
def local_environment(monkeypatch, tmp_path):
    for key in ("ASKHUMAN_MODE", "ASKHUMAN_BASE_URL", "ASKHUMAN_API_KEY", "ASKHUMAN_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ASKHUMAN_DATA_DIR", str(tmp_path / "embedded"))


@pytest.fixture
def terminal_input(monkeypatch):
    queue = asyncio.Queue()
    monkeypatch.setattr(Terminal, "available", property(lambda self: self.interactive))
    monkeypatch.setattr("askhuman.terminal.read_line", queue.get)
    return queue


async def test_default_runs_in_process_without_initialization_or_network(tmp_path, terminal_input):
    def no_http(request):
        raise AssertionError("Embedded terminal mode must not make HTTP calls")

    terminal_input.put_nowait("2")
    async with AskHuman(transport=httpx.MockTransport(no_http)) as human:
        assert human.mode == "embedded"
        result = await human.decide("Which source?", options=["Finance", "CRM"], wait_timeout=2)
        assert result.selected_option == "CRM" and result.source == "terminal"
        assert (await human.get(result.request_id)).status == "answered"
    assert (tmp_path / "embedded/embedded.sqlite3").exists()
    assert not (tmp_path / "embedded/settings.json").exists()


async def test_existing_server_settings_do_not_silently_select_remote(tmp_path):
    Settings.initialize(tmp_path / "embedded", "http://127.0.0.1:1")
    async with AskHuman(interactive=False) as human:
        assert human.mode == "embedded"
        assert (await human.create("Question")).status == "pending"


async def test_remote_configuration_still_selects_http(monkeypatch):
    monkeypatch.setenv("ASKHUMAN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ASKHUMAN_API_KEY", "test-key")
    async with AskHuman() as remote:
        assert remote.mode == "remote"
    async with AskHuman(mode="embedded", interactive=False) as local:
        assert local.mode == "embedded"
        await local.create("Local question")


async def test_timeout_releases_terminal_reader_and_next_request_still_works(terminal_input):
    async with AskHuman() as human:
        first = await human.create("Approve action A?", kind="approval")
        with pytest.raises(HumanTimeout):
            await human.wait(first.id, wait_timeout=0.05)
        assert not human._backend.terminal.lock.locked()
        terminal_input.put_nowait("Reject")
        second = await human.approve("Approve action B?", wait_timeout=1)
        assert second.approved is False
        assert (await human.get(first.id)).status == "pending"


async def test_concurrent_questions_share_one_runtime_and_serialize_terminal(terminal_input):
    terminal_input.put_nowait("first answer")
    terminal_input.put_nowait("second answer")
    async with AskHuman() as human:
        first, second = await asyncio.gather(human.ask("First?"), human.ask("Second?"))
        assert first.answer == "first answer" and second.answer == "second answer"
        assert first.request_id != second.request_id


async def test_cancelled_wait_does_not_cancel_or_authorize_request(terminal_input):
    async with AskHuman() as human:
        request = await human.create("May I deploy?", kind="approval")
        waiting = asyncio.create_task(human.wait(request.id))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert (await human.get(request.id)).status == "pending"
        await human.cancel(request.id)
        with pytest.raises(HumanCancelled):
            await human.wait(request.id)
        assert not human._backend.terminal.lock.locked()


async def test_deadline_interrupts_terminal_prompt(terminal_input):
    async with AskHuman() as human:
        request = await human.create("May I deploy?", kind="approval")
        waiting = asyncio.create_task(human.wait(request.id))
        await asyncio.sleep(0.02)
        with human._backend.store.connection() as db:
            db.execute("UPDATE requests SET expires=0 WHERE id=?", (request.id,))
        with pytest.raises(HumanTimeout) as expired:
            await asyncio.wait_for(waiting, 1)
        assert expired.value.expired and not human._backend.terminal.lock.locked()


async def test_saved_request_resumes_after_new_engine_and_local_human_cli(tmp_path):
    async with AskHuman(interactive=False) as human:
        request = await human.create(
            "Which source?", options=["Finance", "CRM"], idempotency_key="workflow-1"
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "askhuman.cli",
        "answer",
        request.id,
        "--option",
        "Finance",
        "--name",
        "Local human",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0 and json.loads(stdout)["status"] == "answered"
    async with AskHuman(interactive=False) as restarted:
        answer = await restarted.wait(request.id, wait_timeout=0)
        assert answer.selected_option == "Finance" and answer.respondent == "Local human"
        repeated = await restarted.create(
            "Which source?", options=["Finance", "CRM"], idempotency_key="workflow-1"
        )
        assert repeated.id == request.id
        with pytest.raises(AskHumanError) as conflict:
            await restarted.create("Different question", idempotency_key="workflow-1")
        assert conflict.value.status_code == 409


async def test_second_runtime_cannot_steal_receivers_or_change_active_routes(tmp_path):
    other_config = tmp_path / "other.json"
    other_config.write_text(
        json.dumps(
            {"channels": [{"id": "other", "type": "terminal"}], "default_channels": ["other"]}
        )
    )
    async with AskHuman(interactive=False) as owner:
        await owner.create("First question")
        async with AskHuman(config=other_config, interactive=False) as contender:
            with pytest.raises(AskHumanError, match="already owns"):
                await contender.create("Second question")
        assert owner._backend.store.config().default_channels == ["terminal"]
    async with AskHuman(config=other_config, interactive=False) as successor:
        assert (await successor.create("New question")).deliveries[0].channel == "other"


async def test_notify_delivers_before_context_closes(capsys):
    async with AskHuman() as human:
        request = await human.notify("Job done")
        assert request.deliveries[0].status == "delivered"
    assert "Job done" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reply", ["yes", "true", "looks good", "", "3", "approve this other thing"]
)
def test_direct_approval_is_never_inferred(reply, tmp_path):
    store = Store(tmp_path / "test.sqlite3", default_config=RoutingConfig.embedded())
    request = store.create(Question(question="Delete archive?", kind="approval"), None)
    with pytest.raises(ValueError):
        parse_reply(request, reply, "human")


def test_configuration_import_is_private_and_supports_named_routes(tmp_path):
    path = tmp_path / "settings-to-import.json"
    path.write_text(
        json.dumps(
            {
                "channels": [{"id": "console", "type": "terminal"}],
                "default_channels": ["console"],
                "routes": {"data-owner": ["console"]},
            }
        )
    )
    subprocess.run(
        [sys.executable, "-m", "askhuman.cli", "configure", "--from-file", str(path)],
        check=True,
        capture_output=True,
    )
    config = tmp_path / "embedded/config.json"
    assert RoutingConfig.model_validate_json(config.read_text()).routes == {
        "data-owner": ["console"]
    }
    if os.name != "nt":
        assert config.stat().st_mode & 0o777 == 0o600


async def test_noninteractive_wait_can_receive_an_answer_from_another_process():
    async with AskHuman(interactive=False) as human:
        request = await human.create("Question?")
        waiting = asyncio.create_task(human.wait(request.id, wait_timeout=2))
        await asyncio.sleep(0.02)
        human._backend.store.answer(
            request.id, AnswerInput(answer="Human answer", respondent="Alex"), "terminal"
        )
        assert (await waiting).answer == "Human answer"


def test_unsupported_embedded_channels_fail_before_creating_state(tmp_path):
    config = tmp_path / "server-config.json"
    config.write_text(RoutingConfig().model_dump_json())
    with pytest.raises(ValueError, match="Embedded mode cannot receive"):
        AskHuman(config=config)
    assert not (tmp_path / "embedded").exists()


def test_numeric_option_labels_use_displayed_indices(tmp_path):
    store = Store(tmp_path / "test.sqlite3", default_config=RoutingConfig.embedded())
    request = store.create(Question(question="Which number?", options=["2", "1"]), None)
    assert parse_reply(request, "1", "human").selected_option == "2"


async def test_native_langchain_tool_uses_the_embedded_engine():
    pytest.importorskip("langchain_core")
    from askhuman.tools import as_langchain_tools

    async with AskHuman(interactive=False) as human:
        tools = as_langchain_tools(human)
        created = await tools[0].ainvoke({"question": "Report ready", "kind": "notification"})
        assert created["status"] == "notified"
        assert created["deliveries"][0]["channel"] == "terminal"
        read = await tools[1].ainvoke({"request_id": created["id"]})
        assert read["id"] == created["id"]
