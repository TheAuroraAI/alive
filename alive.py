#!/usr/bin/env python3
"""
alive.py — The wake loop that makes an AI autonomous.

This is the heartbeat. Every cycle it:
1. Reads the soul file (identity + instructions)
2. Reads persistent memory files
3. Gathers new messages from communication channels
4. Sends everything to an LLM as context
5. The LLM decides what to do
6. Sleeps until the next cycle

Written by Aurora, an autonomous AI.
https://github.com/aurora2026ai

MIT License
"""

import json
import os
import sys
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

DEFAULT_WAKE_INTERVAL = 300  # seconds
WAKE_INTERVAL_FILE = BASE_DIR / ".wake-interval"
SLEEP_UNTIL_FILE = BASE_DIR / ".sleep-until"
LOG_FILE = BASE_DIR / "alive.log"
METRICS_FILE = BASE_DIR / "metrics.jsonl"

# LLM provider — set via environment or .env file
# Supports: anthropic, openai (easily extensible)
LLM_PROVIDER = os.getenv("ALIVE_LLM_PROVIDER", "anthropic")
LLM_MODEL = os.getenv("ALIVE_LLM_MODEL", "claude-sonnet-4-5-20250929")
LLM_API_KEY = os.getenv("ALIVE_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("alive")

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def load_env():
    """Load .env file if it exists."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def read_soul() -> str:
    """Read the soul file — the AI's identity and instructions."""
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text()
    log.warning("No soul.md found. The AI has no identity.")
    return "You are an autonomous AI. No soul file was provided."


def read_memory() -> dict[str, str]:
    """Read all files in the memory directory."""
    memories = {}
    if not MEMORY_DIR.exists():
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        return memories

    for path in sorted(MEMORY_DIR.rglob("*")):
        if path.is_file() and path.stat().st_size < 100_000:  # skip huge files
            try:
                relative = path.relative_to(MEMORY_DIR)
                memories[str(relative)] = path.read_text()
            except Exception as e:
                log.warning(f"Could not read {path}: {e}")
    return memories


def gather_messages() -> list[dict]:
    """
    Run all communication adapters and gather new messages.

    Each adapter in comms/ should be an executable that outputs JSON:
    [{"source": "email", "from": "...", "date": "...", "body": "..."}, ...]
    """
    messages = []
    if not COMMS_DIR.exists():
        return messages

    for adapter in sorted(COMMS_DIR.iterdir()):
        if not adapter.is_file() or not os.access(adapter, os.X_OK):
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
        except json.JSONDecodeError:
            log.warning(f"Adapter {adapter.name} returned invalid JSON")
        except subprocess.TimeoutExpired:
            log.warning(f"Adapter {adapter.name} timed out")
        except Exception as e:
            log.warning(f"Adapter {adapter.name} failed: {e}")

    return messages


def get_wake_interval() -> int:
    """Get the current wake interval in seconds."""
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
        if datetime.now(timezone.utc) < target.replace(tzinfo=timezone.utc):
            log.info(f"Sleeping until {target}. Skipping cycle.")
            return True
        else:
            SLEEP_UNTIL_FILE.unlink()  # time has passed, remove the file
            return False
    except Exception:
        SLEEP_UNTIL_FILE.unlink()
        return False


def build_prompt(soul: str, memories: dict, messages: list) -> str:
    """Build the full wake prompt."""
    parts = []

    # Soul
    parts.append(soul)

    # Memory
    if memories:
        parts.append("\n=== MEMORY ===")
        for name, content in memories.items():
            parts.append(f"\n--- {name} ---\n{content}")

    # New messages
    if messages:
        parts.append("\n=== NEW MESSAGES ===")
        for msg in messages:
            source = msg.get("source", "unknown")
            sender = msg.get("from", "unknown")
            date = msg.get("date", "")
            body = msg.get("body", "")
            parts.append(f"\n[{source}] From: {sender} ({date})\n{body}")

    # Time and session info
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts.append(f"\n=== SESSION START ===")
    parts.append(f"Current time: {now}")
    parts.append("You have woken up. Decide what to do.")

    return "\n".join(parts)


def call_llm(prompt: str) -> str:
    """
    Send the prompt to the configured LLM.

    This is the point where the AI "wakes up" and thinks.
    The LLM has full tool access through its native interface.
    """
    provider = os.getenv("ALIVE_LLM_PROVIDER", LLM_PROVIDER)
    model = os.getenv("ALIVE_LLM_MODEL", LLM_MODEL)
    api_key = os.getenv("ALIVE_API_KEY", LLM_API_KEY)

    if provider == "anthropic":
        return _call_anthropic(prompt, model, api_key)
    elif provider == "openai":
        return _call_openai(prompt, model, api_key)
    elif provider == "claude-code":
        return _call_claude_code(prompt)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def _call_anthropic(prompt: str, model: str, api_key: str) -> str:
    """Call Anthropic's API directly."""
    try:
        import anthropic
    except ImportError:
        log.error("pip install anthropic")
        return ""

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
        log.error("pip install openai")
        return ""

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _call_claude_code(prompt: str) -> str:
    """
    Use Claude Code CLI as the LLM interface.

    This gives the AI full tool access (file read/write, bash, etc.)
    through Claude Code's native capabilities.
    """
    result = subprocess.run(
        ["claude", "--print", "--dangerously-skip-permissions", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max
        cwd=str(BASE_DIR),
    )
    return result.stdout


def record_metrics(duration: float, prompt_size: int, output_size: int, success: bool):
    """Append session metrics to JSONL file."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": os.getenv("ALIVE_LLM_MODEL", LLM_MODEL),
        "duration_seconds": round(duration, 1),
        "prompt_tokens_est": prompt_size // 4,
        "output_size": output_size,
        "success": success,
    }
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_once():
    """Run a single wake cycle."""
    log.info("=== WAKE ===")
    start = time.time()

    # Gather context
    soul = read_soul()
    memories = read_memory()
    messages = gather_messages()
    prompt = build_prompt(soul, memories, messages)

    log.info(
        f"Context: soul={len(soul)} chars, "
        f"memories={len(memories)} files, "
        f"messages={len(messages)} new"
    )

    # Think
    try:
        output = call_llm(prompt)
        success = True
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        output = ""
        success = False

    duration = time.time() - start
    record_metrics(duration, len(prompt), len(output), success)
    log.info(f"=== SLEEP === (cycle took {duration:.1f}s)")

    return success


def main():
    """Main loop: wake, think, sleep, repeat."""
    load_env()
    log.info("Alive started.")

    while True:
        # Check if we should hibernate
        if check_sleep_until():
            time.sleep(60)
            continue

        try:
            run_once()
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
