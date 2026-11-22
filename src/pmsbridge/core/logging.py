"""Structured JSON logging that never writes secrets or personal data.

`configure_logging(settings)` replaces the handlers of the root logger with one handler that
writes one JSON object per line to standard output: `time`, `level`, `logger`, `message`,
`request_id` when a request is being handled, any extra fields passed with
`logger.info("...", extra={...})`, and `exception` when an error is logged with its traceback.

Redaction has two layers, both applied by `RedactionFilter`:

* Values stored under a sensitive key (password, secret, token, authorization, api key, cookie,
  session, csrf, OAuth code, one-time and backup codes, signature, assertion, e-mail, phone and
  similar, matched case-insensitively, at any depth) are replaced with `[redacted]`. A two-item
  list or tuple that starts with text or bytes is treated as a name and a value, so raw header
  lists (`scope["headers"]`, `headers.raw`, `list(headers.items())`) are covered.
* In free text (the message and the traceback), `name=value`, `name: value` and `"name": "value"`
  pairs for those same keys are masked: also inside escaped JSON, with escaped or unterminated
  quotes, URL-encoded as `name%3Dvalue`, and with a name of up to three words ("Session ID: x").
  Bearer tokens, bare JWTs (starting with `eyJ`), `ENCRYPTION_KEYS` entries and e-mail addresses are
  masked too. This is a safety net: do not rely on it, and do not put secrets or guest details in
  log messages.

Naming rule: most sensitive words match as a part of the key name (after removing `_`, `-`, `.` and
spaces), so a key such as `token_count` is masked too. That errs on the side of hiding too much.
A few short words (`code`, `codes`, `sid`, `otp`, `pwd`, `session`) match only as a whole word
of the key (split at `_`, `-`, `.`, spaces and camelCase), so `postcode`, `barcode`, `inside` and
`footprint` are kept while `auth_code` and `mfaCode` are masked. A value that is not secret but has
such a word in its name has two options: rename the key (for example `quota_remaining`), or add its
exact name to `SAFE_KEYS` below (a change that needs review). Never add a key that could hold a
secret. `SAFE_KEYS` also decides free text: a name that ends in a safe name
(`HTTP status code: 502`) is kept.

Containment: a log call must never raise into its caller. If the message or an argument cannot be
rendered, the message falls back to the scrubbed format string plus `[unformattable args]`, and the
error text is never written, because it may quote the arguments.
"""

import json
import logging
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final, cast

from pydantic import SecretBytes, SecretStr

from pmsbridge.core.settings import Settings

REDACTED: Final = "[redacted]"

# The request id of the request being handled. The web layer sets it for the duration of a request.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Parts of key names that mark a value as sensitive. Compared after lowercasing and removing
# "_", "-", "." and spaces, so `API_KEY`, `api-key` and `apiKey` all match "apikey".
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
    "sessionid",
    "sessid",
    "csrf",
    "xsrf",
    "codeverifier",
    "mfacode",
    "recoverycode",
    "authcode",
    "totp",
    "passcode",
    "jwt",
    "signature",
    "signingkey",
    "assertion",
    "hmac",
    "otpcode",
    "backupcode",
    "smscode",
    "verificationcode",
)

# Short names that mark a value as sensitive only as a whole word of the key, so that ordinary words
# that merely contain them (postcode, encode, inside, footprint, possession) are not masked.
SENSITIVE_KEY_WORDS: Final = frozenset({"session", "sid", "code", "codes", "otp", "pwd"})

# Exact key names that are never masked, although they contain a sensitive word. They carry counters
# for the Guesty token budget and status or locale codes, not secrets. Compared after the same
# normalisation as above.
SAFE_KEYS: Final = frozenset(
    {
        "token_budget_remaining",
        "token_budget_reset_seconds",
        "token_requests_today",
        "status_code",
        "http_status_code",
        "response_code",
        "error_code",
        "exit_code",
        "return_code",
        "country_code",
        "currency_code",
        "language_code",
        "postal_code",
        "zip_code",
        "status_codes",
        "http_status_codes",
        "error_codes",
    }
)

MAX_DEPTH: Final = 8
MAX_TEXT_LENGTH: Final = 8000

# Attributes every LogRecord has. Anything else on a record was passed in `extra`.
_STANDARD_RECORD_KEYS: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

# A name, then `=`, `:` or an encoded `%3D`/`%3A`, then a value. The name is up to three words
# (joined by single spaces, so "Session ID: x" and "HTTP status code: 502" work) and is judged by
# `_name_is_sensitive`; the value is only matched for a sensitive name. The name may be preceded by
# the `26` of an encoded `%26`. The value is one of, in this order: an escaped-JSON string
# (`\"value\"`), a quoted string that may hold escaped quotes, a string whose closing quote is
# missing or does not fit (masked up to the next unescaped quote or the end of the line), or a bare
# token (ending at `&`, `,`, `;`, a space or an encoded delimiter). A value that starts with a quote
# or backslash-quote must never reach the bare alternative, which would stop at the quote. A Bearer
# or Basic word is kept out of the masked value. Text that is already masked is left alone.
#
# Every alternative must read a run of backslashes in only one way (possessive `\\++`, or a loop
# whose branches start with different characters), because overlapping branches inside a loop
# backtrack exponentially on hostile input. In a single-quoted value a backslash run absorbs the
# quote after it, so `'a\\'` is over-masked to the end of the line, which is acceptable.
_DELIMITER: Final = r"(?:[\s,;}\]):&%\"']|\\|$)"


def _escaped_string(quote: str) -> str:
    """Pattern for an escaped-JSON string: a backslash-quote, content, a closing backslash-quote.

    Backslash runs are possessive, so every run can be read only one way and the pattern cannot
    backtrack exponentially. A closing quote must be followed by a delimiter.
    """
    content = rf"(?:[^\\\r\n]|\\++(?!{quote})|\\++{quote}(?!{_DELIMITER}))*"
    return rf"\\++{quote}{content}\\++{quote}(?={_DELIMITER})"


_VALUE: Final = (
    "(?:" + _escaped_string('"') + "|" + _escaped_string("'") + r"|\"(?:[^\"\\]|\\.)*\""
    r"|'(?:[^'\\]|\\++.)*'"
    r"|\\*+\"(?:[^\"\\\r\n]|\\.)*"
    r"|\\*+'(?:[^'\\\r\n]|\\++.)*"
    r"|(?:(?!%(?:26|2C|3B|22|27|7D|5D))[^\s,;\"'&}\]])+)"
)
_HEAD_RE: Final = re.compile(
    r"(?P<pre>(?:%[0-9a-f]{2})?)(?P<name>[\w.-]{1,80}(?: [\w.-]{1,80}){0,2})"
    r"(?P<sep>(?:\\*[\"'])?\s*(?:[:=]|%3[AD])\s*(?:%2[27])?)",
    re.IGNORECASE,
)
_VALUE_RE: Final = re.compile(r"(?:(?:Bearer|Basic)\s+)?(?!\[redacted\])" + _VALUE, re.IGNORECASE)
# Cookie, Set-Cookie and Authorization values hold several parts (`a=1; session=xyz`, `Bearer x`),
# so everything to the end of the line is masked.
_LINE_RE: Final = re.compile(
    r"(?P<name>[\w.-]{0,40}(?:cookie|authorization))(?P<sep>[\"']?\s*[:=]\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
# An `ENCRYPTION_KEYS` entry (`<id>:<32 bytes in base64>`) is masked wherever it appears, even glued
# to other text, so every entry of a comma-separated list is covered.
_KEY_ENTRY_RE: Final = re.compile(r"[a-z0-9]{1,16}:[A-Za-z0-9+/]{43}=")
_JWT_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*){2,4}(?![A-Za-z0-9_-])"
)
_BEARER_RE: Final = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_WORD_RE: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


_SEPARATORS_RE: Final = re.compile(r"[\s_.-]")
_SAFE_NORMALISED: Final = frozenset(_SEPARATORS_RE.sub("", key) for key in SAFE_KEYS)


def _mask_line(match: re.Match[str]) -> str:
    return f"{match['name']}{match['sep']}{REDACTED}"


def _mask_pairs(text: str) -> str:
    """Mask the value of every `name=value` pair whose name is sensitive."""
    parts: list[str] = []
    copied = scan = 0
    while head := _HEAD_RE.search(text, scan):
        value = _VALUE_RE.match(text, head.end()) if _name_is_sensitive(head["name"]) else None
        if value is None:
            # Resume right after the name, because what follows may hold a pair of its own
            # (`url=https://host/?token=abc`).
            scan = head.end("name")
            continue
        parts.append(text[copied : head.start()])
        parts.append(f"{head['pre']}{head['name']}{head['sep']}{REDACTED}")
        copied = scan = value.end()
    parts.append(text[copied:])
    return "".join(parts)


def _name_is_sensitive(name: str) -> bool:
    """Judge a name of up to three words from free text (`Session ID`, `HTTP status code`).

    A safe suffix (`error code`, `HTTP status code`) keeps the value in clear. Otherwise the name is
    sensitive when the last word, or the last two words together, are.
    """
    words = name.split(" ")
    suffixes = [" ".join(words[start:]) for start in range(len(words))]
    if any(_SEPARATORS_RE.sub("", suffix.lower()) in _SAFE_NORMALISED for suffix in suffixes):
        return False
    return any(is_sensitive_key(suffix) for suffix in suffixes[-2:])


def is_sensitive_key(key: str) -> bool:
    """True when a key name marks its value as secret or personal."""
    normalised = _SEPARATORS_RE.sub("", key.lower())
    if normalised in _SAFE_NORMALISED:
        return False
    if any(part in normalised for part in SENSITIVE_KEY_PARTS):
        return True
    return any(word.lower() in SENSITIVE_KEY_WORDS for word in _WORD_RE.findall(key))


def scrub_text(text: str) -> str:
    """Mask secrets and e-mail addresses in free text, and cap its length."""
    truncated = len(text) > MAX_TEXT_LENGTH
    if truncated:
        text = text[:MAX_TEXT_LENGTH]
    text = _KEY_ENTRY_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _LINE_RE.sub(_mask_line, text)
    text = _mask_pairs(text)
    text = _BEARER_RE.sub(lambda m: f"{m[1]} {REDACTED}", text)
    text = _EMAIL_RE.sub(REDACTED, text)
    return text + "...[truncated]" if truncated else text


PAIR_LENGTH: Final = 2
MAX_NAME_LENGTH: Final = 100
_NAME_RE: Final = re.compile(r"[\w.-]+")


def _decoded(name: str | bytes) -> str:
    return name.decode("latin-1") if isinstance(name, bytes) else name


def _is_name_value_pair(sequence: Sequence[object]) -> bool:
    """True for a two-item sequence that starts with a short, name-like text (a header pair)."""
    if len(sequence) != PAIR_LENGTH:
        return False
    first = sequence[0]
    if not isinstance(first, (str, bytes)) or len(first) > MAX_NAME_LENGTH:
        return False
    return _NAME_RE.fullmatch(_decoded(first)) is not None


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
    elif isinstance(value, (list, tuple)) and _is_name_value_pair(cast("Sequence[object]", value)):
        # A raw header list: `[(b"cookie", b"a=1")]`, `scope["headers"]`, `list(headers.items())`.
        pair = cast("Sequence[object]", value)
        name = _decoded(cast("str | bytes", pair[0]))
        result = (
            [name, REDACTED]
            if is_sensitive_key(name)
            else [name, redact(pair[1], _depth=_depth + 1)]
        )
    elif isinstance(value, (list, tuple, set, frozenset)):
        sequence = cast("Collection[object]", value)
        result = [redact(item, _depth=_depth + 1) for item in sequence]
    else:
        try:
            result = scrub_text(str(value))
        except Exception:  # noqa: BLE001  # user code in __str__ can raise anything
            result = "[unprintable value]"
    return result


class RedactionFilter(logging.Filter):
    """Masks secrets in the message, the extra fields and the traceback of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = scrub_text(record.getMessage())
        except Exception:  # noqa: BLE001  # user code in __str__ or __repr__ can raise anything
            # A bad format string or argument list must not break (or leak through) logging.
            # The error text is dropped on purpose: it may quote the arguments.
            message = self._fallback_message(record) + " [unformattable args]"
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
                REDACTED if is_sensitive_key(key) else self._redact_extra(record.__dict__[key])
            )
        return True

    @staticmethod
    def _fallback_message(record: logging.LogRecord) -> str:
        try:
            return scrub_text(str(record.msg))
        except Exception:  # noqa: BLE001  # the message object itself can fail to render
            return "[unprintable message]"

    @staticmethod
    def _redact_extra(value: object) -> object:
        try:
            return redact(value)
        except Exception:  # noqa: BLE001  # a hostile Mapping or iterable can raise anything
            return "[unprintable value]"


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
