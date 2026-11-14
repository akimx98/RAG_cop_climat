# Image du service COP31 (tableau de bord + MCP + veilleur). Ne contient PAS les
# données : data/ est monté en volume (documents internes inclus).
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    "pyyaml>=6" "httpx>=0.27" "beautifulsoup4>=4.12" "lxml>=5" "PyMuPDF>=1.24.3" "python-docx>=1.1" \
    "feedparser>=6" "trafilatura>=1.12" "numpy>=1.26" "openai>=1.40" "mcp>=1.20,<2" "pyjwt[crypto]>=2.8" \
    "starlette>=0.37" "uvicorn>=0.30" "jinja2>=3.1" "python-multipart>=0.0.9" "aiosmtpd>=1.4.6" "tzdata"
COPY pyproject.toml README.md ./
COPY src ./src
COPY catalog ./catalog
COPY clients ./clients
RUN pip install --no-cache-dir -e . --no-deps
ENV COP_DATA_DIR=/app/data COP_CATALOG=/app/catalog PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"
CMD ["cop", "serve", "--host", "0.0.0.0", "--port", "8000"]
