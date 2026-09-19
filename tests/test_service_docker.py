"""Line-based sanity checks for the Docker deployment artefacts
(deliverable 2.f): ``Dockerfile`` and ``docker-compose.yml``.

Deliberately not a YAML/Dockerfile parse -- a few grep-shaped
assertions over the raw text, matching this repo's stdlib-only,
no-dependencies posture (no PyYAML in ``dependencies``/``test`` extras
to parse the compose file properly with). ``tests/test_service_egress.py``
already covers the *service's own* no-outbound-connection guarantee at
the Python level; these tests instead catch a docker-compose.yml/Dockerfile
edit that silently drops a hardening line.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# -- docker-compose.yml (deliverable 2.b/2.f) --------------------------------


def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.is_file()


def test_compose_binds_claude_home_read_only() -> None:
    lines = _lines(COMPOSE_FILE)
    ro_mounts = [line for line in lines if ":/data/claude:ro" in line]
    assert ro_mounts, "no read-only bind mount of the Claude config dir found"


def test_compose_names_a_data_volume_for_token_lens_state() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "/data/token-lens" in text
    assert "token-lens-data:" in text  # the named volume itself declared


def test_compose_publishes_port_loopback_only() -> None:
    lines = _lines(COMPOSE_FILE)
    port_lines = [line.strip() for line in lines if "8765:8765" in line]
    assert port_lines, "no 8765:8765 port mapping found"
    assert all(line.strip("\"'- ").startswith("127.0.0.1:") for line in port_lines), port_lines


def test_compose_sets_read_only_root_filesystem() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "read_only: true" in text


def test_compose_drops_all_capabilities() -> None:
    lines = _lines(COMPOSE_FILE)
    cap_drop_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("cap_drop:")), None)
    assert cap_drop_idx is not None, "no cap_drop: key found"
    following = "\n".join(lines[cap_drop_idx : cap_drop_idx + 3])
    assert "ALL" in following


def test_compose_sets_no_new_privileges() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "no-new-privileges:true" in text


def test_compose_uses_bridge_network_not_none() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "network_mode: bridge" in text
    # "none" would break the published port -- make sure that mistake
    # never sneaks back in as the active setting.
    active_network_lines = [
        line.strip() for line in _lines(COMPOSE_FILE) if line.strip().startswith("network_mode:")
    ]
    assert active_network_lines == ["network_mode: bridge"], active_network_lines


def test_compose_restarts_unless_stopped() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "restart: unless-stopped" in text


# -- Dockerfile (deliverable 2.a/2.f) ----------------------------------------


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file()


def test_dockerfile_uses_python_slim_base() -> None:
    lines = _lines(DOCKERFILE)
    from_lines = [line for line in lines if line.strip().upper().startswith("FROM")]
    assert from_lines, "no FROM line found"
    assert any("python:3.12-slim" in line for line in from_lines), from_lines


def test_dockerfile_has_a_non_root_user_line() -> None:
    lines = _lines(DOCKERFILE)
    user_lines = [line.strip() for line in lines if line.strip().upper().startswith("USER ")]
    assert user_lines, "no USER instruction found -- image would run as root"
    assert not any(line.split()[-1].lower() in ("root", "0") for line in user_lines), user_lines


def test_dockerfile_chowns_every_compose_mount_point_before_switching_user() -> None:
    """Regression test for review finding 4 (blocking): docker-compose.yml
    mounts a *named* volume at ``/data/token-lens`` (the SQLite store's own
    directory) and a bind mount at ``/data/claude``. A named volume with no
    prior contents is seeded from the image's own directory at that path,
    owned by whoever the daemon (root) created it as if the image never
    created it first -- leaving the non-root ``token-lens`` user unable to
    write ``service.db`` on first start. The ``Dockerfile`` must create and
    ``chown -R`` both paths to the non-root user *before* its ``USER``
    instruction switches away from root (a ``chown`` issued while already
    running as the unprivileged user cannot chown anything).

    Fails against the pre-fix ``Dockerfile`` (which had no such
    ``mkdir``/``chown`` line at all) and passes once it does.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    lines = _lines(DOCKERFILE)
    user_idx = next(
        (i for i, line in enumerate(lines) if line.strip().upper().startswith("USER ") and "root" not in line.lower()),
        None,
    )
    assert user_idx is not None, "no non-root USER instruction found"

    # Every /data/... path docker-compose.yml mounts must also appear in
    # a RUN chown line before that USER switch.
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    mounted_paths = sorted(set(re.findall(r"(/data/[\w-]+)", compose_text)))
    assert mounted_paths, "no /data/... mount points found in docker-compose.yml"

    chown_lines_before_user = [
        line for line in lines[:user_idx] if line.strip().upper().startswith("RUN") and "chown" in line.lower()
    ]
    assert chown_lines_before_user, "no RUN ... chown instruction found before the non-root USER switch"
    chown_text = "\n".join(chown_lines_before_user)
    for path in mounted_paths:
        assert path in chown_text, f"{path!r} (mounted in docker-compose.yml) is never chowned in the Dockerfile: {chown_text}"

    assert "chown -R" in text, "chown should be recursive (-R) to cover files Docker seeds into a fresh named volume"


def test_dockerfile_installs_the_package_from_the_build_context() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install" in text
    assert "COPY src" in text


def test_dockerfile_never_installs_a_network_cli_tool() -> None:
    # Comments are allowed to mention curl/wget/apt-get by name when
    # explaining why the image deliberately avoids them (see the
    # Dockerfile's own HEALTHCHECK comment) -- only a real instruction
    # line invoking one is forbidden.
    instruction_lines = [
        line for line in _lines(DOCKERFILE) if line.strip() and not line.strip().startswith("#")
    ]
    for forbidden in ("curl", "wget", "apt-get"):
        offending = [line for line in instruction_lines if forbidden in line.lower()]
        assert not offending, f"Dockerfile instruction references {forbidden!r}: {offending}"


def test_dockerfile_healthcheck_uses_stdlib_urllib_against_api_health() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "urllib" in text
    assert "/api/health" in text


def test_dockerfile_entrypoint_is_the_serve_subcommand() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["claude-token-lens", "serve"]' in text


def test_dockerfile_default_cmd_binds_remote_for_port_publishing() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "--bind" in text and "0.0.0.0" in text
    assert "--allow-remote" in text


# -- .dockerignore ------------------------------------------------------------


def test_dockerignore_excludes_tests_fixtures_and_vcs_state() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for expected in ("tests/", ".venv/", ".git/", ".claude/"):
        assert expected in text, f"{expected!r} not excluded from the Docker build context"


__all__: list[str] = []
