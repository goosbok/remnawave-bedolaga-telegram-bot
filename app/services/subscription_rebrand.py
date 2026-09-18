"""Pure builder for MAX-branded, full-tunnel subscription documents.

Turns a vendor's raw ``vless://``/``trojan://`` links into a base64
subscription document under our own branding. No routing header is emitted,
so clients fall back to a full tunnel (no vendor whitelist is carried over).

Pure module: no I/O, no network, no DB.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass


@dataclass
class RebrandedDocument:
    body: str  # base64 subscription document
    headers: dict[str, str]


def _rewrite_remark(link: str, remark: str) -> str:
    base = link.split('#', 1)[0]
    return f'{base}#{remark}'


def rebrand_links(links: list[str], *, title: str, remark_prefix: str, support_url: str = '') -> RebrandedDocument:
    rewritten = [_rewrite_remark(link, f'{remark_prefix} {i + 1}') for i, link in enumerate(links)]
    joined = '\n'.join(rewritten)
    body = base64.b64encode(joined.encode()).decode() if rewritten else ''
    # title/support_url come straight from config — strip so a stray newline
    # (or trailing whitespace) can never leak into an HTTP header value.
    headers = {'profile-title': title.strip(), 'profile-update-interval': '12'}
    support_url = support_url.strip()
    if support_url:
        headers['support-url'] = support_url
    return RebrandedDocument(body=body, headers=headers)
