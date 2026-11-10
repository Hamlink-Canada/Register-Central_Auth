# syntax=docker/dockerfile:1.7
# Named contexts: rpt_protobuf=../rpt_protobuf and pycommon=../pyCommon
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /build/service
# Copy only library build inputs; never copy a sibling's credentials or Git metadata.
COPY --from=rpt_protobuf /src/ /build/rpt_protobuf/src/
COPY --from=rpt_protobuf /pyproject.toml /README.md /LICENSE /build/rpt_protobuf/
COPY --from=pycommon /src/ /build/pyCommon/src/
COPY --from=pycommon /pyproject.toml /README.md /LICENSE /build/pyCommon/
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
ARG VCS_REF=development
LABEL org.opencontainers.image.source="https://github.com/Hamlink-Canada/Register-Central_Auth" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY settings.toml ./
USER 10001:10001
EXPOSE 50051/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 CMD ["rpt-central-auth-health", "readiness"]
ENTRYPOINT ["rpt-central-auth"]
