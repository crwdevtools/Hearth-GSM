FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HEARTH_PANEL_DATA=/var/lib/hearth-panel
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hearth_panel ./hearth_panel
COPY web ./web

RUN useradd --system --uid 10001 hearth && mkdir -p /var/lib/hearth-panel && chown hearth /var/lib/hearth-panel
USER hearth
VOLUME ["/var/lib/hearth-panel"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/auth/state', timeout=4)" || exit 1
CMD ["python", "-m", "hearth_panel.main"]
