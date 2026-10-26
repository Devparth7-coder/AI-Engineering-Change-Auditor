"""Configuration: secrets never leak, defaults stay safe."""

from __future__ import annotations

from app.config import Settings


def test_redacted_hides_every_secret() -> None:
    settings = Settings(
        gitlab_token="glpat-real-token",
        api_auth_token="bearer-real-token",
        database_url="sqlite:///var/auditor.db",
    )
    red = settings.redacted()
    assert red["gitlab_token"] == "***redacted***"
    assert red["api_auth_token"] == "***redacted***"
    assert red["database_url"] == "***redacted***"
    assert red["duo_enabled"] is False


def test_auto_approval_can_never_default_on() -> None:
    assert Settings().auto_approve_patches is False


def test_allow_list_parses_and_ignores_garbage() -> None:
    settings = Settings(gitlab_allowed_project_ids="1, 2 , x, ")
    assert settings.allowed_project_ids == {1, 2}


def test_duo_privilege_parsing() -> None:
    settings = Settings(duo_agent_privileges="1,2,4", duo_pre_approved_privileges="2")
    assert settings.duo_privilege_ids == [1, 2, 4]
    assert settings.duo_pre_approved_ids == [2]
    # read_write_gitlab (3) must never be part of the default grant
    assert 3 not in settings.duo_privilege_ids


def test_gitlab_url_trailing_slash_stripped() -> None:
    assert Settings(gitlab_url="https://gitlab.example.com/").gitlab_url == (
        "https://gitlab.example.com"
    )
