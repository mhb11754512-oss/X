# Project Structure

Use `DEPLOYMENT.md` as the source of truth for what should be uploaded to GitHub and what should stay local/runtime-only.

Recommended repo layout:

```text
project_x/
├─ X.py
├─ wsgi.py
├─ Procfile
├─ render.yaml
├─ .python-version
├─ requirements.txt
├─ README.md
├─ DEPLOYMENT.md
├─ .env.example
├─ .gitignore
├─ templates/
├─ static/
└─ phone_apk_app/
   └─ app source files only
```

Runtime-only folders:

- `database/`
- `logs/`
- `uploads/`
- `data/` on Render when using the persistent disk

Local/Android artifacts to keep out of GitHub:

- `.env`
- `.venv/`
- `cloudflared*.exe`
- `phone_apk_app/.tools/`
- `phone_apk_app/local.properties`
- `phone_apk_app/app/build/`
