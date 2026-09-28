import importlib.util


def test_capability_registry_exists():
    assert importlib.util.find_spec('awg_capabilities') is not None


def test_format_only_and_legacy_clients_never_become_mihomo():
    from awg_capabilities import identify
    for ua in ['', 'Clash/1.19.30', 'ClashX/1.118', 'Stash/2.4', 'sing-box/1.13.0', 'v2rayNG/1.9', 'Happ/3.1']:
        assert identify(ua).family != 'mihomo', ua


def test_client_families():
    from awg_capabilities import identify
    for ua in ['mihomo/1.19.30', 'mihomo/1.20.0', 'clash-verge/v2.2.3', 'FlClash/0.8.80', 'ClashMetaForAndroid/2.11',
               'ClashX Meta/1.4', 'Clash Nyanpasu/1.6', 'koala-clash/1.0']:
        assert identify(ua).renderer == 'mihomo', ua
    assert identify('Throne/1.2.0').renderer == 'throne'
    assert identify('INCY/3.8.8 (iOS)').renderer == 'incy'
    assert identify('v2rayNG/1.9').renderer == 'incy', 'generic URI lists get the canonical amneziawg:// line'
    assert identify('Happ/3.1.0').renderer is None, 'Happ documents no WireGuard/AmneziaWG support'
