#!/usr/bin/env python3
"""Strict verifier with explicit AnyRouter before/after balance evidence.

The upstream checker already reads AnyRouter balance both before and after
/api/user/sign_in, but it only prints the pre-check value when there is no
notification-worthy balance change. This adapter instruments a temporary copy
of the upstream script at runtime so the existing request/login logic remains
unchanged while the strict verifier receives both values.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed local interpreter invocation only
import sys
from pathlib import Path

import strict_checkin as base

ORIGINAL_PARSE_OUTPUT = base.parse_output
RUNTIME_CHECKER = Path('.strict_checkin_runtime.py')
PRE_BALANCE_RE = re.compile(r'^\[STRICT-BALANCE-BEFORE\]\s+(.+?): quota=\$(-?\d+(?:\.\d+)?), used=\$(-?\d+(?:\.\d+)?)$')
POST_BALANCE_RE = re.compile(r'^\[STRICT-BALANCE-AFTER\]\s+(.+?): quota=\$(-?\d+(?:\.\d+)?), used=\$(-?\d+(?:\.\d+)?)$')


def _instrument_upstream_source(source: str) -> str:
	before_needle = "\t\t\t\tprint(user_info_before['display'])"
	before_replacement = (
		before_needle
		+ '\n\t\t\t\tprint('
		+ "\n\t\t\t\t\tf'[STRICT-BALANCE-BEFORE] {account_name}: '"
		+ '\n\t\t\t\t\tf\'quota=${user_info_before["quota"]:.2f}, used=${user_info_before["used_quota"]:.2f}\''
		+ '\n\t\t\t\t)'
	)

	after_needle = (
		'\t\t\t\tuser_info_after = get_user_info(client, headers, user_info_url)\n'
		'\t\t\t\treturn success, user_info_before, user_info_after'
	)
	after_replacement = (
		'\t\t\t\tuser_info_after = get_user_info(client, headers, user_info_url)'
		+ "\n\t\t\t\tif user_info_after and user_info_after.get('success'):"
		+ '\n\t\t\t\t\tprint('
		+ "\n\t\t\t\t\t\tf'[STRICT-BALANCE-AFTER] {account_name}: '"
		+ '\n\t\t\t\t\t\tf\'quota=${user_info_after["quota"]:.2f}, used=${user_info_after["used_quota"]:.2f}\''
		+ '\n\t\t\t\t\t)'
		+ '\n\t\t\t\treturn success, user_info_before, user_info_after'
	)

	if source.count(before_needle) != 1:
		raise RuntimeError('upstream pre-check balance print seam changed')
	if source.count(after_needle) != 1:
		raise RuntimeError('upstream manual-check post-balance seam changed')

	source = source.replace(before_needle, before_replacement, 1)
	return source.replace(after_needle, after_replacement, 1)


def run_upstream() -> tuple[int, list[str]]:
	child_env = base.os.environ.copy()
	for key in base.NOTIFICATION_ENV_KEYS:
		child_env.pop(key, None)

	source = Path('checkin.py').read_text(encoding='utf-8')
	instrumented = _instrument_upstream_source(source)
	RUNTIME_CHECKER.write_text(instrumented, encoding='utf-8')

	command = [sys.executable, str(RUNTIME_CHECKER)]
	try:
		process = subprocess.Popen(  # nosec B603 - fixed argv, shell=False
			command,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			encoding='utf-8',
			errors='replace',
			bufsize=1,
			env=child_env,
		)
		lines: list[str] = []
		assert process.stdout is not None
		for line in process.stdout:
			print(line, end='', flush=True)
			lines.append(line.rstrip('\n'))
		return process.wait(), lines
	finally:
		RUNTIME_CHECKER.unlink(missing_ok=True)


def parse_output(lines: list[str]) -> list[base.Observation]:
	observations = ORIGINAL_PARSE_OUTPUT(lines)
	by_name = {item.name: item for item in observations}

	for raw_line in lines:
		line = raw_line.strip()
		match = PRE_BALANCE_RE.match(line)
		if match:
			name, quota, used = match.groups()
			obs = by_name.get(name)
			if obs is not None:
				obs.before_quota = float(quota)
				obs.before_used = float(used)
			continue

		match = POST_BALANCE_RE.match(line)
		if match:
			name, quota, used = match.groups()
			obs = by_name.get(name)
			if obs is not None:
				obs.after_quota = float(quota)
				obs.after_used = float(used)
				obs.current_quota = float(quota)
				obs.current_used = float(used)

	return observations


def main() -> int:
	base.run_upstream = run_upstream
	base.parse_output = parse_output
	return base.main()


if __name__ == '__main__':
	raise SystemExit(main())
