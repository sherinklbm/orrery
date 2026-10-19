FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
WORKDIR /app
COPY common.py auth.py aiconfig.py modelcatalog.py n8nclient.py bridgeclient.py server.py ./
COPY static ./static
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "server.py"]
