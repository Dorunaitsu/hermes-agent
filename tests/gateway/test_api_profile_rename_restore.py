"""An API session of a multiplexed profile still resumes after ``hermes profile rename`` (#150651).

The binding receipt stores the route and origin profile derived from the profile NAME; restore
regenerates both from the current name. The rename's identity migration must move the receipt with
the session row, or every API session of the renamed profile is refused with ``profile_mismatch``.
"""
import json

import pytest

from hermes_state_local import API_BINDING_PREFIX
from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


async def _boot(root, homes):
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    await initialize_gateway_runtime(runner)
    return runner


def _shutdown(runner, homes):
    from gateway.runtime_ownership import process_ownership
    for authority in list(getattr(runner, 'session_authorities', None) or []):
        authority.db.close()
    for _, home in homes:
        process_ownership.release(home)


@pytest.mark.asyncio
async def test_renamed_profile_api_session_restores(tmp_path, monkeypatch):
    from gateway.session_api import bind_api_session, declared_api_session, restore_api_session
    from hermes_cli.profile_identity import _migrate_profile_identity
    root, homes = _reserve_homes(tmp_path, monkeypatch)
    runner = await _boot(root, homes)
    try:
        alpha = runner.session_authorities.for_home(homes[1][1])
        alpha.db.create_session('api-renamed', source='api_server')
        bind_api_session(alpha, 'api-renamed', declared_key='my-convo')
    finally:
        _shutdown(runner, homes)

    # What ``rename_profile`` does with no live multiplexer: move the home, then migrate identity.
    renamed = root / 'profiles' / 'gamma'
    homes[1][1].rename(renamed)
    assert _migrate_profile_identity('alpha', 'gamma', False)

    homes = [homes[0], ('gamma', renamed), homes[2]]
    runner = await _boot(root, homes)
    try:
        gamma = runner.session_authorities.for_home(renamed)
        receipt = json.loads(gamma.db.get_meta(API_BINDING_PREFIX + 'api-renamed'))
        assert receipt['entry']['session_key'] == 'agent:gamma:api_server:dm:api-renamed'
        gamma.sessions.clear()

        restored = restore_api_session(gamma, 'api-renamed')
        live = gamma.sessions[restored.session_id]
        assert (live.source.profile, live.route) == ('gamma', 'agent:gamma:api_server:dm:api-renamed')
        # The API edge binds again on the next request, and the caller's declared key still resolves.
        bind_api_session(gamma, 'api-renamed', declared_key='my-convo')
        assert declared_api_session(gamma.db, 'my-convo') == 'api-renamed'
    finally:
        _shutdown(runner, homes)
