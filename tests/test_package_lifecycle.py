"""发布生命周期定义契约。"""

from pathlib import Path

import pytest

from scripts.package_smoke import ArtifactContract


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("target", "runner"),
    [
        ("windows-x64", "windows-2022"),
        ("linux-appimage-x64", "ubuntu-24.04"),
        ("linux-deb-amd64", "ubuntu-24.04"),
        ("linux-rpm-x64", "ubuntu-24.04"),
        ("macos-arm64", "macos-14"),
        ("macos-x86_64", "macos-15-intel"),
    ],
)
def test_lifecycle_contract_pins_runner_images_and_inputs(target, runner):
    """Given a release target, when probes are defined, then runners and inputs pin."""
    contract = ArtifactContract.default(target, "0.6.2")

    assert {probe.runner for probe in contract.lifecycle_probes} == {runner}
    assert all(probe.tool_lockfile == "mise.lock" for probe in contract.lifecycle_probes)
    assert all("sha256" in probe.input_digest for probe in contract.lifecycle_probes)
    assert "latest" not in runner


@pytest.mark.parametrize("workflow", ("build.yml", "nightly.yml"))
def test_package_workflows_do_not_use_floating_runner_images(workflow):
    """Given a package workflow, when runner images are read, then all are pinned."""
    content = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    assert "windows-latest" not in content
    assert "ubuntu-latest" not in content
    assert "macos-latest" not in content

