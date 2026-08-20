from datetime import datetime, timedelta, timezone

from strict_checkin import Observation, classify_agentrouter, classify_anyrouter, parse_output


def test_anyrouter_success_without_delta_is_already_checked():
	obs = Observation(
		name='AnyRouter',
		provider='anyrouter',
		endpoint_success=True,
		before_quota=75.0,
		before_used=0.0,
		after_quota=75.0,
		after_used=0.0,
	)

	verdict = classify_anyrouter(obs)

	assert verdict.status == 'ALREADY_CHECKED'
	assert verdict.delta == 0


def test_anyrouter_positive_delta_is_rewarded():
	obs = Observation(
		name='AnyRouter',
		provider='anyrouter',
		endpoint_success=True,
		before_quota=75.0,
		before_used=0.0,
		after_quota=100.0,
		after_used=0.0,
	)

	verdict = classify_anyrouter(obs)

	assert verdict.status == 'REWARDED'
	assert verdict.delta == 25.0


def test_agentrouter_first_strict_run_establishes_baseline():
	now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
	obs = Observation(
		name='AgentRouter',
		provider='agentrouter',
		auth_method='email/password',
		login_success=True,
		after_quota=225.0,
		after_used=0.0,
	)

	verdict = classify_agentrouter(obs, None, now)

	assert verdict.status == 'BASELINE_ESTABLISHED'


def test_agentrouter_cross_run_increase_is_rewarded():
	now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
	previous = {
		'total': 200.0,
		'baseline_at': (now - timedelta(hours=6)).isoformat(),
		'observed_at': (now - timedelta(hours=6)).isoformat(),
	}
	obs = Observation(
		name='AgentRouter',
		provider='agentrouter',
		auth_method='email/password',
		login_success=True,
		after_quota=225.0,
		after_used=0.0,
	)

	verdict = classify_agentrouter(obs, previous, now)

	assert verdict.status == 'REWARDED'
	assert verdict.delta == 25.0


def test_agentrouter_unchanged_inside_reward_window_is_already_checked():
	now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
	previous = {
		'total': 225.0,
		'baseline_at': (now - timedelta(hours=6)).isoformat(),
		'last_reward_at': (now - timedelta(hours=6)).isoformat(),
		'observed_at': (now - timedelta(hours=6)).isoformat(),
	}
	obs = Observation(
		name='AgentRouter',
		provider='agentrouter',
		auth_method='email/password',
		login_success=True,
		after_quota=225.0,
		after_used=0.0,
	)

	verdict = classify_agentrouter(obs, previous, now)

	assert verdict.status == 'ALREADY_CHECKED'


def test_agentrouter_unchanged_after_grace_window_is_unconfirmed():
	now = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
	previous = {
		'total': 225.0,
		'baseline_at': (now - timedelta(hours=31)).isoformat(),
		'last_reward_at': (now - timedelta(hours=31)).isoformat(),
		'observed_at': (now - timedelta(hours=6)).isoformat(),
	}
	obs = Observation(
		name='AgentRouter',
		provider='agentrouter',
		auth_method='email/password',
		login_success=True,
		after_quota=225.0,
		after_used=0.0,
	)

	verdict = classify_agentrouter(obs, previous, now)

	assert verdict.status == 'UNCONFIRMED'


def test_parse_output_extracts_both_provider_paths():
	lines = [
		'[PROCESSING] Starting to process AnyRouter',
		'[INFO] AnyRouter: Using provider "anyrouter" (https://anyrouter.top)',
		':money: Current balance: $75.0, Used: $0.0',
		'[NETWORK] AnyRouter: Executing check-in',
		'[SUCCESS] AnyRouter: Check-in successful!',
		'[PROCESSING] Starting to process AgentRouter',
		'[INFO] AgentRouter: Using provider "agentrouter" (https://agentrouter.org)',
		'[SUCCESS] AgentRouter: Login successful, got 2 cookies',
		'[AUTH] AgentRouter: Using auth method -> email/password',
		':money: Current balance: $225.0, Used: $0.0',
		'[CHECK-IN] AnyRouter',
		'签到前',
		'余额: $75.00  |  累计消耗: $0.00',
		'签到后',
		'余额: $75.00  |  累计消耗: $0.00',
		'[CHECK-IN] AgentRouter',
		'签到前',
		'余额: $225.00  |  累计消耗: $0.00',
		'签到后',
		'余额: $225.00  |  累计消耗: $0.00',
	]

	observations = {item.provider: item for item in parse_output(lines)}

	assert observations['anyrouter'].endpoint_success is True
	assert observations['anyrouter'].before_quota == 75.0
	assert observations['anyrouter'].after_quota == 75.0
	assert observations['agentrouter'].login_success is True
	assert observations['agentrouter'].auth_method == 'email/password'
	assert observations['agentrouter'].after_quota == 225.0
