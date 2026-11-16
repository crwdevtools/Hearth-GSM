FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hearth_agent ./hearth_agent

# Runs as root: it needs the Docker socket, which is root-equivalent on the host anyway.
# Server data must be mounted at the same path on the host and in here (see README).
VOLUME ["/var/lib/hearth"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["python", "-m", "hearth_agent"]
