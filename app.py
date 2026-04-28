"""
md2pdf-web - Web app to convert Markdown to PDF.
Supports Mermaid, Obsidian callouts, tables, code blocks with syntax highlight,
links, images, nested lists, footnotes, strikethrough, highlights and more.

Usage:
    python app.py                    # Runs at http://localhost:8050
    python app.py --port 9000        # Custom port
    python app.py --host 0.0.0.0     # Expose on network (for server)
"""
import re
import os
import uuid
import tempfile
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

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "footnotes", "attr_list", "def_list"]


def _md(text):
    """Run the markdown parser on a piece of text."""
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

    # --- PHASE 3: Pre-process extra extensions ---
    # Obsidian-style blockquotes: each > line gets a separate break
    # (standard markdown joins everything into a single paragraph)
    text = re.sub(r"^(>.*\S)\s*$", r"\1  ", text, flags=re.MULTILINE)

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

WIDTH_PRESETS = {
    "a4":      {"css_width": "100%", "pdf_width": "210mm",  "viewport_px": 794},
    "letter":  {"css_width": "100%", "pdf_width": "216mm",  "viewport_px": 816},
    "wide":    {"css_width": "100%", "pdf_width": "280mm",  "viewport_px": 1058},
    "compact": {"css_width": "100%", "pdf_width": "170mm",  "viewport_px": 643},
}

MARGIN_PRESETS = {
    "none":   {"top": "0",    "right": "0",    "bottom": "0",   "left": "0"},
    "tight":  {"top": "5mm",  "right": "10mm", "bottom": "3mm", "left": "10mm"},
    "normal": {"top": "10mm", "right": "18mm", "bottom": "5mm", "left": "18mm"},
    "wide":   {"top": "15mm", "right": "28mm", "bottom": "10mm", "left": "28mm"},
}


def get_pdf_style(margin_preset="normal"):
    """Generate PDF CSS with the given margin preset."""
    m = MARGIN_PRESETS.get(margin_preset, MARGIN_PRESETS["normal"])
    padding = f"{m['top']} {m['right']} {m['bottom']} {m['left']}"
    return f"""
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: auto !important; min-height: 0 !important; }}

@page {{ margin: 0; size: auto; }}

/* Prevent page breaks — we generate a single continuous page */
* {{ break-inside: auto; }}
pre, blockquote, table, .mermaid, .callout {{
    break-inside: avoid;
    page-break-inside: avoid;
}}
h1, h2, h3, h4, h5, h6 {{
    break-after: avoid;
    page-break-after: avoid;
}}

body {{
    font-family: 'Segoe UI', -apple-system, 'Helvetica Neue', Arial, sans-serif;
    line-height: 1.75;
    color: #1e293b;
    background: #fff;
    max-width: 100%;
    font-size: 13.5px;
    padding: {padding};
}}

h1 {{ font-size: 1.85em; margin: 28px 0 14px; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; color: #0f172a; font-weight: 700; }}
h2 {{ font-size: 1.45em; margin: 24px 0 12px; color: #1e293b; font-weight: 600; border-bottom: 1px solid #f1f5f9; padding-bottom: 5px; }}
h3 {{ font-size: 1.2em; margin: 20px 0 10px; color: #334155; font-weight: 600; }}
h4 {{ font-size: 1.05em; margin: 16px 0 8px; color: #475569; font-weight: 600; }}
h5, h6 {{ font-size: 0.95em; margin: 12px 0 6px; color: #64748b; font-weight: 600; }}

p {{ margin: 8px 0; }}
a {{ color: #2563eb; text-decoration: none; border-bottom: 1px solid #93c5fd; }}
strong {{ font-weight: 600; color: #0f172a; }}
del {{ text-decoration: line-through; color: #94a3b8; }}
mark {{ background: #fef08a; padding: 1px 4px; border-radius: 2px; }}

ul, ol {{ margin: 8px 0 8px 24px; }}
li {{ margin: 4px 0; }}
li > ul, li > ol {{ margin: 2px 0 2px 20px; }}

table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.82em; line-height: 1.4; }}
th, td {{ border: 1px solid #e2e8f0; padding: 5px 8px; text-align: left; }}
th {{ background: #f8fafc; font-weight: 600; color: #1e293b; }}
tr:nth-child(even) td {{ background: #fafbfc; }}

pre {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 14px 16px; overflow-x: auto; margin: 12px 0; line-height: 1.5; }}
code {{ font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', Consolas, monospace; font-size: 0.87em; }}
p code, li code, td code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; color: #be185d; font-size: 0.85em; }}
pre code.hljs {{ padding: 0; background: transparent; }}

blockquote {{ border-left: 4px solid #6366f1; background: #eef2ff; padding: 12px 16px; margin: 14px 0; border-radius: 0 6px 6px 0; color: #3730a3; }}
blockquote p {{ margin: 4px 0; }}

hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 24px 0; }}
img {{ max-width: 100%; height: auto; border-radius: 4px; }}

.mermaid {{ background: #fafbfc; border-radius: 8px; padding: 20px; margin: 16px 0; text-align: center; border: 1px solid #e2e8f0; }}

dt {{ font-weight: 600; margin-top: 12px; }}
dd {{ margin-left: 24px; margin-bottom: 8px; }}

.footnote {{ font-size: 0.85em; color: #64748b; border-top: 1px solid #e2e8f0; margin-top: 32px; padding-top: 16px; }}
"""


def wrap_for_pdf(body_html, title, margin_preset="normal"):
    style = get_pdf_style(margin_preset)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>{style}</style>
</head>
<body>
<div id="content">{body_html}</div>
<script>
mermaid.initialize({{ startOnLoad: true, theme: 'default' }});
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

            # Wait for JS rendering (Mermaid + highlight.js)
            await page.evaluate("""async () => {
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

            # Emulate print media for accurate measurement
            await page.emulate_media(media='print')
            await page.wait_for_timeout(200)

            # Measure actual rendered content height.
            # Uses getBoundingClientRect on #content + body padding for
            # sub-pixel accuracy. scrollHeight can round and cause
            # an extra blank page.
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

                // getBoundingClientRect.bottom = distance from viewport top
                // to the end of #content (already includes body paddingTop).
                // Add body paddingBottom to complete the spacing.
                const rect = content.getBoundingClientRect();
                const bodyStyle = getComputedStyle(document.body);
                const paddingBottom = parseFloat(bodyStyle.paddingBottom) || 0;
                return Math.ceil(rect.bottom + paddingBottom);
            }""")

            # Convert height from viewport pixels to mm using the same
            # width ratio (e.g.: 794px = 210mm), avoiding mismatch
            # between screen pixel measurement and print mm rendering.
            # +2mm safety margin — doesn't affect result since it's a single page.
            pdf_width_mm = float(wp["pdf_width"].replace("mm", ""))
            mm_per_px = pdf_width_mm / wp["viewport_px"]
            content_height_mm = (content_height_px * mm_per_px) + 2

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

    title = _extract_title(content, filename)
    body = md_to_html(content)
    full_html = wrap_for_pdf(body, title, margin_preset=margin)
    pdf_bytes = await html_to_pdf_bytes(full_html, width_preset=page_width)

    # Save temp with unique name to avoid collision
    safe_filename = re.sub(r'[^\w\-.]', '_', filename)
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
