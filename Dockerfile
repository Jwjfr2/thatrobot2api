FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV HOST=0.0.0.0 \
    PORT=8080 \
    TR_SESSION_FILE=/app/cookie.txt

EXPOSE 8080
CMD ["thatrobot2api"]
