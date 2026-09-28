import hashlib
import hmac
from remnawave_client import verify_webhook


def test_webhook_verifies_original_bytes_and_never_trusts_unsigned_body():
    body = b'{"event":"user.deleted","data":{}}'
    signature = hmac.new(b'secret', body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, signature, 'secret')
    assert not verify_webhook(body+b' ', signature, 'secret')
    assert not verify_webhook(body, signature, '')
