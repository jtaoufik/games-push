FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py campaigns.json ./
ENV TZ=Europe/Paris PORT=8000
EXPOSE 8000
# python:slim has no curl/wget, so Coolify's own healthcheck cannot run here;
# a stdlib probe gives Docker (and Coolify's status column) a real health signal.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
