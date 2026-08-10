from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_api_uses_the_published_image_and_watchtower_label() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert 'image: "${API_IMAGE:-ghcr.io/qinbatista/video-url-download:latest}"' in compose
    assert 'com.centurylinklabs.watchtower.enable: "true"' in compose


def test_workflow_publishes_a_multiarch_api_image() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/publish-image.yml").read_text()

    assert "packages: write" in workflow
    assert "needs: test" in workflow
    assert "docker run --rm video-url-download-test" in workflow
    assert "linux/amd64,linux/arm64" in workflow
    assert "ghcr.io/${{ github.repository_owner }}/video-url-download:latest" in workflow
