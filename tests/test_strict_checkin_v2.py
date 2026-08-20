import strict_checkin_v2 as v2
from strict_checkin_v2 import _instrument_upstream_source, parse_output


def test_instrumentation_adds_explicit_before_after_balance_markers():
	source = (
		"\t\t\t\tprint(user_info_before['display'])\n"
		'\t\t\t\tuser_info_after = get_user_info(client, headers, user_info_url)\n'
		'\t\t\t\treturn success, user_info_before, user_info_after\n'
	)

	instrumented = _instrument_upstream_source(source)

	assert '[STRICT-BALANCE-BEFORE]' in instrumented
	assert '[STRICT-BALANCE-AFTER]' in instrumented


def test_parse_output_uses_explicit_anyrouter_postcheck_evidence():
	lines = [
		'[PROCESSING] Starting to process AnyRouter',
		'[INFO] AnyRouter: Using provider "anyrouter" (https://anyrouter.top)',
		':money: Current balance: $75.0, Used: $0.0',
		'[STRICT-BALANCE-BEFORE] AnyRouter: quota=$75.00, used=$0.00',
		'[SUCCESS] AnyRouter: Check-in successful!',
		'[STRICT-BALANCE-AFTER] AnyRouter: quota=$75.00, used=$0.00',
	]

	observations = parse_output(lines)

	assert len(observations) == 1
	obs = observations[0]
	assert obs.before_quota == 75.0
	assert obs.before_used == 0.0
	assert obs.after_quota == 75.0
	assert obs.after_used == 0.0


def test_parse_output_does_not_recurse_after_base_hook(monkeypatch):
	lines = [
		'[PROCESSING] Starting to process AnyRouter',
		'[INFO] AnyRouter: Using provider "anyrouter" (https://anyrouter.top)',
		':money: Current balance: $75.0, Used: $0.0',
		'[STRICT-BALANCE-BEFORE] AnyRouter: quota=$75.00, used=$0.00',
		'[SUCCESS] AnyRouter: Check-in successful!',
		'[STRICT-BALANCE-AFTER] AnyRouter: quota=$75.00, used=$0.00',
	]

	monkeypatch.setattr(v2.base, 'parse_output', v2.parse_output)
	observations = v2.parse_output(lines)

	assert len(observations) == 1
	assert observations[0].after_quota == 75.0
