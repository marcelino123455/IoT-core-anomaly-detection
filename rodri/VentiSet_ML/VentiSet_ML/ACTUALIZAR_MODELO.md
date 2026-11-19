# Actualizar el modelo de ML (VentiSet)

Guía paso a paso para reentrenar el detector de anomalías con los **datos nuevos de vibración**.
El deploy a AWS lo hace otra persona: aquí solo se genera el archivo del modelo.

---

## 1. Dónde trabajar

```
iot-proyecto/rodri/VentiSet_ML/VentiSet_ML/
├── main.py                 <- script de entrenamiento (el que hay que modificar)
├── data/
│   ├── VentiSet.csv        <- datos VIEJOS (no sirven para golpes, ver punto 2)
│   └── capturas/           <- datos NUEVOS (usar estos)
│       ├── captura3.csv
│       ├── captura4.csv
│       └── captura_carga3.csv
└── models/
    └── best_model.joblib   <- aquí sale el modelo entrenado
```

Entra a esa carpeta antes de todo:

```bash
cd iot-proyecto/rodri/VentiSet_ML/VentiSet_ML
pip install -r requirements.txt
pip install scikit-learn==1.9.1     # OBLIGATORIO: la misma versión que usa la Lambda
```

---

## 2. Por qué hay que reentrenar

El modelo actual usa `temperatura_C`, `aceleracion_x/y/z` y `corriente_mV`.
**Ninguna de esas distingue "girando normal" de "girando con golpes"**:

- `aceleracion_x/y/z` es UNA muestra instantánea: salta al azar por la vibración del motor.
- `corriente_mV` tiene deriva (cambia sola ~30 mV en minutos) y no cambia con golpes.
- `temperatura_C` casi no se mueve (±0.2 °C).

Resultado: en AWS marca casi **todo** como anomalía.

El ESP32 ahora mide 200 muestras por lectura y envía estadísticas de vibración.
**Esas son las variables que sí separan los casos.**

---

## 3. Variables (lo más importante)

| Variable | Qué es | Normal | Golpes | ¿Usar? |
|---|---|---|---|---|
| **`vibracion_pico`** | (máx − mín)/2 de la magnitud en la ventana | **~0.9** | **2 – 7** | **SÍ (la principal)** |
| **`vibracion_std`** | desviación estándar de la magnitud | **~0.5** | **~1.0** | **SÍ** |
| `acel_media_x/y/z` | orientación promedio del sensor | estable | estable | opcional (detecta si lo inclinan) |
| `temperatura_C` | DS18B20 | ~30 °C | ~30 °C | opcional (no ayuda con golpes) |
| `corriente_mV` | ACS712 | deriva | deriva | **NO** (solo sirve para encendido/apagado) |
| `aceleracion_x/y/z` | muestra instantánea | ruido | ruido | **NO** |
| `errores_i2c`, `muestras_descartadas`, `mpu_reconfig` | diagnóstico del sensor | – | – | **NO** (no son física del ventilador) |

**Recomendación:**

```python
FEATURES = ['vibracion_pico', 'vibracion_std']
```

(Si quieren detectar también que muevan/inclinen el ventilador, agregar `'acel_media_x','acel_media_y','acel_media_z'`.)

---

## 4. Los datos nuevos

Columnas importantes de los CSV en `data/capturas/`:

| Columna | Uso |
|---|---|
| `etiqueta` | qué estaba pasando: `inicio`, `normal`, `golpe`, `sin_carga`, `con_carga`, `sin_carga2` |
| `estado_ventilador` | `ENCENDIDO` / `APAGADO` |
| `vibracion_pico`, `vibracion_std`, ... | las features |

Qué usar para qué:

| Archivo | Entrenar (solo "normal") | Probar |
|---|---|---|
| `captura3.csv` | filas `etiqueta == normal` | filas `golpe` → deben salir **anomalía** |
| `captura4.csv` | filas `etiqueta == normal` | filas `golpe` → deben salir **anomalía** |
| `captura_carga3.csv` | filas `sin_carga`, `sin_carga2` | – |

Reglas de limpieza:

1. Solo filas con `estado_ventilador == ENCENDIDO`.
2. Descartar filas `inicio` (motor arrancando).
3. Descartar las **3 primeras filas de cada fase** (la persona tarda en reaccionar al cambio).
4. Descartar filas con `errores_i2c > 20` (lecturas con mucho ruido del bus).

⚠️ `captura3.csv` se grabó con un firmware anterior: algunas filas `normal` tienen
`vibracion_pico ≈ 5.2` por un error del sensor ya corregido. **Quitar de `normal` las filas con
`vibracion_pico > 2`** (en ese archivo y en `captura4.csv`).

---

## 5. Cambios en `main.py`

### 5.1 Features (línea ~25)

```python
FEATURES = ['vibracion_pico', 'vibracion_std']
```

### 5.2 Cargar los CSV nuevos

El `load_dataset` actual lee el CSV viejo **por posición** de columnas; los nuevos tienen más
columnas y cabecera, así que se leen **por nombre**. Reemplazar la carga por algo así:

```python
from pathlib import Path
import pandas as pd

def load_dataset(carpeta: Path):
    dfs = []
    for p in sorted(carpeta.glob('*.csv')):
        df = pd.read_csv(p)
        df['archivo'] = p.name
        # quitar las 3 primeras filas de cada fase (transicion)
        df['fase'] = (df['etiqueta'] != df['etiqueta'].shift()).cumsum()
        df = df[df.groupby('fase').cumcount() >= 3]
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)

    df = df[(df.estado_ventilador == 'ENCENDIDO') & (df.etiqueta != 'inicio')]
    df = df[df.errores_i2c.fillna(0) <= 20]
    df = df.dropna(subset=FEATURES)

    es_normal = df.etiqueta.isin(['normal', 'sin_carga', 'sin_carga2'])
    normal = df[es_normal & (df.vibracion_pico <= 2)]       # entrenamiento
    golpes = df[df.etiqueta == 'golpe']                      # prueba
    return normal, golpes
```

Y en `main` usar `--csv data/capturas` (la carpeta).

### 5.3 Validar con los golpes reales

`main.py` hoy valida con anomalías **sintéticas**. Agregar la prueba con golpes reales:

```python
s_normal = -pipeline.score_samples(normal_test[FEATURES])
s_golpe  = -pipeline.score_samples(golpes[FEATURES])
print('Falsas alarmas en normal :', (s_normal > threshold).mean())
print('Golpes detectados        :', (s_golpe  > threshold).mean())
```

**Objetivo:** falsas alarmas < 5 % y golpes detectados > 80 %.
Referencia, ya medida con este `load_dataset` sobre `data/capturas/` (161 filas normal, 131 golpe):

| | `vibracion_pico` | `vibracion_std` |
|---|---|---|
| normal | 0.66 – 1.26 (media 0.88) | 0.36 – 0.69 |
| golpe  | 1.83 – 13.7 (media 4.71) | 0.75 – 3.37 |

Un umbral simple en el p99 de `vibracion_pico` normal (≈ 1.1) ya detecta el **100 %** de los golpes,
así que el modelo debería lograrlo sin problema.

### 5.4 NO cambiar el formato del archivo del modelo

La Lambda espera **exactamente** estas claves en el `.joblib` (ya las genera `main.py`, línea ~158):

```python
artifact = {
    'pipeline': ...,              # objeto con .score_samples() (ej. IsolationForest en un Pipeline)
    'features': FEATURES,         # la Lambda lee las features DE AQUÍ
    'threshold': float(...),      # score > threshold => anomalía
    'calibration_scores': np.sort(...),
    'version': 'ventiset-iforest-v3-vibracion',   # <- subir la versión
}
```

- El score es `-pipeline.score_samples(X)`: **más alto = más anómalo**.
- Los nombres en `features` deben ser **idénticos** a los del JSON del ESP32
  (`vibracion_pico`, `vibracion_std`, ...).

---

## 6. Entrenar

```bash
python main.py --csv data/capturas
```

Sale en `models/best_model.joblib`. Revisar en consola las falsas alarmas y los golpes detectados
(punto 5.3) y las gráficas en `plots/`.

---

## 7. Entregar el modelo

Copiar el archivo a la carpeta de la Lambda:

```bash
cp models/best_model.joblib ../../../infra/inference/model/best_model.joblib
```

y avisar para el deploy. **No hace falta tocar `infra/inference/app.py`**: lee las features
desde el propio modelo.

### Checklist antes de entregar

- [ ] `scikit-learn==1.9.1` (si no, la Lambda no puede abrir el modelo)
- [ ] `artifact['features']` contiene `vibracion_pico` y `vibracion_std`
- [ ] Falsas alarmas en normal < 5 %
- [ ] Golpes detectados > 80 %
- [ ] `version` actualizada

---

## 8. Más datos (opcional)

Para grabar más capturas con el ESP32 conectado (Windows):

```powershell
py ruta\a\iot-proyecto\scripts\interceptar_lecturas.py --port COM9 --out C:\capturas\nueva.csv --protocolo --ciclos 2
```

Guía por pitidos: 1 pitido = no tocar (normal), 2 pitidos agudos = golpecitos en la base cada 1–2 s.
Copiar el CSV resultante a `data/capturas/` y reentrenar.
