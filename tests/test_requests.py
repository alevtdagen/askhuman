import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from askhuman import AskHumanError, HumanCancelled, HumanTimeout
from askhuman.models import AnswerInput, Question
from askhuman.store import Conflict, Store


async def test_question_answer_resume_after_client_and_store_restart(
    human, app, http, admin_headers
):
    request = await human.create(
        "Which definition?", options=["Finance", "CRM"], idempotency_key="job-42-definition"
    )
    with pytest.raises(HumanTimeout) as pending:
        await human.wait(request.id, wait_timeout=0)
    assert pending.value.request_id == request.id
    assert pending.value.expired is False
    response = await http.post(
        f"/api/admin/requests/{request.id}/answer",
        headers=admin_headers,
        json={"selected_option": "Finance", "respondent": "Sam"},
    )
    assert response.status_code == 200
    answer = await human.wait(request.id, wait_timeout=0)
    assert answer.answer == answer.selected_option == "Finance"
    assert answer.respondent == "Sam"
    assert answer.source == "admin"
    reopened = Store(app.state.store.path)
    assert reopened.get(request.id).response == answer
    same = await human.create(
        "Which definition?", options=["Finance", "CRM"], idempotency_key="job-42-definition"
    )
    assert same.id == request.id and same.status == "answered"


async def test_wait_unblocks_on_human_answer(human, http, admin_headers):
    request = await human.create("What is the project code?")

    async def answer_later():
        await asyncio.sleep(0.05)
        return await http.post(
            f"/api/admin/requests/{request.id}/answer",
            headers=admin_headers,
            json={"answer": "Orchid", "respondent": "Alex"},
        )

    answer, posted = await asyncio.gather(human.wait(request.id, wait_timeout=2), answer_later())
    assert posted.status_code == 200 and answer.answer == "Orchid"


async def test_idempotency_mismatch_does_not_create_question(human, app):
    await human.create("May I deploy version 1?", kind="approval", idempotency_key="deploy")
    with pytest.raises(AskHumanError) as conflict:
        await human.create("May I deploy version 2?", kind="approval", idempotency_key="deploy")
    assert conflict.value.status_code == 409
    assert len(app.state.store.list()) == 1


@pytest.mark.parametrize("approved", [True, False])
async def test_explicit_approval_and_rejection(human, http, admin_headers, approved):
    request = await human.create("Deploy abc123 to staging?", kind="approval")
    reply = await http.post(
        f"/api/admin/requests/{request.id}/answer",
        headers=admin_headers,
        json={"approved": approved, "respondent": "Reviewer"},
    )
    assert reply.status_code == 200
    answer = await human.wait(request.id, wait_timeout=0)
    assert answer.approved is approved
    assert answer.selected_option == ("Approve" if approved else "Reject")
    duplicate = await http.post(
        f"/api/admin/requests/{request.id}/answer",
        headers=admin_headers,
        json={"approved": not approved, "respondent": "Someone else"},
    )
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"answer": "yes"},
        {"approved": "false"},
        {"approved": 1},
        {"approved": True, "selected_option": "Reject"},
    ],
)
async def test_approval_never_inferred_or_coerced(human, http, admin_headers, payload):
    request = await human.create("Delete archive?", kind="approval")
    result = await http.post(
        f"/api/admin/requests/{request.id}/answer",
        headers=admin_headers,
        json=payload | {"respondent": "Sam"},
    )
    assert result.status_code == 422
    assert (await human.get(request.id)).status == "pending"


async def test_expired_and_cancelled_never_accept_answers(human, app, http, admin_headers):
    expired = await human.create("Approve expense?", kind="approval")
    with app.state.store.connection() as db:
        db.execute("UPDATE requests SET expires=0 WHERE id=?", (expired.id,))
    with pytest.raises(HumanTimeout) as result:
        await human.wait(expired.id, wait_timeout=0)
    assert result.value.expired
    rejected = await http.post(
        f"/api/admin/requests/{expired.id}/answer",
        headers=admin_headers,
        json={"approved": True, "respondent": "Sam"},
    )
    assert rejected.status_code == 409
    cancelled = await human.create("Still relevant?")
    await human.cancel(cancelled.id)
    assert (await human.cancel(cancelled.id)).status == "cancelled"
    with pytest.raises(HumanCancelled):
        await human.wait(cancelled.id)
    assert app.state.store.get(expired.id).response is None


@pytest.mark.parametrize(
    "question",
    [
        {"question": " "},
        {"question": "Choose", "kind": "decision", "options": ["one"]},
        {"question": "Choose", "options": ["one", "one"]},
        {"question": "Approve", "kind": "approval", "options": ["yes", "no"]},
        {"question": "Notice", "kind": "notification", "options": ["yes"]},
        {"question": "Test", "timeout_seconds": 0},
        {"question": "Test", "timeout_seconds": 1.5},
        {"question": "Test", "channel": "attacker"},
    ],
)
def test_invalid_requests(question):
    with pytest.raises(ValidationError):
        Question(**question)


async def test_notifications_do_not_block_or_accept_responses(human, http, admin_headers):
    request = await human.notify("Job completed")
    assert request.status == "notified"
    assert request.deliveries[0].status == "delivered"
    with pytest.raises(AskHumanError, match="Notifications"):
        await human.wait(request.id)
    result = await http.post(
        f"/api/admin/requests/{request.id}/answer",
        headers=admin_headers,
        json={"answer": "OK", "respondent": "Sam"},
    )
    assert result.status_code == 409


def test_concurrent_responses_have_one_winner(app):
    store = app.state.store
    request = store.create(Question(question="Choose", options=["A", "B"]), None)

    def answer(index):
        try:
            return store.answer(
                request.id, AnswerInput(selected_option="A", respondent=str(index)), "reply_link"
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(answer, range(16)))
    assert sum(result is not None for result in results) == 1
    assert len([e for e in store.events(request.id) if e["event"] == "answered"]) == 1


def test_concurrent_idempotent_creates_enqueue_once(app):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: app.state.store.create(
                    Question(question="Which source?"), "single-question"
                ),
                range(16),
            )
        )
    assert len({r.id for r in results}) == 1
    assert len(app.state.store.list()) == 1
