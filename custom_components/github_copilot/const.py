"""Constants for the GitHub Copilot integration."""

DOMAIN = "github_copilot"
NAME = "GitHub Copilot"

AUTH_METHOD_OAUTH = "oauth"
AUTH_METHOD_PAT = "pat"
CONF_AUTH_METHOD = "auth_method"
CONF_ACCOUNT_ID = "account_id"
DATA_CREDENTIAL_LOCKS = f"{DOMAIN}_credential_locks"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN_EXPIRES_AT = "access_token_expires_at"
CONF_REFRESH_TOKEN_EXPIRES_AT = "refresh_token_expires_at"
CONF_MODEL = "model"
CONF_MODEL_BILLING = "model_billing"
CONF_ENABLE_DEVICE_CONTROL = "enable_device_control"
CONF_PROMPT = "prompt"

# Public Client ID of the project's GitHub OAuth App (device flow, no secret).
OAUTH_CLIENT_ID = "Ov23ctvtguJtOQzqvUYH"
OAUTH_DEVICE_CODE_URL = "https://github.com/login/device/code"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
OAUTH_SCOPE = "offline_access"
OAUTH_DEVICE_VERIFICATION_URL = "https://github.com/login/device"
OAUTH_TIMEOUT_SECONDS = 900
# The SDK refreshes tokens with one hour remaining; allow for callback latency.
OAUTH_REFRESH_MARGIN_SECONDS = 3660
SDK_TOKEN_MIN_LIFETIME_SECONDS = 3600

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful Home Assistant voice assistant. Use only the tools "
    "explicitly provided for this request. Do not claim that an action succeeded "
    "unless the tool result confirms it."
)
SESSION_IDLE_SECONDS = 300
MAX_ACTIVE_SESSIONS = 8
MAX_CONCURRENT_TURNS = 2
REQUEST_TIMEOUT_SECONDS = 60
MODEL_CACHE_SECONDS = 3600

ISSUE_MODEL_FALLBACK = "model_fallback"
ISSUE_MODEL_UNAVAILABLE = "model_unavailable"
ISSUE_QUOTA_EXCEEDED = "quota_exceeded"
