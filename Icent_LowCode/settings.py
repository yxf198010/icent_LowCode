"""
Django settings for Icent_LowCode project.

Optimized for security, maintainability, and multi-environment support.
"""

import os
from pathlib import Path
import logging
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from Icent_LowCode.version import __version__

print(f"Running Icent LowCode v{__version__}")

# Load .env file (if exists)
env_path = Path('.env')
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    # Optional: warn in dev, fail in prod?
    if not os.getenv("SECRET_KEY"):
        print("[WARNING] No .env file found and SECRET_KEY not set via environment.")  # 替换特殊字符

BASE_DIR = Path(__file__).resolve().parent.parent


def get_env_var(var_name: str, default=None, required=False, cast=None):
    """
    获取环境变量，支持类型转换。

    Args:
        var_name: 环境变量名
        default: 默认值
        required: 是否必须存在
        cast: 转换函数，如 int, float, lambda x: x.lower() == 'true'
    """
    value = os.getenv(var_name, default)
    if required and value is None:
        raise ImproperlyConfigured(f"Environment variable {var_name} is required but not set.")
    if cast and value is not None:
        try:
            return cast(value)
        except (ValueError, TypeError) as e:
            raise ImproperlyConfigured(f"Invalid value for {var_name}: {value}") from e
    return value


# ----------------------------
# Core Settings
# ----------------------------

SECRET_KEY = get_env_var("SECRET_KEY", required=True)

DEBUG = get_env_var("DEBUG", default="False", cast=lambda x: x.lower() in ("true", "1", "yes"))

ALLOWED_HOSTS = [host.strip() for host in get_env_var("ALLOWED_HOSTS", default="localhost,127.0.0.1").split(",") if
                 host.strip()]

# Application definition
INSTALLED_APPS = [
    # Local
    'lowcode',
    # Django 内置应用
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'rest_framework.authtoken',
    'django_celery_results',
    'drf_spectacular',
    'django_filters',
    'health_check',
    'health_check.db',
    'health_check.cache',
    'health_check.storage',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django.middleware.locale.LocaleMiddleware',
]

ROOT_URLCONF = 'Icent_LowCode.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            BASE_DIR / 'lowcode/templates',  # 统一使用 Path 语法，替换 os.path.join
            BASE_DIR / 'templates',  # 统一使用 Path 语法，替换 os.path.join
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.debug',  # 启用debug变量
            ],
            # 注册自定义模板标签（关键）
            'libraries': {
                'template_tags': 'lowcode.templatetags.template_tags',
            }
        },
    },
]

WSGI_APPLICATION = 'Icent_LowCode.wsgi.application'

# ----------------------------
# Database
# ----------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": get_env_var("DB_NAME", default="icent_LowCode"),
        "USER": get_env_var("DB_USER", default="safin"),
        "PASSWORD": get_env_var("DB_PASSWORD", default="safin123"),
        "HOST": get_env_var("DB_HOST", default="localhost"),
        "PORT": get_env_var("DB_PORT", default="5433"),
        # "OPTIONS": {
        #     "isolation_level": "READ COMMITTED",
        # },
    }
}

# ----------------------------
# Password validation
# ----------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ----------------------------
# Internationalization
# ----------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
# USE_L10N is deprecated since Django 4.0 — removed

LANGUAGES = [
    ('en', 'English'),
    ('zh-hans', 'Simplified Chinese'),
]

# ----------------------------
# Static & Media Files (修正后)
# ----------------------------

STATIC_URL = '/static/'
# 统一使用 Path 语法，避免混用 os.path
STATICFILES_DIRS = [
    BASE_DIR / 'static',  # 根目录静态资源（存放 Vue 打包的 lowcode_designer）
    BASE_DIR / 'lowcode' / 'static',  # lowcode 应用内静态资源
]
# 生产环境静态文件收集目录
STATIC_ROOT = BASE_DIR / 'staticfiles'

# 显式配置静态文件查找器（可选，增强兼容性）
STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
]

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Safely create media directory (skip if read-only FS)
try:
    MEDIA_ROOT.mkdir(exist_ok=True)
except OSError:
    pass  # e.g., read-only filesystem in container

# ----------------------------
# Default Primary Key Field
# ----------------------------

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ----------------------------
# REST Framework
# ----------------------------

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Icent LowCode API',
    'DESCRIPTION': 'Low-code platform backend API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SWAGGER_UI_SETTINGS': {
        'deepLinking': True,
        'persistAuthorization': True,
    },
}

# ----------------------------
# Logging (最终修正版：移除 StreamHandler 的 encoding 参数)
# ----------------------------

LOGS_DIR = BASE_DIR / "log"
try:
    LOGS_DIR.mkdir(exist_ok=True)
except OSError:
    pass

def get_lowcode_log_level():
    """智能确定 lowcode 日志级别"""
    # 如需保留环境变量控制，可注释上面一行，启用下面逻辑：
    # level = get_env_var("LOWCODE_LOG_LEVEL", default="").strip().upper()
    # if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    #     return level
    # return "DEBUG" if DEBUG else "INFO"

    # 强制设置为 INFO，不再输出 DEBUG 日志
    return "INFO"  # 默认 INFO，不再根据 DEBUG 模式切换

# DEBUG = False  # 替代原来的 get_env_var 动态获取
# 或保留环境变量，但强制默认值为 False：
# DEBUG = get_env_var("DEBUG", default="False", cast=lambda x: x.lower() in ("true", "1", "yes"))

# 最终修正版 LOGGING 配置
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,

    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
        'audit': {  # 新增审计日志格式
            'format': '{levelname} {asctime} {user} {method} {params} {message}',
            'style': '{',
        },
    },

    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': LOGS_DIR / 'django.log',
            'formatter': 'verbose',
            'encoding': 'utf-8',  # FileHandler 支持 encoding，保留
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            # 移除 encoding 参数：StreamHandler 不支持该参数，避免初始化报错
        },
        'audit_file': {  # 审计日志处理器
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': LOGS_DIR / 'audit.log',
            'formatter': 'audit',
            'encoding': 'utf-8',  # FileHandler 支持 encoding，保留
        },
    },

    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',    # 根日志器级别为 INFO，过滤 DEBUG
    },

    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',     # Django 核心日志级别为 INFO
            'propagate': False,
        },
        'lowcode': {
            'handlers': ['console', 'file'],
            'level': get_lowcode_log_level(),   # 现在返回 INFO,若只想屏蔽 lowcode 应用的 DEBUG 日志，可直接将其日志级别设为 WARNING：
            'propagate': False,
        },
        'lowcode.decorators.audit_log': {  # 审计日志专用 logger
            'handlers': ['audit_file'],
            'level': 'INFO',     # 审计日志级别为 INFO
            'propagate': False,
        },
    },
}

# ----------------------------
# LowCode Custom Settings
# ----------------------------

LOWCODE_ENABLE_LOGGING = get_env_var("LOWCODE_ENABLE_LOGGING", default="True", cast=lambda x: x.lower() == "true")

LOWCODE_LOAD_CONFIG = {
    'max_retries': get_env_var("LOWCODE_MAX_RETRIES", default="2", cast=int),
    'retry_interval': get_env_var("LOWCODE_RETRY_INTERVAL", default="1", cast=float),
    'initial_delay': get_env_var("LOWCODE_INITIAL_DELAY", default="0.5", cast=float),
    'daemon_thread': True,
}

# Unified transaction retry configuration (for custom decorators/utils)
UNIVERSAL_TRANSACTION_DEFAULTS = {
    "timeout": get_env_var("TRANSACTION_TIMEOUT", default="5.0", cast=float),
    "retry_times": get_env_var("TRANSACTION_RETRY_TIMES", default="2", cast=int),
    "retry_delay": get_env_var("TRANSACTION_RETRY_DELAY", default="0.5", cast=float),
    "allowed_exceptions_patterns": [
        "deadlock", "Deadlock", "could not serialize",
        "concurrent update", "lock timeout", "Lock wait timeout"
    ]
}

# ----------------------------
# Celery
# ----------------------------

CELERY_BROKER_URL = get_env_var("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = get_env_var("CELERY_RESULT_BACKEND", default="django-db")
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60

# ----------------------------
# Sentry (Optional)
# ----------------------------

SENTRY_DSN = get_env_var("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=1.0,
        send_default_pii=False,
    )

DYNAMIC_MODEL_CONFIG_PATH = BASE_DIR / 'config' / 'lowcode_models.json'

# 启用低代码方法调用审计日志（默认关闭）
LOWCODE_METHOD_LOGGING_ENABLED = True  # 开发/审计时开启，生产按需

# ----------------------------
# CORS Settings
# ----------------------------
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = ["http://localhost:5173"]  # Vue Dev Server 默认端口