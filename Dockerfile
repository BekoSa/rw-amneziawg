FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/packages/contracts:/workspace/packages/remnawave-client:/workspace/packages/awg-config:/workspace/packages/subscription-renderers:/workspace/packages/capabilities:/workspace/apps/controller:/workspace/apps/node-agent:/workspace/apps/subscription-gateway:/workspace/apps/tui
WORKDIR /workspace
COPY requirements.txt requirements.lock /workspace/
RUN pip install --no-cache-dir -c requirements.lock -r requirements.txt \
    && groupadd --gid 10001 awg \
    && useradd --uid 10001 --gid 10001 --no-create-home awg
COPY packages /workspace/packages
COPY apps /workspace/apps
USER 10001:10001
EXPOSE 8080
