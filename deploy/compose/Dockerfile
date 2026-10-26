FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG TARGETARCH
RUN test "${TARGETARCH:-amd64}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git build-essential libgomp1 fonts-noto-cjk ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    ANONYMIZED_TELEMETRY=False

WORKDIR /opt/qq-ai-bot

# Explicit source copies and the Dockerfile-specific context allowlist keep all
# existing QQ accounts, API keys, databases and downloaded runtimes out of layers.
COPY deploy/compose/prepare.py deploy/compose/sources.lock.json /opt/qq-ai-bot/deploy/compose/
RUN python deploy/compose/prepare.py sources
RUN python -m pip install --no-cache-dir uv==0.11.15 \
    && cd langbot && uv sync --frozen --no-dev --no-cache

ENV VIRTUAL_ENV=/opt/qq-ai-bot/langbot/.venv \
    PATH=/opt/qq-ai-bot/langbot/.venv/bin:$PATH

COPY qunling/requirements.txt /opt/qq-ai-bot/qunling/requirements.txt
RUN uv pip install --python /opt/qq-ai-bot/langbot/.venv/bin/python --no-cache -r qunling/requirements.txt
COPY scripts/Apply-RuntimePatches.py scripts/Setup-ArchiveTools.py scripts/Setup-FileRuntime.py scripts/Run-LangBot.py scripts/chat_style.py /opt/qq-ai-bot/scripts/
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /opt/qq-ai-bot/
COPY licenses /opt/qq-ai-bot/licenses
COPY config/chat-style.txt /opt/qq-ai-bot/config/chat-style.txt
COPY patches /opt/qq-ai-bot/patches
RUN python scripts/Apply-RuntimePatches.py --python /opt/qq-ai-bot/langbot/.venv/bin/python --apply \
    && python scripts/Setup-ArchiveTools.py --platform linux-x64 \
    && ln -s /opt/qq-ai-bot/.local/tools/7zip/linux-x64/7zz /usr/local/bin/7zz

COPY custom-plugins/AIImagePlugin /opt/qq-ai-bot/custom-plugins/AIImagePlugin
COPY custom-plugins/LongTermMemory /opt/qq-ai-bot/custom-plugins/LongTermMemory
COPY custom-plugins/QQComicTranslator /opt/qq-ai-bot/custom-plugins/QQComicTranslator
COPY custom-plugins/QQGroupContext /opt/qq-ai-bot/custom-plugins/QQGroupContext
COPY custom-plugins/QQTextReader /opt/qq-ai-bot/custom-plugins/QQTextReader
COPY custom-plugins/QQWebSearch /opt/qq-ai-bot/custom-plugins/QQWebSearch
COPY qunling /opt/qq-ai-bot/qunling
RUN python deploy/compose/prepare.py packages \
    && python deploy/compose/prepare.py embedding

COPY bootstrap /opt/qq-ai-bot/bootstrap
COPY deploy/compose /opt/qq-ai-bot/deploy/compose
RUN python -m compileall -q bootstrap deploy/compose qunling scripts/Run-LangBot.py \
    && python scripts/Setup-FileRuntime.py --check \
    && python -c "import runpy; runpy.run_path('scripts/Setup-FileRuntime.py')['register_package']()" \
    && python scripts/Apply-RuntimePatches.py --python /opt/qq-ai-bot/langbot/.venv/bin/python

EXPOSE 5300 5310
ARG QQBOT_REVISION=unknown
LABEL org.opencontainers.image.revision=$QQBOT_REVISION
ENTRYPOINT ["python", "/opt/qq-ai-bot/deploy/compose/entrypoint.py"]
CMD ["app"]
