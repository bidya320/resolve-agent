FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY data data
COPY seed.py .
COPY start.sh .
RUN chmod +x start.sh
RUN useradd -m app && mkdir /data && chown app /data
USER app
ENV DB_PATH=/data/support.db KB_DIR=/srv/data/kb
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"
CMD ["./start.sh"]

