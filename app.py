"""
md2pdf-web - Web app to convert Markdown to PDF.
Supports Mermaid, Obsidian callouts, tables, code blocks with syntax highlight,
links, images, nested lists, footnotes, strikethrough, highlights and more.

Usage:
    python app.py                    # Runs at http://localhost:8050
    python app.py --port 9000        # Custom port
    python app.py --host 0.0.0.0     # Expose on network (for server)
"""
import html as html_mod
import re
import os
import uuid
import tempfile
import unicodedata
from pathlib import Path

import markdown
import uvicorn
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from starlette.background import BackgroundTask

app = FastAPI(title="md2pdf", description="Markdown to PDF converter")


# ---------------------------------------------------------------------------
# Markdown -> HTML converter (python-markdown)
# ---------------------------------------------------------------------------

CALLOUT_COLORS = {
    "warning": "#e67e22", "caution": "#e74c3c", "important": "#3498db",
    "note": "#2ecc71", "tip": "#1abc9c", "info": "#3498db",
    "danger": "#e74c3c", "success": "#2ecc71", "question": "#f39c12",
    "example": "#9b59b6", "quote": "#95a5a6", "bug": "#e74c3c",
    "abstract": "#00b8d4", "todo": "#ff9800", "failure": "#e74c3c",
    "check": "#2ecc71",
}

CALLOUT_ICONS = {
    "warning": "\u26a0\ufe0f", "caution": "\U0001f534", "important": "\u2139\ufe0f",
    "note": "\U0001f4dd", "tip": "\U0001f4a1", "info": "\u2139\ufe0f",
    "danger": "\u26a1", "success": "\u2705", "question": "\u2753",
    "example": "\U0001f4cb", "quote": "\U0001f4ac", "bug": "\U0001f41b",
    "abstract": "\U0001f4c4", "todo": "\U0001f4cc", "failure": "\u274c",
    "check": "\u2705",
}

CALLOUT_TYPES_RE = "|".join(CALLOUT_COLORS.keys())

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "footnotes", "attr_list", "def_list", "nl2br"]


_LIST_ITEM_RE = re.compile(r"^([-*+]|\d+[.)])\s+")


def _ensure_blank_line_before_lists(text):
    """Inserts a blank line before a list that starts right after plain text.

    python-markdown requires a blank line before a list — unlike GitHub,
    Obsidian and CommonMark, it will NOT let a list interrupt a paragraph.
    Typing "Some text:" then, on the very next line, "1. foo" (a very common
    pattern) makes it treat the list lines as plain text glued to the
    paragraph with <br> instead of rendering a real <ol>/<ul>. This inserts
    the blank line python-markdown needs whenever a top-level list item
    immediately follows non-list text.
    """
    lines = text.split("\n")
    out = []
    for line in lines:
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if indent == 0 and _LIST_ITEM_RE.match(stripped) and out:
            prev = out[-1]
            prev_stripped = prev.lstrip(" \t")
            if prev.strip() != "" and not _LIST_ITEM_RE.match(prev_stripped):
                out.append("")
        out.append(line)
    return "\n".join(out)


def _normalize_list_indentation(text):
    """Remaps list indentation to multiples of 4 spaces.

    python-markdown only recognises a sub-list when it is indented by a full
    tab-stop (4 spaces) relative to its parent item. Most editors (and most
    people typing by hand) use 2-space indents for nested bullets, which
    python-markdown treats as siblings of the parent item instead of
    children — collapsing the whole hierarchy to one level. This walks the
    document tracking each list's original indent widths and rewrites them
    (and their continuation lines) to the 4-space steps python-markdown
    expects, regardless of how the source was indented.
    """
    lines = text.split("\n")
    out = []
    stack = []  # list of (orig_indent, new_indent)

    for line in lines:
        expanded = line.replace("\t", "    ")
        stripped = expanded.lstrip(" ")
        indent = len(expanded) - len(stripped)

        if stripped == "":
            out.append(line)
            continue

        if _LIST_ITEM_RE.match(stripped):
            while stack and stack[-1][0] > indent:
                stack.pop()
            if stack and stack[-1][0] == indent:
                new_indent = stack[-1][1]
            else:
                parent_new = stack[-1][1] if stack else -4
                new_indent = parent_new + 4
                stack.append((indent, new_indent))
            out.append(" " * new_indent + stripped)
            continue

        if not stack:
            out.append(line)
            continue

        if indent == 0:
            stack = []
            out.append(line)
            continue

        level = None
        for orig, new in reversed(stack):
            if orig < indent:
                level = (orig, new)
                break

        if level is None:
            stack = []
            out.append(line)
            continue

        shift = level[1] - level[0]
        out.append(" " * (indent + shift) + stripped)

    return "\n".join(out)


def _md(text):
    """Run the markdown parser on a piece of text."""
    text = _ensure_blank_line_before_lists(text)
    text = _normalize_list_indentation(text)
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


def md_to_html(md_text):
    """Converts Markdown to HTML with full support for all elements."""
    text = md_text
    placeholders = {}

    # --- PHASE 1: Extract Mermaid blocks (protect from parser) ---
    def save_mermaid(m):
        code = m.group(1).strip()
        key = f"MERMAID{uuid.uuid4().hex}"
        placeholders[key] = f'<div class="mermaid">\n{code}\n</div>'
        return f"\n\n{key}\n\n"
    text = re.sub(r"```mermaid\s*\n(.*?)```", save_mermaid, text, flags=re.DOTALL)

    # --- PHASE 1b: Extract indented fenced code blocks ---
    # python-markdown's fenced_code extension does NOT recognise ``` fences
    # that are indented (e.g. inside a list item or blockquote).  They end up
    # rendered as inline <code> — collapsing multi-line content to one line.
    # We extract them here and restore after markdown processing, just like
    # we do for Mermaid blocks.
    def save_indented_fence(m):
        lang = (m.group(2) or "").strip()
        code = m.group(3)
        # De-indent the code body: remove the same leading whitespace that
        # the fence itself had.
        indent = m.group(1)
        if indent:
            code = re.sub(r"^" + re.escape(indent), "", code, flags=re.MULTILINE)
        code = code.rstrip("\n")
        escaped = html_mod.escape(code)
        lang_attr = f' class="language-{lang}"' if lang else ""
        key = f"FENCEDCODE{uuid.uuid4().hex}"
        placeholders[key] = f'<pre><code{lang_attr}>{escaped}\n</code></pre>'
        return f"\n\n{key}\n\n"

    # Match fenced code blocks that start with 1+ spaces of indentation.
    # The regex captures: (indent)(```lang\n)(content)(indent```\n)
    text = re.sub(
        r"^([ \t]+)```(\w*)\s*\n(.*?)^\1```\s*$",
        save_indented_fence, text, flags=re.DOTALL | re.MULTILINE,
    )

    # --- PHASE 2: Convert Obsidian callouts ---
    def callout_block(m):
        ctype = m.group(1).lower()
        custom_title = (m.group(2) or "").strip()
        body = m.group(3).strip()
        body_lines = [re.sub(r"^>\s?", "", l) for l in body.split("\n")]
        body_html = _md("\n".join(body_lines))

        color = CALLOUT_COLORS.get(ctype, "#95a5a6")
        icon = CALLOUT_ICONS.get(ctype, "\U0001f4cc")
        title = custom_title if custom_title else ctype.upper()

        key = f"CALLOUT{uuid.uuid4().hex}"
        placeholders[key] = (
            f'<div class="callout" style="border-left:4px solid {color}; background:{color}10; '
            f'border-radius:6px; margin:16px 0; overflow:hidden;">'
            f'<div class="callout-title" style="padding:10px 16px; font-weight:600; '
            f'font-size:0.92em; color:{color};">{icon} {title}</div>'
            f'<div class="callout-body" style="padding:4px 16px 12px;">{body_html}</div>'
            f'</div>'
        )
        return f"\n\n{key}\n\n"

    text = re.sub(
        rf">\s*\[!({CALLOUT_TYPES_RE})\](.*?)\s*\n((?:>.*\n?)*)",
        callout_block, text, flags=re.IGNORECASE,
    )

    # --- PHASE 2b: Convert plain blockquotes to styled HTML ---
    # After callouts have been extracted, remaining `>` blocks are plain
    # blockquotes.  We convert them into styled HTML divs so they render
    # with proper line breaks and visual polish in the PDF.
    def plain_blockquote(m):
        raw = m.group(0)
        lines = raw.split("\n")
        # Strip the leading `> ` or `>` from each line
        cleaned = []
        for line in lines:
            stripped = re.sub(r"^>\s?", "", line)
            cleaned.append(stripped)

        # Group consecutive non-empty lines into paragraphs,
        # preserving explicit blank-line paragraph breaks.
        paragraphs = []
        current = []
        for line in cleaned:
            if line.strip() == "":
                if current:
                    paragraphs.append(current)
                    current = []
            else:
                current.append(line)
        if current:
            paragraphs.append(current)

        # Render each paragraph group through markdown, then join
        body_parts = []
        for para_lines in paragraphs:
            body_parts.append(_md("\n".join(para_lines)))
        body_html = "\n".join(body_parts)

        key = f"BLOCKQUOTE{uuid.uuid4().hex}"
        placeholders[key] = (
            f'<blockquote class="styled-quote">'
            f'{body_html}'
            f'</blockquote>'
        )
        return f"\n\n{key}\n\n"

    # Match contiguous blocks of lines starting with `>`
    text = re.sub(
        r"(?:^>.*$\n?)+",
        plain_blockquote, text, flags=re.MULTILINE,
    )

    # --- PHASE 3: Pre-process extra extensions ---

    # Task lists (unicode checkboxes for clean PDF rendering)
    text = re.sub(r"^(\s*)- \[ \]\s*", "\\1- ☐ ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)- \[[xX]\]\s*", "\\1- ☑ ", text, flags=re.MULTILINE)

    # Strikethrough ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)

    # Highlight ==text==
    text = re.sub(r"==(.+?)==", r"<mark>\1</mark>", text)

    # --- PHASE 4: Convert with python-markdown ---
    html = _md(text)

    # --- PHASE 5: Restore placeholders ---
    for key, value in placeholders.items():
        html = html.replace(f"<p>{key}</p>", value)
        html = html.replace(key, value)

    return html


# ---------------------------------------------------------------------------
# PDF Styles (optimized for professional printing)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Presets for page width and margins
# ---------------------------------------------------------------------------

# viewport_px = mm * (794 / 210) — mesma densidade do A4 em todas as larguras
WIDTH_PRESETS = {
    "a5":        {"css_width": "100%", "pdf_width": "148mm",  "viewport_px": 560},
    "compact":   {"css_width": "100%", "pdf_width": "170mm",  "viewport_px": 643},
    "a4":        {"css_width": "100%", "pdf_width": "210mm",  "viewport_px": 794},
    "letter":    {"css_width": "100%", "pdf_width": "216mm",  "viewport_px": 816},
    "wide":      {"css_width": "100%", "pdf_width": "280mm",  "viewport_px": 1058},
    "a3":        {"css_width": "100%", "pdf_width": "297mm",  "viewport_px": 1123},
    "ultrawide": {"css_width": "100%", "pdf_width": "320mm",  "viewport_px": 1210},
}

# Vertical simétrica (top = bottom) e proporcional à lateral (~80%),
# para a mancha de texto não parecer esmagada em cima/embaixo.
MARGIN_PRESETS = {
    "none":        {"top": "0",    "right": "0",    "bottom": "0",    "left": "0"},
    "minimal":     {"top": "4mm",  "right": "5mm",  "bottom": "4mm",  "left": "5mm"},
    "tight":       {"top": "8mm",  "right": "10mm", "bottom": "8mm",  "left": "10mm"},
    "normal":      {"top": "14mm", "right": "18mm", "bottom": "14mm", "left": "18mm"},
    "comfortable": {"top": "18mm", "right": "22mm", "bottom": "18mm", "left": "22mm"},
    "wide":        {"top": "22mm", "right": "28mm", "bottom": "22mm", "left": "28mm"},
    "extra":       {"top": "28mm", "right": "35mm", "bottom": "28mm", "left": "35mm"},
}

# Fontes do corpo do PDF. "default" mantém a pilha de sistema atual;
# as demais são carregadas do Google Fonts no HTML renderizado.
FONT_PRESETS = {
    "default":      {"gf": "Inter:wght@400;600;700",        "family": "'Inter', 'Segoe UI', -apple-system, sans-serif"},
    "inter":        {"gf": "Inter:wght@400;600;700",        "family": "'Inter', sans-serif"},
    "roboto":       {"gf": "Roboto:wght@400;500;700",       "family": "'Roboto', sans-serif"},
    "open-sans":    {"gf": "Open+Sans:wght@400;600;700",    "family": "'Open Sans', sans-serif"},
    "ubuntu":       {"gf": "Ubuntu:wght@400;500;700",       "family": "'Ubuntu', sans-serif"},
    "lora":         {"gf": "Lora:wght@400;500;700",         "family": "'Lora', serif"},
    "source-serif": {"gf": "Source+Serif+4:wght@400;600;700", "family": "'Source Serif 4', serif"},
    "merriweather": {"gf": "Merriweather:wght@400;700",     "family": "'Merriweather', serif"},
}

# Fontes monoespaçadas (blocos de código e código inline do PDF)
MONO_FONT_PRESETS = {
    "ubuntu-mono":     {"gf": "Ubuntu+Mono:wght@400;700",     "family": "'Ubuntu Mono', 'Cascadia Code', Consolas, monospace"},
    "jetbrains-mono":  {"gf": "JetBrains+Mono:wght@400;700",  "family": "'JetBrains Mono', Consolas, monospace"},
    "fira-code":       {"gf": "Fira+Code:wght@400;700",       "family": "'Fira Code', Consolas, monospace"},
    "source-code-pro": {"gf": "Source+Code+Pro:wght@400;700", "family": "'Source Code Pro', Consolas, monospace"},
    "roboto-mono":     {"gf": "Roboto+Mono:wght@400;700",     "family": "'Roboto Mono', Consolas, monospace"},
    "ibm-plex-mono":   {"gf": "IBM+Plex+Mono:wght@400;700",   "family": "'IBM Plex Mono', Consolas, monospace"},
    "inconsolata":     {"gf": "Inconsolata:wght@400;700",     "family": "'Inconsolata', Consolas, monospace"},
}


def get_pdf_style(margin_preset="normal", font_family=None, mono_family=None, custom_margins=None):
    """Generate PDF CSS with the given margins and fonts.

    custom_margins (dict top/right/bottom/left, valores com unidade)
    tem prioridade sobre o preset.
    """
    m = custom_margins or MARGIN_PRESETS.get(margin_preset, MARGIN_PRESETS["normal"])
    padding = f"{m['top']} {m['right']} {m['bottom']} {m['left']}"
    font_stack = font_family or "'Inter', 'Segoe UI', -apple-system, sans-serif"
    font_stack += ", 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', 'Android Emoji', sans-serif"
    mono_stack = mono_family or MONO_FONT_PRESETS["ubuntu-mono"]["family"]
    mono_stack += ", 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', 'Android Emoji', monospace"
    return f"""
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: auto !important; min-height: 0 !important; }}

@page {{ margin: 0; }}

/* Single continuous page — disable ALL page breaks */
* {{
    break-inside: auto !important;
    break-before: auto !important;
    break-after: auto !important;
    page-break-inside: auto !important;
    page-break-before: auto !important;
    page-break-after: auto !important;
}}

body {{
    font-family: {font_stack};
    line-height: 1.55;
    color: #1e293b;
    background: #fff;
    max-width: 100%;
    font-size: 13.5px;
    padding: {padding};
}}

h1 {{ font-size: 1.85em; margin: 28px 0 14px; border-bottom: 2px solid #E7E5E4; padding-bottom: 8px; color: #0C0A09; font-weight: 700; }}
h2 {{ font-size: 1.45em; margin: 24px 0 12px; color: #1C1917; font-weight: 600; border-bottom: 1px solid #F5F5F4; padding-bottom: 5px; }}
h3 {{ font-size: 1.2em; margin: 20px 0 10px; color: #44403C; font-weight: 600; }}
h4 {{ font-size: 1.05em; margin: 16px 0 8px; color: #57534E; font-weight: 600; }}
h5, h6 {{ font-size: 0.95em; margin: 12px 0 6px; color: #78716C; font-weight: 600; }}

p {{ margin: 12px 0; }}
a {{ color: #92400E; text-decoration: none; border-bottom: 1px solid rgba(146,64,14,0.30); }}
strong {{ font-weight: 600; color: #0C0A09; }}
del {{ text-decoration: line-through; color: #78716C; }}
mark {{ background: #FEF3C7; color: #713F12; padding: 1px 4px; border-radius: 2px; }}

ul {{ margin: 8px 0; padding-left: 22px; list-style-type: disc; }}
ol {{ margin: 8px 0; padding-left: 22px; list-style-type: decimal; }}
li {{ margin: 3px 0; }}
li::marker {{ color: #A8A29E; }}
li > ul, li > ol {{
    margin: 3px 0 3px 2px;
    padding-left: 20px;
    border-left: 1px solid #E7E5E4;
}}
li > ul {{ list-style-type: circle; }}

table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.82em; line-height: 1.4; }}
th, td {{ border: 1px solid #E7E5E4; padding: 5px 8px; text-align: left; word-break: break-word; }}
th {{ background: #F5F5F4; font-weight: 600; color: #1C1917; }}
tr:nth-child(even) td {{ background: #FAFAF9; }}

pre {{ background: #FAFAF9; border: 1px solid #E7E5E4; border-radius: 6px; padding: 14px 16px; overflow-x: hidden; overflow-wrap: break-word; word-break: break-all; white-space: pre-wrap; margin: 12px 0; line-height: 1.5; }}
code {{ font-family: {mono_stack}; font-size: 0.92em; }}
p code, li code, td code {{ background: #F5F5F4; padding: 2px 6px; border-radius: 4px; color: #9A3412; font-size: 0.9em; }}
pre code.hljs {{ padding: 0; background: transparent; }}

blockquote {{
    border-left: 4px solid #D97706;
    background: #FBF3E7;
    padding: 14px 20px;
    margin: 16px 0;
    border-radius: 0 8px 8px 0;
    color: #713F12;
}}
blockquote p {{
    margin: 6px 0;
}}
blockquote p:first-child {{ margin-top: 0; }}
blockquote p:last-child {{ margin-bottom: 0; }}
/* Nested blockquotes */
blockquote blockquote {{
    border-left-color: #B45309;
    background: #F7E9D4;
    margin: 10px 0;
    font-size: 0.95em;
}}

hr {{ border: none; border-top: 1px solid #E7E5E4; margin: 24px 0; }}
img {{ max-width: 100%; height: auto; border-radius: 4px; }}

.mermaid {{ background: #FAFAF9; border-radius: 8px; padding: 20px; margin: 16px 0; text-align: center; border: 1px solid #E7E5E4; }}

dt {{ font-weight: 600; margin-top: 12px; }}
dd {{ margin-left: 24px; margin-bottom: 8px; }}

.footnote {{ font-size: 0.85em; color: #78716C; border-top: 1px solid #E7E5E4; margin-top: 32px; padding-top: 16px; }}
"""


def _watermark_css(text):
    """Builds a tiled, rotated SVG watermark as a CSS background overlay."""
    from urllib.parse import quote
    esc = html_mod.escape(text[:60])
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='420' height='300'>"
        "<text x='210' y='150' font-family='Arial, sans-serif' font-size='26' "
        "fill='#1e293b' fill-opacity='0.07' font-weight='600' text-anchor='middle' "
        f"transform='rotate(-30 210 150)'>{esc}</text></svg>"
    )
    data_uri = "data:image/svg+xml;charset=utf-8," + quote(svg)
    return f"""
body {{ position: relative; }}
#watermark {{
    position: absolute;
    inset: 0;
    z-index: 999;
    pointer-events: none;
    background-image: url("{data_uri}");
    background-repeat: repeat;
}}
"""


def wrap_for_pdf(body_html, title, margin_preset="normal", mermaid_layout="adaptive",
                 font="default", watermark="", font_mono="ubuntu-mono", custom_margins=None):
    font_cfg = FONT_PRESETS.get(font, FONT_PRESETS["default"])
    mono_cfg = MONO_FONT_PRESETS.get(font_mono, MONO_FONT_PRESETS["ubuntu-mono"])
    style = get_pdf_style(margin_preset, font_family=font_cfg["family"],
                          mono_family=mono_cfg["family"], custom_margins=custom_margins)

    # Fonte mono (código) sempre presente + Noto Color Emoji + fonte do corpo quando não-padrão
    gf_families = [mono_cfg["gf"], "Noto+Color+Emoji"]
    if font_cfg["gf"]:
        gf_families.append(font_cfg["gf"])
    families = "&".join(f"family={f}" for f in gf_families)
    font_link = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?{families}&display=swap">'
    )

    watermark = (watermark or "").strip()
    watermark_div = ""
    if watermark:
        style += _watermark_css(watermark)
        watermark_div = '<div id="watermark" aria-hidden="true"></div>'

    # CSS dinâmico para Mermaid no PDF
    if mermaid_layout == "hierarchical":
        style += """
        .mermaid { overflow-x: auto; }
        .mermaid svg { max-width: none !important; }
        """
    else:
        style += """
        .mermaid svg { max-width: 100% !important; height: auto !important; }
        """

    renderer = 'dagre' if mermaid_layout == 'hierarchical' else 'elk'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><title>{title}</title>
{font_link}
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  import elkLayouts from 'https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0.2.2/dist/mermaid-layout-elk.esm.min.mjs';
  mermaid.registerLayoutLoaders(elkLayouts);
  window.mermaid = mermaid;
  mermaid.initialize({{ startOnLoad: true, theme: 'default', flowchart: {{ defaultRenderer: '{renderer}' }} }});
</script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-light.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>{style}</style>
</head>
<body>
<div id="content">{body_html}</div>
{watermark_div}
<script>
hljs.highlightAll();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# HTML -> PDF via Playwright
# ---------------------------------------------------------------------------

async def html_to_pdf_bytes(html_content, width_preset="a4"):
    """Renders HTML with Playwright and generates a continuous (single-page) PDF."""
    from playwright.async_api import async_playwright

    wp = WIDTH_PRESETS.get(width_preset, WIDTH_PRESETS["a4"])

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html_content)
        html_path = f.name

    pdf_path = html_path.replace(".html", ".pdf")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            # Viewport width must match PDF width
            # so that text layout is identical in measurement and final PDF.
            # Large viewport height (10000px) avoids scroll and ensures
            # getBoundingClientRect returns correct absolute coordinates.
            page = await browser.new_page(viewport={"width": wp["viewport_px"], "height": 10000})
            await page.goto(Path(html_path).as_uri(), wait_until="networkidle")

            # Wait for JS rendering (webfonts + Mermaid + highlight.js)
            await page.evaluate("""async () => {
                if (document.fonts && document.fonts.ready) await document.fonts.ready;
                if (typeof hljs !== 'undefined') hljs.highlightAll();

                const mermaidDivs = document.querySelectorAll('.mermaid');
                if (mermaidDivs.length > 0) {
                    let attempts = 0;
                    while (attempts < 30) {
                        const rendered = document.querySelectorAll('.mermaid svg').length;
                        if (rendered >= mermaidDivs.length) break;
                        await new Promise(r => setTimeout(r, 200));
                        attempts++;
                    }
                }
            }""")

            await page.wait_for_timeout(400)

            # Emulate print media and force light color scheme to prevent Mermaid rendering dark boxes
            await page.emulate_media(media='print', color_scheme='light')
            await page.wait_for_timeout(200)

            # Measure actual rendered content height.
            # We use multiple strategies and take the maximum to be safe:
            # 1. getBoundingClientRect on #content + body padding
            # 2. body.scrollHeight
            # This guards against print-media reflow differences.
            content_height_px = await page.evaluate("""() => {
                // Remove empty elements at the end of content
                const content = document.getElementById('content');
                const children = content.children;
                for (let i = children.length - 1; i >= 0; i--) {
                    const el = children[i];
                    if (el.textContent.trim() === '' && el.tagName !== 'HR' && !el.querySelector('img, svg, .mermaid')) {
                        el.remove();
                    } else {
                        break;
                    }
                }

                const rect = content.getBoundingClientRect();
                const bodyStyle = getComputedStyle(document.body);
                const paddingBottom = parseFloat(bodyStyle.paddingBottom) || 0;
                const rectHeight = Math.ceil(rect.bottom + paddingBottom);
                const scrollHeight = document.body.scrollHeight;

                return Math.max(rectHeight, scrollHeight);
            }""")

            # Convert height from viewport pixels to mm using the same
            # width ratio (e.g.: 794px = 210mm), avoiding mismatch
            # between screen pixel measurement and print mm rendering.
            # Small safety buffer for print-media reflow: 1% + 2mm fixed.
            # (5% proporcional criava um rabo em branco crescente no fim.)
            pdf_width_mm = float(wp["pdf_width"].replace("mm", ""))
            mm_per_px = pdf_width_mm / wp["viewport_px"]
            content_height_mm = (content_height_px * mm_per_px) * 1.01 + 2

            await page.pdf(
                path=pdf_path,
                width=wp["pdf_width"],
                height=f"{content_height_mm:.1f}mm",
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                print_background=True,
            )
            await browser.close()

        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in [html_path, pdf_path]:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_title(md_text, fallback="document"):
    """Extract title from the first H1 in markdown, or use fallback."""
    m = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback.replace("-", " ").replace("_", " ").title()


def _first_non_empty_line(md_text):
    """Return the first non-empty line of the text (stripped), or ''."""
    for line in md_text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def slugify_filename(text, fallback="document", max_len=80):
    """Slugify text into a safe filename: only [a-z0-9_-].

    - Transliterates accents (á -> a, ç -> c, ...)
    - Lowercases
    - Keeps underscores; turns any other run of non-alphanumerics into a dash
    - Collapses repeated dashes and trims leading/trailing separators
    """
    # Strip accents/diacritics down to ASCII
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    # Keep [a-z0-9_]; everything else becomes a dash
    text = re.sub(r"[^a-z0-9_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip("-_")
    text = text[:max_len].strip("-_")
    return text or fallback


def build_output_name(content, fallback="document"):
    """Derive a safe filename from the first non-empty line of the content."""
    return slugify_filename(_first_non_empty_line(content), fallback=slugify_filename(fallback, "document"))


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.post("/api/convert")
async def convert_markdown(
    file: UploadFile = File(None),
    markdown: str = Form(None),
    filename: str = Form("document"),
    page_width: str = Form("a4"),
    margin: str = Form("normal"),
    mermaid_layout: str = Form("adaptive"),
    font: str = Form("default"),
    font_mono: str = Form("ubuntu-mono"),
    watermark: str = Form(""),
    margin_top: str = Form(""),
    margin_right: str = Form(""),
    margin_bottom: str = Form(""),
    margin_left: str = Form(""),
):
    """Converts markdown (upload or text) to PDF."""
    if file:
        content = (await file.read()).decode("utf-8")
        filename = Path(file.filename).stem if file.filename else filename
    elif markdown:
        content = markdown
    else:
        return JSONResponse({"error": "Send a .md file or markdown text"}, status_code=400)

    # Validate presets (fallback to defaults)
    if page_width not in WIDTH_PRESETS:
        page_width = "a4"
    if margin not in MARGIN_PRESETS:
        margin = "normal"

    # Strip trailing whitespace/newlines to prevent blank space at end of PDF
    content = content.rstrip()

    # Margens personalizadas (estilo Word): valores em mm, clampados 0-80
    custom_margins = None
    if margin == "custom":
        def _mm(value, default):
            try:
                v = float(str(value).strip().replace(",", "."))
            except (TypeError, ValueError):
                v = default
            return f"{max(0.0, min(80.0, v)):g}mm"
        custom_margins = {
            "top": _mm(margin_top, 14),
            "right": _mm(margin_right, 18),
            "bottom": _mm(margin_bottom, 14),
            "left": _mm(margin_left, 18),
        }

    title = _extract_title(content, filename)
    body = md_to_html(content)
    full_html = wrap_for_pdf(body, title, margin_preset=margin, mermaid_layout=mermaid_layout,
                             font=font, watermark=watermark, font_mono=font_mono,
                             custom_margins=custom_margins)
    pdf_bytes = await html_to_pdf_bytes(full_html, width_preset=page_width)

    # Filename comes from the first non-empty line of the content,
    # sanitized to [a-z0-9_-]. Falls back to the provided filename.
    safe_filename = build_output_name(content, fallback=filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{safe_filename}_{uuid.uuid4().hex[:8]}.pdf")
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)

    # BackgroundTask cleans up the file after response is sent
    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=f"{safe_filename}.pdf",
        background=BackgroundTask(lambda p=out_path: os.unlink(p) if os.path.exists(p) else None),
    )


@app.post("/api/preview")
async def preview_markdown(markdown: str = Form(...)):
    """Returns rendered HTML for preview."""
    body = md_to_html(markdown)
    return JSONResponse({"html": body})


TEMPLATE_DIR = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Main page with editor and preview."""
    html_path = TEMPLATE_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")




# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="md2pdf web server")
    parser.add_argument("--host", default="127.0.0.1", help="Host (use 0.0.0.0 for network)")
    parser.add_argument("--port", type=int, default=8050, help="Port (default: 8050)")
    args = parser.parse_args()

    print(f"\n  md2pdf running at http://{args.host}:{args.port}")
    print(f"  Ctrl+C to stop\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
