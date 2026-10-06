import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from askhuman import AskHuman
from askhuman.server import create_app
from askhuman.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings.initialize(tmp_path / "state", "http://127.0.0.1:8765")


@pytest.fixture
def app(settings):
    return create_app(settings, run_worker=False)


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture
async def human(app, settings):
    async with AskHuman(
        settings.base_url, settings.api_key, transport=httpx.ASGITransport(app=app)
    ) as human:
        yield human


@pytest.fixture
def admin_headers(settings):
    return {"Authorization": f"Bearer {settings.admin_key}"}


@pytest.fixture
def agent_headers(settings):
    return {"Authorization": f"Bearer {settings.api_key}"}


@pytest.fixture
def live_server(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings.initialize(tmp_path / "live", f"http://127.0.0.1:{port}")
    env = dict(
        os.environ,
        ASKHUMAN_DATA_DIR=str(settings.data_dir),
        ASKHUMAN_BASE_URL=settings.base_url,
        ASKHUMAN_API_KEY=settings.api_key,
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "askhuman.cli", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Live service exited during startup")
            try:
                if httpx.get(settings.base_url + "/health", timeout=1, trust_env=False).is_success:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("Live service did not start")
        yield settings, env
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
