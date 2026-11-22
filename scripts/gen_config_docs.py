"""Generate docs/configuration.md from the Settings model and the stack variables table.

Usage:
    python scripts/gen_config_docs.py           # write docs/configuration.md
    python scripts/gen_config_docs.py --check   # exit 1 if the file is missing or out of date

The application settings come from `pmsbridge.core.settings.Settings` (name, type, default and
description). The variables that only the Docker Compose stack reads are listed in STACK_VARIABLES
below; a test checks that every variable used in deploy/compose.yaml is documented.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import SecretStr

from pmsbridge.core.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "configuration.md"


@dataclass(frozen=True)
class StackVariable:
    name: str
    default: str
    required: str
    description: str


# Variables read by deploy/compose.yaml or Caddy rather than by the application settings.
STACK_VARIABLES: tuple[StackVariable, ...] = (
    StackVariable(
        "TLS_MODE",
        "`auto`",
        "no",
        "`auto` gets a real certificate for your domain. `internal` is for testing only. "
        "See [TLS and domains](tls-and-domains.md).",
    ),
    StackVariable(
        "ACME_EMAIL",
        "none",
        "yes, when `TLS_MODE=auto`",
        "An address the certificate authority can contact about your certificate.",
    ),
    StackVariable(
        "ACME_CA",
        "`production`",
        "no",
        "`production` or `staging`. Use `staging` only while experimenting, to avoid certificate "
        "limits.",
    ),
    StackVariable(
        "HTTP_PORT",
        "`80`",
        "no",
        "The server port that receives plain `http://` visits (they are redirected to HTTPS). "
        "Change it only for testing.",
    ),
    StackVariable(
        "HTTPS_PORT",
        "`443`",
        "no",
        "The server port that receives `https://` visits. Change it only for testing.",
    ),
    StackVariable(
        "TRUSTED_PROXIES",
        "Caddy's address on the private network",
        "no",
        "The addresses the application believes when they say who the visitor is. Leave it alone "
        "unless you know why you need to change it.",
    ),
    StackVariable(
        "BACKEND_SUBNET",
        "`172.31.250.0/24`",
        "no",
        "The private network between Caddy and the application. Change it only if it clashes with "
        "a network you use.",
    ),
    StackVariable(
        "CADDY_BACKEND_IP",
        "`172.31.250.10`",
        "no",
        "Caddy's fixed address on that private network. It must lie inside `BACKEND_SUBNET`.",
    ),
    StackVariable(
        "PMSBRIDGE_IMAGE",
        "`pmsbridge:local`",
        "no",
        "Which application image to run. The installer and the update command set this.",
    ),
    StackVariable(
        "PMSBRIDGE_CADDY_IMAGE",
        "`pmsbridge-caddy:local`",
        "no",
        "Which Caddy image to run. The installer and the update command set this.",
    ),
    StackVariable(
        "PMSBRIDGE_ENV_FILE",
        "`.env`",
        "no",
        "The settings file passed to the application. Only automated tests change it.",
    ),
)


PLAIN_TYPE_NAMES: dict[object, str] = {
    str: "text",
    int: "whole number",
    bool: "yes or no",
    Path: "path",
    SecretStr: "secret text",
}


def _type_name(annotation: object) -> str:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _type_name(get_args(annotation)[0])
    if origin is Literal:
        return "one of " + ", ".join(f"`{value}`" for value in get_args(annotation))
    if origin in (Union, types.UnionType):
        return " or ".join(_type_name(arg) for arg in get_args(annotation) if arg is not type(None))
    return PLAIN_TYPE_NAMES.get(annotation, str(getattr(annotation, "__name__", annotation)))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _setting_rows() -> list[str]:
    rows: list[str] = []
    for name, field in Settings.model_fields.items():
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        default = extra.get("default_text")
        if default is None:
            default = (
                "none"
                if field.is_required()
                else f"`{field.get_default(call_default_factory=False)}`"
            )
        required = "yes" if extra.get("required_in_production") else "no"
        rows.append(
            f"| `{name.upper()}` | {_cell(_type_name(field.annotation))} | {_cell(str(default))} "
            f"| {required} | {_cell(field.description or '')} |"
        )
    return rows


def _paragraph(*parts: str) -> str:
    return textwrap.fill(" ".join(parts), width=100)


def render() -> str:
    sections = [
        "<!-- Generated by scripts/gen_config_docs.py. Do not edit by hand. -->",
        "# Configuration",
        _paragraph(
            "pmsbridge is configured with environment variables. You set them in a file named",
            "`.env` next to `deploy/compose.yaml`; the installer creates that file for you. The",
            "application reads nothing else: there is no other configuration file and no web page",
            "for these settings. Never commit the `.env` file or share it: it holds your",
            "encryption keys.",
        ),
        "After you change `.env`, restart the stack so the change takes effect:",
        "```\ndocker compose -f deploy/compose.yaml up -d\n```",
        _paragraph(
            "If a setting is wrong, the application stops at start-up and the logs name the",
            "setting and say what is wrong. Look at them with",
            "`docker compose -f deploy/compose.yaml logs app`.",
        ),
        "## Application settings",
        _paragraph(
            "In production (the default), `PUBLIC_BASE_URL` must start with `https://` and its",
            "host must be exactly `DOMAIN`. With `ENVIRONMENT=development`, both may be left",
            "out and default to a local address.",
        ),
        "\n".join(
            [
                "| Variable | Type | Default | Required in production | What it does |",
                "|---|---|---|---|---|",
                *_setting_rows(),
            ]
        ),
        "## Your encryption keys",
        _paragraph(
            "`ENCRYPTION_KEYS` protects the PMS credentials and tokens that pmsbridge stores.",
            "The installer generates it. Back it up somewhere safe, away from the server (for",
            "example in a password manager). If you lose it, the stored credentials cannot be",
            "recovered and you would have to enter them again. Never share it.",
        ),
        _paragraph(
            "Until the installer creates it for you, create the key yourself, once, with this",
            "command (it adds one line to `deploy/.env`):",
        ),
        "```\n(umask 077; printf 'ENCRYPTION_KEYS=k1:%s\\n' \\\n"
        '  "$(openssl rand -base64 32)" >> deploy/.env)\n```',
        _paragraph(
            "Then save that line in a password manager. `k1` is just a short name for the key",
            "(letters and digits only). The `umask` makes the file readable only by you.",
        ),
        _paragraph(
            "Commands to replace the key (add a new one, re-encrypt everything stored, then retire",
            "the old one) arrive in a later release, together with the `pmsbridge rotate-keys`",
            "host command. Until then, keep the single key you created. Before any PMS",
            "credentials are stored there is nothing to re-encrypt, so there is nothing to lose",
            "yet.",
        ),
        _paragraph(
            "Keep each retired key line for as long as you keep backups made before a rotation.",
            "To restore a backup made under an older key, edit the existing `ENCRYPTION_KEYS` line",
            "in `deploy/.env` so that it lists the active key first and the retired entry after",
            "it, separated by a comma. Copy the retired entry's `<name>:<key>` unchanged: the name",
            "is how the application finds the key that decrypts each stored value. Never add a",
            "second `ENCRYPTION_KEYS` line: only one of them is used.",
        ),
        "```\nENCRYPTION_KEYS=k2:<new key>,k1:<old key>\n```",
        _paragraph(
            "When rotation is available, a new key is made the same way as the first one, with a",
            "new name; it is just 32 random bytes in base64:",
        ),
        "```\nopenssl rand -base64 32\n```",
        "## Stack settings",
        _paragraph(
            "These are read by the Docker Compose stack and Caddy, not by the application itself.",
            "`DOMAIN` (above) is read by both.",
        ),
        "\n".join(
            [
                "| Variable | Default | Required | What it does |",
                "|---|---|---|---|",
                *(
                    f"| `{item.name}` | {_cell(item.default)} | {_cell(item.required)} "
                    f"| {_cell(item.description)} |"
                    for item in STACK_VARIABLES
                ),
            ]
        ),
        "## If the application stops after an update",
        _paragraph(
            "When the application is updated, it upgrades its database when it starts. If that",
            "cannot be done, the application stops and the container restarts again and again",
            "(`docker compose -f deploy/compose.yaml ps` shows it restarting). Nothing is lost:",
            "the upgrade is rolled back, so the database is left exactly as it was, and the log",
            "(`docker compose -f deploy/compose.yaml logs app`) says what went wrong. A message",
            "saying the database was created by a newer version means you started an older",
            "version than the one that last ran.",
        ),
        _paragraph(
            "The way out is to go back to the previous version of pmsbridge and start it. If you",
            "need to restore the data as well, use the backup the application took just before the",
            "upgrade. Backups are in `BACKUP_DIR` (`/data/backups` in the container) and are named",
            "`pre-migration-<date and time>-<number>-from-<version>.db`. The newest one is the one",
            "taken for the failed upgrade. To restore one:",
        ),
        "1. Stop the stack: `docker compose -f deploy/compose.yaml stop`.",
        _paragraph(
            "2. With the stack stopped, copy the backup over the database file (use the real",
            "file name from `/data/backups`):",
        ),
        "\n".join(
            [
                "```",
                "docker compose -f deploy/compose.yaml run --rm --no-deps --entrypoint sh app \\",
                "  -c 'cp /data/backups/BACKUP_FILE /data/pmsbridge.db \\",
                "      && rm -f /data/pmsbridge.db-wal /data/pmsbridge.db-shm'",
                "```",
            ]
        ),
        "3. Make sure the previous version of pmsbridge is the one that will start, then run "
        "`docker compose -f deploy/compose.yaml up -d`.",
        "## Where data is stored",
        _paragraph(
            "Inside the container everything lives under `DATA_DIR` (`/data`), which is a Docker",
            "volume, so it survives restarts and updates. The database file and the backups are",
            "readable only by the application's own user. Before each database upgrade the",
            "application takes a backup of the database into `BACKUP_DIR` and keeps the newest",
            "`PRE_MIGRATION_BACKUPS_KEEP` of them.",
        ),
    ]
    return "\n\n".join(sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/configuration.md.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    args = parser.parse_args(argv)
    content = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else None
        if current != content:
            sys.stderr.write(
                f"{OUTPUT.relative_to(ROOT)} is out of date. "
                "Run: uv run python scripts/gen_config_docs.py\n"
            )
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    sys.stdout.write(f"Wrote {OUTPUT.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
