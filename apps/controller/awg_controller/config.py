import base64
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class Settings:
    database_url: str
    remnawave_url: str
    remnawave_token: str
    admin_token: str
    gateway_token: str
    webhook_secret: str
    encryption_key: bytes
    agent_ca: str
    controller_cert: str
    controller_key: str
    reconcile_seconds: int = 30
    ready_max_age: int = 90
    quarantine_seconds: int = 300
    quota_policy: str = 'combined'
    encryption_key_id: str = 'default'
    previous_encryption_keys: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls):
        required = ['AWG_DATABASE_URL','REMNAWAVE_API_URL','REMNAWAVE_API_TOKEN','AWG_ADMIN_TOKEN',
                    'AWG_GATEWAY_TOKEN','AWG_WEBHOOK_SECRET','AWG_KEY_ENCRYPTION_KEY',
                    'AWG_AGENT_CA','AWG_CONTROLLER_CERT','AWG_CONTROLLER_KEY']
        for name in required:
            if not os.environ.get(name):
                raise ValueError('Missing setting: '+name)
        if os.environ['AWG_ADMIN_TOKEN'] == os.environ['AWG_GATEWAY_TOKEN']:
            raise ValueError('Admin and gateway credentials must differ')
        mode = os.environ.get('AWG_ENVIRONMENT', 'production')
        remnawave = urlsplit(os.environ['REMNAWAVE_API_URL'])
        # Plain HTTP only to a container on the private Docker network (single-label name, e.g. `remnawave`),
        # exactly how the stock subscription page talks to the panel.
        internal = re.fullmatch(r'[a-z0-9][a-z0-9_-]*', remnawave.hostname or '') is not None
        if remnawave.scheme != 'https' and mode != 'development' and not internal:
            raise ValueError('Remnawave API requires TLS unless it is a container on the private Docker network')
        policy = os.environ.get('AWG_QUOTA_POLICY','combined')
        if policy not in {'combined','awg_only','observe_only'}:
            raise ValueError('Invalid quota policy')
        return cls(*(os.environ[name] for name in required[:6]),
            base64.b64decode(os.environ['AWG_KEY_ENCRYPTION_KEY'], validate=True),
            *(os.environ[name] for name in required[7:]),
            reconcile_seconds=max(5,int(os.environ.get('AWG_RECONCILE_SECONDS','30'))),
            ready_max_age=max(10,int(os.environ.get('AWG_READY_MAX_AGE','90'))),
            quarantine_seconds=max(0,int(os.environ.get('AWG_QUARANTINE_SECONDS','300'))), quota_policy=policy,
            encryption_key_id=os.environ.get('AWG_KEY_ENCRYPTION_KEY_ID','default'),
            # Comma-separated `key_id:base64key` entries still needed to decrypt older rows.
            previous_encryption_keys={item.split(':',1)[0]: base64.b64decode(item.split(':',1)[1], validate=True)
                for item in os.environ.get('AWG_KEY_ENCRYPTION_PREVIOUS_KEYS','').split(',') if item.strip()})
