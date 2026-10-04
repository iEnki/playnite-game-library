FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    SITE_DIR=/site \
    DATA_DIR=/data \
    CONF_DIR=/conf

WORKDIR /app
# only the Python standard library is used – no pip install needed
COPY updater.py gen.py app.html ./

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/status',timeout=4)" || exit 1

CMD ["python", "-u", "/app/updater.py"]
