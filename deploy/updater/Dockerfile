FROM docker:29.1.3-cli@sha256:4fa0ee1f3a7e4354c4ea34558b6d4ee32859baf4973d4c8ccc8e7fe3dd730c04 AS cli
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
WORKDIR /opt/qqbot
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /opt/qqbot/
COPY deploy/updater /opt/qqbot/deploy/updater
COPY deploy/compose/compose.yaml /opt/qqbot/deploy/compose/compose.yaml
ARG QQBOT_REVISION=unknown
LABEL org.opencontainers.image.revision=$QQBOT_REVISION
ENTRYPOINT ["python", "-m", "deploy.updater.server"]
CMD ["serve"]
