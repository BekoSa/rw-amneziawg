"""`python -m awg_tui` — terminal UI; `status`, `peers`, `reconcile` — one-shot commands for scripts."""
import asyncio
import os
import sys

from .api import APIError, ControllerAPI
from .app import AWGApp, human_bytes, peer_state


def client() -> ControllerAPI:
    token = os.environ.get('AWG_ADMIN_TOKEN', '')
    if not token:
        raise SystemExit('AWG_ADMIN_TOKEN is not set')
    return ControllerAPI(os.environ.get('AWG_CONTROLLER_URL', 'http://controller:8080'), token)


async def one_shot(command: str) -> int:
    api = client()
    try:
        if command == 'status':
            for node in await api.items('nodes'):
                states = ', '.join(f'{d["state"]} rev {d.get("applied_revision")}/{d.get("desired_revision")}'
                                   for d in node['deployments']) or 'нет профиля'
                version = (node.get('capabilities') or {}).get('agent_version', 'unknown')
                print(f'{node["registration"]["name"]}: {"online" if node["online"] else "offline"} · {states} · {version}')
        elif command == 'peers':
            for peer in await api.items('peers'):
                print(f'{peer.get("username") or peer["user_id"]:24} {peer_state(peer):22} {peer["ipv4"]:15} '
                      f'↑{human_bytes(peer["rx_total"]):>10} ↓{human_bytes(peer["tx_total"]):>10}')
        elif command == 'reconcile':
            await api.reconcile()
            print('reconcile requested')
        else:
            print(__doc__)
            return 2
        return 0
    except APIError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    finally:
        await api.close()


def main():
    if len(sys.argv) > 1:
        raise SystemExit(asyncio.run(one_shot(sys.argv[1])))
    # The HTTP client lives on Textual's event loop and is closed there (AWGApp.on_unmount).
    AWGApp(client()).run()


if __name__ == '__main__':
    main()
