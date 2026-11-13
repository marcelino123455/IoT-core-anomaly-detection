# API VentiSet: datos del ventilador para el gemelo digital

API HTTP con el estado del ventilador en tiempo real (el ESP32 manda 1 lectura por segundo) y un comando para prenderlo o apagarlo.

- **Base URL:** `https://<id>.lambda-url.us-east-1.on.aws` (la pasa quien despliega; sale como `ViewerUrl` en los outputs de `sam deploy`)
- **Lectura** (`GET`): no necesita autenticación.
- **Comandos** (`POST /api/command`): necesitan la cabecera `X-Api-Key`.
- **CORS:** abierto (`*`), así que se puede llamar con `fetch` desde cualquier web.
- **Parámetro común:** `device` (opcional, por defecto `esp32-26`).

---

## `GET /api/latest`: estado actual

Es el endpoint del gemelo. Puedes llamarlo cada 1 s, porque es una sola lectura de base de datos y responde rápido.

```json
{
  "device": "esp32-26",
  "online": true,
  "last_seen_ms": 1794533857357,
  "age_s": 0.8,
  "fan": "ENCENDIDO",
  "reading": {
    "temperatura_C": 26.1,
    "aceleracion_x": -8.8,
    "aceleracion_y": 0.02,
    "aceleracion_z": 4.0,
    "corriente_mV": 1317
  },
  "prediction": { "is_anomaly": false, "score": 0.373, "threshold": 0.528 },
  "consecutive_anomalies": 0,
  "shutdown_threshold": 3
}
```

| Campo | Tipo | Significado |
|---|---|---|
| `online` | bool | `false` si no llega ninguna lectura hace más de 10 s (ESP32 apagado o sin WiFi). |
| `last_seen_ms` | int \| null | Hora de la última lectura (epoch en ms). |
| `age_s` | float \| null | Segundos desde la última lectura. |
| `fan` | `"ENCENDIDO"` \| `"APAGADO"` \| null | Estado del relé del ventilador. |
| `reading` | objeto \| null | Última lectura de sensores: temperatura (°C), aceleración del MPU6050 (m/s², ejes x/y/z) y corriente del ACS712 (mV). |
| `prediction` | objeto \| null | Resultado del detector de anomalías. **Es `null` cuando el ventilador está APAGADO**, porque el modelo solo evalúa con el ventilador encendido. |
| `prediction.is_anomaly` | bool | `true` si la lectura se ve rara (`score > threshold`). |
| `prediction.score` | float | Qué tan rara es la lectura: más alto, más raro. **No es una probabilidad.** |
| `consecutive_anomalies` | int | Anomalías seguidas. Al llegar a `shutdown_threshold` se pide apagar el ventilador. |

Si el dispositivo nunca envió datos, responde igual con `200`, `online: false` y los demás campos en `null`.

## `GET /api/history?limit=30`: últimas lecturas

Sirve para gráficos o una línea de tiempo. `limit` va de 1 a 100 (por defecto 30). **No lo llames más de una vez cada 3 s.**

```json
{
  "device": "esp32-26",
  "consecutive_anomalies": 0,
  "predictions": [
    {
      "ts_ms": 1794532842700,
      "estado_ventilador": "ENCENDIDO",
      "is_anomaly": true,
      "score": 0.760,
      "threshold": 0.528,
      "consecutive_anomalies": 2,
      "shutdown_requested": false,
      "reading": { "temperatura_C": 28.75, "aceleracion_x": -8.39, "aceleracion_y": -3.73, "aceleracion_z": -2.32, "corriente_mV": 1265.9 }
    }
  ]
}
```

Vienen ordenadas **de la más reciente a la más antigua**. Solo incluye lecturas con el ventilador ENCENDIDO.

## `POST /api/command`: prender o apagar el ventilador

Necesita la cabecera **`X-Api-Key`**. La clave te la pasa quien despliega.

```http
POST /api/command
Content-Type: application/json
X-Api-Key: <clave>

{"action": "OFF"}
```

`action` acepta `"ON"` u `"OFF"` (sin importar mayúsculas). Respuesta `202`:

```json
{"sent": true, "action": "OFF", "topic": "esp32/sub", "note": "el estado se ve en /api/latest con la siguiente lectura (~1 s)"}
```

`202` significa que **la orden se envió al ESP32**, no que ya se ejecutó. Para confirmar, mira que `fan` cambie en `/api/latest` y que `last_seen_ms` sea posterior al momento del envío. Normalmente tarda 1–2 s. Si el ESP32 está offline, la orden se pierde: no queda en cola.

```js
await fetch(`${API}/api/command`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Api-Key": CLAVE },
  body: JSON.stringify({ action: "ON" }),
});
```

> La clave queda visible en el código de cualquier web que la use desde el navegador. Para la demo está bien; no la publiques en un repositorio público.

---

## Ejemplo para el gemelo

```js
const API = "https://<id>.lambda-url.us-east-1.on.aws";

async function actualizarGemelo() {
  try {
    const r = await fetch(`${API}/api/latest?device=esp32-26`);
    const s = await r.json();

    // Ejemplos de cómo mapear los datos al modelo 3D:
    const girando = s.online && s.fan === "ENCENDIDO";        // animar las aspas
    const alerta  = s.prediction?.is_anomaly ?? false;         // pintar de rojo
    const critico = s.consecutive_anomalies >= s.shutdown_threshold;
    const temp    = s.reading?.temperatura_C;                  // color o etiqueta de temperatura
    const vib     = s.reading && Math.hypot(s.reading.aceleracion_x, s.reading.aceleracion_y, s.reading.aceleracion_z);

    // ...aplicar a la escena (three.js, Babylon, etc.)
  } catch (e) {
    // Sin red: dejar el último estado y mostrar "sin conexión".
  }
}

setInterval(actualizarGemelo, 1000);
```

## Errores

| Código | Cuándo |
|---|---|
| `400` | `limit` no es un número, o el cuerpo del comando no es `{"action":"ON"\|"OFF"}`. |
| `401` | Falta `X-Api-Key` o es incorrecta. |
| `404` | La ruta no existe. |
| `405` | Método incorrecto: `POST` en una ruta de lectura, o `GET` en `/api/command`. |
| `503` | La API no tiene configurado el endpoint de IoT. |

Los cuerpos de error tienen la forma `{"error": "mensaje"}`.
