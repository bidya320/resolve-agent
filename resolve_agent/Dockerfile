FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY data data
COPY seed.py .
COPY start.sh .
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh
RUN useradd -m app && chown -R app /srv
USER app
ENV DB_PATH=/srv/support.db KB_DIR=/srv/data/kb
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"
CMD ["./start.sh"]


