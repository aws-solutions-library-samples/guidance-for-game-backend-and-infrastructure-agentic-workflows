"""Unit tests for the code-owned image reference projection (#529)."""

# Third-party packages
import pytest

# Local modules
from agents.image_reference import MAX_IMAGE_REFERENCE_LENGTH, project_image_reference

pytestmark = pytest.mark.unit

ACCOUNT = "000000000000"


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/game/echo:1.2",
            {
                "registryType": "ecr-private",
                "registryRegion": "us-west-2",
                "repository": "game/echo",
                "tag": "1.2",
                "complete": True,
            },
        ),
        (
            "public.ecr.aws/gamelift/sample:latest",
            {"registryType": "ecr-public", "repository": "gamelift/sample", "tag": "latest", "complete": True},
        ),
        ("nginx:1.25", {"registryType": "docker-hub", "repository": "library/nginx", "tag": "1.25", "complete": True}),
        (
            "us-docker.pkg.dev/agones-images/examples/simple-game-server:0.43",
            {
                "registryType": "other",
                "repository": "agones-images/examples/simple-game-server",
                "tag": "0.43",
                "complete": True,
            },
        ),
        (
            "registry.example.invalid:5000/team/app@sha256:" + "b" * 64,
            {"registryType": "other", "repository": "team/app", "digest": "sha256:" + "b" * 64, "complete": True},
        ),
    ],
)
def test_projection(value, expected):
    assert project_image_reference(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/app:1",
        "registry.example.invalid:5000/app:1",
        "10.0.0.1:5000/app:1",
    ],
)
def test_registry_host_never_returned(value):
    out = project_image_reference(value)
    blob = repr(out)
    assert ACCOUNT not in blob and "example.invalid" not in blob and "10.0.0.1" not in blob and "5000" not in blob


def test_account_like_repository_is_dropped_and_marked_incomplete():
    out = project_image_reference(f"ghcr.io/{ACCOUNT}/app:1")
    assert "repository" not in out and out["complete"] is False


@pytest.mark.parametrize("value", [None, 7, "", "a" * (MAX_IMAGE_REFERENCE_LENGTH + 1), "app:1\nINJECT", "app :1"])
def test_unusable_values(value):
    assert project_image_reference(value) is None


def test_bad_digest_marks_incomplete():
    out = project_image_reference("app@sha256:xyz")
    assert "digest" not in out and out["complete"] is False
