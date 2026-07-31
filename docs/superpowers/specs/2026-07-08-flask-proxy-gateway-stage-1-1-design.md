# Flask Proxy Gateway Stage 1.1 Design

## Scope

Build the first slice of a local HTTP gateway for the publishing-side proxy session router. This stage only initializes the Flask service, loads proxy settings from `.env`, and exposes a health endpoint.

## Architecture

Use a small package under `gateway/` so later proxy routing tasks can be added without reshaping the project. `gateway.app` owns the Flask application factory and routes. `gateway.config` owns environment loading and typed access to proxy settings.

## Components

- `gateway/config.py`: loads `.env` with `python-dotenv` and returns proxy configuration values.
- `gateway/app.py`: creates the Flask app and registers `GET /ping`.
- `app.py`: local entrypoint for running the service on `127.0.0.1:5000`.
- `tests/`: verifies `/ping` and proxy configuration loading.

## Behavior

`GET /ping` returns HTTP 200 with JSON body `{"status": "ok"}`.

Proxy environment variables are read from `.env`:

- `PROXY_HOST`
- `PROXY_PORT`
- `PROXY_USER`
- `PROXY_PASS`

## Testing

Use `pytest` with Flask's test client for route behavior. Use isolated environment variables for config loading tests so tests do not depend on the user's local `.env`.
