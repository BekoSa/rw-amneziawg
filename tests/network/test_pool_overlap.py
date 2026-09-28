from scripts.check_pool_overlap import conflicts

CONFIG = {'networks': {'lab': {'ipam': {'config': [{'subnet': '10.240.0.0/24'}]}}}}

def test_reject_vpn_pool_overlap():
    assert conflicts(CONFIG, [{'dst': '10.240.0.0/16', 'dev': 'throne-tun'}])

def test_default_route_is_not_pool_conflict():
    assert not conflicts(CONFIG, [{'dst': 'default', 'dev': 'throne-tun'}])

def test_separate_lan_safe():
    assert not conflicts(CONFIG, [{'dst': '192.168.5.0/24', 'dev': 'eth0'}])
