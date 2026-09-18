import base64

from app.services.subscription_rebrand import rebrand_links


def test_rewrites_remark_and_omits_routing():
    links = [
        'vless://a@de.example:443?type=tcp#Netherlands 1',
        'trojan://b@nl.example:443#Obhod Kaskad',
    ]
    doc = rebrand_links(links, title='MAX VPN', remark_prefix='MAX')
    decoded = base64.b64decode(doc.body).decode()
    lines = decoded.strip().splitlines()
    assert lines[0].endswith('#MAX 1')
    assert lines[1].endswith('#MAX 2')
    # no whitelist routing header → full tunnel by default
    assert 'routing' not in {k.lower() for k in doc.headers}
    assert doc.headers['profile-title'] == 'MAX VPN'


def test_empty_links_produce_empty_body():
    doc = rebrand_links([], title='MAX VPN', remark_prefix='MAX')
    assert doc.body == '' or base64.b64decode(doc.body).decode() == ''
