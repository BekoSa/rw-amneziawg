"""Generate development mTLS credentials into named volumes, never host files."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NODE = os.getenv('AWG_NODE_ID', '00000000-0000-4000-8000-000000000001')
CONTROLLER = os.getenv('AWG_CONTROLLER_ID', '00000000-0000-4000-8000-000000000002')


# The Controller runs as 10001; the Agent runs as root with only NET_ADMIN (no DAC override),
# so each identity must be owned by the account that reads it.
OWNERS = {'/pki-ca': 0, '/pki-agent': 0, '/pki-controller': 10001}


def save(directory, name, value, private=False):
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o600 if private else 0o644)
    os.chown(path, OWNERS[directory], OWNERS[directory])


def key_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def generate():
    if Path('/pki-ca/ca.key').exists():
        ca_key = serialization.load_pem_private_key(Path('/pki-ca/ca.key').read_bytes(), None)
        ca = x509.load_pem_x509_certificate(Path('/pki-ca/ca.crt').read_bytes())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'AWG disposable lab CA')])
        now = datetime.now(timezone.utc)
        ca = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True).sign(ca_key, hashes.SHA256())
        save('/pki-ca', 'ca.key', key_bytes(ca_key), True)
        save('/pki-ca', 'ca.crt', ca.public_bytes(serialization.Encoding.PEM))
    for role, san, usage in [
        ('agent', x509.DNSName(f'{NODE}.agents.awg.internal'), ExtendedKeyUsageOID.SERVER_AUTH),
        ('controller', x509.UniformResourceIdentifier(f'spiffe://awg/controller/{CONTROLLER}'), ExtendedKeyUsageOID.CLIENT_AUTH),
    ]:
        directory = f'/pki-{role}'
        save(directory, 'ca.crt', ca.public_bytes(serialization.Encoding.PEM))
        if (Path(directory) / f'{role}.key').exists():
            continue
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)])).issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=7)).add_extension(x509.SubjectAlternativeName([san]), False).add_extension(x509.ExtendedKeyUsage([usage]), False).sign(ca_key, hashes.SHA256())
        save(directory, f'{role}.key', key_bytes(key), True)
        save(directory, f'{role}.crt', cert.public_bytes(serialization.Encoding.PEM))
    for directory, owner in OWNERS.items():
        for path in Path(directory).iterdir():
            os.chown(path, owner, owner)
    print('Disposable lab mTLS identities provisioned in named volumes')


if __name__ == '__main__':
    generate()
