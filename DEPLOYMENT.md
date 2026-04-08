# Deployment Layout

## Upload To GitHub

Keep these in the repository:

- `X.py`
- `wsgi.py`
- `Procfile`
- `render.yaml`
- `.python-version`
- `requirements.txt`
- `README.md`
- `DEPLOYMENT.md`
- `.env.example`
- `.gitignore`
- `templates/`
- `static/`
- `phone_apk_app/` source files only

## Keep Out Of GitHub

Do not commit runtime or local-only files:

- `.env`
- `.venv/`
- `database/`
- `logs/`
- `uploads/`
- `data/` on Render when using the persistent disk
- `cloudflared*.exe`
- `phone_apk_app/.tools/`
- `phone_apk_app/local.properties`
- Android build outputs such as `phone_apk_app/app/build/`

## Production Run

Use the WSGI entrypoint with a production server:

```bash
gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 1 --bind 0.0.0.0:$PORT wsgi:application
```

## Notes

- `GitHub Pages` is not enough for this app because it has a Python backend.
- Keep `ENABLE_CLOUDFLARED_TUNNEL=0` on hosted deployments.
- Set `SECRET_KEY`, `JWT_SECRET`, `DATABASE_PATH`, and the `WAWP_*` values in the host environment.
- If you need persistent data, use a persistent volume or an external database.
- Render uses `.python-version` or `PYTHON_VERSION`; `runtime.txt` is not used here.
- Keep the Render web service at 1 instance unless you move off SQLite and in-memory device state.
