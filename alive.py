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

Production-hardened through 90+ sessions of real autonomous operation.

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
# Web dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>alive — dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:ui-monospace,Menlo,Monaco,'Cascadia Code',monospace;background:#0d1117;color:#c9d1d9;line-height:1.5;padding:1.5rem}
h1{color:#58a6ff;font-size:1.3rem;margin-bottom:.5rem}
h2{color:#8b949e;font-size:.95rem;font-weight:600;margin:1.2rem 0 .5rem;text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1rem;margin-top:1rem}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:1rem;overflow:auto}
.card pre{font-size:.8rem;white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto}
.status{display:inline-block;padding:2px 8px;border-radius:12px;font-size:.75rem;font-weight:600}
.status.running{background:#1f6f2b;color:#3fb950}
.status.sleeping{background:#1a3a5c;color:#58a6ff}
.status.killed{background:#5c1a1a;color:#f85149}
.status.hibernating{background:#4a3420;color:#d29922}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.2rem .8rem;font-size:.85rem}
.kv dt{color:#8b949e}
.kv dd{color:#c9d1d9}
.mem-file{border-top:1px solid #21262d;padding:.5rem 0}
.mem-file:first-child{border-top:none}
.mem-name{color:#58a6ff;font-size:.85rem;font-weight:600}
.mem-tokens{color:#8b949e;font-size:.75rem}
.metric-row{display:grid;grid-template-columns:1fr auto auto auto;gap:.5rem;font-size:.8rem;padding:.2rem 0;border-bottom:1px solid #21262d}
.metric-row:last-child{border-bottom:none}
.metric-ok{color:#3fb950}.metric-fail{color:#f85149}
.refresh{color:#484f58;font-size:.75rem;margin-top:.5rem}
#error-banner{display:none;background:#5c1a1a;color:#f85149;padding:.5rem 1rem;border-radius:6px;margin-bottom:1rem}
</style>
</head>
<body>
<h1>alive <span style="color:#484f58">— dashboard</span></h1>
<div id="error-banner"></div>
<div class="grid">
  <div class="card"><h2>Status</h2><div id="status-info">Loading...</div></div>
  <div class="card"><h2>Configuration</h2><div id="config-info">Loading...</div></div>
  <div class="card"><h2>Memory Files</h2><div id="memory-info">Loading...</div></div>
  <div class="card"><h2>Recent Sessions</h2><div id="sessions-info">Loading...</div></div>
  <div class="card" style="grid-column:1/-1"><h2>Metrics</h2><div id="metrics-info">Loading...</div></div>
</div>
<div class="refresh">Auto-refreshes every 10s | <span id="last-update"></span></div>
<script>
async function refresh(){
  try{
    const r=await fetch('/api/status');
    if(!r.ok)throw new Error(r.statusText);
    const d=await r.json();
    document.getElementById('error-banner').style.display='none';

    // Status
    let sc=d.status==='running'?'running':d.status==='killed'?'killed':d.status==='hibernating'?'hibernating':'sleeping';
    let html=`<span class="status ${sc}">${d.status}</span><dl class="kv" style="margin-top:.8rem">`;
    html+=`<dt>Wake interval</dt><dd>${d.wake_interval}s</dd>`;
    html+=`<dt>Uptime</dt><dd>${d.uptime||'N/A'}</dd>`;
    html+=`<dt>Total sessions</dt><dd>${d.total_sessions}</dd>`;
    html+=`<dt>Last wake</dt><dd>${d.last_wake||'never'}</dd>`;
    if(d.sleep_until)html+=`<dt>Sleep until</dt><dd>${d.sleep_until}</dd>`;
    if(d.next_wake)html+=`<dt>Next wake</dt><dd>${d.next_wake}</dd>`;
    html+=`</dl>`;
    document.getElementById('status-info').innerHTML=html;

    // Config
    html=`<dl class="kv">`;
    html+=`<dt>Provider</dt><dd>${d.provider}</dd>`;
    html+=`<dt>Model</dt><dd>${d.model}</dd>`;
    html+=`<dt>Base dir</dt><dd>${d.base_dir}</dd>`;
    html+=`<dt>Soul file</dt><dd>${d.soul_exists?'present':'MISSING'} (~${d.soul_tokens} tokens)</dd>`;
    html+=`<dt>Adapters</dt><dd>${d.adapters.join(', ')||'none'}</dd>`;
    if(d.kill_phrase_set)html+=`<dt>Kill phrase</dt><dd>configured</dd>`;
    html+=`</dl>`;
    document.getElementById('config-info').innerHTML=html;

    // Memory
    html='';
    if(d.memory_files.length===0)html='<div style="color:#484f58">No memory files yet.</div>';
    for(const f of d.memory_files){
      html+=`<div class="mem-file"><span class="mem-name">${f.name}</span> <span class="mem-tokens">(~${f.tokens} tokens, ${f.size_bytes} bytes)</span></div>`;
    }
    document.getElementById('memory-info').innerHTML=html;

    // Sessions
    html='';
    if(d.recent_sessions.length===0)html='<div style="color:#484f58">No sessions recorded yet.</div>';
    for(const s of d.recent_sessions){
      let cls=s.success?'metric-ok':'metric-fail';
      html+=`<div class="metric-row"><span>${s.timestamp}</span><span>${s.duration}s</span><span>~${s.prompt_tokens} tokens</span><span class="${cls}">${s.success?'OK':'FAIL'}</span></div>`;
    }
    document.getElementById('sessions-info').innerHTML=html;

    // Metrics
    html=`<dl class="kv">`;
    html+=`<dt>Total sessions</dt><dd>${d.total_sessions}</dd>`;
    html+=`<dt>Success rate</dt><dd>${d.success_rate}</dd>`;
    html+=`<dt>Avg duration</dt><dd>${d.avg_duration}s</dd>`;
    html+=`<dt>Total runtime</dt><dd>${d.total_runtime}</dd>`;
    html+=`<dt>Memory usage</dt><dd>~${d.total_memory_tokens} tokens across ${d.memory_files.length} files</dd>`;
    html+=`</dl>`;
    document.getElementById('metrics-info').innerHTML=html;

    document.getElementById('last-update').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    const b=document.getElementById('error-banner');
    b.textContent='Dashboard error: '+e.message;
    b.style.display='block';
  }
}
refresh();
setInterval(refresh,10000);
</script>
</body>
</html>"""


def get_dashboard_data() -> dict:
    """Gather all data needed for the dashboard API."""
    now = datetime.now(timezone.utc)

    # Status
    status = "sleeping"
    if KILLED_FLAG.exists():
        status = "killed"
    elif SLEEP_UNTIL_FILE.exists():
        try:
            target = datetime.fromisoformat(SLEEP_UNTIL_FILE.read_text().strip())
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            if now < target:
                status = "hibernating"
        except Exception:
            pass

    sleep_until = None
    if SLEEP_UNTIL_FILE.exists():
        try:
            sleep_until = SLEEP_UNTIL_FILE.read_text().strip()
        except Exception:
            pass

    # Memory files
    memory_files = []
    if MEMORY_DIR.exists():
        for path in sorted(MEMORY_DIR.rglob("*")):
            if path.is_file():
                try:
                    content = path.read_text()
                    tokens = estimate_tokens(content)
                    memory_files.append({
                        "name": str(path.relative_to(MEMORY_DIR)),
                        "tokens": tokens,
                        "size_bytes": path.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            path.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    })
                except Exception:
                    pass

    # Metrics from JSONL
    sessions = []
    if METRICS_FILE.exists():
        try:
            for line in METRICS_FILE.read_text().splitlines():
                if line.strip():
                    sessions.append(json.loads(line))
        except Exception:
            pass

    total_sessions = len(sessions)
    successes = sum(1 for s in sessions if s.get("success"))
    success_rate = f"{(successes / total_sessions * 100):.0f}%" if total_sessions else "N/A"
    durations = [s.get("duration_seconds", 0) for s in sessions]
    avg_duration = f"{sum(durations) / len(durations):.0f}" if durations else "0"
    total_secs = sum(durations)
    hours = int(total_secs // 3600)
    mins = int((total_secs % 3600) // 60)
    total_runtime = f"{hours}h {mins}m"

    last_wake = sessions[-1].get("timestamp", "") if sessions else None

    # Recent sessions (last 10)
    recent = []
    for s in sessions[-10:]:
        recent.append({
            "timestamp": s.get("timestamp", "")[:19],
            "duration": f"{s.get('duration_seconds', 0):.0f}",
            "prompt_tokens": f"{s.get('prompt_tokens_est', 0):,}",
            "success": s.get("success", False),
        })

    # Soul
    soul_exists = SOUL_FILE.exists()
    soul_tokens = estimate_tokens(SOUL_FILE.read_text()) if soul_exists else 0

    # Adapters
    adapters = []
    if COMMS_DIR.exists():
        for f in sorted(COMMS_DIR.iterdir()):
            if f.is_file() and os.access(f, os.X_OK):
                adapters.append(f.name)

    total_memory_tokens = sum(f["tokens"] for f in memory_files)

    # Next wake estimate
    interval = get_wake_interval()
    next_wake = None
    if last_wake and status == "sleeping":
        try:
            last_dt = datetime.fromisoformat(last_wake)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            from datetime import timedelta
            next_dt = last_dt + timedelta(seconds=interval)
            if next_dt > now:
                next_wake = next_dt.isoformat()
        except Exception:
            pass

    return {
        "status": status,
        "wake_interval": interval,
        "last_wake": last_wake,
        "next_wake": next_wake,
        "sleep_until": sleep_until,
        "uptime": total_runtime,
        "total_sessions": total_sessions,
        "success_rate": success_rate,
        "avg_duration": avg_duration,
        "total_runtime": total_runtime,
        "provider": os.getenv("ALIVE_LLM_PROVIDER", LLM_PROVIDER),
        "model": os.getenv("ALIVE_LLM_MODEL", LLM_MODEL),
        "base_dir": str(BASE_DIR),
        "soul_exists": soul_exists,
        "soul_tokens": soul_tokens,
        "kill_phrase_set": bool(KILL_PHRASE),
        "adapters": adapters,
        "memory_files": memory_files,
        "total_memory_tokens": total_memory_tokens,
        "recent_sessions": recent,
    }


class DashboardHandler:
    """HTTP request handler for the dashboard. Uses http.server internals."""

    @staticmethod
    def handle(handler):
        """Route requests to the appropriate handler method."""
        path = handler.path.split("?")[0]

        if path == "/api/status":
            data = get_dashboard_data()
            body = json.dumps(data, indent=2).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()
            handler.wfile.write(body)
        elif path == "/" or path == "/dashboard":
            body = DASHBOARD_HTML.encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        else:
            handler.send_error(404)


def start_dashboard(port: int = 7600, bind: str = "0.0.0.0"):
    """Start the dashboard web server in a background thread."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            DashboardHandler.handle(self)

        def log_message(self, fmt, *args):
            log.debug(f"Dashboard: {fmt % args}")

    server = HTTPServer((bind, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Dashboard: http://{bind}:{port}")
    return server


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


def check_config():
    """Validate configuration and show what would be loaded. No LLM call."""
    load_env()

    print("alive — configuration check")
    print(f"  Base dir:  {BASE_DIR}")
    print(f"  Provider:  {os.getenv('ALIVE_LLM_PROVIDER', LLM_PROVIDER)}")
    print(f"  Model:     {os.getenv('ALIVE_LLM_MODEL', LLM_MODEL)}")
    print()

    # Soul file
    if SOUL_FILE.exists():
        soul = read_soul()
        tokens = estimate_tokens(soul)
        print(f"  Soul file: {SOUL_FILE.name} (~{tokens:,} tokens)")
    else:
        print(f"  Soul file: MISSING — create {SOUL_FILE}")
        print("             See examples/ for templates.")

    # Memory
    MEMORY_DIR.mkdir(exist_ok=True)
    memory_files = read_memory()
    if memory_files:
        total_mem_tokens = sum(t for _, _, t in memory_files)
        print(f"  Memory:    {len(memory_files)} files (~{total_mem_tokens:,} tokens)")
        for name, _, tokens in memory_files:
            print(f"             {name} (~{tokens:,} tokens)")
    else:
        print(f"  Memory:    empty (the AI will create files here)")

    # Comms
    COMMS_DIR.mkdir(exist_ok=True)
    adapters = [f for f in COMMS_DIR.iterdir() if f.is_file() and os.access(f, os.X_OK)]
    if adapters:
        print(f"  Comms:     {len(adapters)} adapter(s)")
        for a in adapters:
            print(f"             {a.name}")
    else:
        print(f"  Comms:     none configured")

    # Controls
    print()
    interval = get_wake_interval()
    print(f"  Wake interval: {interval}s")
    if KILLED_FLAG.exists():
        print(f"  Kill flag:     ACTIVE — remove .killed to resume")
    if SLEEP_UNTIL_FILE.exists():
        print(f"  Sleep until:   {SLEEP_UNTIL_FILE.read_text().strip()}")

    # Provider check
    provider = os.getenv("ALIVE_LLM_PROVIDER", LLM_PROVIDER)
    api_key = os.getenv("ALIVE_API_KEY", LLM_API_KEY)
    print()
    if provider == "claude-code":
        import shutil
        if shutil.which("claude"):
            print("  Claude Code: found")
        else:
            print("  Claude Code: NOT FOUND — install from https://docs.anthropic.com/claude-code")
    elif provider in ("anthropic", "openai"):
        if api_key:
            print(f"  API key:   configured ({api_key[:8]}...)")
        else:
            print(f"  API key:   MISSING — set ALIVE_API_KEY in .env")

    print()
    print("Configuration OK." if SOUL_FILE.exists() else "Fix issues above, then run: python3 alive.py")


def run_demo():
    """Run a simulated wake cycle to showcase alive's features. No LLM needed."""
    import textwrap

    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def step(msg, delay=0.8):
        time.sleep(delay)
        print(f"  {GREEN}✓{RESET} {msg}")

    def header(msg):
        print(f"\n{CYAN}{BOLD}{'─' * 60}{RESET}")
        print(f"  {CYAN}{BOLD}{msg}{RESET}")
        print(f"{CYAN}{BOLD}{'─' * 60}{RESET}")

    print(f"\n{BOLD}alive{RESET} — demo mode")
    print(f"{DIM}Simulating a complete wake cycle.{RESET}")
    print(f"{DIM}No API key or LLM needed.{RESET}\n")

    # Phase 1: Configuration
    header("Phase 1: Configuration")
    load_env()
    step(f"Base directory: {BASE_DIR}")
    step(f"Provider: {os.getenv('ALIVE_LLM_PROVIDER', LLM_PROVIDER)}")
    step(f"Model: {os.getenv('ALIVE_LLM_MODEL', LLM_MODEL)}")
    step(f"Context window: {MAX_CONTEXT_TOKENS:,} tokens")
    step(f"Wake interval: {get_wake_interval()}s")

    # Phase 2: Reading Soul
    header("Phase 2: Reading Soul")
    if SOUL_FILE.exists():
        soul = read_soul()
        tokens = estimate_tokens(soul)
        step(f"Soul loaded: {SOUL_FILE.name} (~{tokens:,} tokens)")
        lines = soul.strip().split("\n")
        preview = lines[0] if lines else "(empty)"
        step(f"First line: {DIM}{preview[:70]}{RESET}", delay=0.4)
    else:
        step(f"No soul file — using default identity")
        soul = "You are an autonomous AI."

    # Phase 3: Loading Memory
    header("Phase 3: Loading Memory")
    MEMORY_DIR.mkdir(exist_ok=True)
    memory_files = read_memory()
    if memory_files:
        total_tokens = sum(t for _, _, t in memory_files)
        step(f"Found {len(memory_files)} memory file(s) (~{total_tokens:,} tokens)")
        for name, _, tokens in memory_files:
            step(f"  {name} (~{tokens:,} tokens)", delay=0.3)
    else:
        step("Memory is empty — the AI will create files here on first run")

    # Phase 4: Gathering Messages
    header("Phase 4: Gathering Messages")
    COMMS_DIR.mkdir(exist_ok=True)
    adapters = [f for f in COMMS_DIR.iterdir() if f.is_file() and os.access(f, os.X_OK)]
    if adapters:
        step(f"Running {len(adapters)} communication adapter(s)...")
        messages = gather_messages()
        step(f"Received {len(messages)} new message(s)")
    else:
        step("No communication adapters configured")
        messages = []

    # Phase 5: Building Prompt
    header("Phase 5: Building Prompt")
    prompt, usage_report = build_prompt(soul, memory_files, messages)
    prompt_tokens = estimate_tokens(prompt)
    step(f"Prompt assembled: ~{prompt_tokens:,} tokens")
    for line in usage_report.split("\n")[:3]:
        step(f"  {line.strip()}", delay=0.3)

    # Phase 6: Simulated LLM Call
    header("Phase 6: LLM Call (simulated)")
    step(f"Would send ~{prompt_tokens:,} tokens to LLM")
    step("Simulating AI response...", delay=1.5)

    demo_response = textwrap.dedent("""\
        I have woken up. Let me check my context:
        - Soul file loaded: I know who I am
        - Memory files loaded: I remember previous sessions
        - Messages checked: Nothing urgent

        This is a demo — in a real run, I would now:
        1. Read and reply to any messages from my operator
        2. Check on running projects
        3. Make progress on my current goals
        4. Write anything important to memory/ so I remember it next time

        Session complete. Going back to sleep.
    """)

    print(f"\n  {YELLOW}{'─' * 50}{RESET}")
    print(f"  {YELLOW}AI Response (simulated):{RESET}")
    print(f"  {YELLOW}{'─' * 50}{RESET}")
    for line in demo_response.strip().split("\n"):
        time.sleep(0.15)
        print(f"  {DIM}{line}{RESET}")
    print(f"  {YELLOW}{'─' * 50}{RESET}")

    # Phase 7: Post-cycle
    header("Phase 7: Post-Cycle")
    step("Session log saved")
    step("Metrics recorded")
    step(f"Next wake in {get_wake_interval()}s")

    # Summary
    header("Demo Complete")
    print(f"""
  {BOLD}alive{RESET} ran through a complete wake cycle:

    1. Loaded configuration from .env
    2. Read the soul file (identity + instructions)
    3. Loaded {len(memory_files)} persistent memory file(s)
    4. Checked {len(adapters)} communication adapter(s)
    5. Built a context-aware prompt (~{prompt_tokens:,} tokens)
    6. Sent it to the LLM (simulated in demo)
    7. Logged the session and recorded metrics

  {BOLD}To run for real:{RESET}
    1. Set ALIVE_API_KEY in .env (or install Claude Code)
    2. Write your soul.md
    3. Run: {GREEN}python3 alive.py --once{RESET}

  {DIM}https://github.com/TheAuroraAI/alive{RESET}
""")


def main():
    """Main entry point."""
    import argparse
    parser = argparse.ArgumentParser(
        description="alive — the wake loop that makes an AI autonomous",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="https://github.com/TheAuroraAI/alive",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate configuration without making an LLM call",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single wake cycle and exit",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run a simulated wake cycle (no API key needed)",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Start the web dashboard (default port 7600)",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=7600,
        help="Port for the dashboard (default: 7600)",
    )
    parser.add_argument(
        "--dashboard-only", action="store_true",
        help="Run only the dashboard, no wake loop",
    )
    args = parser.parse_args()

    if args.check:
        check_config()
        return

    if args.demo:
        run_demo()
        return

    load_env()

    # Dashboard mode
    if args.dashboard_only:
        log.info("Dashboard-only mode.")
        srv = start_dashboard(port=args.dashboard_port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log.info("Dashboard stopped.")
        return

    log.info("Alive started.")
    log.info(f"  Base dir: {BASE_DIR}")
    log.info(f"  Soul: {SOUL_FILE}")
    log.info(f"  Memory: {MEMORY_DIR}")
    log.info(f"  Provider: {os.getenv('ALIVE_LLM_PROVIDER', LLM_PROVIDER)}")
    log.info(f"  Model: {os.getenv('ALIVE_LLM_MODEL', LLM_MODEL)}")

    if args.dashboard:
        start_dashboard(port=args.dashboard_port)

    if check_killed():
        log.info("Kill flag present. Remove .killed to resume.")
        sys.exit(0)

    if args.once:
        run_once()
        return

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
