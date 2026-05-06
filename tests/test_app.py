from fastapi.testclient import TestClient

import app as webhook_app


def test_health_check() -> None:
    client = TestClient(webhook_app.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": "true"}


def test_webhook_rejects_wrong_secret_path(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET_PATH", "telegram/secret")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "header-secret")
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telegram/wrong",
        headers={"X-Telegram-Bot-Api-Secret-Token": "header-secret"},
        json={},
    )

    assert response.status_code == 404


def test_webhook_rejects_wrong_secret_header(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET_PATH", "telegram/secret")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "header-secret")
    client = TestClient(webhook_app.app)

    response = client.post(
        "/telegram/secret",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={},
    )

    assert response.status_code == 403


def test_should_process_command_message() -> None:
    assert webhook_app.should_process_update_payload({"message": {"text": "/price lugia 138"}})


def test_should_process_short_price_command_message() -> None:
    assert webhook_app.should_process_update_payload({"message": {"text": "/p lugia 138"}})


def test_should_process_addressed_command_message() -> None:
    assert webhook_app.should_process_update_payload({"message": {"text": "/price@MyBot lugia 138"}})


def test_should_skip_plain_text_message() -> None:
    assert not webhook_app.should_process_update_payload({"message": {"text": "lugia 138"}})


def test_should_skip_unknown_command_message() -> None:
    assert not webhook_app.should_process_update_payload({"message": {"text": "/random lugia 138"}})


def test_should_process_callback_query() -> None:
    assert webhook_app.should_process_update_payload({"callback_query": {"data": "card:abc:0"}})
