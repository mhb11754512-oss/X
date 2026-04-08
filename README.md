# Project X - Remote Device Control System

Remote Device Control Platform with WhatsApp, geolocation, and Socket.IO realtime control.

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Web_App-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Socket.IO](https://img.shields.io/badge/Socket.IO-Realtime-010101?logo=socketdotio&logoColor=white)](https://socket.io/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

نظام تحكم عن بعد في الأجهزة مع دعم WhatsApp، الموقع الجغرافي، والتشغيل اللحظي عبر Socket.IO.

## Overview

Project X is a web-based remote device control system that supports:

- Real-time device registration via Socket.IO or HTTP fallback
- Location permission requests and geolocation sharing
- WhatsApp notifications through WAWP
- Command queueing for offline or fallback-only devices
- Pending actions polling and automatic execution

## Features

- Real-time communications through Socket.IO WebSocket
- Dual registration flow: realtime or HTTP fallback
- Location sharing with browser permission handling
- Pending queue for deferred location requests
- WhatsApp integration for visit and location notifications
- Adaptive mobile detection based on viewport checks
- Dashboard for device management and command dispatch
- Mobile phone page with hidden controls and auto-polling

## Tech Stack

- Backend: Python Flask + Flask-SocketIO
- Frontend: HTML5 and JavaScript with Socket.IO client
- Database: SQLite
- Deployment: WSGI/Gunicorn for hosted deployments; Cloudflare tunnel optional for local testing
- External APIs: WAWP for WhatsApp, Groq/X.AI for LLM features

## Project Structure

```
project_x/
├── X.py
├── wsgi.py
├── Procfile
├── render.yaml
├── .python-version
├── requirements.txt
├── README.md
├── DEPLOYMENT.md
├── .env.example
├── .gitignore
├── templates/
├── static/
├── phone_apk_app/
├── database/    # local runtime only
├── logs/        # local runtime only
├── uploads/     # local runtime only
└── data/        # Render persistent disk mount path
```

## Installation

### Prerequisites

- Python 3.8+
- Git
- Node.js/npm only if you plan to work on the Android or auxiliary frontend tooling

### Setup

1. Clone the repository.

```bash
git clone https://github.com/yourusername/project-x.git
cd project-x
```

2. Create and activate a virtual environment.

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# or
source .venv/bin/activate  # macOS/Linux
```

3. Install dependencies.

```bash
pip install -r requirements.txt
```

4. Configure environment variables.

```bash
cp .env.example .env
# Edit .env with your API keys and settings
```

5. Run the application.

```bash
python X.py
```

The app starts on `http://localhost:5000`.

For Render, use `render.yaml` or `wsgi.py` + `Procfile`, keep `ENABLE_CLOUDFLARED_TUNNEL=0`, and pin Python with `.python-version`.
See `DEPLOYMENT.md` for the exact upload list and production command.

## API Endpoints

### Device Management

- `POST /api/register-device` - Register a device through HTTP fallback
- `GET /api/pending-actions?device_id=X` - Retrieve pending actions for a device

### Commands

- `socket.emit('register_device', data)` - Register through Socket.IO for realtime updates
- `socket.emit('send_command', data)` - Send a command to a target device
- `socket.on('command', handler)` - Receive commands on realtime-connected devices

### Web Interface

- `GET /` - Dashboard, requires login
- `GET /phone` - Phone/device page
- `GET /login` - Login page

## Socket.IO Events

### Client → Server

```javascript
socket.emit('register_device', {
  device_id: 'unique_device_id',
  device_name: 'My Device',
  device_type: 'phone'
});

socket.emit('send_command', {
  device_id: 'target_device_id',
  command: 'location'
});
```

### Server → Client

```javascript
socket.on('command', (data) => {
  // Receive command from dashboard
  // data = { command: 'location', ... }
});

socket.on('location_received', (data) => {
  // Location successfully sent
});
```

## Environment Variables

See `.env.example` for the full list. Key variables:

- `AI_PROVIDER` - AI provider selection (`groq`, `xai`, or `huggingface`)
- `GROQ_API_KEY` - Groq API key
- `XAI_API_KEY` - X.AI API key
- `HF_API_KEY` - Hugging Face API key
- `NGROK_AUTH_TOKEN` - Ngrok authentication token, optional

## Database

SQLite stores:

- Registered devices
- Permission states
- Activity logs
- Pending requests

Database file: `database/project_x.db` (auto-created on first run)

## Troubleshooting

### Devices not registering?

- Check `is_likely_real_mobile_client()`
- Verify viewport dimensions are within 200-3000px
- Check browser console for Socket.IO connection errors

### Commands not executing?

- Verify the device has `realtime_connected: True` in the database
- Fallback devices rely on polling through `/api/pending-actions`
- Check the registration state in the database

### Location not being sent?

- Ensure browser geolocation permission is granted
- Check that the device is online through Socket.IO or polling
- Review browser console for permission errors

## Contributing

Feel free to submit issues and enhancement requests.

## License

MIT License - see LICENSE file for details.

## Author

Sherif - sherif@example.com

---

**Note**: This system requires valid API keys configured in `.env`. Never commit real secrets to version control.
