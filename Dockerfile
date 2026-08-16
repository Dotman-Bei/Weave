FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY weave ./weave
COPY benchmarks ./benchmarks
COPY data ./data

RUN pip install --no-cache-dir -e ".[hydra,llm]"

EXPOSE 8000

CMD ["weave", "serve", "--host", "0.0.0.0", "--port", "8000"]
