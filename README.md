# Project X - Remote Device Control System

نظام التحكم عن بعد في الأجهزة مع دعم WhatsApp والموقع الجغرافي

## Overview

Project X is a web-based remote device control system that allows:
- Real-time device registration (via Socket.IO or HTTP fallback)
- Location permission requests and geolocation sharing
- WhatsApp integration for notifications
- Command queue system for offline devices
- Pending actions handling and auto-polling

## Features

✅ **Real-time Communications** - Socket.IO WebSocket for instant command delivery  
✅ **Dual Registration** - Socket.IO (realtime) or HTTP fallback (informational)  
✅ **Location Sharing** - Browser geolocation with permission management  
✅ **Pending Queue** - Automatic location request queuing when device offline  
✅ **WhatsApp Integration** - Notifications via WAWP API  
✅ **Mobile Detection** - Adaptive validation for various viewport sizes  
✅ **Dashboard** - Web-based control interface  
✅ **Phone Interface** - Mobile-friendly device page with auto-polling  

## Tech Stack

- **Backend**: Python Flask + Flask-SocketIO
- **Frontend**: HTML5, JavaScript (Socket.IO client)
- **Database**: SQLite
- **Deployment**: WSGI/Gunicorn for hosted deployments; Cloudflare tunnel is optional for local testing
- **External APIs**: WAWP (WhatsApp), Groq/X.AI (LLM)

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
- Node.js/npm (optional, for frontend build tools)
- Git

### Setup

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/project-x.git
cd project-x
```

2. **Create virtual environment**
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# or
source .venv/bin/activate  # macOS/Linux
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Configure environment**
```bash
cp .env.example .env
# Edit .env with your API keys and settings
```

5. **Run the application**
```bash
python X.py
```

The app will start on `http://localhost:5000`.

For Render, use `render.yaml` or `wsgi.py` + `Procfile`, keep `ENABLE_CLOUDFLARED_TUNNEL=0`, and pin Python with `.python-version`.
See `DEPLOYMENT.md` for the exact upload list and production command.

## API Endpoints

### Device Management
- `POST /api/register-device` - Register device via HTTP fallback
- `GET /api/pending-actions?device_id=X` - Get pending actions for device

### Commands
- `socket.emit('register_device', data)` - Register via Socket.IO (realtime)
- `socket.emit('send_command', data)` - Send command to device
- `socket.on('command', handler)` - Receive commands (realtime devices only)

### Web Interface
- `GET /` - Dashboard (needs login)
- `GET /phone` - Phone device page
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
  command: 'location' // or other commands
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

See `.env.example` for all available options. Key variables:

- `AI_PROVIDER` - AI provider (groq, xai, huggingface)
- `GROQ_API_KEY` - Groq API key
- `XAI_API_KEY` - X.AI API key
- `HF_API_KEY` - Hugging Face API key
- `NGROK_AUTH_TOKEN` - Ngrok authentication (optional)

## Database

SQLite is used for storing:
- Registered devices
- Permission states
- Activity logs
- Pending requests

Database file: `database/project_x.db` (auto-created on first run)

## Troubleshooting

### Devices not registering?
- Check mobile detection settings in `is_likely_real_mobile_client()`
- Verify viewport dimensions are within 200-3000px
- Check browser console for Socket.IO connection errors

### Commands not executing?
- Verify device has `realtime_connected: True` in database
- Fallback devices require polling via `/api/pending-actions`
- Check database for device registration status

### Location not being sent?
- Ensure browser geolocation permission is granted
- Check that device is online (Socket.IO connected or polling)
- Review browser console for permission errors

## Contributing

Feel free to submit issues and enhancement requests!

## License

MIT License - see LICENSE file for details

## Author

Sherif - sherif@example.com

---

**Note**: This system requires proper API keys configured in `.env`. Never commit actual API keys to version control.
