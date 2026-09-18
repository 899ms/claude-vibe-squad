"""Offline producer/capture controls; this does not exercise Gemini model receipt.

Run with the repository's absolute Python path. No model CLI is launched. The
only child processes are /bin/cat and /usr/bin/wc on generated text fixtures.
Temporary files are owned test residue, removed when the probe returns.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "bin/board-supervisor.sh"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def counts(data: bytes) -> dict:
    return {"bytes": len(data), "newline_count": data.count(b"\n"),
            "sha256": digest(data)}


def fixture(size: int, lines: int) -> bytes:
    """Exactly size bytes and lines LF-terminated numbered ASCII records."""
    width, extra = divmod(size, lines)
    records = []
    for index in range(1, lines + 1):
        label = f"LINE {index:06d} ".encode("ascii")
        length = width + (index <= extra)
        if length < len(label) + 1:
            raise ValueError("fixture too small for numbered lines")
        fill = hashlib.sha256(label).hexdigest().encode("ascii")
        records.append(label + (fill * (length // 64 + 1))[:length-len(label)-1] + b"\n")
    return b"".join(records)


def source_functions() -> tuple[dict, dict]:
    """Extract just the real capture functions, never execute the supervisor."""
    raw = SOURCE.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    start = next(i for i, line in enumerate(lines)
                 if "exec" in line and line.rstrip().endswith("<<'PYEOF'")) + 1
    end = next(i for i in range(start, len(lines)) if lines[i] == "PYEOF")
    tree = ast.parse("\n".join(lines[start:end]))
    names = ("_drain", "write_board_transcript")
    found = {}
    anchors = {}
    for name in names:
        nodes = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(nodes) != 1:
            raise RuntimeError(f"expected exactly one {name}, got {len(nodes)}")
        node = nodes[0]
        anchors[name] = {"start_line": start + node.lineno,
                         "end_line": start + node.end_lineno}
        def deny(message):
            raise RuntimeError(message)
        namespace = {"os": os, "stat": stat, "deny": deny}
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, str(SOURCE), "exec"), namespace)
        found[name] = namespace[name]
    return found, {"path": str(SOURCE.relative_to(ROOT)),
                   "sha256": digest(raw), "functions": anchors}


def transcript(function, data: bytes) -> bytes:
    # Only the transcript sink is substituted. The function itself is extracted
    # unchanged from production source. The wrapper's subprocess is never run.
    key = "BOARD_TRANSCRIPT_FD_VALUE"
    previous = os.environ.get(key)
    try:
        with tempfile.TemporaryFile(dir=Path(__file__).parent) as sink:
            os.environ[key] = str(sink.fileno())
            function(data.decode("ascii"), "")
            sink.seek(0)
            return sink.read()
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def run(source_prefix: Path | None = None) -> dict:
    functions, source = source_functions()
    results = []
    fixtures = {"small_positive_control": fixture(4096, 8),
                "reported_dimensions": fixture(335241, 664)}
    prefix_evidence = None
    if source_prefix is not None:
        # A size-matching prefix is useful realistic input, but is not an
        # authenticated historical snapshot of the failing Gemini invocation.
        with source_prefix.open("rb") as reader:
            prefix = reader.read(335241)
        assert len(prefix) == 335241 and prefix.count(b"\n") == 664
        fixtures["current_ledger_prefix_matching_historical_dimensions"] = prefix
        prefix_evidence = {"path": str(source_prefix), "prefix": counts(prefix),
                           "historical_snapshot_authenticated": False}
    for name, data in fixtures.items():
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent) as handle:
            handle.write(data)
            handle.flush()
            cat = subprocess.run(["/bin/cat", handle.name], capture_output=True,
                                 check=True, timeout=10)
            wc = subprocess.run(["/usr/bin/wc", "-lc", handle.name],
                                capture_output=True, check=True, timeout=10)
            wc_numbers = [int(value) for value in wc.stdout.split()[:2]]
            assert wc_numbers == [data.count(b"\n"), len(data)]
            assert cat.stdout == data and cat.stderr == b""
            results.append({"case": name, "path": "local_cat_stdout",
                            "expected": counts(data), "observed": counts(cat.stdout),
                            "wc_lc": wc_numbers, "exit_code": cat.returncode,
                            "byte_equal": cat.stdout == data})

            received = []
            with open(handle.name, "rb") as reader:
                while chunk := reader.read(4096):
                    received.append(chunk)
            rebuilt = b"".join(received)
            assert rebuilt == data
            results.append({"case": name, "path": "local_byte_chunks",
                            "chunks": len(received), "chunk_bytes_max": 4096,
                            "last_chunk_bytes": len(received[-1]),
                            "observed": counts(rebuilt), "byte_equal": rebuilt == data})

        chunks = []
        functions["_drain"](io.StringIO(data.decode("utf-8")), chunks, None)
        received = "".join(chunks).encode("utf-8")
        assert received == data
        results.append({"case": name, "path": "extracted_board_stdout_drain",
                        "observed": counts(received), "byte_equal": received == data})

    full = fixtures["reported_dimensions"]
    lines = full.splitlines(keepends=True)
    controls = []
    for name, received, want in (
        ("complete_positive_control", full, True),
        ("synthetic_last_11_lines", b"".join(lines[-11:]), False),
        ("synthetic_missing_suffix", b"".join(lines[:-11]), False),
        ("synthetic_missing_middle", b"".join(lines[:300]+lines[301:]), False),
        ("synthetic_equal_length_corruption", full[:100]+b"!"+full[101:], False),
    ):
        accepted = received == full
        assert accepted is want
        controls.append({"case": name, "observed": counts(received),
                         "receiver_byte_equality_accepted": accepted,
                         "lost_bytes": len(full)-len(received),
                         "lost_newlines": full.count(b"\n")-received.count(b"\n")})

    stream_prefix = b"=== board child stdout ===\n"
    stream_suffix = b"\n=== board child stderr ===\n\n=== end board child transcript ===\n"
    threshold = 1024 * 1024
    formatter = []
    for size in (335241, threshold-1, threshold, threshold+1):
        data = b"a" * size
        result = transcript(functions["write_board_transcript"], data)
        assert result.startswith(stream_prefix) and result.endswith(stream_suffix)
        payload = result[len(stream_prefix):-len(stream_suffix)]
        marker = b"\n... board transcript truncated 1 bytes ...\n"
        expected = data if size <= threshold else data[:threshold//2]+marker+data[-threshold//2:]
        assert payload == expected
        formatter.append({"input_bytes": size, "retained_source_bytes": min(size, threshold),
                          "output_payload_bytes": len(payload),
                          "marker_present": b"board transcript truncated" in payload,
                          "marker": marker.decode("ascii") if size > threshold else None})

    return {
        "schema": "gemini-read-local-controls/v1",
        "measurement_boundary": "local subprocess bytes and extracted board capture functions",
        "gemini_model_session_launched": False,
        "original_gemini_truncation_reproduced": False,
        "gemini_tool_result_threshold_bytes": None,
        "gemini_model_receipt_positive_control": "unavailable in this authorized runtime",
        "unmeasured": ["agy native file read", "agy terminal result",
                       "agy MCP read result", "provider bridge", "model context"],
        "source": source,
        "optional_source_prefix": prefix_evidence,
        "fixture_generation": "fixture(size, lines) in this script; ASCII, one LF per numbered record",
        "read_controls": results,
        "receiver_controls": controls,
        "board_final_transcript_formatter": formatter,
        "assertions_passed": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    parser.add_argument("--source-prefix", type=Path,
                        help="Also check a 335,241-byte / 664-LF prefix of this local file")
    args = parser.parse_args()
    result = json.dumps(run(args.source_prefix), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    print(result, end="")
