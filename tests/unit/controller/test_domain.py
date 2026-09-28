from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from cryptography.exceptions import InvalidTag
from awg_controller.domain import KeyVault, counter_delta, eligible, free_address
from remnawave_client import User


def user(**changes):
    data = dict(uuid=str(uuid4()), shortUuid='shortid', username='alice', status='ACTIVE',
                expireAt=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                activeInternalSquads=[], trafficLimitBytes=1000,
                userTraffic={'usedTrafficBytes': 100}, lastTrafficResetAt=None)
    data.update(changes)
    return User.model_validate(data)


def test_key_encryption_is_random_and_bound_to_identity():
    vault = KeyVault(b'k'*32)
    owner = str(uuid4())
    secret, public = vault.generate(owner)
    assert len(vault.decrypt(owner, secret)) == 44
    assert public not in secret
    with pytest.raises(InvalidTag):
        vault.decrypt(str(uuid4()), secret)


@pytest.mark.parametrize('status', ['DISABLED', 'LIMITED', 'EXPIRED'])
def test_inactive_users_have_no_entitlement(status):
    u = user(status=status)
    assert not eligible(u, [], [u.user_uuid], 0, 'combined')


def test_squad_and_combined_quota_and_expiration():
    squad = uuid4()
    u = user(activeInternalSquads=[{'uuid': str(squad), 'name': 'premium'}])
    assert eligible(u, [squad], [], 899, 'combined')
    assert not eligible(u, [squad], [], 900, 'combined')
    assert not eligible(u, [], [], 0, 'combined')
    assert eligible(u, [squad], [], 900, 'awg_only')
    assert not eligible(user(expireAt='2000-01-01T00:00:00Z'), [], [u.user_uuid], 0, 'combined')


def test_counter_duplicate_reset_and_new_epoch():
    assert counter_delta(100, 130, False) == 30
    assert counter_delta(100, 100, False) == 0
    assert counter_delta(100, 20, False) == 20
    assert counter_delta(100, 20, True) == 20


def test_ipam_reserves_server_and_detects_exhaustion():
    assert free_address('10.81.0.0/29', {'10.81.0.2'}, '10.81.0.1/29') == '10.81.0.3'
    with pytest.raises(ValueError, match='exhausted'):
        free_address('10.81.0.0/30', {'10.81.0.2'}, '10.81.0.1/30')


def test_encryption_key_rotation_decrypts_old_and_reencrypts():
    owner = str(uuid4())
    old = KeyVault(b'o'*32, key_id='old')
    blob, public = old.generate(owner)
    new = KeyVault(b'n'*32, key_id='new', previous_keys={'old':b'o'*32})
    rotated = new.encrypt(owner,new.decrypt(owner,blob))
    assert rotated.startswith('v1:new:')
    assert new.decrypt(owner,rotated) == old.decrypt(owner,blob)
    with pytest.raises(ValueError):
        KeyVault(b'n'*32,key_id='new').decrypt(owner,blob)


def test_legacy_blob_decrypts_with_any_configured_key():
    import base64, os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    owner = str(uuid4())
    nonce = os.urandom(12)
    legacy = base64.b64encode(nonce + AESGCM(b'o'*32).encrypt(nonce, b'secret', owner.encode())).decode()
    vault = KeyVault(b'n'*32, key_id='new', previous_keys={'old': b'o'*32})
    assert vault.decrypt(owner, legacy) == 'secret' and vault.needs_rotation(legacy)
    with pytest.raises(ValueError):
        KeyVault(b'n'*32, key_id='new').decrypt(owner, legacy)
