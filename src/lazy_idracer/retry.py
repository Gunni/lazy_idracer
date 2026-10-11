"""Controller-specified retry delays."""
import math
import httpx

def telemetry_retry_delay(exc):
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 503:
        return None
    try:
        response = exc.response
        delay = response.headers.get('Retry-After')
        if delay is None:
            body = response.json()
            entries = body.get('error', body).get('@Message.ExtendedInfo', [])
            delay = next((entry.get('MessageArgs', [None])[0] for entry in entries
                          if str(entry.get('MessageId', '')).endswith('.ServiceTemporarilyUnavailable')
                          and entry.get('MessageArgs')), None)
        seconds = float(delay)
        return math.ceil(seconds) if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, TypeError, KeyError):
        return None

