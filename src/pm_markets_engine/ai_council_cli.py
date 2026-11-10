"""Multi-AI Quant Council CLI Usage Contract (`gpt-6-astra` & `claude-opus-5-5`).

Implements Section 8.3 & 8.4 of the Master Blueprint (`AGENTS.md` / `GEMINI.md`):
- Fixed `argv` CLI contract (`shell=False`) for:
  - OpenAI Codex (`gpt-6-astra` via `/Users/belavarga/.local/bin/codex`)
  - Anthropic Claude (`claude-opus-5-5` via `/Users/belavarga/.local/bin/claude`)
  - xAI Grok (`grok-4.7` via `/Users/belavarga/.local/bin/grok`)
  - Google Antigravity (`agy` via `/Users/belavarga/.local/bin/agy`)
- Empty isolated working directory (`empty cwd`) + optional `sandbox-exec` boundary.
- Per-provider `flock` mutex (`data/pm_markets/ai_locks/<provider>.lock`), streaming watchdog
  (`idle_timeout` vs `max_hard_ceiling`), and process-group cleanup (`os.killpg`).
- Model identity verification, zero-tool/zero-subagent enforcement, Zero-Anchoring prompt guard,
  and SHA-256 disposition verification by `agy`.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple


MAX_OUTPUT_BYTES = 262144
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "config" / "ai_council_contract.json"

# Regex guard enforcing the 2026-11-04 Zero Prompt Anchoring Directive
_ANCHORING_PATTERN = re.compile(
    r"(\(\s*(?:e\.g\.|i\.e\.|pl\.|például\b|such as\b)[^)]*\)|(?:^|\s)(?:e\.g\.|i\.e\.|pl\.|például\b))",
    flags=re.IGNORECASE,
)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_zero_anchoring_prompt(prompt: str) -> None:
    """Enforces 2026-11-04 User Directive: rejects prompts containing parenthetical example lists or anchoring menus."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be a non-empty string.")
    match = _ANCHORING_PATTERN.search(prompt)
    if match:
        raise ValueError(
            f"PROMPT_ANCHORING_VIOLATION: Forbidden exemplar/anchoring phrase detected ({match.group(0)!r}). "
            "Present only raw empirical observations and the objective mathematical goal."
        )


def build_cli_command(
    provider: str,
    config: Dict[str, Any],
    work_dir: Path,
    prompt: str,
) -> Tuple[List[str], Dict[str, str], Optional[str]]:
    """Constructs fixed `argv` (`shell=False`), sanitized `env`, and optional `stdin` payload."""
    validate_zero_anchoring_prompt(prompt)
    binary = str(config["binary"])
    model_name = str(config.get("model", "")).strip()
    model_args = ["--model", model_name] if model_name else []

    base_env_keys = ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG")
    env: Dict[str, str] = {k: os.environ[k] for k in base_env_keys if k in os.environ}

    if provider == "codex":
        if model_name != "gpt-6-astra":
            raise ValueError(f"CODEX_CONTRACT_VIOLATION: Expected model 'gpt-6-astra', got {model_name!r}")
        auth_keys = ("OPENAI_API_KEY", "CODEX_HOME")
        argv = [
            binary,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            *model_args,
            prompt,
        ]
        stdin_payload = None
    elif provider == "claude":
        if model_name != "claude-opus-5-5":
            raise ValueError(f"CLAUDE_CONTRACT_VIOLATION: Expected model 'claude-opus-5-5', got {model_name!r}")
        auth_keys = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR")
        argv = [
            binary,
            "--print",
            "--safe-mode",
            "--restricted",
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format",
            "json",
            *model_args,
        ]
        stdin_payload = prompt
    elif provider == "grok":
        auth_keys = ("XAI_API_KEY",)
        prompt_file = work_dir / "brief.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv = [
            binary,
            "--prompt-file",
            str(prompt_file),
            "--tools",
            "",
            "--no-subagents",
            "--no-memory",
            "--disable-web-search",
            "--permission-mode",
            "dontAsk",
            "--deny",
            "*",
            "--max-turns",
            "1",
            "--output-format",
            "json",
            "--verbatim",
            *model_args,
        ]
        stdin_payload = None
    elif provider == "agy":
        auth_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        argv = [
            binary,
            "--print",
            prompt,
            "--output-format",
            "stream-json",
            "--mode",
            "plan",
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            "80s",
            *model_args,
        ]
        stdin_payload = None
    else:
        raise ValueError(f"Unknown AI Council provider: {provider!r}")

    for k in auth_keys:
        if k in os.environ:
            env[k] = os.environ[k]

    protected_root = config.get("_protected_root")
    if protected_root:
        profile = work_dir / "project-boundary.sb"
        resolved_root = str(Path(protected_root).resolve())
        profile.write_text(
            f"(version 1)\n(allow default)\n(deny file-read* file-write* (subpath {json.dumps(resolved_root)}))\n",
            encoding="utf-8",
        )
        argv = ["/usr/bin/sandbox-exec", "-f", str(profile), *argv]

    return argv, env, stdin_payload


def decode_cli_response(
    provider: str,
    result: Dict[str, Any],
    expected_model: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Validates returncode, model identity (`gpt-6-astra` / `claude-opus-5-5`), and zero tool activity."""
    if result.get("state"):
        return str(result["state"]), None

    stdout_txt = str(result.get("stdout", ""))
    stderr_txt = str(result.get("stderr", ""))
    combined_lower = f"{stdout_txt}\n{stderr_txt}".lower()

    if any(msg in combined_lower for msg in ("not logged in", "please run /login", "please sign in", "authentication required")):
        return "authentication_required", None
    if result.get("returncode") != 0:
        return "cli_error", None

    try:
        if provider == "agy":
            events = [json.loads(line) for line in stdout_txt.splitlines() if line.strip()]
            if any(
                e.get("step_update", {}).get("step_type") == "tool"
                or e.get("step_update", {}).get("subagent_info")
                for e in events
            ):
                return "unexpected_tool_activity", None
            finals = [e["result"] for e in events if e.get("event") == "result"]
            if len(finals) != 1 or finals[0].get("status") != "SUCCESS":
                return "invalid_output", None
            payload = finals[0]
        else:
            try:
                payload = json.loads(stdout_txt)
            except (ValueError, json.JSONDecodeError):
                if provider == "codex" and stdout_txt.strip():
                    payload = {"response": stdout_txt.strip()}
                else:
                    raise

        if not isinstance(payload, dict) or payload.get("is_error") or payload.get("error"):
            return "provider_error", None

        if expected_model:
            if provider == "codex":
                observed = (
                    [expected_model]
                    if (f"model: {expected_model}" in stderr_txt or f"--model {expected_model}" in stderr_txt)
                    else list(payload.get("modelUsage", {}))
                )
            elif provider == "claude":
                usage = payload.get("modelUsage", {})
                observed = [
                    k for k, v in usage.items()
                    if k == expected_model and (not isinstance(v, dict) or v.get("canonicalModel", k) == expected_model)
                ]
            elif provider == "grok":
                rep = str(payload.get("model") or payload.get("reported_model") or "")
                keys = list(payload.get("modelUsage", {}).keys())
                observed = [expected_model] if any(expected_model in k for k in keys + [rep]) else keys
            else:
                observed = [expected_model]
            if expected_model not in observed:
                return "model_not_verified", None

        if payload.get("permission_denials") or payload.get("subagent_stats", {}).get("spawned", 0):
            return "unexpected_tool_activity", None

        field_map = {"codex": "response", "claude": "result", "agy": "response", "grok": "text"}
        answer = payload.get(field_map.get(provider, "response")) or payload.get("result") or payload.get("text") or payload.get("response")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_OUTPUT_BYTES:
            return "invalid_output", None
        return "received_unreviewed", answer.strip()
    except (AttributeError, KeyError, TypeError, ValueError):
        return "invalid_output", None


def validate_helper_reviews(reviews: List[Dict[str, str]], advice_items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Verifies that `agy` explicitly reviewed every helper output with matching SHA-256 and disposition."""
    if not advice_items and not reviews:
        return []
    if not isinstance(reviews, list) or len(reviews) != len(advice_items):
        raise ValueError("Google Antigravity (agy) must review every received helper output.")
    expected = {a["id"]: a["sha256"] for a in advice_items}
    seen = set()
    for item in reviews:
        item_id = item.get("id", "")
        if item_id in seen or item_id not in expected:
            raise ValueError(f"Unknown or duplicate helper review id: {item_id!r}")
        if item.get("sha256") != expected[item_id]:
            raise ValueError(f"Helper review SHA-256 mismatch for {item_id!r}")
        if item.get("disposition") not in ("used", "partly_used", "rejected") or not str(item.get("reason", "")).strip():
            raise ValueError(f"Helper review for {item_id!r} requires a valid disposition and mathematical reason.")
        seen.add(item_id)
    return reviews


@contextmanager
def provider_lock(lock_dir: Path, provider: str, timeout_sec: float = 60.0) -> Iterator[bool]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{provider}.lock"
    with open(lock_path, "a") as handle:
        start_t = time.monotonic()
        acquired = False
        while time.monotonic() - start_t < timeout_sec:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


class AICouncilCLI:
    """Executes bounded non-interactive CLI calls to `gpt-6-astra` (`codex`) and `claude-opus-5-5` (`claude`)."""

    def __init__(
        self,
        contract_path: Path | str = DEFAULT_CONTRACT_PATH,
        lock_dir: Path | str = "data/pm_markets/ai_locks",
    ) -> None:
        self.contract_path = Path(contract_path)
        self.lock_dir = Path(lock_dir)
        self.contract = json.loads(self.contract_path.read_text(encoding="utf-8"))

    def get_provider_config(self, provider: str) -> Dict[str, Any]:
        providers = self.contract.get("providers", {})
        if provider not in providers:
            raise KeyError(f"Provider {provider!r} not configured in {self.contract_path}")
        return dict(providers[provider])

    def invoke_provider(
        self,
        provider: str,
        prompt: str,
        timeout_sec: float = 120.0,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Runs `provider` CLI under `flock` with streaming watchdog and returns `(status, answer, sha256)`."""
        cfg = self.get_provider_config(provider)
        if not cfg.get("enabled", False):
            return ("provider_disabled", None, None)

        with provider_lock(self.lock_dir, provider, timeout_sec=60.0) as acquired:
            if not acquired:
                return ("provider_busy", None, None)
            raw_res = self._run_subprocess_with_watchdog(provider, cfg, prompt, timeout_sec=timeout_sec)
            status, answer = decode_cli_response(provider, raw_res, expected_model=cfg.get("model"))
            digest = sha256_hex(answer) if answer else None
            return (status, answer, digest)

    @staticmethod
    def _run_subprocess_with_watchdog(
        provider: str,
        cfg: Dict[str, Any],
        prompt: str,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix=f"pm-ai-{provider}-") as tmp:
            work_dir = Path(tmp)
            argv, env, stdin_text = build_cli_command(provider, cfg, work_dir, prompt)
            empty_cwd = work_dir / "empty"
            empty_cwd.mkdir()

            proc = subprocess.Popen(
                argv,
                cwd=empty_cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            buffers = [bytearray(), bytearray()]
            sel = selectors.DefaultSelector()
            assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
            for stream, idx in ((proc.stdout, 0), (proc.stderr, 1)):
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ, idx)

            if stdin_text:
                proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()

            idle_timeout = float(cfg.get("idle_timeout", min(60.0, timeout_sec)))
            max_hard_ceiling = float(cfg.get("max_hard_ceiling", max(timeout_sec * 2.5, 300.0)))
            started = time.monotonic()
            last_activity = started
            state: Optional[str] = None

            try:
                while sel.get_map() or proc.poll() is None:
                    elapsed = time.monotonic() - started
                    idle_elapsed = time.monotonic() - last_activity
                    if elapsed >= max_hard_ceiling or (idle_elapsed >= idle_timeout and elapsed >= timeout_sec):
                        state = "timeout"
                        break
                    for key, _ in sel.select(timeout=0.1):
                        fobj = key.fileobj
                        try:
                            chunk = os.read(fobj.fileno(), 8192)  # type: ignore[arg-type]
                        except BlockingIOError:
                            continue
                        if not chunk:
                            sel.unregister(fobj)
                            fobj.close()  # type: ignore[union-attr]
                            continue
                        last_activity = time.monotonic()
                        buf = buffers[key.data]
                        room = MAX_OUTPUT_BYTES - len(buf)
                        buf.extend(chunk[: max(0, room)])
                        if len(chunk) > room:
                            state = "output_limit"
                    if state:
                        break
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                sel.close()

            return {
                "returncode": proc.returncode,
                "state": state,
                "stdout": buffers[0].decode("utf-8", errors="replace"),
                "stderr": buffers[1].decode("utf-8", errors="replace"),
            }
