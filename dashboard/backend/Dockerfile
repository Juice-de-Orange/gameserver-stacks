FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# No steamcmd here: Steam Workshop downloads run in a separate helper image that is pulled only
# when an active game needs one (`app/services/steam_download.py`, `dashboard/images/steamcmd`).

COPY app ./app

# The API reads and writes the game servers' data directories through the DATA_ROOT bind mount,
# so those directories must be owned by APP_UID (build arg, default 1000).
# /app/data holds the graceful stop/start record (`app.cli`).
ARG APP_UID=1000
RUN useradd -u "${APP_UID}" -m app && mkdir -p /app/data && chown -R app:app /app/data
USER app

EXPOSE 8000
# Exactly ONE worker (uvicorn's default): the login throttle, the role-matrix cache and the
# used-WebSocket-ticket list are process-local.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
