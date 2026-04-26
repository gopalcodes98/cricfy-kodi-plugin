FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency installation
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY . .

# Install dependencies
RUN uv sync --no-dev

# Create resources directory (populated at runtime from env vars)
RUN mkdir -p resources

EXPOSE 8000

CMD ["uv", "run", "python", "main.py"]
