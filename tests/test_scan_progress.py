"""Cold-scan progress, cancellation, and safe resume."""

import asyncio
import json
import threading
import time

import pytest
from textual.widgets import Static

import cc_usage.parser as parser_module
from cc_usage.app import CCUsageApp
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.parser import Parser, ScanCancelled, ScanProgress
from cc_usage.pricing import load_pricing


def _write_transcript(path, count=12):
    rows = []
    for index in range(count):
        rows.append(
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": f"request-{index}",
                    "timestamp": "2026-07-12T12:00:00Z",
                    "message": {
                        "id": f"message-{index}",
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": index + 1, "output_tokens": 1},
                    },
                }
            )
        )
    path.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")


def test_parser_reports_byte_progress_and_resumes_after_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    transcript = tmp_path / "project" / "session.jsonl"
    transcript.parent.mkdir()
    _write_transcript(transcript)

    pricing, _warnings = load_pricing()
    parser = Parser(pricing, cache_path=None)
    cancel = threading.Event()
    first_updates = []

    def first_progress(update):
        first_updates.append(update)
        if update.phase == "parsing" and update.bytes_done > 0:
            cancel.set()

    with pytest.raises(ScanCancelled):
        parser.scan(progress=first_progress, cancelled=cancel.is_set)

    assert 0 < len(parser.records) < 12
    assert [update.phase for update in first_updates[:2]] == ["discovering", "parsing"]
    assert first_updates[1].bytes_total == transcript.stat().st_size

    resumed_updates = []
    parser.scan(progress=resumed_updates.append)

    assert len(parser.records) == 12
    assert len({record.input_tokens for record in parser.records}) == 12
    assert resumed_updates[0].phase == "discovering"
    assert resumed_updates[-1].phase == "complete"
    assert resumed_updates[-1].files_done == resumed_updates[-1].files_total
    assert resumed_updates[-1].bytes_done == resumed_updates[-1].bytes_total


def test_tui_cancel_then_r_resumes_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    engine = Engine(Config(), cache_path=None)
    started = threading.Event()
    calls = []

    def controlled_scan(progress=None, cancelled=None):
        calls.append(len(calls) + 1)
        if calls[-1] == 1:
            if progress is not None:
                progress(ScanProgress(phase="discovering"))
                progress(
                    ScanProgress(
                        phase="parsing",
                        files_total=10,
                        bytes_total=1000,
                        bytes_done=100,
                    )
                )
            started.set()
            while cancelled is None or not cancelled():
                time.sleep(0.005)
            raise ScanCancelled("cancelled in test")
        engine._scanned = True
        if progress is not None:
            progress(
                ScanProgress(
                    phase="complete",
                    files_done=10,
                    files_total=10,
                    bytes_done=1000,
                    bytes_total=1000,
                )
            )

    monkeypatch.setattr(engine, "scan", controlled_scan)
    monkeypatch.setattr(engine, "save_cache", lambda: None)
    monkeypatch.setattr(engine, "refresh_limits", lambda: None)
    app = CCUsageApp(engine)

    async def scenario():
        async with app.run_test() as pilot:
            for _ in range(20):
                if started.is_set():
                    break
                await pilot.pause()
            assert started.is_set()

            await pilot.press("c")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = str(app.query_one("#spend", Static).renderable)
            assert "scan cancelled" in status
            assert engine.is_scanned is False

            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [1, 2]
            assert engine.is_scanned is True

    asyncio.run(scenario())