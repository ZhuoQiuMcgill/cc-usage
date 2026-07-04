"""Incremental parsing (M6 / T0 §9): only newly appended lines are read each scan;
unchanged files are skipped; truncation/rotation is handled without crashing."""

import json

import cc_usage.parser as P
from cc_usage.parser import Parser

PRICING = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}


def _line(req: str, mid: str, inp: int, out: int = 0) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": mid,
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": inp, "output_tokens": out},
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


def test_streaming_merge_across_scans(tmp_path, monkeypatch):
    """A message whose final streaming line lands in a LATER scan still ends up with
    the final counts — the per-message index persists across scans (T9)."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "session.jsonl"
    # scan 1: two partial streaming snapshots of ONE message (output 7, then 500)
    f.write_text(_line("r", "m", 1000, 7) + _line("r", "m", 1000, 500))
    p = Parser(PRICING)
    p.scan()
    assert p.stats.records == 1
    assert p.records[0].output_tokens == 500  # best snapshot so far

    # scan 2: the FINAL line for the same message is appended after the first scan
    with open(f, "a") as fh:
        fh.write(_line("r", "m", 1000, 2000))
    p.scan()
    assert p.stats.records == 1  # merged into the existing record, not a new one
    assert len(p.records) == 1
    assert p.records[0].output_tokens == 2000  # final counts now reflected
    assert p.stats.lines_read == 3  # 2 in scan 1 + only the 1 appended line in scan 2


def test_streaming_merge_across_files(tmp_path, monkeypatch):
    """The same (requestId, message.id) split across TWO files — the parent session
    holding the partial snapshot and a subagent transcript holding the final counts
    (the layout iter_transcript_files descends for) — merges to the final counts
    exactly once. The scan order of the two files is a sort detail; field-wise max
    makes the merged result the same either way (T9)."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    sub = proj / "session" / "subagents" / "wf_1"
    sub.mkdir(parents=True)
    (proj / "session.jsonl").write_text(_line("r", "m", 1000, 7))  # partial snapshot
    (sub / "agent.jsonl").write_text(_line("r", "m", 1000, 2000))  # final counts

    p = Parser(PRICING)
    p.scan()
    assert p.stats.records == 1  # one record, not one per file
    assert p.stats.duplicates == 1  # the other file's line merged in
    assert len(p.records) == 1
    assert p.records[0].output_tokens == 2000  # final counts, whichever file read first
    assert p.records[0].input_tokens == 1000  # identical fields not summed to 2000


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
