"""Incremental parsing (M6 / T0 §9): only newly appended lines are read each scan;
unchanged files are skipped; truncation/rotation is handled without crashing."""

import json

import cc_usage.parser as P
from cc_usage.parser import Parser

PRICING = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}


def _line(req: str, mid: str, inp: int) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": mid,
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": inp, "output_tokens": 0},
                },
            }
        )
        + "\n"
    )


def test_incremental_reads_only_appended_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "session.jsonl"
    f.write_text(_line("r1", "m1", 100))

    p = Parser(PRICING)
    p.scan()
    assert p.stats.records == 1
    assert p.stats.lines_read == 1

    with open(f, "a") as fh:
        fh.write(_line("r2", "m2", 200))
    p.scan()
    assert p.stats.records == 2
    assert p.stats.lines_read == 2  # only +1 line read — old line not re-parsed
    assert sum(r.input_tokens for r in p.records) == 300


def test_unchanged_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "s.jsonl").write_text(_line("r1", "m1", 100))

    p = Parser(PRICING)
    p.scan()
    lr = p.stats.lines_read
    p.scan()  # nothing changed since last scan
    assert p.stats.lines_read == lr  # file skipped entirely, no re-read


def test_partial_trailing_line_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "s.jsonl"
    f.write_text(_line("r1", "m1", 100))

    p = Parser(PRICING)
    p.scan()
    # write a line WITHOUT a trailing newline (a mid-append snapshot)
    with open(f, "a") as fh:
        fh.write(_line("r2", "m2", 200).rstrip("\n"))
    p.scan()
    assert p.stats.records == 1  # incomplete line held back
    # now the newline arrives
    with open(f, "a") as fh:
        fh.write("\n")
    p.scan()
    assert p.stats.records == 2  # completed line now ingested exactly once


def test_truncation_is_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "s.jsonl"
    f.write_text(_line("r1", "m1", 100) + _line("r2", "m2", 200))

    p = Parser(PRICING)
    p.scan()
    assert p.stats.records == 2
    f.write_text(_line("r3", "m3", 50))  # rewrite shorter -> offset reset, re-read
    p.scan()
    assert any(r.input_tokens == 50 for r in p.records)
