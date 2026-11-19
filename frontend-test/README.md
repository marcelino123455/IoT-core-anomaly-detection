# VentiSet: consumir la API en tiempo real (frontend-test)

Cómo leer los datos del ventilador que entrega la Lambda y mostrarlos en vivo en una web.
`index.html` de esta carpeta es un ejemplo completo y funcional: úsalo como referencia.

---

## 1. Probar la web de ejemplo

1. Abre `index.html` en el navegador (doble clic, no necesita servidor).
2. En el campo de arriba pega la **URL de la API**:
   ```
   https://6whtx5hzyyqq7ch4b7f6o622m40kzrxw.lambda-url.us-east-1.on.aws
   ```
3. Dispositivo: `esp32-26`.
4. Conecta. Con el ESP32 encendido y con WiFi, los datos se actualizan cada **1 s**.

> Si la URL cambia tras un deploy, sale como `ViewerUrl` en la salida de `sam deploy`.

---

## 2. Cómo funciona el "tiempo real"

No hay WebSocket: la web **pregunta periódicamente** (polling).

```
ESP32 --(1 lectura/s)--> AWS IoT --> Lambda --> DynamoDB <-- API (GET) <-- tu web
```

| Endpoint | Cada cuánto llamarlo | Para qué |
|---|---|---|
| `GET /api/latest` | **1 s** | Estado actual: tarjetas, gemelo, alertas |
| `GET /api/history?limit=30` | **5 s** (mínimo 3 s) | Gráficos / línea de tiempo |
| `POST /api/command` | al hacer clic | Prender / apagar (requiere `X-Api-Key`) |

- Sin autenticación para los `GET`. CORS abierto: funciona con `fetch` desde cualquier web.
- Parámetro común: `?device=esp32-26`.

---

## 3. `GET /api/latest`

```json
{
  "device": "esp32-26",
  "online": true,
  "age_s": 0.8,
  "fan": "ENCENDIDO",
  "reading": {
    "temperatura_C": 30.4,
    "corriente_mV": 1395,
    "aceleracion_x": 1.2, "aceleracion_y": -0.3, "aceleracion_z": 10.2,

    "vibracion_pico": 0.91,
    "vibracion_std": 0.48,
    "acel_media_x": 1.21, "acel_media_y": -0.31, "acel_media_z": 10.20,
    "errores_i2c": 0,
    "muestras_descartadas": 1,
    "muestras_vib": 197,
    "mpu_reconfig": 0
  },
  "prediction": { "is_anomaly": false, "score": 0.37, "threshold": 0.53 },
  "consecutive_anomalies": 0,
  "shutdown_threshold": 3
}
```

### Campos

| Campo | Significado |
|---|---|
| `online` | `false` si no llega lectura hace más de 10 s (ESP32 apagado / sin WiFi) |
| `age_s` | segundos desde la última lectura |
| `fan` | `"ENCENDIDO"` / `"APAGADO"` |
| **`reading.vibracion_pico`** | **NUEVO · la variable más importante.** Intensidad de vibración (m/s²). **~0.9 = normal · 2–7 = golpes / anomalía** |
| **`reading.vibracion_std`** | **NUEVO.** Vibración promedio de la ventana (m/s²). **~0.5 = normal · ~1+ = golpes** |
| **`reading.acel_media_x/y/z`** | **NUEVO.** Orientación del ventilador (estable). Si cambia, lo inclinaron/movieron |
| **`reading.errores_i2c`** | **NUEVO · diagnóstico.** Lecturas fallidas del sensor (de 200). Ideal 0; > 20 = cable flojo / ruido |
| **`reading.muestras_descartadas`** | **NUEVO · diagnóstico.** Muestras corruptas filtradas en el ESP32 |
| `reading.temperatura_C` | DS18B20 (°C) |
| `reading.corriente_mV` | ACS712 (mV). Útil solo para encendido/apagado: tiene deriva |
| `reading.aceleracion_x/y/z` | Muestra instantánea (ruidosa). Preferir `vibracion_*` |
| `prediction.is_anomaly` | `true` si el modelo ve algo raro. **`null` con el ventilador APAGADO** |
| `prediction.score` | más alto = más raro (no es probabilidad) |
| `consecutive_anomalies` | anomalías seguidas; al llegar a `shutdown_threshold` se pide apagar |

> Los campos **NUEVO** solo aparecen en lecturas enviadas por el firmware actual. Lecturas viejas
> no los traen: maneja `undefined` / `null` (ver el `?? "–"` del ejemplo).

> Hasta que se despliegue el modelo reentrenado, `prediction` viene del modelo viejo y marca casi
> todo como anomalía. Mientras tanto, para ver golpes mira directamente **`vibracion_pico`**.

---

## 4. `GET /api/history?limit=30`

Últimas lecturas (1–100), **de la más reciente a la más antigua**, solo con el ventilador ENCENDIDO.
Cada elemento trae el mismo `reading` que `/api/latest` (incluidos los campos nuevos):

```json
{
  "predictions": [
    { "ts_ms": 1794532842700, "is_anomaly": false, "score": 0.37, "threshold": 0.53,
      "reading": { "vibracion_pico": 0.91, "vibracion_std": 0.48, "temperatura_C": 30.4, "...": "..." } }
  ]
}
```

Para graficar en orden cronológico: `predictions.slice().reverse()`.

---

## 5. `POST /api/command`

```js
await fetch(`${API}/api/command`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Api-Key": CLAVE },
  body: JSON.stringify({ action: "OFF" }),   // "ON" | "OFF"
});
```

Responde `202` = **orden enviada**, no ejecutada. Confírmalo viendo que `fan` cambie en
`/api/latest` (~1–2 s). Si el ESP32 está offline, la orden se pierde.

---

## 6. Código mínimo en tiempo real

```html
<div>Ventilador: <span id="fan">–</span></div>
<div><b>Vibración pico: <span id="pico">–</span></b></div>
<div><b>Vibración std: <span id="std">–</span></b></div>
<div>Temperatura: <span id="temp">–</span></div>
<div id="alerta"></div>

<script>
const API = "https://6whtx5hzyyqq7ch4b7f6o622m40kzrxw.lambda-url.us-east-1.on.aws";
const DEVICE = "esp32-26";
const fmt = (v, d = 2) => (typeof v === "number" ? v.toFixed(d) : "–");

async function actualizar() {
  try {
    const r = await fetch(`${API}/api/latest?device=${DEVICE}`, { cache: "no-store" });
    const s = await r.json();
    const rd = s.reading || {};

    document.getElementById("fan").textContent  = s.online ? (s.fan ?? "–") : "OFFLINE";
    document.getElementById("pico").textContent = fmt(rd.vibracion_pico);
    document.getElementById("std").textContent  = fmt(rd.vibracion_std);
    document.getElementById("temp").textContent = fmt(rd.temperatura_C) + " °C";

    // Alerta simple por vibración (no depende del modelo):
    const golpe = (rd.vibracion_pico ?? 0) > 1.5;
    document.getElementById("alerta").textContent = golpe ? "⚠ Vibración anormal" : "";
  } catch (e) {
    console.warn("API no disponible", e);
  }
}

actualizar();
setInterval(actualizar, 1000);   // 1 s: tiempo real
</script>
```

Umbral sugerido para la alerta por vibración: **`vibracion_pico > 1.5`** (normal ≈ 0.9, golpes 2–7).

---

## 7. Problemas comunes

| Síntoma | Causa |
|---|---|
| `online: false` | ESP32 apagado, sin WiFi o sin AWS. Revisa el Monitor Serial |
| `vibracion_pico` no aparece | Lectura vieja (firmware anterior) o el ESP32 aún no envía. Espera ~1 s |
| `errores_i2c` alto (> 20) | Cable del MPU6050 flojo o ruido del motor |
| `prediction` siempre anomalía | Modelo viejo; esperar el modelo reentrenado |
| Error de CORS / red en consola | URL mal pegada (sin `https://` o con espacios) |
