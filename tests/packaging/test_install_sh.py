"""The installer (install.sh), tested with stub commands in front of the real PATH.

Every test runs the real script in a child bash. Nothing here installs Docker, opens a firewall port
or touches the network. The compose commands, service names and variable names the script uses are
checked against the real deploy files at the end of this module.
"""

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from packaging_helpers import (
    FAKE_KEY_BASE64,
    FAKE_LINK,
    FAKE_LINK_TOKEN,
    INSTALL_SH,
    REPO_ROOT,
    Sandbox,
    env_value,
)

from pmsbridge.core.crypto import parse_encryption_keys

GOOD = ("--domain", "mcp.example.com", "--email", "ops@example.com", "--yes")
KEY_LINE = re.compile(r"^k1:[A-Za-z0-9+/]{43}=$")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------------------------------------
# Script hygiene
# ------------------------------------------------------------------------------------------------


def test_script_header_and_executable_bit() -> None:
    lines = INSTALL_SH.read_text().splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    assert mode_of(INSTALL_SH) & stat.S_IXUSR


def test_shellcheck_is_clean() -> None:
    bin_dir = Path(sys.executable).parent
    found = shutil.which("shellcheck", path=f"{bin_dir}:/usr/bin:/usr/local/bin:/opt/homebrew/bin")
    if found is None:
        pytest.skip("shellcheck is not installed")
    done = subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [found, str(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_script_is_plain_ascii_without_forbidden_words() -> None:
    text = INSTALL_SH.read_text()
    text.encode("ascii")
    assert "api.ipify" not in text
    assert "get.docker.com" not in text  # never the convenience script


# ------------------------------------------------------------------------------------------------
# Options, refusals and missing values
# ------------------------------------------------------------------------------------------------


def test_help_exits_zero_and_lists_options(sandbox: Sandbox) -> None:
    done = sandbox.run("--help", env={"FAKE_OS": "Darwin", "FAKE_UID": "501"})
    assert done.code == 0
    for option in (
        "--domain",
        "--email",
        "--admin-email",
        "--dir",
        "--ref",
        "--tls",
        "--no-install-docker",
        "--no-firewall",
        "--no-wait-dns",
        "--yes",
    ):
        assert option in done.out


def test_unknown_option_is_refused(sandbox: Sandbox) -> None:
    done = sandbox.run("--frobnicate")
    assert done.code == 2
    assert "Unknown option: --frobnicate" in done.err


def test_option_without_value_is_refused(sandbox: Sandbox) -> None:
    done = sandbox.run("--domain")
    assert done.code == 2
    assert "--domain needs a value" in done.err


def test_bad_tls_value_is_refused(sandbox: Sandbox) -> None:
    done = sandbox.run("--tls", "magic")
    assert done.code == 2
    assert "auto or internal" in done.err


def test_refuses_macos(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_OS": "Darwin"})
    assert done.code == 1
    assert "Linux" in done.err
    assert "Darwin" in done.err
    assert not sandbox.called("docker")
    assert not sandbox.env_file.exists()


def test_refuses_non_root_and_suggests_sudo(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_UID": "1000"})
    assert done.code == 1
    assert "sudo" in done.err
    assert "must run as root" in done.err
    assert not sandbox.called("apt-get")
    assert not sandbox.env_file.exists()


def test_missing_domain_without_terminal_names_the_flag_before_installing_anything(
    sandbox: Sandbox,
) -> None:
    sandbox.remove("docker")
    done = sandbox.run("--email", "ops@example.com")
    assert done.code == 1
    assert "--domain" in done.err
    assert "no terminal" in done.err
    assert "Step 1 of 10" in done.err
    assert not sandbox.called("apt-get")


def test_missing_email_without_terminal_names_the_flag(sandbox: Sandbox) -> None:
    done = sandbox.run("--domain", "mcp.example.com")
    assert done.code == 1
    assert "--email" in done.err
    assert "no terminal" in done.err


def test_piped_missing_value_shows_the_curl_form(sandbox: Sandbox) -> None:
    done = sandbox.run_piped("--dir", str(sandbox.opt))
    assert done.code == 1
    assert "curl -fsSL" in done.err
    assert "sudo bash -s -- --domain" in done.err


def test_invalid_domain_flag_stops_with_the_reason(sandbox: Sandbox) -> None:
    done = sandbox.run("--domain", "https://mcp.example.com/", "--email", "a@b.co", "--yes")
    assert done.code == 1
    assert "Step 5 of 10" in done.err
    assert "without https://" in done.err
    assert not sandbox.env_file.exists()


def test_invalid_email_flag_stops_with_the_reason(sandbox: Sandbox) -> None:
    done = sandbox.run("--domain", "mcp.example.com", "--email", "not-an-email", "--yes")
    assert done.code == 1
    assert "does not look like an email" in done.err
    assert not sandbox.env_file.exists()


# ------------------------------------------------------------------------------------------------
# Validation helpers
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain",
    [
        "mcp.example.com",
        "a.b",
        "my-host.example.co.uk",
        "pmsbridge.localhost",
        "xn--bcher-kva.example",
        "a1.b2.c3.example",
    ],
)
def test_domain_accepts(sandbox: Sandbox, domain: str) -> None:
    assert sandbox.call(f"domain_error '{domain}'").out == ""


@pytest.mark.parametrize(
    ("domain", "reason"),
    [
        ("", "empty"),
        ("localhost", "at least one dot"),
        ("https://mcp.example.com", "without https://"),
        ("mcp.example.com/path", "without https://"),
        ("mcp.example.com:8443", "without https://"),
        ("user@mcp.example.com", "without https://"),
        ("mcp example.com", "spaces"),
        ("203.0.113.10", "not an IP address"),
        ("-bad.example.com", "hyphens"),
        ("bad-.example.com", "hyphens"),
        ("a..example.com", "hyphens"),
        (".example.com", "hyphens"),
        ("under_score.example.com", "hyphens"),
        ("a" * 64 + ".example.com", "63 characters"),
        ("a." * 130 + "com", "too long"),
    ],
)
def test_domain_rejects(sandbox: Sandbox, domain: str, reason: str) -> None:
    out = sandbox.call(f"domain_error '{domain}'").out
    assert reason in out


@pytest.mark.parametrize(
    "email",
    ["you@example.com", "first.last+tag@sub.example.co", "a_b%c@x-y.org"],
)
def test_email_accepts(sandbox: Sandbox, email: str) -> None:
    assert sandbox.call(f"email_error '{email}'").out == ""


@pytest.mark.parametrize(
    "email",
    ["", "plain", "a@b", "a b@example.com", "a@example..com", "<a>@example.com", "a@@example.com"],
)
def test_email_rejects(sandbox: Sandbox, email: str) -> None:
    assert sandbox.call(f"email_error '{email}'").out != ""


# ------------------------------------------------------------------------------------------------
# Platform detection
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("os_id", "version", "codename", "arch", "supported"),
    [
        ("ubuntu", "22.04", "jammy", "x86_64", "1"),
        ("ubuntu", "24.04", "noble", "aarch64", "1"),
        ("ubuntu", "20.04", "focal", "x86_64", "0"),
        ("debian", "12", "bookworm", "x86_64", "1"),
        ("debian", "13", "trixie", "arm64", "1"),
        ("debian", "11", "bullseye", "x86_64", "0"),
        ("fedora", "40", "", "x86_64", "0"),
        ("ubuntu", "24.04", "noble", "armv7l", "0"),
        ("debian", "", "", "x86_64", "0"),
    ],
)
def test_platform_detection(
    sandbox: Sandbox, os_id: str, version: str, codename: str, *, arch: str, supported: str
) -> None:
    sandbox.set_os(os_id, version, codename, os_id)
    done = sandbox.call(
        'detect_platform; echo "$PLATFORM_SUPPORTED $DISTRO_ID $DISTRO_CODENAME"',
        env={"FAKE_ARCH": arch},
    )
    assert done.out.split()[0] == supported


def test_unsupported_system_without_docker_explains_and_stops(sandbox: Sandbox) -> None:
    sandbox.set_os("fedora", "40", "", "Fedora Linux")
    sandbox.remove("docker")
    done = sandbox.run(*GOOD)
    assert done.code == 1
    assert "Step 1 of 10" in done.err
    assert "not supported" in done.err
    assert "docs.docker.com/engine/install" in done.err
    assert not sandbox.called("apt-get")


def test_unsupported_system_with_docker_continues_with_a_warning(sandbox: Sandbox) -> None:
    sandbox.set_os("fedora", "40", "", "Fedora Linux")
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert "not one pmsbridge is tested on" in done.err
    assert not sandbox.called("apt-get")


# ------------------------------------------------------------------------------------------------
# Docker
# ------------------------------------------------------------------------------------------------


def test_docker_already_present_is_not_reinstalled(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert not sandbox.called("apt-get")
    assert sandbox.called("systemctl enable --now docker")
    assert not sandbox.apt_sources.exists()


def test_docker_is_installed_from_the_official_repository(sandbox: Sandbox) -> None:
    sandbox.remove("docker")
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    listing = (sandbox.apt_sources / "docker.list").read_text()
    assert listing == (
        f"deb [arch=amd64 signed-by={sandbox.keyrings}/docker.asc] "
        "https://download.docker.com/linux/ubuntu noble stable\n"
    )
    assert (sandbox.keyrings / "docker.asc").exists()
    calls = sandbox.call_log()
    assert any(
        "download.docker.com/linux/ubuntu/gpg" in line and line.startswith("curl") for line in calls
    )
    install = next(line for line in calls if line.startswith("apt-get install -y docker-ce"))
    for package in (
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ):
        assert package in install
    assert sandbox.called("systemctl enable --now docker")
    assert not any("get.docker.com" in line for line in calls)


def test_docker_repository_uses_the_debian_path_and_arm_architecture(sandbox: Sandbox) -> None:
    sandbox.set_os("debian", "12", "bookworm", "Debian GNU/Linux")
    sandbox.remove("docker")
    done = sandbox.run(*GOOD, env={"FAKE_ARCH": "aarch64"})
    assert done.code == 0, done.text
    listing = (sandbox.apt_sources / "docker.list").read_text()
    assert "arch=arm64" in listing
    assert "download.docker.com/linux/debian bookworm stable" in listing


def test_only_the_compose_plugin_is_added_when_docker_exists(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_NO_COMPOSE": "1"})
    assert done.code == 0, done.text
    install = next(line for line in sandbox.call_log() if line.startswith("apt-get install -y doc"))
    assert install == "apt-get install -y docker-compose-plugin"


def test_no_install_docker_stops_when_docker_is_missing(sandbox: Sandbox) -> None:
    sandbox.remove("docker")
    done = sandbox.run(*GOOD, "--no-install-docker")
    assert done.code == 1
    assert "Step 2 of 10" in done.err
    assert "--no-install-docker" in done.err
    assert not sandbox.called("apt-get")


def test_failed_docker_install_names_the_step_and_how_to_fix(sandbox: Sandbox) -> None:
    sandbox.remove("docker")
    done = sandbox.run(*GOOD, env={"FAKE_APT_RC": "100"})
    assert done.code == 1
    assert "Step 2 of 10 (Docker) failed" in done.err
    assert "What to do:" in done.err
    assert "run the installer again" in done.err or "run again" in done.err
    assert not sandbox.env_file.exists()


def test_docker_not_running_is_reported(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_DOCKER_INFO_RC": "1"})
    assert done.code == 1
    assert "not running" in done.err
    assert "systemctl enable --now docker" in done.err


# ------------------------------------------------------------------------------------------------
# Firewall
# ------------------------------------------------------------------------------------------------


def test_ufw_active_opens_80_and_443(sandbox: Sandbox) -> None:
    sandbox.add("ufw")
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert sandbox.called("ufw allow 80/tcp")
    assert sandbox.called("ufw allow 443/tcp")
    assert "provider has its own firewall" in done.out


def test_ufw_inactive_is_left_alone(sandbox: Sandbox) -> None:
    sandbox.add("ufw")
    done = sandbox.run(*GOOD, env={"FAKE_UFW": "inactive"})
    assert done.code == 0, done.text
    assert not sandbox.called("ufw allow")
    assert "nothing was changed" in done.out


def test_firewalld_running_gets_http_and_https_permanently(sandbox: Sandbox) -> None:
    sandbox.add("firewall-cmd")
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert sandbox.called("firewall-cmd --permanent --add-service=http --add-service=https")
    assert sandbox.called("firewall-cmd --reload")


def test_firewalld_not_running_is_left_alone(sandbox: Sandbox) -> None:
    sandbox.add("firewall-cmd")
    done = sandbox.run(*GOOD, env={"FAKE_FIREWALLD": "not running"})
    assert done.code == 0, done.text
    assert not sandbox.called("firewall-cmd --permanent")


def test_no_firewall_flag_changes_nothing(sandbox: Sandbox) -> None:
    sandbox.add("ufw")
    sandbox.add("firewall-cmd")
    done = sandbox.run(*GOOD, "--no-firewall")
    assert done.code == 0, done.text
    assert not sandbox.called("ufw")
    assert not sandbox.called("firewall-cmd")
    assert "--no-firewall" in done.out


# ------------------------------------------------------------------------------------------------
# The code
# ------------------------------------------------------------------------------------------------


def test_downloaded_copy_is_used_as_it_is(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert not sandbox.called("git")
    assert f"Using the pmsbridge folder you ran the installer from: {sandbox.site}" in done.out


def test_piped_run_clones_into_the_chosen_folder(sandbox: Sandbox) -> None:
    done = sandbox.run_piped(*GOOD, "--dir", str(sandbox.opt), "--ref", "v0.1.0")
    assert done.code == 0, done.text
    clone = next(line for line in sandbox.call_log() if line.startswith("git clone"))
    assert clone.endswith(f"github.com/faridmostafaei259/pmsbridge.git {sandbox.opt}")
    assert sandbox.called(f"git -C {sandbox.opt} checkout -q v0.1.0")
    env_file = sandbox.opt / "deploy" / ".env"
    assert env_file.exists()
    assert mode_of(env_file) == 0o600


def test_piped_run_installs_git_when_missing(sandbox: Sandbox) -> None:
    sandbox.remove("git")
    done = sandbox.run_piped(*GOOD, "--dir", str(sandbox.opt))
    assert done.code == 0, done.text
    assert sandbox.called("apt-get install -y git")
    assert sandbox.called("git clone")


def test_piped_run_updates_an_existing_download_without_touching_settings(
    sandbox: Sandbox,
) -> None:
    (sandbox.opt / ".git").mkdir(parents=True)
    (sandbox.opt / "deploy").mkdir()
    (sandbox.opt / "deploy" / "compose.yaml").write_text("services: {}\n")
    done = sandbox.run_piped(*GOOD, "--dir", str(sandbox.opt))
    assert done.code == 0, done.text
    assert not sandbox.called("git clone")
    assert sandbox.called(f"git -C {sandbox.opt} fetch --tags --prune origin")
    assert sandbox.called(f"git -C {sandbox.opt} checkout -q -B main origin/main")


def test_piped_run_refuses_a_folder_that_is_not_a_download(sandbox: Sandbox) -> None:
    sandbox.opt.mkdir(parents=True)
    (sandbox.opt / "precious.txt").write_text("keep me")
    done = sandbox.run_piped(*GOOD, "--dir", str(sandbox.opt))
    assert done.code == 1
    assert "Step 4 of 10" in done.err
    assert "is not a pmsbridge download" in done.err
    assert (sandbox.opt / "precious.txt").read_text() == "keep me"
    assert not sandbox.called("git clone")


def test_unknown_version_is_reported(sandbox: Sandbox) -> None:
    done = sandbox.run_piped(
        *GOOD, "--dir", str(sandbox.opt), "--ref", "nope", env={"FAKE_CHECKOUT_RC": "1"}
    )
    assert done.code == 1
    assert "does not exist" in done.err


# ------------------------------------------------------------------------------------------------
# Questions on the terminal
# ------------------------------------------------------------------------------------------------


def test_piped_run_reads_answers_from_the_terminal_not_from_the_script(sandbox: Sandbox) -> None:
    done = sandbox.run_piped(
        "--dir",
        str(sandbox.opt),
        "--no-wait-dns",
        tty_input="Mcp.Example.ORG\nops@example.org\ny\n",
    )
    assert done.code == 0, done.text
    env_file = sandbox.opt / "deploy" / ".env"
    assert env_value(env_file, "DOMAIN") == "mcp.example.org"
    assert env_value(env_file, "PUBLIC_BASE_URL") == "https://mcp.example.org"
    assert env_value(env_file, "ACME_EMAIL") == "ops@example.org"
    assert "Domain name: " in done.err
    assert "Continue? [Y/n]" in done.err


def test_invalid_answers_are_asked_again(sandbox: Sandbox) -> None:
    done = sandbox.run(
        "--no-wait-dns",
        tty_input="localhost\nhttps://x.example.com\nmcp.example.com\nbad\nops@example.com\ny\n",
    )
    assert done.code == 0, done.text
    assert "at least one dot" in done.out
    assert "without https://" in done.out
    assert "does not look like an email" in done.out
    assert env_value(sandbox.env_file, "DOMAIN") == "mcp.example.com"


def test_answering_no_to_the_summary_stops_before_anything_starts(sandbox: Sandbox) -> None:
    done = sandbox.run("--domain", "mcp.example.com", "--email", "ops@example.com", tty_input="n\n")
    assert done.code == 1
    assert "Stopped at your request" in done.err
    assert not sandbox.env_file.exists()
    assert not sandbox.called("docker compose -f")


def test_running_out_of_answers_stops_cleanly(sandbox: Sandbox) -> None:
    done = sandbox.run(tty_input="")
    assert done.code == 1
    assert "no terminal" in done.err or "is needed" in done.err


# ------------------------------------------------------------------------------------------------
# DNS
# ------------------------------------------------------------------------------------------------


def dns(sandbox: Sandbox, env: dict[str, str], *, domain: str = "mcp.example.com") -> str:
    done = sandbox.call(f'DOMAIN={domain}; dns_evaluate; echo "$DNS_STATUS"', env=env)
    return done.out.strip()


def test_dns_match_when_the_name_points_at_a_local_public_address(sandbox: Sandbox) -> None:
    assert dns(
        sandbox, {"FAKE_A": "203.0.113.10", "FAKE_LOCAL_ADDRS": "203.0.113.10 10.0.0.4"}
    ) == ("match")


def test_dns_mismatch_when_the_name_points_elsewhere(sandbox: Sandbox) -> None:
    assert (
        dns(sandbox, {"FAKE_A": "198.51.100.7", "FAKE_LOCAL_ADDRS": "203.0.113.10"}) == "mismatch"
    )


def test_dns_unresolved_when_the_name_has_no_record(sandbox: Sandbox) -> None:
    assert dns(sandbox, {"FAKE_A": "", "FAKE_LOCAL_ADDRS": "203.0.113.10"}) == "unresolved"


def test_dns_nat_when_the_server_only_has_private_addresses(sandbox: Sandbox) -> None:
    status = dns(sandbox, {"FAKE_A": "198.51.100.7", "FAKE_LOCAL_ADDRS": "10.0.0.4 172.17.0.1"})
    assert status == "nat"


def test_dns_ipv6_record_for_another_server_is_a_mismatch(sandbox: Sandbox) -> None:
    status = dns(
        sandbox,
        {
            "FAKE_A": "203.0.113.10",
            "FAKE_AAAA": "2001:db8::99",
            "FAKE_LOCAL_ADDRS": "203.0.113.10",
        },
    )
    assert status == "mismatch"


def test_dns_ipv6_record_for_this_server_matches(sandbox: Sandbox) -> None:
    status = dns(
        sandbox,
        {
            "FAKE_A": "203.0.113.10",
            "FAKE_AAAA": "2001:db8::5",
            "FAKE_LOCAL_ADDRS": "203.0.113.10 2001:db8::5",
        },
    )
    assert status == "match"


@pytest.mark.parametrize(
    ("address", "private"),
    [
        ("10.1.2.3", True),
        ("172.16.0.1", True),
        ("172.31.250.10", True),
        ("172.32.0.1", False),
        ("192.168.1.1", True),
        ("100.64.0.1", True),
        ("169.254.1.1", True),
        ("127.0.0.1", True),
        ("203.0.113.10", False),
        ("8.8.8.8", False),
    ],
)
def test_private_address_detection(sandbox: Sandbox, address: str, private: bool) -> None:
    done = sandbox.call(f"is_private_ipv4 {address} && echo private || echo public")
    assert done.out.strip() == ("private" if private else "public")


def table(sandbox: Sandbox, env: dict[str, str], name: str = "mcp") -> str:
    return sandbox.call(
        f"LOG_FILE=/dev/null; compute_server_addresses; print_record_table '{name}'", env=env
    ).out


def test_record_table_for_a_public_ipv4_address(sandbox: Sandbox) -> None:
    out = table(sandbox, {"FAKE_LOCAL_ADDRS": "10.0.0.4 203.0.113.10"})
    assert "| Type | Name (host) | Value (points to)  | TTL           |" in out
    assert "| A    | mcp         | 203.0.113.10       | Auto (or 300) |" in out
    assert "AAAA" not in out
    assert "<your server's public IP>" not in out
    assert "control panel" not in out
    assert out.count("+------+") == 3
    lines = [line for line in out.splitlines() if line.startswith("    ")]
    assert len({len(line) for line in lines}) == 1, "every table row has the same width"


def test_record_table_with_only_private_addresses_uses_a_placeholder(sandbox: Sandbox) -> None:
    out = table(sandbox, {"FAKE_LOCAL_ADDRS": "10.0.0.4 172.17.0.1 192.168.1.5"})
    assert "<your server's public IP>" in out
    assert "| A    | mcp" in out
    assert "AAAA" not in out
    assert "Use the public IP" in out
    assert "address shown in your hosting provider's control panel" in out


def test_record_table_asks_for_one_a_record_even_with_public_ipv6(sandbox: Sandbox) -> None:
    out = table(sandbox, {"FAKE_LOCAL_ADDRS": "203.0.113.10 2001:db8::5"})
    assert "| A    | mcp         | 203.0.113.10       |" in out
    assert "AAAA" not in out
    assert out.count("+------+") == 3


def test_record_table_ignores_private_loopback_and_link_local_ipv6(sandbox: Sandbox) -> None:
    out = table(sandbox, {"FAKE_LOCAL_ADDRS": "203.0.113.10 fd12:3456::1 fe80::1 ::1"})
    assert "AAAA" not in out


@pytest.mark.parametrize(
    ("addresses", "v4", "v6"),
    [
        ("203.0.113.10", "203.0.113.10", ""),
        ("10.0.0.4 8.8.4.4", "8.8.4.4", ""),
        ("10.0.0.4 169.254.1.1 127.0.0.1 100.64.0.9", "", ""),
        ("2001:db8::5", "", "2001:db8::5"),
        ("10.0.0.4 2001:db8::5 2001:db8::6", "", "2001:db8::5"),
        ("", "", ""),
    ],
)
def test_server_public_addresses_come_from_the_interfaces(
    sandbox: Sandbox, addresses: str, v4: str, v6: str
) -> None:
    done = sandbox.call(
        'compute_server_addresses; echo "$DNS_PUBLIC_V4|$DNS_PUBLIC_V6"',
        env={"FAKE_LOCAL_ADDRS": addresses},
    )
    assert done.out.strip() == f"{v4}|{v6}"


def test_no_address_lookup_service_is_ever_called(sandbox: Sandbox) -> None:
    sandbox.run(
        *GOOD,
        env={"FAKE_A": "198.51.100.7"},
    )
    assert not any(
        "ipify" in line or "ifconfig" in line or "icanhazip" in line for line in sandbox.call_log()
    )


def test_dns_guidance_is_shown_before_the_domain_is_asked_for(sandbox: Sandbox) -> None:
    done = sandbox.run(
        "--email", "ops@example.com", "--no-wait-dns", tty_input="mcp.example.com\ny\n"
    )
    assert done.code == 0, done.text
    guidance = done.out.index("First, decide which web address pmsbridge will have")
    assert guidance < done.out.index("Step 6 of 10")
    assert "subdomain of a domain you already own, for example mcp.yourcompany.com" in done.out
    assert "Your main website keeps working untouched." in done.out
    assert '"Name" is only the subdomain part' in done.out
    assert 'just "mcp"' in done.out
    assert 'full name "mcp.yourcompany.com"' in done.out
    assert "Use an A record." in done.out
    assert "server123.provider.net" in done.out
    assert "Never use a CNAME on your bare main domain" in done.out
    assert "| 203.0.113.10 " in done.out
    assert "a few minutes, sometimes up to an hour" in done.out
    assert "Domain name: " in done.err


def test_dns_guidance_is_not_shown_when_the_domain_is_given_or_for_internal_tls(
    sandbox: Sandbox,
) -> None:
    given = sandbox.run(*GOOD)
    assert "First, decide which web address" not in given.out
    asked = sandbox.run(
        "--email", "ops@example.com", "--yes", "--tls", "internal", tty_input="mcp.example.com\n"
    )
    assert asked.code == 0, asked.text
    assert "First, decide which web address" not in asked.out


def test_guidance_for_a_private_only_server_uses_the_placeholder(sandbox: Sandbox) -> None:
    done = sandbox.run(
        "--email",
        "ops@example.com",
        "--no-wait-dns",
        env={"FAKE_LOCAL_ADDRS": "10.0.0.4", "FAKE_A": "198.51.100.7"},
        tty_input="mcp.example.com\ny\ny\n",
    )
    assert done.code == 0, done.text
    assert "<your server's public IP>" in done.out
    assert "Use the public IP" in done.out


def test_step_six_reprints_the_filled_in_record_with_the_real_name(sandbox: Sandbox) -> None:
    done = sandbox.run(
        "--domain",
        "app.shop.example.org",
        "--email",
        "ops@example.com",
        "--yes",
        "--no-wait-dns",
        env={
            "FAKE_A": "198.51.100.7",
            "FAKE_LOCAL_ADDRS": "203.0.113.10 2001:db8::5",
            "FAKE_AAAA": "2001:db8::99",
        },
    )
    assert done.code == 0, done.text
    assert "The name app.shop.example.org points to: 198.51.100.7 2001:db8::99" in done.out
    assert "| A    | app         | 203.0.113.10       | Auto (or 300) |" in done.out
    assert "| AAAA |" not in done.out
    assert "Delete it: only the" in done.out
    assert 'full name "app.shop.example.org"' in done.out
    assert "a few minutes, sometimes up to an hour" in done.out


def test_dns_check_passes_quietly_when_it_matches(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert "mcp.example.com points at this server." in done.out
    assert not sandbox.called("curl https://api")


def test_dns_mismatch_shows_the_exact_record_and_waits_until_it_appears(sandbox: Sandbox) -> None:
    done = sandbox.run(
        *GOOD,
        env={"FAKE_A": "198.51.100.7", "FAKE_FLIP_AFTER": "2", "FAKE_A_LATER": "203.0.113.10"},
    )
    assert done.code == 0, done.text
    assert "| A    | mcp " in done.out
    assert "| 203.0.113.10 " in done.out
    assert "now points at this server" in done.out
    assert "a few minutes, sometimes up to an hour" in done.out
    assert sandbox.called("sleep 30")


def test_dns_unresolved_shows_the_record_to_create(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, "--no-wait-dns", env={"FAKE_A": ""})
    assert done.code == 0, done.text
    assert "does not point anywhere yet" in done.out
    assert "| 203.0.113.10 " in done.out
    assert "--no-wait-dns" in done.err


def test_dns_that_never_matches_fails_after_the_wait(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_A": "198.51.100.7"})
    assert done.code == 1
    assert "Step 6 of 10" in done.err
    assert "did not point at this server in time" in done.err
    assert not sandbox.env_file.exists()
    assert sum(1 for line in sandbox.call_log() if line.startswith("sleep 30")) == 4


def test_dns_behind_nat_says_it_cannot_confirm_and_continues_with_yes(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_A": "198.51.100.7", "FAKE_LOCAL_ADDRS": "10.0.0.4"})
    assert done.code == 0, done.text
    assert "cannot confirm" in done.out
    assert sandbox.env_file.exists()


def test_dns_behind_nat_asks_and_can_stop(sandbox: Sandbox) -> None:
    done = sandbox.run(
        "--domain",
        "mcp.example.com",
        "--email",
        "ops@example.com",
        env={"FAKE_A": "198.51.100.7", "FAKE_LOCAL_ADDRS": "10.0.0.4"},
        tty_input="y\nn\n",
    )
    assert done.code == 1
    assert "cannot confirm" in done.out
    assert "stopped at your request" in done.err
    assert not sandbox.env_file.exists()


def test_internal_tls_skips_dns_and_warns(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, "--tls", "internal", env={"FAKE_A": ""})
    assert done.code == 0, done.text
    assert "does not need public DNS" in done.out
    assert "testing only" in done.err
    assert env_value(sandbox.env_file, "TLS_MODE") == "internal"


# ------------------------------------------------------------------------------------------------
# Settings file
# ------------------------------------------------------------------------------------------------


def test_settings_file_is_private_complete_and_has_a_valid_key(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    env_file = sandbox.env_file
    assert mode_of(env_file) == 0o600
    assert env_value(env_file, "ENVIRONMENT") == "production"
    assert env_value(env_file, "DOMAIN") == "mcp.example.com"
    assert env_value(env_file, "PUBLIC_BASE_URL") == "https://mcp.example.com"
    assert env_value(env_file, "TLS_MODE") == "auto"
    assert env_value(env_file, "ACME_EMAIL") == "ops@example.com"
    key = env_value(env_file, "ENCRYPTION_KEYS")
    assert key is not None
    assert KEY_LINE.match(key)
    assert key == f"k1:{FAKE_KEY_BASE64}"
    assert parse_encryption_keys(key)[0][0] == "k1"
    assert not list(env_file.parent.glob(".env.*")), "no temporary file is left behind"


def test_real_key_generation_without_openssl_is_valid(sandbox: Sandbox) -> None:
    sandbox.remove("openssl")
    done = sandbox.call("generate_encryption_key")
    key = done.out
    assert KEY_LINE.match(key)
    assert len(parse_encryption_keys(key)) == 1
    assert key != sandbox.call("generate_encryption_key").out


def test_the_key_is_new_when_no_settings_exist_and_the_warning_is_shown_once(
    sandbox: Sandbox,
) -> None:
    first = sandbox.run(*GOOD)
    assert "BACK UP YOUR ENCRYPTION KEY" in first.out
    assert "sudo grep ENCRYPTION_KEYS" in first.out
    assert str(sandbox.env_file) in first.out
    second = sandbox.run(*GOOD)
    assert second.code == 0, second.text
    assert "BACK UP YOUR ENCRYPTION KEY" not in second.out
    assert "Keeping the encryption key" in second.out


def test_the_warning_box_is_ascii_only(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    done.out.encode("ascii")


def test_rerun_keeps_the_key_and_every_other_setting(sandbox: Sandbox) -> None:
    sandbox.make_checkout()
    sandbox.env_file.write_text(
        "# kept comment\n"
        "ENCRYPTION_KEYS=k1:KEEPMEKEEPMEKEEPMEKEEPMEKEEPMEKEEPMEKEEPME=\n"
        "ENVIRONMENT=production\n"
        "DOMAIN=mcp.example.com\n"
        "PUBLIC_BASE_URL=https://mcp.example.com\n"
        "ACME_EMAIL=old@example.com\n"
        "TLS_MODE=auto\n"
        "HTTP_PORT=8080\n"
        "BACKEND_SUBNET=10.99.0.0/24\n"
    )
    sandbox.env_file.chmod(0o640)
    done = sandbox.run("--email", "new@example.com", "--yes")
    assert done.code == 0, done.text
    text = sandbox.env_file.read_text()
    assert "ENCRYPTION_KEYS=k1:KEEPMEKEEPMEKEEPMEKEEPMEKEEPMEKEEPMEKEEPME=\n" in text
    assert text.count("ENCRYPTION_KEYS=") == 1
    assert "# kept comment\n" in text
    assert "HTTP_PORT=8080\n" in text
    assert "BACKEND_SUBNET=10.99.0.0/24\n" in text
    assert "ACME_EMAIL=new@example.com\n" in text
    assert "old@example.com" not in text
    assert "DOMAIN=mcp.example.com\n" in text
    assert mode_of(sandbox.env_file) == 0o600
    assert FAKE_KEY_BASE64 not in text


def test_rerun_with_a_new_domain_updates_only_the_domain_values(sandbox: Sandbox) -> None:
    first = sandbox.run(*GOOD)
    assert first.code == 0, first.text
    key = env_value(sandbox.env_file, "ENCRYPTION_KEYS")
    done = sandbox.run("--domain", "other.example.com", "--yes")
    assert done.code == 0, done.text
    assert env_value(sandbox.env_file, "DOMAIN") == "other.example.com"
    assert env_value(sandbox.env_file, "PUBLIC_BASE_URL") == "https://other.example.com"
    assert env_value(sandbox.env_file, "ACME_EMAIL") == "ops@example.com"
    assert env_value(sandbox.env_file, "ENCRYPTION_KEYS") == key


def test_rerun_without_flags_reuses_the_saved_domain_and_email(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    done = sandbox.run("--yes")
    assert done.code == 0, done.text
    assert "Domain:                mcp.example.com" in done.out
    assert "Email for notices:     ops@example.com" in done.out


def test_existing_data_without_a_key_is_refused_rather_than_given_a_new_key(
    sandbox: Sandbox,
) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_VOLUME_RC": "0"})
    assert done.code == 1
    assert "Step 7 of 10" in done.err
    assert "earlier installation" in done.err
    assert "restore the original deploy/.env" in done.err
    assert not sandbox.env_file.exists()
    assert not sandbox.called("docker compose -f")


def test_existing_data_with_a_key_is_fine(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    done = sandbox.run(*GOOD, env={"FAKE_VOLUME_RC": "0"})
    assert done.code == 0, done.text


def test_env_set_replaces_the_first_match_and_drops_duplicates(sandbox: Sandbox) -> None:
    target = sandbox.root / "sample.env"
    target.write_text("A=1\nB=2\nA=3\n# A=4\nC=x=y\n")
    done = sandbox.call(f"env_set '{target}' A 'new=value'")
    assert done.code == 0, done.text
    assert target.read_text() == "A=new=value\nB=2\n# A=4\nC=x=y\n"
    sandbox.call(f"env_set '{target}' D 5")
    assert target.read_text().endswith("C=x=y\nD=5\n")
    assert mode_of(target) == 0o600


def test_env_get_reads_values_with_equals_signs(sandbox: Sandbox) -> None:
    target = sandbox.root / "sample.env"
    target.write_text("KEY=a=b=c\nOTHER=1\n")
    assert sandbox.call(f"env_get '{target}' KEY").out == "a=b=c"
    assert sandbox.call(f"env_get '{target}' MISSING").out == ""
    assert sandbox.call(f"env_get '{sandbox.root}/none.env' KEY").out == ""


# ------------------------------------------------------------------------------------------------
# Starting, the first administrator, and what never reaches the log
# ------------------------------------------------------------------------------------------------


def test_full_run_issues_the_documented_commands_in_order(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    docker_calls = [line for line in sandbox.call_log() if line.startswith("docker compose")]
    assert docker_calls[0] == "docker compose version"
    assert "docker compose -f deploy/compose.yaml up -d --build --wait" in docker_calls
    assert (
        "docker compose -f deploy/compose.yaml exec -T app python -m pmsbridge.admin "
        "users create-admin --email ops@example.com"
    ) in docker_calls
    up = docker_calls.index("docker compose -f deploy/compose.yaml up -d --build --wait")
    create = next(i for i, line in enumerate(docker_calls) if "create-admin" in line)
    assert up < create
    for number in range(1, 11):
        assert f"Step {number} of 10:" in done.out


def test_health_check_goes_to_this_server_directly(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    health = next(line for line in sandbox.call_log() if "healthz" in line)
    assert "--resolve mcp.example.com:443:127.0.0.1" in health
    assert health.endswith("https://mcp.example.com/healthz")
    assert " -k " not in health


def test_health_check_ignores_the_certificate_only_for_the_internal_mode(
    sandbox: Sandbox,
) -> None:
    sandbox.run(*GOOD, "--tls", "internal")
    health = next(line for line in sandbox.call_log() if "healthz" in line)
    assert " -k " in health


def test_health_check_uses_a_custom_https_port_from_the_settings(sandbox: Sandbox) -> None:
    sandbox.make_checkout()
    sandbox.env_file.write_text("HTTPS_PORT=8443\n")
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    health = next(line for line in sandbox.call_log() if "healthz" in line)
    assert "--resolve mcp.example.com:8443:127.0.0.1" in health
    assert health.endswith("https://mcp.example.com:8443/healthz")


def test_no_third_party_address_lookup_is_made(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    curls = [line for line in sandbox.call_log() if line.startswith("curl")]
    assert curls
    assert all("mcp.example.com" in line or "download.docker.com" in line for line in curls)


def test_the_setup_link_is_printed_but_never_logged(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert FAKE_LINK in done.out
    assert done.out.index("Your one-time setup link") < done.out.index(FAKE_LINK)
    assert FAKE_LINK_TOKEN not in sandbox.log_text()
    assert "setup?token" not in sandbox.log_text()
    assert "Step 9 of 10" in sandbox.log_text()
    for folder in (sandbox.site, sandbox.opt):
        for path in folder.rglob("*"):
            if path.is_file():
                assert FAKE_LINK_TOKEN not in path.read_bytes().decode("utf-8", "replace")
    assert FAKE_LINK_TOKEN not in sandbox.calls.read_text()


def test_the_key_is_never_printed_or_logged(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert done.code == 0, done.text
    assert FAKE_KEY_BASE64 not in done.out
    assert FAKE_KEY_BASE64 not in done.err
    assert FAKE_KEY_BASE64 not in sandbox.log_text()
    assert FAKE_KEY_BASE64 not in "\n".join(sandbox.call_log())
    assert FAKE_KEY_BASE64 in sandbox.env_file.read_text()


def test_the_key_stays_out_of_the_log_on_a_rerun_too(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    sandbox.run(*GOOD)
    assert FAKE_KEY_BASE64 not in sandbox.log_text()
    assert FAKE_LINK_TOKEN not in sandbox.log_text()


def test_log_file_is_private(sandbox: Sandbox) -> None:
    sandbox.run(*GOOD)
    assert mode_of(sandbox.log) == 0o600
    assert "pmsbridge installer started" in sandbox.log_text()


def test_the_audit_line_from_the_admin_command_does_not_reach_the_screen(
    sandbox: Sandbox,
) -> None:
    done = sandbox.run(*GOOD)
    assert '{"time"' not in done.out
    assert '{"time"' not in done.err


def test_finish_text_lists_urls_next_steps_and_commands(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD)
    assert "Dashboard:  https://mcp.example.com" in done.out
    assert "https://mcp.example.com/mcp" in done.out
    assert "two-factor" in done.out
    assert "Guesty connection" in done.out
    assert "docker compose -f deploy/compose.yaml ps" in done.out
    assert "docker compose -f deploy/compose.yaml logs app" in done.out
    assert "docker compose -f deploy/compose.yaml stop" in done.out
    assert "docker compose -f deploy/compose.yaml up -d" in done.out


def test_admin_email_option_is_used_for_the_first_admin(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, "--admin-email", "boss@example.com")
    assert done.code == 0, done.text
    assert sandbox.called("docker compose -f deploy/compose.yaml exec -T app python")
    create = next(line for line in sandbox.call_log() if "create-admin" in line)
    assert create.endswith("--email boss@example.com")
    assert env_value(sandbox.env_file, "ACME_EMAIL") == "ops@example.com"


def test_existing_account_for_the_same_email_gets_the_reset_command(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_ADMIN_MODE": "exists"})
    assert done.code == 0, done.text
    assert "already exists" in done.out
    assert "reset-admin-password --email ops@example.com" in done.out
    assert FAKE_LINK_TOKEN not in done.out


def test_another_existing_administrator_means_no_second_one_is_created(sandbox: Sandbox) -> None:
    listing = (
        "EMAIL ROLE STATUS TWO-FACTOR LAST SIGN-IN\nboss@example.com  admin  active  on  never"
    )
    done = sandbox.run(*GOOD, env={"FAKE_USERS_LIST": listing})
    assert done.code == 0, done.text
    assert "An administrator already exists" in done.out
    assert not any("create-admin" in line for line in sandbox.call_log())
    assert "reset-admin-password" in done.out


def test_pending_administrator_for_the_same_email_gets_a_fresh_link(sandbox: Sandbox) -> None:
    listing = (
        "EMAIL ROLE STATUS TWO-FACTOR LAST SIGN-IN\nOps@Example.com  admin  pending  off  never"
    )
    done = sandbox.run(*GOOD, env={"FAKE_USERS_LIST": listing})
    assert done.code == 0, done.text
    assert FAKE_LINK in done.out


def test_admin_command_failure_stops_with_the_step_and_message(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_ADMIN_MODE": "broken"})
    assert done.code == 1
    assert "Step 9 of 10" in done.err
    assert "the database could not be used" in done.err
    assert FAKE_LINK_TOKEN not in sandbox.log_text()


def test_admin_command_without_a_link_is_a_failure(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_ADMIN_MODE": "nolink"})
    assert done.code == 1
    assert "did not return a setup link" in done.err
    assert "no link here" not in sandbox.log_text()


def test_compose_failure_shows_logs_and_hints(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_UP_RC": "1"})
    assert done.code == 1
    assert "Step 8 of 10 (Start pmsbridge) failed" in done.err
    assert "last 30 lines of the app log" in done.out
    assert "last 30 lines of the caddy log" in done.out
    assert "port is already in use" in done.err
    assert "run the installer again" in done.err
    assert "--tail 30 app" in " ".join(sandbox.call_log())


def test_health_timeout_in_auto_mode_points_at_dns_and_ports(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_HEALTH": "down"})
    assert done.code == 1
    assert "Step 8 of 10" in done.err
    assert "certificate" in done.err
    assert "ports 80 and 443" in done.err
    assert "last 30 lines of the caddy log" in done.out


def test_every_failure_message_names_the_log_and_says_rerun_is_safe(sandbox: Sandbox) -> None:
    done = sandbox.run(*GOOD, env={"FAKE_UP_RC": "1"})
    assert f"Details are in {sandbox.log}" in done.err
    assert "safe to run again" in done.err


def test_unwritable_log_does_not_stop_the_install(sandbox: Sandbox) -> None:
    done = sandbox.run(
        *GOOD, env={"PMSBRIDGE_INSTALL_LOG": str(sandbox.root / "no" / "dir" / "x.log")}
    )
    assert done.code == 0, done.text
    assert "cannot write the log file" in done.err


# ------------------------------------------------------------------------------------------------
# The script against the real deploy files
# ------------------------------------------------------------------------------------------------

SCRIPT = INSTALL_SH.read_text()
COMPOSE = yaml.safe_load((REPO_ROOT / "deploy" / "compose.yaml").read_text())


def test_compose_file_and_services_the_script_uses_exist() -> None:
    assert "-f deploy/compose.yaml" in SCRIPT
    assert (REPO_ROOT / "deploy" / "compose.yaml").exists()
    assert (REPO_ROOT / "Dockerfile").exists()
    assert {"app", "caddy"} <= set(COMPOSE["services"])
    assert "exec -T app python -m pmsbridge.admin users" in SCRIPT


def test_data_volume_name_matches_the_compose_project_and_volume() -> None:
    assert COMPOSE["name"] == "pmsbridge"
    assert "app_data" in COMPOSE["volumes"]
    assert "pmsbridge_app_data" in SCRIPT


def test_settings_file_location_matches_the_compose_env_file() -> None:
    env_file = COMPOSE["services"]["app"]["env_file"]
    assert env_file == "${PMSBRIDGE_ENV_FILE:-.env}"
    assert '"$INSTALL_DIR/deploy/.env"' in SCRIPT


@pytest.mark.parametrize(
    "variable",
    ["ENVIRONMENT", "DOMAIN", "PUBLIC_BASE_URL", "TLS_MODE", "ACME_EMAIL", "ENCRYPTION_KEYS"],
)
def test_every_variable_the_script_writes_is_documented(variable: str) -> None:
    assert re.search(rf'env_set\w* "\$ENV_FILE" {variable}\b', SCRIPT)
    assert variable in (REPO_ROOT / "deploy" / "env.example").read_text()
    assert variable in (REPO_ROOT / "docs" / "configuration.md").read_text()


def test_script_option_help_matches_the_admin_commands_that_exist() -> None:
    users = (REPO_ROOT / "src" / "pmsbridge" / "admin" / "users.py").read_text()
    for action in ("create-admin", "reset-admin-password", '"list"'):
        assert action in users


def test_service_health_route_the_script_polls_exists() -> None:
    app = (REPO_ROOT / "src" / "pmsbridge" / "app.py").read_text()
    assert '"/healthz"' in app
    assert '\'"status"' not in app
    assert '{"status": "ok"}' in app
