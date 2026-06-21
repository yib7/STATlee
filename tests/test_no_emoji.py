"""Guard: user-facing surfaces stay free of decorative emoji / pictographs
(they read as AI slop). Functional typography is explicitly allowed and NOT
flagged: arrows (U+2192 etc.), box-drawing / geometric diagram glyphs
(U+2500-25FF), and math signs (≈ ≤ ≥, U+2200-22FF).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGETS = [
    'statlee/templates/index.html',
    'statlee/templates/landing.html',
    'README.md',
    'statlee/static/js/api.js',
    'statlee/static/js/ui.js',
    'statlee/static/js/data.js',
    'statlee/static/js/analyze.js',
    'statlee/static/js/converse.js',
    'statlee/static/js/tools.js',
]


def _is_banned(ch):
    o = ord(ch)
    return (
        0x1F000 <= o <= 0x1FAFF       # emoji & pictographs
        or 0x2600 <= o <= 0x26FF      # misc symbols (e.g. high-voltage)
        or 0x2700 <= o <= 0x27BF      # dingbats (four-pointed star, checks, crosses)
        or 0x2B00 <= o <= 0x2BFF      # stars / misc symbols & arrows
        or o in (0x2122, 0x2139, 0x20E3, 0xFE0F)
    )


def test_user_facing_files_have_no_decorative_emoji():
    offenders = []
    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        with open(path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                bad = sorted({hex(ord(c)) for c in line if _is_banned(c)})
                if bad:
                    offenders.append(f"{rel}:{n} -> {bad}")
    assert not offenders, "Decorative emoji found:\n" + "\n".join(offenders)
