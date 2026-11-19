"""Lambda de inferencia VentiSet.

Disparada por una IoT Rule sobre el topico de lecturas del ESP32. Cada lectura
se evalua sola (el modelo no usa ventanas). La logica de "N anomalias seguidas"
se lleva aqui con un contador por dispositivo en DynamoDB.

Payload esperado (una lectura):
  {"device":"esp32-26","temperatura_C":26.5,"aceleracion_x":-8.7,
   "aceleracion_y":-2.8,"aceleracion_z":-1.4,"corriente_mV":1304,
   "estado_ventilador":"ENCENDIDO"}
o un lote: {"device":"esp32-26","samples":[{...},{...}]}
"""
from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from pathlib import Path

import boto3
import joblib
import numpy as np
import pandas as pd

log = logging.getLogger()
log.setLevel(logging.INFO)

MODEL_PATH = Path(__file__).resolve().parent / "model" / "best_model.joblib"
ARTIFACT = joblib.load(MODEL_PATH)  # se carga una vez por cold start
FEATURES = ARTIFACT["features"]
THRESHOLD = float(ARTIFACT["threshold"])
CAL_SCORES = np.asarray(ARTIFACT["calibration_scores"])
VERSION = ARTIFACT.get("version", "unknown")
# Campos extra del firmware (ventana de vibracion + diagnostico). Se GUARDAN en DynamoDB
# aunque el modelo actual no los use, para reentrenar y para el dashboard.
EXTRA_FIELDS = ["vibracion_pico", "vibracion_std", "acel_media_x", "acel_media_y", "acel_media_z",
                "muestras_vib", "muestras_descartadas", "errores_i2c", "mpu_reconfig"]
STORED_FIELDS = list(dict.fromkeys([*FEATURES, *EXTRA_FIELDS]))

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_endpoint = os.environ.get("IOT_DATA_ENDPOINT", "").strip()
IOT = boto3.client("iot-data", endpoint_url=f"https://{_endpoint}") if _endpoint else None

COMMAND_TOPIC = os.environ.get("COMMAND_TOPIC", "esp32/sub")
N_CONSEC = int(os.environ.get("CONSECUTIVE_ANOMALIES", "3"))
ARMED = os.environ.get("ENABLE_REMOTE_SHUTDOWN", "false").lower() == "true"
TTL_DAYS = int(os.environ.get("PREDICTION_TTL_DAYS", "30"))


def predict(rows: list[dict]) -> list[dict]:
    x = pd.DataFrame([[float(r[c]) for c in FEATURES] for r in rows], columns=FEATURES)
    if not np.all(np.isfinite(x.to_numpy())):
        raise ValueError("Valores no finitos")
    scores = -ARTIFACT["pipeline"].score_samples(x)
    out = []
    for s in scores:
        s = float(s)
        pct = 100 * float(np.searchsorted(CAL_SCORES, s, side="right") / len(CAL_SCORES))
        out.append({"is_anomaly": s > THRESHOLD, "score": round(s, 6),
                    "threshold": round(THRESHOLD, 6),
                    "normal_reference_percentile": round(pct, 2),
                    "model_version": VERSION})
    return out


def _reading(sample: dict) -> dict:
    """Campos numericos finitos de la lectura (features + extra), como Decimal para DynamoDB."""
    out = {}
    for c in STORED_FIELDS:
        v = sample.get(c)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v):
            out[c] = Decimal(str(v))
    return out


def update_state(device: str, is_anomaly: bool, sample: dict, estado: str, ts_ms: int,
                 pred: dict | None = None) -> int:
    """Ultimo estado del dispositivo (lo lee el gemelo digital) + contador atomico
    de anomalias consecutivas."""
    values = {":t": int(time.time()), ":e": estado, ":r": _reading(sample), ":ms": ts_ms,
              ":p": json.loads(json.dumps(pred), parse_float=Decimal) if pred else None}
    sets = "updated_at = :t, last_estado = :e, last_reading = :r, last_seen_ms = :ms, last_prediction = :p"
    if is_anomaly:
        expr = f"ADD consecutive :one SET {sets}"
        values[":one"] = 1
    else:
        expr = f"SET consecutive = :zero, {sets}"
        values[":zero"] = 0
    r = TABLE.update_item(Key={"pk": f"DEVICE#{device}", "sk": "STATE"},
                          UpdateExpression=expr, ExpressionAttributeValues=values,
                          ReturnValues="UPDATED_NEW")
    return int(r["Attributes"]["consecutive"])


def publish(topic: str, payload: dict) -> None:
    if IOT is None:
        log.warning("IOT_DATA_ENDPOINT vacio; no se publica en %s", topic)
        return
    IOT.publish(topic=topic, qos=1, payload=json.dumps(payload).encode())


def handler(event, context):
    device = str(event.get("device") or event.get("client_id") or "unknown")
    samples = event.get("samples") or [event]
    results = []

    for i, sample in enumerate(samples):
        estado = str(sample.get("estado_ventilador", event.get("estado_ventilador", "ENCENDIDO"))).strip().upper()
        sample_id = str(sample.get("id") or f"{event.get('received_at', int(time.time()*1000))}-{i}")
        base = {"device": device, "id": sample_id, "estado_ventilador": estado}
        ts_ms = int(event.get("received_at") or time.time() * 1000)

        # El modelo solo se entreno con ENCENDIDO.
        if estado != "ENCENDIDO":
            update_state(device, False, sample, estado, ts_ms)
            results.append({**base, "skipped": "estado distinto de ENCENDIDO"})
            continue
        try:
            pred = predict([sample])[0]
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Lectura invalida %s: %s", sample_id, e)
            results.append({**base, "error": f"lectura invalida: {e}"})
            continue

        consecutive = update_state(device, pred["is_anomaly"], sample, estado, ts_ms,
                                   {"is_anomaly": pred["is_anomaly"], "score": pred["score"],
                                    "threshold": pred["threshold"]})
        shutdown = pred["is_anomaly"] and consecutive >= N_CONSEC
        result = {**base, **pred, "consecutive_anomalies": consecutive,
                  "shutdown_requested": shutdown, "shutdown_sent": False}

        if shutdown:
            cmd = {"message": "OFF", "event_type": "ANOMALY_SHUTDOWN_REQUEST",
                   "device": device, "sample_id": sample_id, "score": pred["score"]}
            if ARMED:
                publish(COMMAND_TOPIC, cmd)
                result["shutdown_sent"] = True
                log.warning("OFF enviado a %s: %s", COMMAND_TOPIC, cmd)
            else:
                log.warning("OFF preparado pero NO enviado (ENABLE_REMOTE_SHUTDOWN=false): %s", cmd)

        TABLE.put_item(Item=json.loads(json.dumps({
            "pk": f"DEVICE#{device}", "sk": f"PRED#{int(time.time()*1000):015d}#{sample_id}",
            **result, "reading": {c: sample.get(c) for c in STORED_FIELDS if sample.get(c) is not None},
            "expires_at": int(time.time()) + TTL_DAYS * 86400}), parse_float=Decimal))
        results.append(result)

    log.info(json.dumps(results))
    return {"device": device, "results": results}
