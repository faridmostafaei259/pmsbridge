"""Structured JSON logging that never writes secrets or personal data.

`configure_logging(settings)` replaces the handlers of the root logger with one handler that
writes one JSON object per line to standard output: `time`, `level`, `logger`, `message`,
`request_id` when a request is being handled, any extra fields passed with
`logger.info("...", extra={...})`, and `exception` when an error is logged with its traceback.

Redaction has two layers, both applied by `RedactionFilter`:

* Values stored under a sensitive key (password, secret, token, authorization, api key, cookie,
  email, phone and similar, matched case-insensitively as a part of the key name, at any depth)
  are replaced with `[redacted]`.
* In free text (the message and the traceback), `name=value` and `"name": "value"` pairs for those
  same keys, `Bearer` tokens and e-mail addresses are masked. This is a safety net: do not rely on
  it, and do not put secrets or guest details in log messages.

Naming rule: because a part of the key name is enough, a key such as `token_count` is masked too.
That errs on the side of hiding too much. A value that is not secret but has such a word in its
name has two options: rename the key (for example `quota_remaining`), or add its exact name to
`SAFE_KEYS` below (a change that needs review). Never add a key that could hold a secret.
"""

import json
import logging
import re
import sys
from collections.abc import Collection, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final, cast

from pydantic import SecretBytes, SecretStr

from pmsbridge.core.settings import Settings

REDACTED: Final = "[redacted]"

# The request id of the request being handled. The web layer sets it for the duration of a request.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Parts of key names that mark a value as sensitive. Compared after lowercasing and removing
# "_", "-" and spaces, so `API_KEY`, `api-key` and `apiKey` all match "apikey".
SENSITIVE_KEY_PARTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "authorization",
    "apikey",
    "cookie",
    "email",
    "phone",
    "credential",
    "privatekey",
    "encryptionkey",
)

# Exact key names that are never masked, although they contain a sensitive word. They carry counters
# for the Guesty token budget, not secrets. Compared after the same normalisation as above.
SAFE_KEYS: Final = frozenset(
    {"token_budget_remaining", "token_budget_reset_seconds", "token_requests_today"}
)

MAX_DEPTH: Final = 8
MAX_TEXT_LENGTH: Final = 8000

# Attributes every LogRecord has. Anything else on a record was passed in `extra`.
_STANDARD_RECORD_KEYS: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

_KEY_PATTERN: Final = (
    r"(?:password|passwd|passphrase|secret|token|authorization|api[_-]?key|cookie|e-?mail|phone"
    r"|credential|private[_-]?key|encryption[_-]?keys?)"
)
_QUOTED_OR_BARE: Final = r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\"'&}\]]+)"
# name=value, name: value and "name": "value" (a quoted value may contain spaces), where the name
# may carry a prefix or suffix such as client_secret or access_token. A Bearer or Basic scheme
# word is kept out of the masked value. Text that is already masked is left alone.
_PAIR_RE: Final = re.compile(
    r"(?P<name>[\w.-]{0,40}" + _KEY_PATTERN + r"[\w.-]{0,40})"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?"
    r"(?!\[redacted\])(?P<value>" + _QUOTED_OR_BARE + r")",
    re.IGNORECASE,
)
# Cookie, Set-Cookie and Authorization values hold several parts (`a=1; session=xyz`, `Bearer x`),
# so everything to the end of the line is masked.
_LINE_RE: Final = re.compile(
    r"(?P<name>[\w.-]{0,40}(?:cookie|authorization))(?P<sep>[\"']?\s*[:=]\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
# An `ENCRYPTION_KEYS` entry (`<id>:<32 bytes in base64>`) is masked wherever it appears, so every
# entry of a comma-separated list is covered, with or without spaces around the commas.
_KEY_ENTRY_RE: Final = re.compile(r"(?<![\w+/])[a-z0-9]{1,16}:[A-Za-z0-9+/]{43}=")
_BEARER_RE: Final = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


_SAFE_NORMALISED: Final = frozenset(re.sub(r"[\s_-]", "", key) for key in SAFE_KEYS)


def _mask_pair(match: re.Match[str]) -> str:
    if not is_sensitive_key(match["name"]):
        return match[0]
    return f"{match['name']}{match['sep']}{REDACTED}"


def is_sensitive_key(key: str) -> bool:
    """True when a key name marks its value as secret or personal."""
    normalised = re.sub(r"[\s_-]", "", key.lower())
    if normalised in _SAFE_NORMALISED:
        return False
    return any(part in normalised for part in SENSITIVE_KEY_PARTS)


def scrub_text(text: str) -> str:
    """Mask secrets and e-mail addresses in free text, and cap its length."""
    truncated = len(text) > MAX_TEXT_LENGTH
    if truncated:
        text = text[:MAX_TEXT_LENGTH]
    text = _KEY_ENTRY_RE.sub(REDACTED, text)
    text = _LINE_RE.sub(_mask_pair, text)
    text = _PAIR_RE.sub(_mask_pair, text)
    text = _BEARER_RE.sub(lambda m: f"{m[1]} {REDACTED}", text)
    text = _EMAIL_RE.sub(REDACTED, text)
    return text + "...[truncated]" if truncated else text


def redact(value: object, *, _depth: int = 0) -> object:
    """Return a copy of `value` that is safe to log: sensitive keys masked, nested data walked."""
    result: object
    if _depth > MAX_DEPTH:
        result = "[too deep]"
    elif value is None or isinstance(value, (bool, int, float)):
        result = value
    elif isinstance(value, (SecretStr, SecretBytes)):
        result = REDACTED
    elif isinstance(value, bytes):
        result = f"[{len(value)} bytes]"
    elif isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        result = {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact(item, _depth=_depth + 1)
            for key, item in mapping.items()
        }
    elif isinstance(value, (list, tuple, set, frozenset)):
        sequence = cast("Collection[object]", value)
        result = [redact(item, _depth=_depth + 1) for item in sequence]
    else:
        result = scrub_text(str(value))
    return result


class RedactionFilter(logging.Filter):
    """Masks secrets in the message, the extra fields and the traceback of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = scrub_text(record.getMessage())
        except (TypeError, ValueError, KeyError, IndexError):
            # A bad format string or argument list must not break (or leak through) logging.
            message = scrub_text(str(record.msg)) + " [unformattable args]"
        record.msg = message
        record.args = None
        if record.exc_info and record.exc_text is None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_text(record.stack_info)
        for key in [k for k in record.__dict__ if k not in _STANDARD_RECORD_KEYS]:
            if key == "request_id":
                continue
            record.__dict__[key] = (
                REDACTED if is_sensitive_key(key) else redact(record.__dict__[key])
            )
        return True


class JsonFormatter(logging.Formatter):
    """Formats a record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            entry["request_id"] = str(request_id)
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key == "request_id" or key in entry:
                continue
            entry[key] = value
        if record.exc_info and record.exc_text is None:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    """Send JSON logs to standard output at the configured level, replacing earlier handlers.

    Safe to call more than once. Uvicorn's loggers are left to propagate to the root logger.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
