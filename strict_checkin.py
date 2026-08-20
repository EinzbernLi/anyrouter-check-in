#!/usr/bin/env python3
"""Run the existing checker and add authoritative result semantics.

The upstream checker has two provider-specific blind spots:
- AnyRouter can explicitly call /api/user/sign_in, but a successful response with
  no balance delta usually means the account was already checked in.
- AgentRouter grants the daily quota while a fresh email/password login is being
  performed, so the upstream "before" balance is already post-login.

This wrapper keeps the proven transport/browser flow intact, stores a small
cross-run balance state, and emits one authoritative verdict per provider.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from utils.notify import notify

STATE_FILE = Path(os.getenv("CHECKIN_STATE_FILE", "checkin_state.json"))
STATE_VERSION = 1
EPSILON = 0.005
# AgentRouter is observed to behave on an approximately 24-hour cadence. Give
# scheduled runs a small grace window before declaring a fresh login unconfirmed.
AGENT_REWARD_GRACE_HOURS = 30.0

NOTIFICATION_ENV_KEYS = (
    "DINGDING_WEBHOOK",
    "EMAIL_USER",
    "EMAIL_PASS",
    "EMAIL_TO",
    "EMAIL_SENDER",
    "CUSTOM_SMTP_SERVER",
    "PUSHPLUS_TOKEN",
    "SERVERPUSHKEY",
    "FEISHU_WEBHOOK",
    "WEIXIN_WEBHOOK",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GOTIFY_URL",
    "GOTIFY_TOKEN",
    "GOTIFY_PRIORITY",
    "BARK_KEY",
    "BARK_SERVER",
)

PROCESSING_RE = re.compile(r"Starting to process\s+(.+?)\s*$")
PROVIDER_RE = re.compile(r"^\[INFO\]\s+(.+?): Using provider \"([^\"]+)\"")
AUTH_RE = re.compile(r"^\[AUTH\]\s+(.+?): Using auth method ->\s*(.+?)\s*$")
MONEY_RE = re.compile(r"Current balance:\s*\$(-?\d+(?:\.\d+)?),\s*Used:\s*\$(-?\d+(?:\.\d+)?)")
SUMMARY_ACCOUNT_RE = re.compile(r"^\[CHECK-IN\]\s+(.+?)\s*$")
SUMMARY_BALANCE_RE = re.compile(
    r"余额:\s*\$(-?\d+(?:\.\d+)?)\s*\|\s*累计消耗:\s*\$(-?\d+(?:\.\d+)?)"
)


@dataclass
class Observation:
    name: str
    provider: str = ""
    auth_method: str = ""
    login_success: bool = False
    endpoint_success: bool = False
    explicit_already_checked: bool = False
    failed: bool = False
    current_quota: float | None = None
    current_used: float | None = None
    before_quota: float | None = None
    before_used: float | None = None
    after_quota: float | None = None
    after_used: float | None = None

    def current_total(self) -> float | None:
        quota = self.after_quota if self.after_quota is not None else self.current_quota
        used = self.after_used if self.after_used is not None else self.current_used
        if quota is None or used is None:
            return None
        return quota + used

    def before_total(self) -> float | None:
        if self.before_quota is None or self.before_used is None:
            return None
        return self.before_quota + self.before_used


@dataclass
class Verdict:
    name: str
    provider: str
    status: str
    detail: str
    total: float | None = None
    delta: float | None = None

    @property
    def is_strict_failure(self) -> bool:
        return self.status in {"FAILED", "UNCONFIRMED"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), dict):
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "accounts": {}}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    state["version"] = STATE_VERSION
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_output(lines: list[str]) -> list[Observation]:
    observations: dict[str, Observation] = {}
    current_name: str | None = None
    summary_name: str | None = None
    summary_phase: str | None = None

    def get(name: str) -> Observation:
        if name not in observations:
            observations[name] = Observation(name=name)
        return observations[name]

    for raw_line in lines:
        line = raw_line.strip()
        match = PROCESSING_RE.search(line)
        if match:
            current_name = match.group(1)
            summary_name = None
            summary_phase = None
            get(current_name)
            continue

        match = PROVIDER_RE.match(line)
        if match:
            name, provider = match.groups()
            get(name).provider = provider.strip().lower()
            current_name = name
            continue

        match = AUTH_RE.match(line)
        if match:
            name, method = match.groups()
            get(name).auth_method = method.strip().lower()
            continue

        match = SUMMARY_ACCOUNT_RE.match(line)
        if match:
            summary_name = match.group(1)
            summary_phase = None
            get(summary_name)
            continue

        if summary_name:
            if line == "签到前":
                summary_phase = "before"
                continue
            if line == "签到后":
                summary_phase = "after"
                continue
            match = SUMMARY_BALANCE_RE.search(line)
            if match and summary_phase:
                quota, used = (float(match.group(1)), float(match.group(2)))
                obs = get(summary_name)
                if summary_phase == "before":
                    obs.before_quota, obs.before_used = quota, used
                else:
                    obs.after_quota, obs.after_used = quota, used
                continue

        match = MONEY_RE.search(line)
        if match and current_name:
            obs = get(current_name)
            obs.current_quota = float(match.group(1))
            obs.current_used = float(match.group(2))

        if current_name:
            obs = get(current_name)
            if f"[SUCCESS] {current_name}: Login successful" in line:
                obs.login_success = True
            if f"[SUCCESS] {current_name}: Check-in successful!" in line:
                obs.endpoint_success = True
            if f"[SUCCESS] {current_name}: Already checked in today" in line:
                obs.endpoint_success = True
                obs.explicit_already_checked = True
            if line.startswith(f"[FAILED] {current_name}:"):
                obs.failed = True

    return list(observations.values())


def classify_anyrouter(obs: Observation) -> Verdict:
    total = obs.current_total()
    before = obs.before_total()
    delta = None if total is None or before is None else total - before

    if obs.failed:
        return Verdict(obs.name, obs.provider, "FAILED", "AnyRouter request flow reported a failure", total, delta)
    if obs.explicit_already_checked:
        return Verdict(obs.name, obs.provider, "ALREADY_CHECKED", "server explicitly reported an existing check-in", total, delta)
    if not obs.endpoint_success:
        return Verdict(obs.name, obs.provider, "UNCONFIRMED", "no confirmed /api/user/sign_in success was observed", total, delta)
    if delta is not None and delta > EPSILON:
        return Verdict(obs.name, obs.provider, "REWARDED", f"/api/user/sign_in succeeded and total quota increased by ${delta:.2f}", total, delta)
    if delta is not None and abs(delta) <= EPSILON:
        return Verdict(obs.name, obs.provider, "ALREADY_CHECKED", "/api/user/sign_in succeeded with no quota increase", total, delta)
    return Verdict(obs.name, obs.provider, "UNCONFIRMED", "/api/user/sign_in succeeded but before/after quota could not be verified", total, delta)


def classify_agentrouter(obs: Observation, previous: dict | None, now: datetime) -> Verdict:
    total = obs.current_total()
    if obs.failed:
        return Verdict(obs.name, obs.provider, "FAILED", "AgentRouter email/password flow reported a failure", total)
    if obs.auth_method != "email/password" or not obs.login_success:
        return Verdict(obs.name, obs.provider, "UNCONFIRMED", "fresh email/password login was not verified", total)
    if total is None:
        return Verdict(obs.name, obs.provider, "UNCONFIRMED", "fresh login succeeded but quota could not be read", total)

    if not previous or not isinstance(previous.get("total"), (int, float)):
        return Verdict(
            obs.name,
            obs.provider,
            "BASELINE_ESTABLISHED",
            f"fresh login verified; stored initial total quota ${total:.2f} for cross-run reward verification",
            total,
        )

    previous_total = float(previous["total"])
    delta = total - previous_total
    if delta > EPSILON:
        return Verdict(
            obs.name,
            obs.provider,
            "REWARDED",
            f"fresh login verified and total quota increased by ${delta:.2f} since the previous run",
            total,
            delta,
        )

    reference_at = parse_iso(previous.get("last_reward_at")) or parse_iso(previous.get("baseline_at"))
    if reference_at is None:
        reference_at = parse_iso(previous.get("observed_at"))
    age_hours = None if reference_at is None else (now - reference_at).total_seconds() / 3600

    if abs(delta) <= EPSILON and age_hours is not None and age_hours <= AGENT_REWARD_GRACE_HOURS:
        return Verdict(
            obs.name,
            obs.provider,
            "ALREADY_CHECKED",
            f"fresh login verified; quota unchanged within {age_hours:.1f}h of the current reward window",
            total,
            delta,
        )

    if abs(delta) <= EPSILON:
        age_text = "unknown age" if age_hours is None else f"{age_hours:.1f}h since the last reward baseline"
        return Verdict(
            obs.name,
            obs.provider,
            "UNCONFIRMED",
            f"fresh login verified but quota did not increase ({age_text})",
            total,
            delta,
        )

    return Verdict(
        obs.name,
        obs.provider,
        "UNCONFIRMED",
        f"fresh login verified but total quota decreased by ${abs(delta):.2f}; reward cannot be inferred safely",
        total,
        delta,
    )


def classify(observations: list[Observation], state: dict, now: datetime) -> list[Verdict]:
    accounts_state = state.setdefault("accounts", {})
    verdicts: list[Verdict] = []
    for obs in observations:
        provider = obs.provider or obs.name.lower()
        previous = accounts_state.get(provider)
        if provider == "anyrouter":
            verdict = classify_anyrouter(obs)
        elif provider == "agentrouter":
            verdict = classify_agentrouter(obs, previous, now)
        else:
            verdict = Verdict(obs.name, provider, "UNCONFIRMED", "unsupported provider in strict verifier", obs.current_total())
        verdicts.append(verdict)
    return verdicts


def update_state(state: dict, verdicts: list[Verdict], now: datetime) -> None:
    accounts_state = state.setdefault("accounts", {})
    now_text = iso(now)
    for verdict in verdicts:
        if verdict.total is None:
            continue
        previous = accounts_state.get(verdict.provider)
        record = dict(previous) if isinstance(previous, dict) else {}
        record.update(
            {
                "name": verdict.name,
                "provider": verdict.provider,
                "total": round(verdict.total, 6),
                "observed_at": now_text,
                "status": verdict.status,
            }
        )
        if "baseline_at" not in record:
            record["baseline_at"] = now_text
        if verdict.status == "REWARDED":
            record["last_reward_at"] = now_text
            record["baseline_at"] = now_text
        accounts_state[verdict.provider] = record


def build_summary(verdicts: list[Verdict], child_code: int) -> str:
    lines = ["[STRICT RESULT] Authoritative check-in verdict"]
    for verdict in verdicts:
        balance = "" if verdict.total is None else f" | total=${verdict.total:.2f}"
        lines.append(f"- {verdict.name} ({verdict.provider}): {verdict.status}{balance} | {verdict.detail}")
    if child_code != 0:
        lines.append(f"- upstream process exit code: {child_code}")
    lines.append("[STRICT RESULT] REWARDED/ALREADY_CHECKED are confirmed; BASELINE_ESTABLISHED is operationally healthy but not a reward claim.")
    return "\n".join(lines)


def has_notification_config() -> bool:
    return any(os.getenv(key, "").strip() for key in NOTIFICATION_ENV_KEYS)


def run_upstream() -> tuple[int, list[str]]:
    child_env = os.environ.copy()
    # Do not let the upstream's legacy result wording send a misleading message.
    # The wrapper sends one authoritative notification after classification.
    for key in NOTIFICATION_ENV_KEYS:
        child_env.pop(key, None)

    command = [sys.executable, "checkin.py"]
    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line.rstrip("\n"))
    return process.wait(), lines


def main() -> int:
    state = load_state()
    now = utc_now()
    child_code, lines = run_upstream()
    observations = parse_output(lines)

    if not observations:
        print("[STRICT RESULT] FAILED: no account observations were parsed", file=sys.stderr)
        return child_code or 2

    verdicts = classify(observations, state, now)
    update_state(state, verdicts, now)
    save_state(state)

    summary = build_summary(verdicts, child_code)
    print("\n" + summary)

    should_notify = any(v.status in {"REWARDED", "FAILED", "UNCONFIRMED", "BASELINE_ESTABLISHED"} for v in verdicts)
    if should_notify and has_notification_config():
        notify.push_message("Router Check-in Result", summary, msg_type="text")

    if child_code != 0 or any(v.is_strict_failure for v in verdicts):
        return child_code or 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
