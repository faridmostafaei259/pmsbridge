"""Fail if any tracked text file (or the given files) contain emoji characters.

Usage:
    python scripts/check_no_emoji.py            # scan files tracked or about to be tracked by git
    python scripts/check_no_emoji.py FILE ...   # scan specific files
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Code point ranges treated as emoji. Kept as integers so this file stays plain ASCII.
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # pictographs, emoticons, transport, extended pictographs
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows (stars, etc.)
    (0x231A, 0x231B),  # watch, hourglass
    (0x23E9, 0x23F3),  # media controls, alarm clock
    (0x23F8, 0x23FA),  # media controls
    (0x25FD, 0x25FE),  # medium small squares
    (0x3030, 0x3030),  # wavy dash
    (0x303D, 0x303D),  # part alternation mark
    (0x3297, 0x3297),  # circled ideograph congratulation
    (0x3299, 0x3299),  # circled ideograph secret
    (0x200D, 0x200D),  # zero width joiner
    (0xFE0F, 0xFE0F),  # emoji presentation selector
)
EMOJI = re.compile(
    "[" + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in EMOJI_RANGES) + "]"
)


def candidate_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(p) for p in out.splitlines() if p]


def scan(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        return []
    return [
        f"{path}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if EMOJI.search(line)
    ]


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] if argv else candidate_files()
    problems = [hit for f in files for hit in scan(f)]
    for hit in problems:
        sys.stderr.write(hit + "\n")
    if problems:
        sys.stderr.write(f"{len(problems)} line(s) contain emoji characters.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
