"""Quick test for the markdown parser."""
import sys
sys.stdout.reconfigure(encoding='utf-8')

from app import md_to_html, _extract_title

md = """# Relatorio Diario

## Secao 1

Texto com **negrito**, *italico*, ~~riscado~~ e ==destaque==.

- [ ] Tarefa pendente
- [x] Tarefa feita

1. Primeiro
2. Segundo
   - Sub item

| Nome | Valor |
|------|-------|
| ABC  | 123   |

[Google](https://google.com)

> [!NOTE]
> Isso e uma nota importante

> [!WARNING]
> Cuidado com isso

```python
def hello():
    print("Hello World")
```

---

Texto com `inline code` e um ![image](https://via.placeholder.com/100).
"""

print("=== TITLE ===")
print(_extract_title(md))
print()
print("=== HTML ===")
html = md_to_html(md)
print(html)
print()

# Verify key features
checks = [
    ("<h1>", "H1 heading"),
    ("<h2>", "H2 heading"),
    ("<strong>", "Bold"),
    ("<em>", "Italic"),
    ("<del>", "Strikethrough"),
    ("<mark>", "Highlight"),
    ("<ul>", "Unordered list"),
    ("<ol>", "Ordered list"),
    ("<table>", "Table"),
    ("<th>", "Table header"),
    ('<a href="https://google.com">', "Link"),
    ("<pre>", "Code block"),
    ('<code class="language-python">', "Code language class"),
    ("<hr", "Horizontal rule"),
    ("<code>", "Inline code"),
    ("<img", "Image"),
    ("callout", "Callout"),
]

print("=== FEATURE CHECK ===")
all_ok = True
for pattern, name in checks:
    found = pattern in html
    status = "OK" if found else "MISSING"
    if not found:
        all_ok = False
    print(f"  [{status}] {name}: {pattern}")

print()
print("ALL FEATURES OK!" if all_ok else "SOME FEATURES MISSING!")
