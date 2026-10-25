"""Build three isolated, fully wired split-page design candidates."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'web/split.html').read_text(encoding='utf-8')
destination = ROOT / 'web/split-concepts'
destination.mkdir(exist_ok=True)
directions = {
    'graphite': ('A', '石墨工作台', '专注每一次创作', '导入素材，配置语言，让字幕制作有条不紊。'),
    'paper': ('B', '纸感创作室', '让好内容，清晰表达。', '从一段音视频开始，完成听写、切分与翻译。'),
    'studio': ('C', '媒体工作室', '下一条精彩，从这里开始。', '素材、参数与处理进度，在同一个工作空间就位。'),
}
for key, (letter, name, title, subtitle) in directions.items():
    html = source.replace('</head>', '<link rel="stylesheet" href="/assets/split-concepts/concepts.css?v=1">\n</head>')
    html = html.replace('<title>切分 · Substar</title>', f'<title>{letter} · {name} · Substar</title>')
    html = html.replace('<main class="page-shell">', f'<main class="page-shell concept-page" data-concept="{key}">')
    toolbar = '<div class="concept-switcher"><span class="concept-label">DESIGN EXPLORATION <b>切分页 · 三个方向</b></span><nav aria-label="切换设计方案">'
    for other, (code, label, *_rest) in directions.items():
        toolbar += f'<button type="button" data-concept-choice="{other}" aria-pressed="{str(other == key).lower()}"><span>{code}</span>{label}</button>'
    toolbar += '</nav><a href="/split">原版 ↗</a></div>'
    intro = f'<section class="concept-intro"><div><span class="concept-eyebrow">SUBSTAR / {letter} — {name}</span><h1>{title}</h1><p>{subtitle}</p></div><span class="concept-local">✦ 本地字幕工作空间</span></section>'
    html = html.replace(f'data-concept="{key}">', f'data-concept="{key}">\n{toolbar}\n{intro}', 1)
    # Top bar and all existing IDs/scripts are copied without modification.
    html = html.replace('</body>', '<script src="/assets/split-concepts/concepts.js?v=1"></script>\n</body>')
    (destination / f'{key}.html').write_text(html, encoding='utf-8')
    assert re.search(r'<header class="app-bar app-header">.*?</header>', html, re.S).group() == re.search(r'<header class="app-bar app-header">.*?</header>', source, re.S).group()
    assert re.findall(r'\bid="([^"]+)"', source) == [i for i in re.findall(r'\bid="([^"]+)"', html) if not i.startswith('concept')]
    print(f'{key}: generated, all original IDs and top bar retained')

# A separate B refinement keeps the three original candidates available.
glass = (destination / 'paper.html').read_text(encoding='utf-8')
glass = glass.replace('data-concept="paper"', 'data-concept="paper" data-finish="dark-glass"', 1)
glass = glass.replace('</head>', '<link rel="stylesheet" href="/assets/split-concepts/dark-glass.css?v=1">\n</head>')
glass = re.sub(r'<div class="concept-switcher">.*?</nav><a href="/split">原版 ↗</a></div>', '''<div class="concept-switcher"><span class="concept-label">B / LIQUID GLASS <b>深色 · 液态玻璃</b></span><div class="glass-materials" role="group" aria-label="玻璃质感"><button type="button" data-glass-material="soft" aria-pressed="true">柔和</button><button type="button" data-glass-material="clear" aria-pressed="false">通透</button></div><a href="/assets/split-concepts/paper.html">对比原 B ↗</a></div>''', glass, count=1, flags=re.S)
glass = glass.replace('</body>', '<script src="/assets/split-concepts/dark-glass.js?v=1"></script>\n</body>')
(destination / 'dark-glass.html').write_text(glass, encoding='utf-8')
print('dark-glass: separate B refinement generated')

dark = (destination / 'paper.html').read_text(encoding='utf-8')
dark = dark.replace('data-concept="paper"', 'data-concept="paper" data-finish="paper-dark"', 1)
dark = dark.replace('</head>', '<link rel="stylesheet" href="/assets/split-concepts/paper-dark.css?v=1">\n</head>')
dark = re.sub(r'<div class="concept-switcher">.*?</nav><a href="/split">原版 ↗</a></div>', '', dark, count=1, flags=re.S)
dark = dark.replace('id="creationTutorialActions"', 'id="creationTutorialActions" hidden inert')
dark = dark.replace('</body>', '<script src="/assets/split-concepts/paper-dark.js?v=1"></script>\n</body>')
(destination / 'paper-dark.html').write_text(dark, encoding='utf-8')
print('paper-dark: original B in dark mode, with clearer configuration groups')
