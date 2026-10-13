"""Atomic JSON writes for the v2 scan and paper state.

A reader must never observe a half-written scan file: the board polls it on a
different cadence than the writer produces it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path | str, payload: object) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".v2-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def read_json(path: Path | str, default: object = None) -> object:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path | str, row: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return target


def read_jsonl(path: Path | str, limit: int | None = None) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return rows
    for line in lines[-limit:] if limit else lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
