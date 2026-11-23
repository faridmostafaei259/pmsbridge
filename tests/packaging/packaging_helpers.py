"""Helpers for the installer tests: a sandbox with stub commands in front of the real PATH.

The installer is run in a child bash with a PATH that starts with a directory of stubs (docker, git,
apt-get, ufw, curl and so on). Each stub appends its command line to a call log and answers from the
FAKE_* environment variables, so the tests can see what the installer asked the system to do.
Nothing here touches the real Docker, the network or the real /etc.
"""

import base64
import shutil
import subprocess
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"

# What the stubs print, so the tests can look for it in the output and the log.
FAKE_KEY_BASE64 = base64.b64encode(bytes(range(32))).decode()
FAKE_LINK_TOKEN = "SECRET-TOKEN-ABC123"  # noqa: S105  (a fake value)
FAKE_LINK = f"https://mcp.example.com/setup?token={FAKE_LINK_TOKEN}"

# The real tools the installer may use. Only these are visible to it (next to the stubs), so a test
# that removes a stub such as docker or git really makes that command missing.
REAL_TOOLS = [
    "awk",
    "sed",
    "grep",
    "tr",
    "date",
    "mktemp",
    "sort",
    "head",
    "tail",
    "tee",
    "ls",
    "df",
    "dirname",
    "cat",
    "chmod",
    "mv",
    "rm",
    "mkdir",
    "install",
    "base64",
    "cp",
    "touch",
    "env",
    "bash",
    "wc",
    "cut",
]

STUBS: dict[str, str] = {
    "uname": """
        case "$1" in
          -m) echo "${FAKE_ARCH:-x86_64}" ;;
          *) echo "${FAKE_OS:-Linux}" ;;
        esac
    """,
    "id": 'echo "${FAKE_UID:-0}"',
    "chown": "exit 0",
    "sleep": "exit 0",
    "systemctl": "exit ${FAKE_SYSTEMCTL_RC:-0}",
    "openssl": f'echo "{FAKE_KEY_BASE64}"',
    "getent": """
        fam="$1"
        if [ "$fam" = ahostsv4 ]; then
          n="$(cat "$STUB_STATE/getent_n" 2>/dev/null || echo 0)"
          n=$((n + 1))
          echo "$n" >"$STUB_STATE/getent_n"
          list="${FAKE_A:-}"
          if [ -n "${FAKE_FLIP_AFTER:-}" ] && [ "$n" -gt "$FAKE_FLIP_AFTER" ]; then
            list="${FAKE_A_LATER:-}"
          fi
        else
          list="${FAKE_AAAA:-}"
        fi
        [ -n "$list" ] || exit 2
        for a in $list; do
          printf '%s STREAM %s\\n%s DGRAM\\n%s RAW\\n' "$a" "$2" "$a" "$a"
        done
    """,
    "ip": """
        i=2
        for a in ${FAKE_LOCAL_ADDRS:-}; do
          case "$a" in
            *:*) echo "$i: eth0    inet6 $a/64 scope global eth0" ;;
            *) echo "$i: eth0    inet $a/24 brd 255.255.255.255 scope global eth0" ;;
          esac
          i=$((i + 1))
        done
    """,
    "curl": """
        prev=""
        for a in "$@"; do
          if [ "$prev" = -o ]; then
            echo "fake key file" >"$a"
            exit 0
          fi
          prev="$a"
        done
        if [ "${FAKE_HEALTH:-ok}" = ok ]; then
          echo '{"status":"ok"}'
        else
          exit 7
        fi
    """,
    "git": """
        case " $* " in
          *" clone "*)
            dest="${@: -1}"
            mkdir -p "$dest/.git" "$dest/deploy"
            touch "$dest/deploy/compose.yaml" "$dest/Dockerfile"
            exit "${FAKE_CLONE_RC:-0}"
            ;;
          *" rev-parse --verify "*) exit "${FAKE_GIT_BRANCH_RC:-0}" ;;
          *" checkout "*) exit "${FAKE_CHECKOUT_RC:-0}" ;;
        esac
        exit "${FAKE_GIT_RC:-0}"
    """,
    "apt-get": """
        case " $* " in
          *docker-ce*)
            cp "$STUB_STATE/docker.stub" "$STUB_BIN/docker"
            touch "$STUB_STATE/compose_installed"
            ;;
          *docker-compose-plugin*) touch "$STUB_STATE/compose_installed" ;;
          *" git "*) cp "$STUB_STATE/git.stub" "$STUB_BIN/git" ;;
        esac
        exit "${FAKE_APT_RC:-0}"
    """,
    "ufw": """
        if [ "$1" = status ]; then
          echo "Status: ${FAKE_UFW:-active}"
        fi
        exit 0
    """,
    "firewall-cmd": """
        if [ "$1" = --state ]; then
          echo "${FAKE_FIREWALLD:-running}"
          [ "${FAKE_FIREWALLD:-running}" = running ] || exit 252
        fi
        exit 0
    """,
    "docker": f"""
        case "$1" in
          info) exit "${{FAKE_DOCKER_INFO_RC:-0}}" ;;
          volume) exit "${{FAKE_VOLUME_RC:-1}}" ;;
          compose) ;;
          *) exit 0 ;;
        esac
        shift
        if [ "$1" = version ]; then
          if [ -n "${{FAKE_NO_COMPOSE:-}}" ] && [ ! -e "$STUB_STATE/compose_installed" ]; then
            exit 1
          fi
          exit 0
        fi
        if [ "$1" = -f ]; then shift 2; fi
        case "$1" in
          up)
            echo "fake compose up output"
            exit "${{FAKE_UP_RC:-0}}"
            ;;
          logs)
            echo "fake log line from $*"
            exit 0
            ;;
          exec)
            case "$*" in
              *"users list"*)
                echo "${{FAKE_USERS_LIST:-There are no users yet.}}"
                exit 0
                ;;
              *create-admin*)
                echo '{{"time": "now", "message": "admin users create-admin"}}' >&2
                case "${{FAKE_ADMIN_MODE:-link}}" in
                  link) echo "{FAKE_LINK}" ;;
                  nolink) echo "no link here" ;;
                  exists)
                    echo "Error: A user with this email address already exists." >&2
                    exit 1
                    ;;
                  broken)
                    echo "Error: the database could not be used." >&2
                    exit 2
                    ;;
                esac
                exit 0
                ;;
            esac
            ;;
        esac
        exit 0
    """,
}

DEFAULT_STUBS = (
    "uname",
    "id",
    "chown",
    "sleep",
    "systemctl",
    "openssl",
    "getent",
    "ip",
    "curl",
    "git",
    "apt-get",
    "docker",
)


@dataclass
class Result:
    code: int
    out: str
    err: str
    sandbox: "Sandbox"

    @property
    def text(self) -> str:
        return self.out + self.err


class Sandbox:
    """A temporary world for one installer run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.state = root / "state"
        self.calls = root / "calls.log"
        self.log = root / "install.log"
        self.opt = root / "opt" / "pmsbridge"
        self.site = root / "site"
        self.keyrings = root / "keyrings"
        self.apt_sources = root / "apt-sources"
        self.os_release = root / "os-release"
        self.no_tty = root / "no-such-tty"
        self.tools = root / "tools"
        self.bin.mkdir()
        self.tools.mkdir()
        for tool in REAL_TOOLS:
            real = shutil.which(tool)
            assert real is not None, f"{tool} is needed to run the installer tests"
            (self.tools / tool).symlink_to(real)
        self.state.mkdir()
        self.calls.touch()
        self.set_os("ubuntu", "24.04", "noble", "Ubuntu")
        for name in DEFAULT_STUBS:
            self.add(name)
        for name in ("docker", "git"):
            stub = self.state / f"{name}.stub"
            stub.write_text(self._script(name))
            stub.chmod(0o755)

    def _script(self, name: str) -> str:
        body = textwrap.dedent(STUBS[name]).strip("\n")
        return f'#!/usr/bin/env bash\necho "{name} $*" >>"$STUB_CALLS"\n{body}\n'

    def add(self, name: str) -> None:
        path = self.bin / name
        path.write_text(self._script(name))
        path.chmod(0o755)

    def remove(self, name: str) -> None:
        (self.bin / name).unlink()

    def set_os(self, os_id: str, version: str, codename: str, name: str) -> None:
        lines = [f"ID={os_id}", f'NAME="{name}"', f'VERSION_ID="{version}"']
        if codename:
            lines.append(f"VERSION_CODENAME={codename}")
        self.os_release.write_text("\n".join(lines) + "\n")

    def make_checkout(self) -> Path:
        """A downloaded copy: install.sh next to deploy/compose.yaml and a Dockerfile."""
        (self.site / "deploy").mkdir(parents=True, exist_ok=True)
        (self.site / "deploy" / "compose.yaml").write_text("services: {}\n")
        (self.site / "Dockerfile").write_text("FROM scratch\n")
        shutil.copy(INSTALL_SH, self.site / "install.sh")
        return self.site

    @property
    def env_file(self) -> Path:
        return self.site / "deploy" / ".env"

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{self.tools}",
            "HOME": str(self.root),
            "LC_ALL": "C",
            "STUB_CALLS": str(self.calls),
            "STUB_STATE": str(self.state),
            "STUB_BIN": str(self.bin),
            "PMSBRIDGE_INSTALL_LOG": str(self.log),
            "PMSBRIDGE_OS_RELEASE": str(self.os_release),
            "PMSBRIDGE_TTY": str(self.no_tty),
            "PMSBRIDGE_KEYRING_DIR": str(self.keyrings),
            "PMSBRIDGE_APT_SOURCES_DIR": str(self.apt_sources),
            "PMSBRIDGE_DNS_WAIT_SECONDS": "120",
            "PMSBRIDGE_DNS_POLL_SECONDS": "30",
            "PMSBRIDGE_HEALTH_WAIT_SECONDS": "10",
            "PMSBRIDGE_HEALTH_POLL_SECONDS": "5",
            "FAKE_LOCAL_ADDRS": "203.0.113.10",
            "FAKE_A": "203.0.113.10",
        }
        if extra:
            env.update(extra)
        return env

    def _bash(self) -> str:
        bash = shutil.which("bash")
        assert bash is not None
        return bash

    def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        tty_input: str | None = None,
    ) -> Result:
        """Run the installer as a downloaded copy (`bash install.sh ...`)."""
        site = self.make_checkout()
        return self._finish(
            subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                [self._bash(), str(site / "install.sh"), *args],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=self.root,
                env=self.env(self._with_tty(env, tty_input)),
                stdin=subprocess.DEVNULL,
            )
        )

    def run_piped(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        tty_input: str | None = None,
    ) -> Result:
        """Run the installer the way `curl ... | sudo bash -s -- ...` does (script on stdin)."""
        return self._finish(
            subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                [self._bash(), "-s", "--", *args],
                input=INSTALL_SH.read_text(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=self.root,
                env=self.env(self._with_tty(env, tty_input)),
            )
        )

    def call(self, snippet: str, env: Mapping[str, str] | None = None) -> Result:
        """Load install.sh without running it, then run a snippet of bash."""
        script = f"source {INSTALL_SH}\n{snippet}\n"
        return self._finish(
            subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                [self._bash(), "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=self.root,
                env=self.env(env),
                stdin=subprocess.DEVNULL,
            )
        )

    def _with_tty(self, env: Mapping[str, str] | None, tty_input: str | None) -> dict[str, str]:
        merged = dict(env or {})
        if tty_input is not None:
            answers = self.root / "tty-answers"
            answers.write_text(tty_input)
            merged["PMSBRIDGE_TTY"] = str(answers)
        return merged

    def _finish(self, done: "subprocess.CompletedProcess[str]") -> Result:
        return Result(done.returncode, done.stdout, done.stderr, self)

    def call_log(self) -> list[str]:
        return self.calls.read_text().splitlines()

    def called(self, prefix: str) -> bool:
        return any(line.startswith(prefix) for line in self.call_log())

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


def env_value(path: Path, key: str) -> str | None:
    for line in path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None
