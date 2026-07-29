from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.utils import base64url_encode

from app.core.config import Settings
from app.main import create_app
from app.repository import InMemoryRepository
from app.schemas import UserRole
from conftest import seeded_candidate_ids


def _signed_supabase_token(
    *,
    user_id: UUID,
    audience: str = "authenticated",
    issuer: str = "https://example.supabase.co/auth/v1",
) -> tuple[str, object]:
    """Create a local RS256 token plus its JWKS-derived verification key."""
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-signing-key",
        "use": "sig",
        "alg": "RS256",
        "n": base64url_encode(
            public_numbers.n.to_bytes(
                (public_numbers.n.bit_length() + 7) // 8,
                "big",
            )
        ).decode(),
        "e": base64url_encode(
            public_numbers.e.to_bytes(
                (public_numbers.e.bit_length() + 7) // 8,
                "big",
            )
        ).decode(),
    }
    token = jwt.encode(
        {
            "sub": str(user_id),
            "aud": audience,
            "iss": issuer,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-signing-key"},
    )
    return token, jwt.PyJWK.from_dict(jwk).key


def _supabase_client_with_signed_token(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audience: str = "authenticated",
    issuer: str = "https://example.supabase.co/auth/v1",
) -> tuple[TestClient, str]:
    auth = import_module("app.core.auth")
    user_id = UUID("00000000-0000-4000-8000-000000000099")
    token, key = _signed_supabase_token(
        user_id=user_id,
        audience=audience,
        issuer=issuer,
    )

    class LocalJwks:
        def get_signing_key_from_jwt(self, candidate: str) -> object:
            assert candidate == token
            return type("SigningKey", (), {"key": key})()

    monkeypatch.setattr(auth, "_jwks_client", lambda _: LocalJwks())
    repository = InMemoryRepository.seeded()
    repository.get_user_role = lambda candidate: (  # type: ignore[method-assign]
        UserRole.viewer if candidate == user_id else None
    )
    settings = Settings(
        environment="local",
        repository_mode="postgres",
        auth_mode="supabase",
        supabase_url="https://example.supabase.co/",
        supabase_anon_key="anon-key",
    )
    return TestClient(create_app(repository, settings=settings)), token


def test_production_rejects_local_auth() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=local"):
        Settings(environment="production", auth_mode="local")


def test_production_requires_supabase_configuration() -> None:
    with pytest.raises(ValueError, match="Supabase"):
        Settings(environment="production", auth_mode="supabase")


def test_editor_dependency_rejects_viewer(client_as_viewer: TestClient) -> None:
    response = client_as_viewer.post(
        "/imports/url",
        json={"url": "https://soundcloud.com/syco23/ritual"},
    )
    assert response.status_code == 403


def test_editor_can_change_candidate(client_as_editor: TestClient) -> None:
    set_id, candidate_id = seeded_candidate_ids(client_as_editor)
    response = client_as_editor.post(
        f"/sets/{set_id}/candidates/{candidate_id}/accept"
    )
    assert response.status_code == 200


def test_local_auth_exposes_configured_identity(client_as_viewer: TestClient) -> None:
    response = client_as_viewer.get("/auth/me")
    assert response.status_code == 200
    assert response.json() == {
        "user_id": "00000000-0000-4000-8000-000000000023",
        "role": "viewer",
    }


def test_auth_me_accepts_a_locally_signed_supabase_jwks_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token = _supabase_client_with_signed_token(monkeypatch)
    with client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "00000000-0000-4000-8000-000000000099",
        "role": "viewer",
    }


def test_auth_me_rejects_a_supabase_token_with_wrong_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token = _supabase_client_with_signed_token(
        monkeypatch,
        audience="another-audience",
    )
    with client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid access token"


def test_auth_me_rejects_a_supabase_token_with_wrong_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, token = _supabase_client_with_signed_token(
        monkeypatch,
        issuer="https://other.supabase.co/auth/v1",
    )
    with client:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid access token"


def test_supabase_role_comes_from_repository_not_user_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = import_module("app.core.auth")
    user_id = UUID("00000000-0000-4000-8000-000000000099")

    class FakeSigningKey:
        key = object()

    class FakeJWKClient:
        def __init__(self, url: str) -> None:
            assert url == "https://example.supabase.co/auth/v1/.well-known/jwks.json"

        def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
            assert token == "signed-token"
            return FakeSigningKey()

    def fake_decode(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "sub": str(user_id),
            "exp": 4_102_444_800,
            "user_metadata": {"role": "admin"},
        }

    monkeypatch.setattr(auth, "PyJWKClient", FakeJWKClient)
    monkeypatch.setattr(auth.jwt, "decode", fake_decode)

    repository = InMemoryRepository.seeded()
    repository.get_user_role = lambda candidate: (  # type: ignore[method-assign]
        UserRole.viewer if candidate == user_id else None
    )
    settings = Settings(
        environment="local",
        repository_mode="postgres",
        auth_mode="supabase",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-key",
    )
    with TestClient(create_app(repository, settings=settings)) as client:
        me = client.get("/auth/me", headers={"Authorization": "Bearer signed-token"})
        forbidden = client.post(
            "/imports/url",
            headers={"Authorization": "Bearer signed-token"},
            json={"url": "https://soundcloud.com/syco23/ritual"},
        )

    assert me.status_code == 200
    assert me.json()["role"] == "viewer"
    assert forbidden.status_code == 403


def test_memory_repository_mode_is_selected_without_a_database_pool() -> None:
    settings = Settings(
        environment="fixture",
        repository_mode="memory",
        auth_mode="local",
    )
    with TestClient(create_app(settings=settings)) as client:
        assert isinstance(client.app.state.repository, InMemoryRepository)


@pytest.mark.parametrize("environment", ["local", "production"])
def test_non_fixture_environment_rejects_memory_repository(
    environment: str,
) -> None:
    with pytest.raises(ValueError, match="REPOSITORY_MODE=memory"):
        Settings(
            environment=environment,
            repository_mode="memory",
            auth_mode="supabase",
            supabase_url="https://example.supabase.co",
            supabase_anon_key="anon-key",
        )


@pytest.mark.parametrize("path", ["/stats", "/search-profiles"])
def test_anonymous_user_cannot_read_authenticated_routes(path: str) -> None:
    settings = Settings(
        environment="local",
        repository_mode="postgres",
        auth_mode="supabase",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-key",
    )
    with TestClient(
        create_app(InMemoryRepository.seeded(), settings=settings)
    ) as client:
        response = client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "post",
            "/search-profiles",
            {"name": "Denied profile", "query": "denied liveset"},
        ),
        (
            "patch",
            "/search-profiles/{profile_id}",
            {"name": "Denied update"},
        ),
        ("delete", "/search-profiles/{profile_id}", None),
        ("post", "/search-profiles/{profile_id}/run", None),
    ],
)
@pytest.mark.parametrize("role_fixture", ["client_as_viewer", "client_as_editor"])
def test_non_admin_cannot_mutate_search_profiles(
    request: pytest.FixtureRequest,
    role_fixture: str,
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    client = request.getfixturevalue(role_fixture)
    profiles = client.get("/search-profiles")
    assert profiles.status_code == 200
    profile_id = profiles.json()[0]["id"]
    response = client.request(
        method,
        path.format(profile_id=profile_id),
        json=payload,
    )
    assert response.status_code == 403


def test_viewer_can_read_stats_and_search_profiles(
    client_as_viewer: TestClient,
) -> None:
    assert client_as_viewer.get("/stats").status_code == 200
    assert client_as_viewer.get("/search-profiles").status_code == 200
