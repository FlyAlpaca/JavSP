FROM python:3.14-slim AS builder

WORKDIR /app

ENV PATH=/root/.local/bin:$PATH
RUN pip install pipx && \
    pipx ensurepath && \
    pipx install poetry
RUN apt update && apt install -y git

COPY . .

RUN poetry self add poetry-dynamic-versioning && \
    poetry config virtualenvs.in-project true && \
    poetry install


FROM python:3.14-slim AS runner

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/javsp /app/javsp
COPY --from=builder /app/data /app/data
COPY --from=builder /app/image /app/image
COPY --from=builder /app/config.yml /app/config.yml
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENTRYPOINT ["/app/.venv/bin/javsp"]
CMD ["-i", "/video"]
