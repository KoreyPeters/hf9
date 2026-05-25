from .base import *

DEBUG = True

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "localhost"
EMAIL_PORT = 1025

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
