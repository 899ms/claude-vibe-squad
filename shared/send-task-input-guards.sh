#!/usr/bin/env bash
# Strict task frontmatter validation for bin/send-task.sh.
# Sourced after die is initialized and before parsing or staging any packet.

parse_task_frontmatter() {
    local file="$1"
    python3 - "$file" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    raw = path.read_bytes()
except OSError as exc:
    raise SystemExit(f"cannot read task file: {exc}") from exc
if b"\0" in raw:
    raise SystemExit("task file contains a NUL byte")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("task file is not valid UTF-8") from exc

# Reject every non-newline separator that could split parser interpretations.
LINE_SPLIT_LOOKALIKES = {
    "\v": "\\v",
    "\f": "\\f",
    "\r": "\\r",
    "\x1c": "\\x1c",
    "\x1d": "\\x1d",
    "\x1e": "\\x1e",
    "\x85": "\\x85",
    "\u2028": "U+2028",
    "\u2029": "U+2029",
}


def reject_line_split_lookalikes(region: str) -> None:
    for character, label in LINE_SPLIT_LOOKALIKES.items():
        if character in region:
            raise SystemExit(
                "task frontmatter contains a non-newline line separator "
                f"({label}); frontmatter lines must be separated by \\n only"
            )


lines = text.split("\n")
if not lines or lines[0] != "---":
    raise SystemExit("task file must begin with an exact '---' delimiter")
try:
    close = lines.index("---", 1)
except ValueError as exc:
    # Unterminated either way; naming the separator first keeps the diagnosis
    # honest for a region whose only terminator is a lookalike-prefixed "---".
    reject_line_split_lookalikes(text)
    raise SystemExit("task frontmatter is unterminated") from exc
reject_line_split_lookalikes("\n".join(lines[: close + 1]))

key_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
fields = {}
for line_number, line in enumerate(lines[1:close], start=2):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    if line[0].isspace():
        raise SystemExit(
            f"frontmatter line {line_number} must be one top-level key/value pair"
        )
    key, separator, raw_value = line.partition(":")
    if not separator or not key_pattern.fullmatch(key):
        raise SystemExit(f"frontmatter line {line_number} has an invalid key/value shape")
    if key in fields:
        raise SystemExit(f"frontmatter field '{key}' is duplicated")
    if any(ord(character) < 0x20 for character in raw_value):
        raise SystemExit(f"frontmatter field '{key}' contains a control character")
    fields[key] = raw_value.strip()

print(json.dumps(
    {"schema": "send-task-frontmatter/v1", "fields": fields},
    ensure_ascii=False,
    separators=(",", ":"),
))
PYEOF
}
