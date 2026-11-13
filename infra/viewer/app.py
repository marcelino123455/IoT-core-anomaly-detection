"""API VentiSet: datos del ventilador + comandos ON/OFF.

Function URL:
  GET  /             -> pagina HTML que se refresca sola
  GET  /api/latest   -> estado actual (para el gemelo digital)     ?device=esp32-26
  GET  /api/history  -> ultimas predicciones (alias: /api)        ?device=esp32-26&limit=30
  POST /api/command  -> {"action":"ON"|"OFF"}, cabecera X-Api-Key -> publica en esp32/sub
CORS lo maneja la Function URL (FunctionUrlConfig.Cors en template.yaml), no este codigo.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
DEFAULT_DEVICE = os.environ.get("DEFAULT_DEVICE", "esp32-26")
SHUTDOWN_AFTER = int(os.environ.get("CONSECUTIVE_ANOMALIES", "3"))
OFFLINE_AFTER_S = 10  # sin lecturas por mas de 10 s -> online=false
MAX_LIMIT = 100
COMMAND_TOPIC = os.environ.get("COMMAND_TOPIC", "esp32/sub")
COMMAND_API_KEY = os.environ.get("COMMAND_API_KEY", "")
_endpoint = os.environ.get("IOT_DATA_ENDPOINT", "").strip()
IOT = boto3.client("iot-data", endpoint_url=f"https://{_endpoint}") if _endpoint else None
HTML = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")


def _plain(o):
    """Decimal de DynamoDB -> int/float para json.dumps."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o).__name__)


def _response(status: int, body, content_type: str = "application/json"):
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
        "body": body if isinstance(body, str) else json.dumps(body, default=_plain),
    }


def get_data(device: str, limit: int) -> dict:
    pk = f"DEVICE#{device}"
    preds = TABLE.query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("PRED#"),
        ScanIndexForward=False,  # mas recientes primero
        Limit=limit,
    )["Items"]
    for p in preds:
        # sk = PRED#<epoch_ms 15 digitos>#<id>
        p["ts_ms"] = int(p["sk"].split("#")[1])
        for k in ("pk", "sk", "expires_at"):
            p.pop(k, None)
    state = TABLE.get_item(Key={"pk": pk, "sk": "STATE"}).get("Item", {})
    return {
        "device": device,
        "consecutive_anomalies": state.get("consecutive", 0),
        "state_updated_at": state.get("updated_at"),
        "predictions": preds,
    }


def get_latest(device: str) -> dict:
    """Estado actual del dispositivo para el gemelo digital (1 GetItem, ~0.5 RCU)."""
    state = TABLE.get_item(Key={"pk": f"DEVICE#{device}", "sk": "STATE"}).get("Item")
    if not state or "last_seen_ms" not in state:
        return {"device": device, "online": False, "last_seen_ms": None, "age_s": None,
                "fan": None, "reading": None, "prediction": None,
                "consecutive_anomalies": 0, "shutdown_threshold": SHUTDOWN_AFTER}
    last_seen = int(state["last_seen_ms"])
    age_s = max(0.0, round(time.time() - last_seen / 1000, 1))
    consecutive = int(state.get("consecutive", 0))
    return {
        "device": device,
        "online": age_s <= OFFLINE_AFTER_S,
        "last_seen_ms": last_seen,
        "age_s": age_s,
        "fan": state.get("last_estado"),
        "reading": state.get("last_reading"),
        "prediction": state.get("last_prediction"),
        "consecutive_anomalies": consecutive,
        "shutdown_threshold": SHUTDOWN_AFTER,
    }


def send_command(event) -> dict:
    """POST /api/command {"action":"ON"|"OFF"} con cabecera X-Api-Key."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not COMMAND_API_KEY or not hmac.compare_digest(headers.get("x-api-key", ""), COMMAND_API_KEY):
        return _response(401, {"error": "X-Api-Key invalida o ausente"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        body = json.loads(raw or "{}")
        action = str(body.get("action", "")).strip().upper()
    except (ValueError, AttributeError):
        return _response(400, {"error": "el cuerpo debe ser JSON: {\"action\": \"ON\"|\"OFF\"}"})
    if action not in ("ON", "OFF"):
        return _response(400, {"error": "action debe ser ON u OFF"})
    if IOT is None:
        return _response(503, {"error": "IOT_DATA_ENDPOINT no configurado"})

    msg = {"message": action, "event_type": "MANUAL_COMMAND", "source": "api",
           "ts_ms": int(time.time() * 1000)}
    IOT.publish(topic=COMMAND_TOPIC, qos=1, payload=json.dumps(msg).encode())
    print(json.dumps({"command_sent": msg, "topic": COMMAND_TOPIC}))
    return _response(202, {"sent": True, "action": action, "topic": COMMAND_TOPIC,
                           "note": "el estado se ve en /api/latest con la siguiente lectura (~1 s)"})


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    qs = event.get("queryStringParameters") or {}

    if path == "/api/command":
        if method != "POST":
            return _response(405, {"error": "usa POST"})
        return send_command(event)
    if method != "GET":
        return _response(405, {"error": "solo GET en esta ruta"})

    if path == "/":
        return _response(200, HTML, "text/html; charset=utf-8")
    device = qs.get("device", DEFAULT_DEVICE)
    if path == "/api/latest":
        return _response(200, get_latest(device))
    if path in ("/api", "/api/history"):
        try:
            limit = max(1, min(MAX_LIMIT, int(qs.get("limit", 30))))
        except ValueError:
            return _response(400, {"error": "limit debe ser un entero"})
        return _response(200, get_data(device, limit))
    return _response(404, {"error": "ruta no encontrada"})
