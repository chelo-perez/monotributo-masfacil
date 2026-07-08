"""Variables de entorno centralizadas."""

import os

# Core
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
FERNET_KEY = os.environ.get("FERNET_KEY", "").encode()
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8001")

# Email (Resend)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@masfacil.com.ar")

# JWT
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8   # 8 horas
REFRESH_TOKEN_EXPIRE_DAYS = 30

# Planes — máximo de monotributistas por plan
PLAN_LIMITES = {
    "basico":  10,
    "estudio": 30,
    "pro":     9999,
}
