"""Browser-facing install page for a provider (Artemida) subscription.

A VPN client fetching ``/a/{token}`` gets the rebranded config (see
``artemida_sub``). A *browser* opening the same link instead gets this MAX-branded
install page — the vendor subscription is not in our panel, so the stock
``remnawave/subscription-page`` (which reads the panel by token) cannot render it,
and we build our own.

Apps, their store links and the "add subscription" deep links are taken from the
SAME RemnaWave app-config the cabinet uses, so INCY/Happ/… and their schemes match
our normal subscriptions exactly.
"""

from __future__ import annotations

import html
import json
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

# Client user-agents that must always receive the config, never the HTML page.
_CLIENT_UA_MARKERS = (
    'happ',
    'v2ray',
    'sing-box',
    'clash',
    'hiddify',
    'karing',
    'nekobox',
    'foxray',
    'streisand',
    'shadowrocket',
    'stash',
    'loon',
    'quantumult',
    'incy',
    'ktor',
    'okhttp',
    'go-http',
    'substore',
    'xray',
    'flclash',
    'koala',
    'prizrak',
)


def wants_install_page(headers: dict[str, str]) -> bool:
    """True when the request looks like a human browser (serve HTML), not a client.

    A VPN client either announces itself in the User-Agent or sends the ``x-hwid``
    device header — both route to the config. Everything that looks like a browser
    (Mozilla/WebKit/Gecko) and carries no ``x-hwid`` gets the install page.
    """
    if headers.get('x-hwid'):
        return False
    ua = (headers.get('user-agent') or '').lower()
    if not ua:
        return False
    if any(marker in ua for marker in _CLIENT_UA_MARKERS):
        return False
    return any(token in ua for token in ('mozilla', 'applewebkit', 'gecko', 'chrome', 'safari', 'firefox'))


def _first_store_link(app: dict[str, Any]) -> str:
    for block in app.get('blocks') or []:
        for button in block.get('buttons') or []:
            link = str(button.get('link') or '')
            if button.get('type') == 'external' and link.startswith('http'):
                return link
    return ''


async def _collect_platforms(sub_url: str) -> list[dict[str, Any]]:
    """Build [{key, title, apps:[{name, store, add}]}] from the RemnaWave app-config."""
    # Imported lazily: the cabinet module pulls in a lot, and this route must stay
    # importable even if that graph changes.
    from app.cabinet.routes.subscription_modules.status import _create_deep_link, _load_app_config_async

    cfg = await _load_app_config_async()
    if not cfg:
        return []
    order = ['ios', 'android', 'macos', 'windows', 'linux', 'appleTV', 'androidTV']
    raw = cfg.get('platforms', {}) or {}
    titles = {
        'ios': 'iPhone / iPad',
        'android': 'Android',
        'macos': 'macOS',
        'windows': 'Windows',
        'linux': 'Linux',
        'appleTV': 'Apple TV',
        'androidTV': 'Android TV',
    }
    result: list[dict[str, Any]] = []
    for key in order + [k for k in raw if k not in order]:
        node = raw.get(key)
        apps_in = node.get('apps', []) if isinstance(node, dict) else (node if isinstance(node, list) else [])
        apps: list[dict[str, Any]] = []
        for app in apps_in:
            if not isinstance(app, dict):
                continue
            try:
                add = _create_deep_link(app, sub_url, None)
            except Exception:
                add = None
            apps.append({'name': str(app.get('name') or 'App'), 'store': _first_store_link(app), 'add': add or ''})
        if apps:
            result.append({'key': key, 'title': titles.get(key, key), 'apps': apps})
    return result


def _sub_info(subscription: Any) -> dict[str, str]:
    from datetime import UTC, datetime

    end = getattr(subscription, 'end_date', None)
    expires = ''
    if end is not None:
        try:
            expires = end.strftime('%d.%m.%Y')
        except Exception:
            expires = ''
    active = getattr(subscription, 'status', '') == 'active'
    if end is not None:
        try:
            e = end if end.tzinfo else end.replace(tzinfo=UTC)
            active = active and e > datetime.now(UTC)
        except Exception:
            pass
    return {'status': 'Активна' if active else 'Неактивна', 'expires': expires or '—'}


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>__BRAND__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f1226; color: #e8eaf6; -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 20px 16px 48px; }
  .brand { display:flex; align-items:center; gap:12px; margin: 8px 0 20px; }
  .brand .logo { width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,#5b6cff,#8b5bff);
    display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;font-size:15px; }
  .brand h1 { font-size:20px; margin:0; letter-spacing:.3px; }
  .card { background:#171a33; border:1px solid #262a4d; border-radius:16px; padding:16px; margin-bottom:16px; }
  .info { display:flex; gap:12px; flex-wrap:wrap; }
  .info .cell { flex:1 1 140px; background:#12152a; border:1px solid #23264a; border-radius:12px; padding:12px; }
  .info .k { font-size:12px; color:#9aa0c7; margin-bottom:4px; }
  .info .v { font-size:15px; font-weight:600; }
  h2 { font-size:16px; margin: 8px 0 12px; }
  .tabs, .apptabs { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  .tab { padding:8px 14px; border-radius:10px; background:#12152a; border:1px solid #23264a; color:#cfd3f0;
    font-size:14px; cursor:pointer; }
  .tab.active { background:#5b6cff; border-color:#5b6cff; color:#fff; }
  .apptab { padding:7px 12px; border-radius:9px; background:#12152a; border:1px solid #23264a; color:#cfd3f0;
    font-size:13px; cursor:pointer; }
  .apptab.active { background:#232a55; border-color:#5b6cff; color:#fff; }
  .step { display:flex; gap:12px; padding:14px 0; border-top:1px solid #23264a; }
  .step:first-child { border-top:0; }
  .step .n { flex:0 0 28px; height:28px; border-radius:50%; background:#232a55; color:#9db0ff;
    display:flex; align-items:center; justify-content:center; font-weight:700; font-size:14px; }
  .step .body { flex:1; }
  .step .t { font-weight:600; margin-bottom:4px; }
  .step .d { color:#9aa0c7; font-size:14px; line-height:1.45; margin-bottom:10px; }
  .btn { display:inline-flex; align-items:center; gap:8px; padding:11px 16px; border-radius:11px;
    text-decoration:none; font-size:14px; font-weight:600; }
  .btn.primary { background:#5b6cff; color:#fff; }
  .muted { color:#9aa0c7; font-size:13px; }
  .copy { display:flex; gap:8px; margin-top:8px; }
  .copy input { flex:1; min-width:0; background:#0d1024; border:1px solid #23264a; border-radius:10px; color:#cfd3f0;
    padding:10px 12px; font-size:12px; }
  .copy button { background:#12152a; color:#cfd3f0; border:1px solid #2b2f57; border-radius:10px; padding:0 14px;
    font-size:13px; cursor:pointer; }
  .support { display:inline-block; margin-top:12px; color:#9db0ff; text-decoration:none; font-weight:600; }
  .foot { text-align:center; margin-top:22px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="logo">MAX</div><h1>__BRAND__</h1></div>

  <div class="card info">
    <div class="cell"><div class="k">Статус</div><div class="v">__STATUS__</div></div>
    <div class="cell"><div class="k">Действует до</div><div class="v">__EXPIRES__</div></div>
  </div>

  <div class="card">
    <h2>Установка</h2>
    <div class="tabs" id="platformTabs"></div>
    <div class="apptabs" id="appTabs"></div>
    <div id="steps"></div>
    <div class="copy">
      <input id="suburl" readonly value="__SUBURL__">
      <button id="copyBtn" type="button">Копировать</button>
    </div>
    <div class="muted" style="margin-top:8px">Не открылось автоматически? Установите приложение, затем нажмите «Добавить подписку» ещё раз или добавьте ссылку вручную.</div>
    __SUPPORT_BLOCK__
  </div>

  <div class="foot muted">__BRAND__</div>
</div>
<script type="application/json" id="data">__DATA_JSON__</script>
<script>
  const PLATFORMS = JSON.parse(document.getElementById('data').textContent);
  const SUB_URL = __SUBURL_JSON__;
  function detectPlatform() {
    const ua = navigator.userAgent || '';
    if (/iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) return 'ios';
    if (/Android/.test(ua)) return 'android';
    if (/Mac OS X|Macintosh/.test(ua)) return 'macos';
    if (/Windows/.test(ua)) return 'windows';
    if (/Linux/.test(ua)) return 'linux';
    return (PLATFORMS[0] || {}).key;
  }
  let curPlatform = detectPlatform();
  if (!PLATFORMS.some(p => p.key === curPlatform)) curPlatform = (PLATFORMS[0] || {}).key;
  let curApp = 0;
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function safeLink(l) { return /^(https?|happ|incy|clash|v2raytun|v2rayng|hiddify|karing|streisand|sing-box|nekobox|flclash|flclashx|koala-clash|prizrak-box):/i.test(l || '') ? l : '#'; }
  function render() {
    const plat = PLATFORMS.find(p => p.key === curPlatform) || PLATFORMS[0];
    if (!plat) return;
    if (curApp >= plat.apps.length) curApp = 0;
    document.getElementById('platformTabs').innerHTML = PLATFORMS.map(p =>
      `<div class="tab ${p.key===curPlatform?'active':''}" data-k="${esc(p.key)}">${esc(p.title)}</div>`).join('');
    document.getElementById('appTabs').innerHTML = plat.apps.map((a,i) =>
      `<div class="apptab ${i===curApp?'active':''}" data-i="${i}">${esc(a.name)}</div>`).join('');
    const app = plat.apps[curApp];
    const steps = [];
    if (app.store) steps.push({ t: 'Установите ' + app.name, d: 'Откройте страницу и установите приложение. При запуске разрешите добавление VPN-конфигурации.', b: [{ t:'Открыть страницу приложения', l: app.store }] });
    if (app.add) steps.push({ t: 'Добавьте подписку', d: 'Нажмите кнопку — откроется ' + app.name + ', и подписка добавится автоматически.', b: [{ t:'Добавить подписку', l: app.add }] });
    steps.push({ t: 'Подключитесь', d: 'Выберите сервер в списке и нажмите кнопку включения. Если сервер работает медленно — вернитесь в список и выберите другой.', b: [] });
    document.getElementById('steps').innerHTML = steps.map((s,i) =>
      `<div class="step"><div class="n">${i+1}</div><div class="body"><div class="t">${esc(s.t)}</div><div class="d">${esc(s.d)}</div>${s.b.map(btn => `<a class="btn primary" href="${esc(safeLink(btn.l))}">${esc(btn.t)}</a>`).join(' ')}</div></div>`).join('');
  }
  document.getElementById('platformTabs').addEventListener('click', e => { const k = e.target.getAttribute('data-k'); if (k) { curPlatform = k; curApp = 0; render(); } });
  document.getElementById('appTabs').addEventListener('click', e => { const i = e.target.getAttribute('data-i'); if (i !== null) { curApp = parseInt(i,10); render(); } });
  document.getElementById('copyBtn').addEventListener('click', () => { const el = document.getElementById('suburl'); el.select(); try { navigator.clipboard.writeText(SUB_URL); } catch(e) { try { document.execCommand('copy'); } catch(_) {} } const b = document.getElementById('copyBtn'); b.textContent = 'Скопировано'; setTimeout(()=>b.textContent='Копировать', 1500); });
  render();
</script>
</body>
</html>"""


async def render_install_page(subscription: Any, *, sub_url: str, brand_title: str, support_url: str) -> str:
    platforms = await _collect_platforms(sub_url)
    info = _sub_info(subscription)
    data_json = json.dumps(platforms, ensure_ascii=False).replace('<', '\\u003c')
    support = html.escape(support_url or '')
    support_block = (
        f'<a class="support" href="{support}" target="_blank" rel="noopener">💬 Поддержка: {support}</a>'
        if support
        else ''
    )
    return (
        _HTML_TEMPLATE.replace('__BRAND__', html.escape(brand_title or 'VPN'))
        .replace('__STATUS__', html.escape(info['status']))
        .replace('__EXPIRES__', html.escape(info['expires']))
        .replace('__SUBURL__', html.escape(sub_url, quote=True))
        .replace('__SUPPORT_BLOCK__', support_block)
        .replace('__DATA_JSON__', data_json)
        .replace('__SUBURL_JSON__', json.dumps(sub_url))
    )
