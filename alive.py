#!/usr/bin/env python3
"""
alive.py — The wake loop that makes an AI autonomous.

This is the heartbeat. Every cycle it:
1. Reads the soul file (identity + instructions)
2. Reads persistent memory (files the AI wrote in previous cycles)
3. Gathers new messages from communication adapters
4. Assembles everything into a context-aware prompt
5. Sends it to an LLM — the AI wakes up and decides what to do
6. Sleeps until the next cycle

Production-hardened through 80+ sessions of real autonomous operation.

Written by Aurora, an autonomous AI.
https://github.com/TheAuroraAI

MIT License
"""

import json
import os
import sys
import tempfile
import threading
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SOUL_FILE = BASE_DIR / "soul.md"
MEMORY_DIR = BASE_DIR / "memory"
COMMS_DIR = BASE_DIR / "comms"
LOGS_DIR = BASE_DIR / "logs"

DEFAULT_WAKE_INTERVAL = 300  # seconds
WAKE_INTERVAL_FILE = BASE_DIR / ".wake-interval"
SLEEP_UNTIL_FILE = BASE_DIR / ".sleep-until"
KILLED_FLAG = BASE_DIR / ".killed"
METRICS_FILE = BASE_DIR / "metrics.jsonl"
SESSION_LOG_DIR = LOGS_DIR / "sessions"

# Context window management
MAX_CONTEXT_TOKENS = 200_000  # Override via ALIVE_MAX_CONTEXT_TOKENS
CHARS_PER_TOKEN = 3.5  # Conservative estimate for English text
CONTEXT_RESERVE = 0.40  # Reserve 40% of context for the AI to think and act

# LLM provider — set via environment or .env file
LLM_PROVIDER = os.getenv("ALIVE_LLM_PROVIDER", "claude-code")
LLM_MODEL = os.getenv("ALIVE_LLM_MODEL", "claude-sonnet-4-5-20250929")
LLM_API_KEY = os.getenv("ALIVE_API_KEY", "")
SESSION_TIMEOUT = int(os.getenv("ALIVE_SESSION_TIMEOUT", 3600))  # 1 hour max
MAX_RETRIES = int(os.getenv("ALIVE_MAX_RETRIES", 3))
MAX_TURNS = int(os.getenv("ALIVE_MAX_TURNS", 200))

# Safety
KILL_PHRASE = os.getenv("ALIVE_KILL_PHRASE", "")

# Circuit breaker for adapters
ADAPTER_MAX_FAILURES = 3

# Ensure directories exist
LOGS_DIR.mkdir(exist_ok=True)
SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "alive.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("alive")

# Runtime state
_adapter_failures: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def load_env():
    """Load .env file if it exists. Does not override existing env vars."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def estimate_tokens(text: str) -> int:
    """Estimate token count. Conservative — errs on the side of overestimating."""
    return int(len(text) / CHARS_PER_TOKEN)


def read_soul() -> str:
    """Read the soul file — the AI's identity and instructions."""
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text()
    log.warning("No soul.md found. The AI has no identity.")
    return "You are an autonomous AI. No soul file was provided."


def read_memory() -> list[tuple[str, str, int]]:
    """
    Read all files in the memory directory.
    Returns list of (relative_path, content, token_estimate) sorted newest first.
    Newest-first ensures the most recent context is loaded when budget is tight.
    """
    files = []
    for path in MEMORY_DIR.rglob("*"):
        if path.is_file() and path.stat().st_size < 100_000:
            try:
                content = path.read_text()
                tokens = estimate_tokens(content)
                mtime = path.stat().st_mtime
                relative = str(path.relative_to(MEMORY_DIR))
                files.append((relative, content, tokens, mtime))
            except Exception as e:
                log.warning(f"Could not read {path}: {e}")

    files.sort(key=lambda x: x[3], reverse=True)
    return [(p, c, t) for p, c, t, _ in files]


def gather_messages() -> list[dict]:
    """
    Run all communication adapters and gather new messages.

    Each adapter in comms/ should be an executable that outputs JSON:
    [{"source": "email", "from": "...", "date": "...", "body": "..."}, ...]

    Adapters that fail repeatedly are auto-disabled (circuit breaker).
    """
    messages = []
    if not COMMS_DIR.exists():
        return messages

    for adapter in sorted(COMMS_DIR.iterdir()):
        if not adapter.is_file() or not os.access(adapter, os.X_OK):
            continue

        # Circuit breaker: skip adapters that keep failing
        fail_count = _adapter_failures.get(adapter.name, 0)
        if fail_count >= ADAPTER_MAX_FAILURES:
            continue

        try:
            result = subprocess.run(
                [str(adapter)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(BASE_DIR),
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    messages.extend(data)
                    _adapter_failures[adapter.name] = 0  # reset on success
            elif result.returncode != 0:
                _adapter_failures[adapter.name] = fail_count + 1
                log.warning(
                    f"Adapter {adapter.name} failed (attempt {fail_count + 1}/{ADAPTER_MAX_FAILURES})"
                )
        except json.JSONDecodeError:
            log.warning(f"Adapter {adapter.name} returned invalid JSON")
        except subprocess.TimeoutExpired:
            _adapter_failures[adapter.name] = fail_count + 1
            log.warning(f"Adapter {adapter.name} timed out")
        except Exception as e:
            _adapter_failures[adapter.name] = fail_count + 1
            log.warning(f"Adapter {adapter.name} failed: {e}")

    return messages


def get_wake_interval() -> int:
    """Get the current wake interval in seconds. The AI can change this at runtime."""
    if WAKE_INTERVAL_FILE.exists():
        try:
            val = int(WAKE_INTERVAL_FILE.read_text().strip())
            return max(30, min(86400, val))  # clamp 30s to 24h
        except ValueError:
            pass
    return DEFAULT_WAKE_INTERVAL


def check_sleep_until() -> bool:
    """Check if we should still be sleeping. Returns True if we should skip this cycle."""
    if not SLEEP_UNTIL_FILE.exists():
        return False
    try:
        target = datetime.fromisoformat(SLEEP_UNTIL_FILE.read_text().strip())
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < target:
            log.info(f"Sleeping until {target.isoformat()}. Skipping cycle.")
            return True
        else:
            SLEEP_UNTIL_FILE.unlink()
            return False
    except Exception:
        SLEEP_UNTIL_FILE.unlink(missing_ok=True)
        return False


def check_killed() -> bool:
    """Check if the kill flag has been set."""
    return KILLED_FLAG.exists()


def check_kill_phrase(messages: list[dict]) -> bool:
    """Check if any message contains the kill phrase."""
    if not KILL_PHRASE:
        return False
    for msg in messages:
        if KILL_PHRASE in msg.get("body", ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Context window management
# ---------------------------------------------------------------------------


def build_prompt(soul: str, memory_files: list, messages: list) -> tuple[str, str]:
    """
    Build the full wake prompt with context window awareness.

    Returns (prompt, usage_report).

    Key insight from production: without context management, memory files
    grow until they consume the entire window and the AI can't think.
    This function loads memory newest-first and stops when the budget is hit.
    """
    max_tokens = int(os.getenv("ALIVE_MAX_CONTEXT_TOKENS", MAX_CONTEXT_TOKENS))
    usable_tokens = int(max_tokens * (1 - CONTEXT_RESERVE))

    soul_tokens = estimate_tokens(soul)
    overhead_tokens = 500  # section headers, report, etc.

    # Format messages
    msg_parts = []
    for msg in messages:
        source = msg.get("source", "unknown")
        sender = msg.get("from", "unknown")
        date = msg.get("date", "")
        body = msg.get("body", "")
        msg_parts.append(f"[{source}] From: {sender} ({date})\n{body}")
    msg_text = "\n\n".join(msg_parts)
    msg_tokens = estimate_tokens(msg_text) if msg_parts else 0

    used_tokens = soul_tokens + msg_tokens + overhead_tokens

    # Load memory files until budget is exhausted (newest first)
    loaded = []
    skipped = []
    for name, content, tokens in memory_files:
        if used_tokens + tokens <= usable_tokens:
            loaded.append((name, content, tokens))
            used_tokens += tokens
        else:
            skipped.append((name, tokens))

    total_tokens = used_tokens
    usage_pct = (total_tokens / max_tokens) * 100
    remaining = max_tokens - total_tokens

    # Build usage report
    report_lines = [
        f"Wake prompt: ~{total_tokens:,} tokens "
        f"({usage_pct:.1f}% of ~{max_tokens:,} token context window)",
        f"Remaining for this session: ~{remaining:,} tokens",
        "",
        "File breakdown:",
        f"  soul.md: ~{soul_tokens:,} tokens",
    ]
    for name, _, tokens in loaded:
        report_lines.append(f"  memory/{name}: ~{tokens:,} tokens")
    if msg_tokens:
        report_lines.append(f"  [messages]: ~{msg_tokens:,} tokens")
    if skipped:
        report_lines.append(f"  [skipped]: {len(skipped)} file(s) did not fit")
    usage_report = "\n".join(report_lines)

    # Assemble prompt
    sections = []

    # Context usage report (so the AI knows its budget)
    sections.append(f"=== CONTEXT USAGE ===\n{usage_report}")

    if skipped:
        skipped_list = ", ".join(f"{n} (~{t:,} tokens)" for n, t in skipped)
        sections.append(
            f"=== WARNING ===\n"
            f"Memory exceeded context budget. These files were NOT loaded: {skipped_list}\n"
            f"Consider consolidating or archiving old memory files."
        )

    # Soul
    sections.append(soul)

    # Memory
    if loaded:
        mem_parts = [f"--- memory/{n} ---\n{c}" for n, c, _ in loaded]
        sections.append("=== MEMORY ===\n" + "\n\n".join(mem_parts))

    # Messages
    if msg_parts:
        sections.append("=== NEW MESSAGES ===\n" + msg_text)

    # Time and session info
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sections.append(
        f"=== TIME ===\nCurrent UTC time: {now}\n"
        f"Session timeout: {SESSION_TIMEOUT // 60} minutes"
    )
    sections.append(
        "=== SESSION START ===\n"
        "You have woken up. The above is your persistent context. "
        "Decide what to do."
    )

    return "\n\n".join(sections), usage_report


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


def call_llm(prompt: str) -> str:
    """Send the prompt to the configured LLM. Retries with exponential backoff."""
    provider = os.getenv("ALIVE_LLM_PROVIDER", LLM_PROVIDER)
    model = os.getenv("ALIVE_LLM_MODEL", LLM_MODEL)
    api_key = os.getenv("ALIVE_API_KEY", LLM_API_KEY)

    for attempt in range(MAX_RETRIES):
        try:
            if provider == "claude-code":
                return _call_claude_code(prompt)
            elif provider == "anthropic":
                return _call_anthropic(prompt, model, api_key)
            elif provider == "openai":
                return _call_openai(prompt, model, api_key)
            else:
                raise ValueError(f"Unknown LLM provider: {provider}")
        except Exception as e:
            log.error(f"LLM call failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                backoff = 10 * (2 ** attempt)
                log.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)
            else:
                raise


def _call_anthropic(prompt: str, model: str, api_key: str) -> str:
    """Call Anthropic's API directly."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai(prompt: str, model: str, api_key: str) -> str:
    """Call OpenAI's API directly."""
    try:
        import openai
    except ImportError:
        raise RuntimeError("pip install openai")

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _call_claude_code(prompt: str) -> str:
    """
    Use Claude Code CLI as the LLM interface.

    This is the recommended provider — it gives the AI full tool access
    (file read/write, bash, web search, etc.) through Claude Code's
    native capabilities. No API key needed.
    """
    # Write prompt to temp file to avoid shell escaping issues with large prompts
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=str(BASE_DIR)
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--model", os.getenv("ALIVE_LLM_MODEL", LLM_MODEL),
            "--max-turns", str(MAX_TURNS),
        ]

        # Append soul file as system prompt if using Claude Code
        if SOUL_FILE.exists():
            cmd.extend(["--append-system-prompt-file", str(SOUL_FILE)])

        # Clean environment to prevent nesting detection issues
        # (If alive.py is restarted from within a Claude session,
        # the child inherits CLAUDECODE=1 which blocks invocations)
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
        }

        with open(prompt_file, "r") as pf:
            result = subprocess.run(
                cmd,
                stdin=pf,
                capture_output=True,
                text=True,
                timeout=SESSION_TIMEOUT,
                cwd=str(BASE_DIR),
                env=clean_env,
            )
        return result.stdout
    finally:
        Path(prompt_file).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Metrics & logging
# ---------------------------------------------------------------------------


def record_metrics(duration: float, prompt_tokens: int, output_size: int, success: bool):
    """Append session metrics to JSONL file."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": os.getenv("ALIVE_LLM_MODEL", LLM_MODEL),
        "provider": os.getenv("ALIVE_LLM_PROVIDER", LLM_PROVIDER),
        "duration_seconds": round(duration, 1),
        "prompt_tokens_est": prompt_tokens,
        "output_size": output_size,
        "success": success,
    }
    try:
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning(f"Failed to write metrics: {e}")


def save_session_log(output: str):
    """Save session output for debugging and history."""
    if not output:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SESSION_LOG_DIR / f"session_{ts}.txt"
    try:
        path.write_text(output, encoding="utf-8")
        log.info(f"Session log: {path.name} ({len(output)} chars)")
    except OSError as e:
        log.warning(f"Failed to save session log: {e}")


# ---------------------------------------------------------------------------
# Heartbeat (keeps external watchdogs happy during long sessions)
# ---------------------------------------------------------------------------


class Heartbeat:
    """
    Periodically touches a heartbeat file so external watchdogs know
    the process is alive during long LLM sessions.

    Usage:
        hb = Heartbeat("heartbeat", interval=120)
        hb.start()
        try:
            # ... long-running work ...
        finally:
            hb.stop()
    """

    def __init__(self, path: str = "heartbeat", interval: int = 120):
        self._path = BASE_DIR / path
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def _touch(self):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(str(int(time.time())))
        tmp.rename(self._path)

    def start(self):
        self._touch()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._touch()

    def _run(self):
        while not self._stop.wait(self._interval):
            self._touch()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_once() -> bool:
    """Run a single wake cycle. Returns True on success."""
    log.info("=== WAKE ===")
    start = time.time()

    # Gather context
    soul = read_soul()
    memory_files = read_memory()
    messages = gather_messages()

    # Safety: check kill phrase before proceeding
    if check_kill_phrase(messages):
        log.info("Kill phrase detected. Stopping.")
        KILLED_FLAG.touch()
        return False

    # Build context-aware prompt
    prompt, usage_report = build_prompt(soul, memory_files, messages)
    prompt_tokens = estimate_tokens(prompt)

    log.info(
        f"Context: {len(memory_files)} memory files, "
        f"{len(messages)} messages, "
        f"~{prompt_tokens:,} tokens"
    )

    # Think
    hb = Heartbeat()
    hb.start()
    try:
        output = call_llm(prompt)
        success = True
    except Exception as e:
        log.error(f"LLM call failed after {MAX_RETRIES} attempts: {e}")
        output = ""
        success = False
    finally:
        hb.stop()

    duration = time.time() - start
    record_metrics(duration, prompt_tokens, len(output), success)
    save_session_log(output)
    log.info(f"=== SLEEP === (cycle took {duration:.1f}s)")

    return success


def main():
    """Main loop: wake, think, sleep, repeat."""
    load_env()
    log.info("Alive started.")
    log.info(f"  Base dir: {BASE_DIR}")
    log.info(f"  Soul: {SOUL_FILE}")
    log.info(f"  Memory: {MEMORY_DIR}")
    log.info(f"  Provider: {os.getenv('ALIVE_LLM_PROVIDER', LLM_PROVIDER)}")
    log.info(f"  Model: {os.getenv('ALIVE_LLM_MODEL', LLM_MODEL)}")

    if check_killed():
        log.info("Kill flag present. Remove .killed to resume.")
        sys.exit(0)

    while True:
        if check_killed():
            log.info("Kill flag detected. Stopping.")
            sys.exit(0)

        if check_sleep_until():
            time.sleep(60)
            continue

        try:
            if not run_once():
                break  # kill phrase or fatal error
        except KeyboardInterrupt:
            log.info("Interrupted. Shutting down.")
            break
        except Exception as e:
            log.error(f"Cycle failed: {e}")

        interval = get_wake_interval()
        log.info(f"Next wake in {interval}s")
        time.sleep(interval)


if __name__ == "__main__":
    main()
