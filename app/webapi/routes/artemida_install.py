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
    return {'status': 'Активна' if active else 'Неактивна', 'expires': expires or '—', 'active': '1' if active else '0'}


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#05060f">
<meta name="robots" content="noindex">
<title>__BRAND__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    color-scheme: dark;
    --bg:#05060f; --ink:#eef1ff; --muted:#9aa2c9;
    --blue:#4f7cff; --violet:#a855f7; --cyan:#38e2ff;
    --grad:linear-gradient(135deg,#4f7cff 0%,#8b5bff 55%,#c04bff 100%);
    --line:rgba(139,123,255,.16);
    --glass:rgba(20,22,45,.55);
    --glow:0 0 40px rgba(120,90,255,.35);
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    color:var(--ink); background:var(--bg); min-height:100vh; overflow-x:hidden;
    -webkit-font-smoothing:antialiased; position:relative;
  }
  /* deep-space gradient + aurora */
  body::before{
    content:""; position:fixed; inset:-20%; z-index:-2; pointer-events:none;
    background:
      radial-gradient(60% 45% at 22% 8%, rgba(79,124,255,.30), transparent 60%),
      radial-gradient(55% 45% at 82% 12%, rgba(168,85,247,.26), transparent 62%),
      radial-gradient(70% 60% at 50% 120%, rgba(56,226,255,.12), transparent 60%),
      #05060f;
    filter:saturate(1.05);
  }
  /* star field */
  .stars,.stars2{position:fixed;inset:0;z-index:-1;pointer-events:none}
  .stars{
    background-image:
      radial-gradient(1px 1px at 20px 30px,#ffffff,transparent),
      radial-gradient(1px 1px at 140px 80px,rgba(255,255,255,.8),transparent),
      radial-gradient(1px 1px at 60px 160px,rgba(200,215,255,.9),transparent),
      radial-gradient(1.4px 1.4px at 220px 40px,#dfe7ff,transparent),
      radial-gradient(1px 1px at 300px 200px,rgba(255,255,255,.7),transparent),
      radial-gradient(1px 1px at 380px 120px,rgba(190,205,255,.8),transparent);
    background-size:400px 260px; opacity:.55; animation:tw 6s ease-in-out infinite;
  }
  .stars2{
    background-image:
      radial-gradient(1px 1px at 90px 20px,rgba(255,255,255,.6),transparent),
      radial-gradient(1.6px 1.6px at 260px 150px,#cfe0ff,transparent),
      radial-gradient(1px 1px at 30px 220px,rgba(255,255,255,.5),transparent);
    background-size:520px 320px; opacity:.4; animation:tw 9s ease-in-out infinite reverse;
  }
  @keyframes tw{0%,100%{opacity:.35}50%{opacity:.7}}
  .wrap{max-width:600px;margin:0 auto;padding:44px 18px 60px;position:relative}

  /* hero */
  .hero{display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:30px;animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
  .orb{position:relative;width:96px;height:96px;border-radius:50%;
    background:radial-gradient(circle at 34% 30%,#7aa0ff 0%,#5a6cff 30%,#7d43e6 62%,#3a1f66 100%);
    box-shadow:0 0 0 1px rgba(150,130,255,.35), 0 0 44px 8px rgba(120,90,255,.55), inset -8px -10px 26px rgba(0,0,0,.55), inset 8px 8px 22px rgba(180,200,255,.30);
    animation:float 6s ease-in-out infinite}
  .orb::after{content:"";position:absolute;inset:-14px;border-radius:50%;
    background:conic-gradient(from 0deg,rgba(79,124,255,0),rgba(168,85,247,.55),rgba(56,226,255,.5),rgba(79,124,255,0));
    filter:blur(8px);opacity:.6;animation:spin 9s linear infinite;z-index:-1}
  .orb b{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:26px;letter-spacing:1px;color:#eaf0ff;
    text-shadow:0 0 14px rgba(180,200,255,.8),0 1px 0 rgba(0,0,0,.4)}
  .word{margin-top:18px;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:30px;letter-spacing:.5px;line-height:1}
  .word .g{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
  .word .v{margin-left:8px;color:var(--ink);opacity:.92}
  .tag{margin-top:9px;color:var(--muted);font-size:13px;letter-spacing:3px;text-transform:uppercase}

  /* cards */
  .card{position:relative;background:var(--glass);border:1px solid var(--line);border-radius:20px;
    padding:18px;margin-bottom:16px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
    box-shadow:0 20px 50px -30px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.04);
    animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
  .card:nth-of-type(2){animation-delay:.06s}
  .card:nth-of-type(3){animation-delay:.12s}
  .info{display:flex;gap:12px;flex-wrap:wrap}
  .cell{flex:1 1 150px;background:rgba(10,12,28,.5);border:1px solid var(--line);border-radius:14px;padding:13px 14px}
  .cell .k{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
  .cell .v{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
  .dot{width:9px;height:9px;border-radius:50%;background:#25d366;box-shadow:0 0 0 4px rgba(37,211,102,.16);animation:pulse 2s infinite}

  h2{font-family:'Space Grotesk',sans-serif;font-size:17px;margin:2px 0 16px;display:flex;align-items:center;gap:9px}
  h2::before{content:"";width:20px;height:20px;border-radius:6px;background:var(--grad);box-shadow:0 0 16px rgba(120,90,255,.6)}

  .tabs,.apptabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .tab,.apptab{padding:9px 14px;border-radius:12px;background:rgba(12,14,32,.7);border:1px solid var(--line);
    color:#cfd4f5;font-size:13.5px;font-weight:500;cursor:pointer;transition:.18s;user-select:none}
  .tab:hover,.apptab:hover{border-color:rgba(139,123,255,.4);color:#fff;transform:translateY(-1px)}
  .apptab{padding:7px 13px;font-size:13px;border-radius:11px}
  .tab.active,.apptab.active{background:var(--grad);border-color:transparent;color:#fff;
    box-shadow:0 8px 22px -8px rgba(120,90,255,.8)}

  .step{display:flex;gap:14px;padding:16px 0;border-top:1px solid rgba(139,123,255,.10)}
  .step:first-child{border-top:0;padding-top:6px}
  .step .n{flex:0 0 30px;height:30px;border-radius:50%;background:linear-gradient(135deg,rgba(79,124,255,.28),rgba(168,85,247,.28));
    border:1px solid rgba(139,123,255,.45);color:#c9d3ff;display:flex;align-items:center;justify-content:center;
    font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:14px}
  .step .body{flex:1;min-width:0}
  .step .t{font-weight:600;margin-bottom:5px;font-size:15px}
  .step .d{color:var(--muted);font-size:13.5px;line-height:1.5;margin-bottom:12px}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:12px 20px;border-radius:13px;text-decoration:none;
    font-size:14px;font-weight:600;letter-spacing:.2px;transition:.18s;border:1px solid transparent}
  .btn.primary{background:var(--grad);color:#fff;box-shadow:0 10px 28px -10px rgba(120,90,255,.85),0 0 0 1px rgba(255,255,255,.06) inset}
  .btn.primary:hover{transform:translateY(-2px);box-shadow:0 16px 34px -10px rgba(140,100,255,1),0 0 26px rgba(120,90,255,.55)}
  .btn.primary::before{content:"";width:8px;height:8px;border-radius:50%;background:#fff;box-shadow:0 0 10px #fff;opacity:.9}

  .copy{display:flex;gap:8px;margin-top:14px}
  .copy input{flex:1;min-width:0;background:rgba(8,10,22,.7);border:1px solid var(--line);border-radius:12px;
    color:#aeb6e0;padding:12px 13px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .copy button{background:rgba(12,14,32,.8);color:#cfd4f5;border:1px solid var(--line);border-radius:12px;
    padding:0 16px;font-size:13px;font-weight:600;cursor:pointer;transition:.18s;white-space:nowrap}
  .copy button:hover{border-color:rgba(139,123,255,.5);color:#fff}
  .hint{color:var(--muted);font-size:12.5px;line-height:1.5;margin-top:12px}
  .support{display:inline-flex;align-items:center;gap:8px;margin-top:14px;padding:11px 16px;border-radius:12px;
    background:rgba(12,14,32,.7);border:1px solid var(--line);color:#c6cffb;text-decoration:none;font-weight:600;font-size:13.5px;transition:.18s}
  .support:hover{border-color:rgba(139,123,255,.5);color:#fff;transform:translateY(-1px)}
  .foot{text-align:center;margin-top:26px;color:var(--muted);font-size:12px;letter-spacing:2px;opacity:.7}

  @keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 4px rgba(37,211,102,.16)}50%{box-shadow:0 0 0 7px rgba(37,211,102,.05)}}
  @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
</head>
<body>
<div class="stars"></div>
<div class="stars2"></div>
<main class="wrap">
  <header class="hero">
    <div class="orb"><b>M</b></div>
    <div class="word"><span class="g">MAX</span><span class="v">VPN</span></div>
    <div class="tag">космос без границ</div>
  </header>

  <section class="card info">
    <div class="cell"><div class="k">Статус</div><div class="v"><span class="dot" id="statusDot"></span>__STATUS__</div></div>
    <div class="cell"><div class="k">Действует до</div><div class="v">__EXPIRES__</div></div>
  </section>

  <section class="card">
    <h2>Установка</h2>
    <div class="tabs" id="platformTabs"></div>
    <div class="apptabs" id="appTabs"></div>
    <div id="steps"></div>
    <div class="copy">
      <input id="suburl" readonly value="__SUBURL__">
      <button id="copyBtn" type="button">Копировать</button>
    </div>
    <div class="hint">Не открылось автоматически? Установите приложение, затем нажмите «Добавить подписку» ещё раз — или добавьте ссылку вручную.</div>
    __SUPPORT_BLOCK__
  </section>

  <div class="foot">MAX&nbsp;VPN</div>
</main>
<script type="application/json" id="data">__DATA_JSON__</script>
<script>
  const PLATFORMS = JSON.parse(document.getElementById('data').textContent);
  const SUB_URL = __SUBURL_JSON__;
  if (document.getElementById('statusDot') && !__ACTIVE__) document.getElementById('statusDot').style.background = '#ff5470';
  function detectPlatform(){
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
  function esc(s){ const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function safeLink(l){ return /^(https?|happ|incy|clash|v2raytun|v2rayng|hiddify|karing|streisand|sing-box|nekobox|flclash|flclashx|koala-clash|prizrak-box):/i.test(l || '') ? l : '#'; }
  function render(){
    const plat = PLATFORMS.find(p => p.key === curPlatform) || PLATFORMS[0];
    if (!plat) return;
    if (curApp >= plat.apps.length) curApp = 0;
    document.getElementById('platformTabs').innerHTML = PLATFORMS.map(p =>
      `<div class="tab ${p.key===curPlatform?'active':''}" data-k="${esc(p.key)}">${esc(p.title)}</div>`).join('');
    document.getElementById('appTabs').innerHTML = plat.apps.map((a,i) =>
      `<div class="apptab ${i===curApp?'active':''}" data-i="${i}">${esc(a.name)}</div>`).join('');
    const app = plat.apps[curApp];
    const steps = [];
    if (app.store) steps.push({ t:'Установите ' + app.name, d:'Откройте страницу и установите приложение. При запуске разрешите добавление VPN-конфигурации.', b:[{t:'Открыть страницу приложения', l:app.store}] });
    if (app.add) steps.push({ t:'Добавьте подписку', d:'Нажмите кнопку — откроется ' + app.name + ', и подписка добавится автоматически.', b:[{t:'Добавить подписку', l:app.add}] });
    steps.push({ t:'Подключитесь', d:'Выберите сервер в списке и нажмите кнопку включения. Если сервер работает медленно — вернитесь в список и выберите другой.', b:[] });
    document.getElementById('steps').innerHTML = steps.map((s,i) =>
      `<div class="step"><div class="n">${i+1}</div><div class="body"><div class="t">${esc(s.t)}</div><div class="d">${esc(s.d)}</div>${s.b.map(btn => `<a class="btn primary" href="${esc(safeLink(btn.l))}">${esc(btn.t)}</a>`).join(' ')}</div></div>`).join('');
  }
  document.getElementById('platformTabs').addEventListener('click', e => { const k = e.target.getAttribute('data-k'); if (k){ curPlatform = k; curApp = 0; render(); } });
  document.getElementById('appTabs').addEventListener('click', e => { const i = e.target.getAttribute('data-i'); if (i !== null){ curApp = parseInt(i,10); render(); } });
  document.getElementById('copyBtn').addEventListener('click', () => { const el = document.getElementById('suburl'); el.select(); try { navigator.clipboard.writeText(SUB_URL); } catch(e){ try { document.execCommand('copy'); } catch(_){} } const b = document.getElementById('copyBtn'); b.textContent = 'Скопировано ✓'; setTimeout(()=>b.textContent='Копировать', 1500); });
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
        .replace('__ACTIVE__', 'true' if info['active'] == '1' else 'false')
        .replace('__SUBURL__', html.escape(sub_url, quote=True))
        .replace('__SUPPORT_BLOCK__', support_block)
        .replace('__DATA_JSON__', data_json)
        .replace('__SUBURL_JSON__', json.dumps(sub_url))
    )
