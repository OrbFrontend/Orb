"""Apply process privacy policy before any application dependency is imported."""

import os

# These libraries cache their switches at import/native initialization time.
# Enforce the opt-outs even when the launching environment enables telemetry.
# Downloads, authentication and configured inference endpoints remain online.
os.environ.update(
    {
        "ORT_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_XET_TELEMETRY_ENABLED": "0",
        "HF_HUB_DISABLE_UPDATE_CHECK": "1",
        "OTEL_SDK_DISABLED": "true",
        "DO_NOT_TRACK": "1",
    }
)
