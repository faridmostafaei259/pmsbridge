"""Checks on the deployment files that need neither Docker nor network access."""

import re
from pathlib import Path

from deploy_helpers import COMPOSE_FILE, DEPLOY_DIR, REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
SYSTEMD_UNIT = DEPLOY_DIR / "systemd" / "pmsbridge.service"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
IMAGE_WITH_DIGEST = re.compile(r"^[\w./:-]+:[\w.-]+@sha256:[0-9a-f]{64}$")


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments removed."""
    joined = re.sub(r"\\\n", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _from_references(dockerfile: Path) -> list[str]:
    """The image reference of every FROM line, exactly as written."""
    references: list[str] = []
    for line in _instructions(dockerfile.read_text()):
        if line.startswith("FROM "):
            parts = line.split()[1:]
            parts = [part for part in parts if not part.startswith("--")]
            references.append(parts[0])
    return references


def test_from_lines_are_literal_pinned_references_dependabot_can_update() -> None:
    for dockerfile in (DOCKERFILE, DEPLOY_DIR / "caddy" / "Dockerfile"):
        references = _from_references(dockerfile)
        assert references, dockerfile
        for reference in references:
            # No build-argument indirection: Dependabot cannot update `FROM ${IMAGE}`.
            assert "$" not in reference, (dockerfile, reference)
            assert IMAGE_WITH_DIGEST.match(reference), (dockerfile, reference)
            assert ":latest" not in reference, (dockerfile, reference)
        assert not any(
            line.startswith("ARG ") and line.split("=")[0].endswith("IMAGE")
            for line in _instructions(dockerfile.read_text())
        )


def test_application_image_stages_use_python_312_slim_and_the_same_digest() -> None:
    python_references = [r for r in _from_references(DOCKERFILE) if r.startswith("python:")]
    assert len(python_references) == 2
    assert python_references[0] == python_references[1]
    assert python_references[0].startswith("python:3.12-slim")


def test_dockerfile_is_multi_stage_and_runs_as_the_fixed_non_root_user() -> None:
    instructions = _instructions(DOCKERFILE.read_text())
    stages = [line for line in instructions if line.startswith("FROM ")]
    assert len(stages) >= 3
    assert "USER 10001:10001" in instructions
    # The last USER instruction decides who runs the container.
    users = [line for line in instructions if line.startswith("USER ")]
    assert users[-1] == "USER 10001:10001"
    assert any("--uid 10001" in line and "--gid 10001" in line for line in instructions)


def test_dockerfile_installs_locked_runtime_dependencies_only() -> None:
    syncs = [line for line in _instructions(DOCKERFILE.read_text()) if "uv sync" in line]
    assert syncs
    for line in syncs:
        assert "--frozen" in line
        assert "--no-dev" in line


def test_dockerfile_has_healthcheck_single_process_entrypoint_and_no_secrets() -> None:
    text = DOCKERFILE.read_text()
    instructions = _instructions(text)
    assert any(line.startswith("HEALTHCHECK ") and "/healthz" in line for line in instructions)
    assert 'ENTRYPOINT ["pmsbridge-app"]' in instructions
    assert not any(re.search(r"(?i)\bworkers?\b", line) for line in instructions)
    for line in instructions:
        if line.startswith(("ENV ", "ARG ")):
            assert not re.search(r"(?i)secret|password|token|api_key|private", line), line
    assert not any(line.startswith("COPY . ") for line in instructions)
    assert "COPY .env" not in text


def test_dockerignore_is_an_allow_list() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = {line for line in lines if line.startswith("!")}
    assert allowed == {"!src/", "!pyproject.toml", "!uv.lock", "!README.md", "!LICENSE"}


def test_caddy_image_is_pinned_by_digest_and_runs_as_the_fixed_user() -> None:
    text = (DEPLOY_DIR / "caddy" / "Dockerfile").read_text()
    match = re.search(r"^FROM\s+(caddy:\S+)\s*$", text, re.MULTILINE)
    assert match is not None
    assert IMAGE_WITH_DIGEST.match(match.group(1))
    instructions = _instructions(text)
    assert "USER 10001:10001" in instructions
    assert any(
        "chown" in line and "10001:10001" in line and "/data" in line for line in instructions
    )
    assert any("chown" in line and "/config" in line for line in instructions)


def test_compose_publishes_no_app_port() -> None:
    text = COMPOSE_FILE.read_text()
    app_block = text.split("  caddy:")[0].split("  app:")[1]
    assert "ports:" not in app_block


def test_caddyfiles_exist_for_the_supported_modes() -> None:
    for name in ("Caddyfile.auto", "Caddyfile.internal", "site.caddy"):
        assert (DEPLOY_DIR / "caddy" / name).is_file(), name
    for name in ("production.caddy", "staging.caddy"):
        assert (DEPLOY_DIR / "caddy" / "acme" / name).is_file(), name


def test_systemd_unit_runs_the_compose_stack() -> None:
    text = SYSTEMD_UNIT.read_text()
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text
    assert "Requires=docker.service" in text
    assert "After=docker.service" in text
    assert "/usr/bin/docker compose" in text
    assert "up -d" in text
    assert "WantedBy=multi-user.target" in text
    # Stopping the unit must never delete volumes.
    assert "--volumes" not in text
    assert " down" not in text


def test_ci_actions_are_pinned_by_full_commit_sha() -> None:
    text = CI_WORKFLOW.read_text()
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE)
    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference), reference


def test_ci_uses_least_privilege_permissions() -> None:
    text = CI_WORKFLOW.read_text()
    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE)
    assert not re.search(r"(?m)^\s*[\w-]+:\s*write\s*$", text)


def test_ci_runs_every_required_check() -> None:
    text = CI_WORKFLOW.read_text()
    for expected in (
        "uv sync --frozen",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run pyright",
        "uv run pytest -q",
        "shellcheck",
        "scripts/check_no_emoji.py",
        "scripts/gen_tool_docs.py",
        "docker build",
        "aquasecurity/trivy-action",
        "ignore-unfixed: true",
        "HIGH,CRITICAL",
        "docker compose",
    ):
        assert expected in text, expected


def test_dependabot_covers_actions_python_and_both_images_weekly() -> None:
    text = DEPENDABOT.read_text()
    entries = re.findall(
        r"package-ecosystem: (\S+)\n\s+directory: (\S+)\n\s+schedule:\n\s+interval: (\S+)", text
    )
    assert set(entries) == {
        ("github-actions", "/", "weekly"),
        ("uv", "/", "weekly"),
        ("docker", "/", "weekly"),
        ("docker", "/deploy/caddy", "weekly"),
    }
    # Dependabot only updates literal references, so check the Dockerfile of every docker entry.
    for ecosystem, directory, _ in entries:
        if ecosystem != "docker":
            continue
        dockerfile = REPO_ROOT / directory.lstrip("/") / "Dockerfile"
        references = _from_references(dockerfile)
        assert references, dockerfile
        assert all("$" not in reference for reference in references), dockerfile


def test_ci_checks_generated_docs_scripts_shell_shebangs_and_dependencies() -> None:
    text = CI_WORKFLOW.read_text()
    for expected in (
        "scripts/gen_tool_docs.py docs/tools.md",
        "scripts/gen_config_docs.py docs/configuration.md",
        "--check",
        "is missing",
        "git ls-files -z",
        "pip-audit",
        "--require-hashes",
        "uv export --frozen",
    ):
        assert expected in text, expected
    # Extensionless scripts (bin/, libexec/) are found by their shebang, not by directory.
    assert "#!" in text


def test_ci_smoke_test_checks_the_edge() -> None:
    text = CI_WORKFLOW.read_text()
    assert '"$readyz" = "404"' in text
    assert '"$redirect" = "308"' in text
    for header in ("x-content-type-options", "x-frame-options", "content-security-policy"):
        assert header in text
    assert "strict-transport-security" in text


def test_trivy_exception_is_scoped_dated_and_applied_to_the_caddy_scan_only() -> None:
    ignore = (REPO_ROOT / ".trivyignore.yaml").read_text()
    entries = re.findall(r"^\s*- id: (\S+)$", ignore, re.MULTILINE)
    assert entries == ["CVE-2026-78661"]
    assert re.search(r"^\s+expired_at: 2026-12-22$", ignore, re.MULTILINE)
    text = CI_WORKFLOW.read_text()
    scans = text.split("uses: aquasecurity/trivy-action@")[1:]
    assert len(scans) == 2
    for scan in scans:
        assert 'exit-code: "1"' in scan.split("- name:")[0]
    app_scan, caddy_scan = (scan.split("- name:")[0] for scan in scans)
    assert "trivyignores" not in app_scan
    assert "trivyignores: .trivyignore.yaml" in caddy_scan
    assert "CADDY_IMAGE_TAG" in caddy_scan


def test_ignore_files_stay_out_of_both_image_build_contexts() -> None:
    root = DOCKERIGNORE.read_text().splitlines()
    assert "*" in root
    assert "!.trivyignore.yaml" not in root
    caddy = (DEPLOY_DIR / "caddy" / ".dockerignore").read_text().splitlines()
    assert "*" in caddy
    assert not any(line.startswith("!") for line in caddy)
