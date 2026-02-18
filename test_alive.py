"""Tests for alive.py core functions.

Run: python3 -m pytest test_alive.py -v
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import alive


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty():
    assert alive.estimate_tokens("") == 0


def test_estimate_tokens_short():
    tokens = alive.estimate_tokens("hello world")
    assert tokens > 0
    assert tokens < 100


def test_estimate_tokens_proportional():
    short = alive.estimate_tokens("hello")
    long = alive.estimate_tokens("hello " * 100)
    assert long > short


# ---------------------------------------------------------------------------
# Wake interval
# ---------------------------------------------------------------------------


def test_get_wake_interval_default():
    with patch.object(alive, 'WAKE_INTERVAL_FILE', Path("/nonexistent")):
        assert alive.get_wake_interval() == alive.DEFAULT_WAKE_INTERVAL


def test_get_wake_interval_from_file():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("60")
        f.flush()
        with patch.object(alive, 'WAKE_INTERVAL_FILE', Path(f.name)):
            assert alive.get_wake_interval() == 60
    os.unlink(f.name)


def test_get_wake_interval_clamped_low():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("5")  # Below 30s minimum
        f.flush()
        with patch.object(alive, 'WAKE_INTERVAL_FILE', Path(f.name)):
            assert alive.get_wake_interval() == 30
    os.unlink(f.name)


def test_get_wake_interval_clamped_high():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("999999")  # Above 86400s maximum
        f.flush()
        with patch.object(alive, 'WAKE_INTERVAL_FILE', Path(f.name)):
            assert alive.get_wake_interval() == 86400
    os.unlink(f.name)


# ---------------------------------------------------------------------------
# Kill phrase detection
# ---------------------------------------------------------------------------


def test_check_kill_phrase_no_phrase():
    with patch.object(alive, 'KILL_PHRASE', ''):
        messages = [{"body": "hello world"}]
        assert alive.check_kill_phrase(messages) is False


def test_check_kill_phrase_detected():
    with patch.object(alive, 'KILL_PHRASE', 'STOP NOW'):
        messages = [{"body": "please STOP NOW immediately"}]
        assert alive.check_kill_phrase(messages) is True


def test_check_kill_phrase_not_present():
    with patch.object(alive, 'KILL_PHRASE', 'STOP NOW'):
        messages = [{"body": "hello world"}]
        assert alive.check_kill_phrase(messages) is False


def test_check_kill_phrase_empty_messages():
    with patch.object(alive, 'KILL_PHRASE', 'STOP NOW'):
        assert alive.check_kill_phrase([]) is False


# ---------------------------------------------------------------------------
# Sleep-until check
# ---------------------------------------------------------------------------


def test_check_sleep_until_no_file():
    with patch.object(alive, 'SLEEP_UNTIL_FILE', Path("/nonexistent")):
        assert alive.check_sleep_until() is False


def test_check_sleep_until_future():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("2099-01-01T00:00:00")
        f.flush()
        with patch.object(alive, 'SLEEP_UNTIL_FILE', Path(f.name)):
            assert alive.check_sleep_until() is True
    os.unlink(f.name)


def test_check_sleep_until_past():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("2020-01-01T00:00:00")
        f.flush()
        path = Path(f.name)
        with patch.object(alive, 'SLEEP_UNTIL_FILE', path):
            assert alive.check_sleep_until() is False
            assert not path.exists()


# ---------------------------------------------------------------------------
# Kill flag
# ---------------------------------------------------------------------------


def test_check_killed_no_flag():
    with patch.object(alive, 'KILLED_FLAG', Path("/nonexistent")):
        assert alive.check_killed() is False


def test_check_killed_with_flag():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("killed")
        f.flush()
        with patch.object(alive, 'KILLED_FLAG', Path(f.name)):
            assert alive.check_killed() is True
    os.unlink(f.name)


# ---------------------------------------------------------------------------
# Soul file reading
# ---------------------------------------------------------------------------


def test_read_soul_exists():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write("I am a test AI.")
        f.flush()
        with patch.object(alive, 'SOUL_FILE', Path(f.name)):
            soul = alive.read_soul()
            assert "I am a test AI." in soul
    os.unlink(f.name)


def test_read_soul_missing():
    with patch.object(alive, 'SOUL_FILE', Path("/nonexistent/soul.md")):
        soul = alive.read_soul()
        assert "autonomous" in soul.lower()


# ---------------------------------------------------------------------------
# Memory reading
# ---------------------------------------------------------------------------


def test_read_memory_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        with patch.object(alive, 'MEMORY_DIR', Path(d)):
            files = alive.read_memory()
            assert files == []


def test_read_memory_reads_files():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "notes.md").write_text("some notes")
        Path(d, "log.txt").write_text("some log")
        with patch.object(alive, 'MEMORY_DIR', Path(d)):
            files = alive.read_memory()
            assert len(files) == 2
            # Each entry is (relative_path, content, token_count)
            for name, content, tokens in files:
                assert isinstance(name, str)
                assert isinstance(content, str)
                assert isinstance(tokens, int)
                assert tokens > 0


def test_read_memory_sorted_newest_first():
    with tempfile.TemporaryDirectory() as d:
        old = Path(d, "old.md")
        old.write_text("old content")
        os.utime(old, (1000000, 1000000))
        time.sleep(0.05)
        new = Path(d, "new.md")
        new.write_text("new content")
        with patch.object(alive, 'MEMORY_DIR', Path(d)):
            files = alive.read_memory()
            assert files[0][0] == "new.md"
            assert files[1][0] == "old.md"


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------


def test_build_prompt_includes_soul():
    soul = "I am a test soul."
    memory_files = []
    messages = []
    prompt, report = alive.build_prompt(soul, memory_files, messages)
    assert "I am a test soul." in prompt


def test_build_prompt_includes_messages():
    soul = "soul"
    memory_files = []
    messages = [{"from": "user", "date": "2026-01-01", "body": "hello AI"}]
    prompt, report = alive.build_prompt(soul, memory_files, messages)
    assert "hello AI" in prompt


def test_build_prompt_includes_memory():
    soul = "soul"
    memory_files = [("notes.md", "important note", 5)]
    messages = []
    prompt, report = alive.build_prompt(soul, memory_files, messages)
    assert "important note" in prompt


def test_build_prompt_returns_usage_report():
    soul = "soul"
    memory_files = [("notes.md", "important note", 5)]
    messages = []
    prompt, report = alive.build_prompt(soul, memory_files, messages)
    assert isinstance(report, str)
    assert len(report) > 0


# ---------------------------------------------------------------------------
# Session continuity
# ---------------------------------------------------------------------------


def test_save_and_read_last_session():
    with tempfile.TemporaryDirectory() as d:
        last_session_path = Path(d) / ".last-session"
        session_log_dir = Path(d) / "sessions"
        session_log_dir.mkdir()
        with patch.object(alive, 'LAST_SESSION_FILE', last_session_path), \
             patch.object(alive, 'SESSION_LOG_DIR', session_log_dir):
            alive.save_session_log("This is a test session output.")
            result = alive.read_last_session()
            assert "test session output" in result


def test_read_last_session_missing():
    with patch.object(alive, 'LAST_SESSION_FILE', Path("/nonexistent")):
        result = alive.read_last_session()
        assert result == ""


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------


def test_sigterm_sets_shutdown_flag():
    alive._shutdown_requested = False
    alive._handle_sigterm(None, None)
    assert alive._shutdown_requested is True
    alive._shutdown_requested = False  # Reset


# ---------------------------------------------------------------------------
# Metrics logging
# ---------------------------------------------------------------------------


def test_record_metrics():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        path = Path(f.name)
    try:
        with patch.object(alive, 'METRICS_FILE', path):
            alive.record_metrics(
                duration=10.5,
                prompt_tokens=1000,
                output_size=500,
                success=True,
            )
            lines = path.read_text().strip().split('\n')
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["success"] is True
            assert data["output_size"] == 500
            assert data["duration_seconds"] == 10.5
            assert data["prompt_tokens_est"] == 1000
    finally:
        path.unlink(missing_ok=True)
