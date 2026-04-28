# md2pdf

Converts Markdown (with Mermaid, callouts, tables) to PDF via web.

## Local Setup

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
playwright install chromium
```

## Local Usage

```bash
python app.py                          # http://localhost:8050
python app.py --port 9000              # Custom port
python app.py --host 0.0.0.0           # Expose on network
```

## Docker Deploy (VPS)

### Quick Start

```bash
docker compose up -d --build
```

The app will be available at `http://<VPS-IP>:8050`.

### With HTTPS (Caddy)

1. Edit the `Caddyfile` and replace `md2pdf.yourdomain.com` with your domain
2. Uncomment the `caddy` block in `docker-compose.yml`
3. Point your domain's DNS to the VPS IP
4. Start everything:

```bash
docker compose up -d --build
```

Caddy automatically obtains and renews TLS certificates.

### With Traefik

Uncomment the Traefik `labels` in `docker-compose.yml` and set your domain.

### Automated Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

### Custom Port

```bash
MD2PDF_PORT=9000 docker compose up -d --build
```

## Features

- Live preview editor
- Upload .md files (click or drag & drop)
- Export PDF with Ctrl+S
- Mermaid, Obsidian callouts, tables, code blocks support
- Width presets (A4, Letter, Wide, Compact) and margin presets
