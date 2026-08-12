# The deployed engine: the FastMCP server, and nothing else.
#
# Note what is NOT started here -- cm_engine.mock_upstream. The mock exists so the
# local demo can run offline; a deployed engine calls the real upstream, and an
# image that could fall back to a simulator is an image that can serve invented
# data while looking healthy.
#
# The image carries the registry that is committed to THIS repository, so what a
# deployed engine serves is exactly what a human merged here -- the deploy is the
# publication step, not a separate one that could drift from it.

FROM python:3.13-slim

# uv resolves from the committed lockfile, so the deployed dependency set is the
# one the tests ran against rather than whatever resolved on build day.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# registry does, and a pin PR should not reinstall the world to add one JSON file.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Bind on every interface so the platform's router can reach it. The port is the
# platform's to choose, so it is read at start rather than baked in.
ENV MCP_HOST=0.0.0.0

# Real upstreams. The image ships no mock to fall back to, and the credential
# arrives from the environment -- never from the image, never from a contract.
ENV DEV_OFFLINE=0

# .venv/bin/python rather than `uv run`: the sandbox spawns sys.executable for
# every generated module, so the interpreter that serves must be the one that
# already has the dependencies -- no resolver step on the hot path.
CMD ["sh", "-c", "MCP_PORT=${PORT:-8765} .venv/bin/python -m cm_engine.server"]
