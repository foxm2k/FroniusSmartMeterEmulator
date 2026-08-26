FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
    && install -d -o app -g app /var/lib/fronius-smart-meter

COPY --chown=10001:10001 fronius_emulator/ ./fronius_emulator/

USER 10001:10001

EXPOSE 1502/tcp
VOLUME ["/var/lib/fronius-smart-meter"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "fronius_emulator.healthcheck"]

ENTRYPOINT ["python", "-m", "fronius_emulator"]
