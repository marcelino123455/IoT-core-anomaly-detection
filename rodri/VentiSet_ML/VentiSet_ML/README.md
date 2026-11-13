# VentiSet ML — detección experimental de anomalías del ventilador

Proyecto listo para entrenar localmente con `data/VentiSet.csv` y preparar integración con AWS.

## Qué incluye

- `data/VentiSet.csv`: dataset real adjunto (4,261 registros).
- `main.py`: preprocesamiento, split cronológico 70/15/15, Isolation Forest, umbral p99 calibrado con registros `ENCENDIDO`, evaluación temporal, ruido sintético progresivo y ocho gráficos.
- `plots/`: imágenes PNG creadas al entrenar.
- `models/best_model.joblib`: mejor modelo elegido por validación (cargado por AWS).
- `models/last_model.joblib`: último candidato entrenado.
- `models/ventiset_model.joblib`: alias del mejor modelo para compatibilidad.
- `results/hyperparameter_trials.csv`: comparación de los 12 candidatos.
- `results/`: métricas JSON y resultados del ensayo de ruido.
- `iot_proy.py`: cliente GET/POST de una API AWS configurable; inferencia al recibir mediciones nuevas; solicitud remota OFF *deshabilitada por defecto*.
- `requirements.txt`, `environment.yml` y `.env.example`.

## 1. Conda (Windows, Anaconda Prompt)

```powershell
conda create -n ventiset python=3.11 -y
conda activate ventiset
python -m pip install -r requirements.txt
```

Alternativa: `conda env create -f environment.yml` y `conda activate ventiset`.

## 2. Entrenar y crear plots

```powershell
python main.py
```

Crea `models/best_model.joblib`, `models/last_model.joblib`, `results/hyperparameter_trials.csv`, `results/metrics.json`, `results/ruido_0_a_100.csv` y ocho archivos PNG en `plots/`.

El entrenamiento por defecto usa CPU: tu NVIDIA RTX 3050 Laptop GPU **no es necesaria**, porque scikit-learn IsolationForest no se entrena en GPU. No hace falta instalar CUDA ni PyTorch.

## 3. Datos y preprocesamiento

El CSV adjunto contiene 8 nombres de columnas en la cabecera, pero algunas filas tienen 9 campos. Se recupera el noveno como `estado_ventilador`; registros sin ese campo reciben `SIN_ESTADO`.

Solo se entrena con `estado_ventilador=ENCENDIDO`. Aun así, **ENCENDIDO no demuestra que el ventilador funcionara normalmente**: comprobar los periodos normales antes de confiar en el modelo. El acelerómetro es sensible a orientación/ubicación, y el ADC del ACS712 depende de su alimentación y calibración entre sesiones. No utilizamos `cambio_mV` como variable primaria por su dependencia de la calibración de arranque. `tiempo_s` y `fecha_hora` se reservan para la segmentación y visualización, no para el detector.

## 4. Experimento de ruido

Se perturban **solo muestras de test**. Ruido 0–100 % significa desde 0 hasta 3× la dispersión robusta de cada feature, con dirección gaussiana fija por repetición. Se promedian 12 repeticiones, se grafica la tasa de muestras marcadas anómalas y el score; no se reporta como probabilidad estadística de avería. No se espera monotonicidad estricta en todas las repeticiones. PCA se utiliza *solo para visualizar*: el detector decide en las cinco dimensiones originales escaladas.

## 5. AWS (preparado, no conectado a una cuenta real)

Copia `.env.example` a `.env`, edita URLs HTTPS y credenciales:

```powershell
copy .env.example .env
python iot_proy.py
```

La API GET debe devolver, de forma ordenada, registros nuevos con ID estable, por ejemplo:

```json
{"samples":[{"id":"sessionA-00001","fecha_hora":"2026-11-11T13:38:39.188","temperatura_C":26.56,"aceleracion_x":-8.74,"aceleracion_y":-2.84,"aceleracion_z":-1.38,"corriente_mV":1304.0,"estado_ventilador":"ENCENDIDO"}]}
```

El cliente registra cada decisión en `results/aws_inference.jsonl`. Con 3 registros anómalos consecutivos estando `ENCENDIDO` prepara un evento POST con `event_type=ANOMALY_SHUTDOWN_REQUEST` y `command=OFF`. **Por defecto nunca lo envía** (`ENABLE_REMOTE_SHUTDOWN=false`). Solo cambiar a true después de validar en banco, confirmar autenticación/autorización, colas/reintentos, idempotencia y ruta real AWS→ESP32. Una respuesta HTTP 200 no demuestra que el relé realmente se apagó. Es importante que el dispositivo pueda apagarse localmente con seguridad incluso si AWS falla. No usar un detector experimental como único control de seguridad.

## Limitaciones y siguientes pasos

- Este proyecto implementa **Isolation Forest**, no Robust Random Cut Forest. Ambas son familias de detectores de cortes aleatorios, pero no equivalentes.
- No hay fallas reales etiquetadas: no pueden estimarse precisión, recall ni probabilidad calibrada de anomalía.
- Registros sucesivos están correlacionados; el split temporal ayuda, pero la validación adecuada requiere varias sesiones independientes y fallas controladas.
- `models/*.joblib` contiene objetos serializados: cargar solo archivos que tú mismo hayas generado y en un entorno confiable.
- El cliente AWS asume eventos nuevos con IDs estables. Para producción debe existir un backend que confirme ejecución de la orden y evite acciones duplicadas.

## Best y Last (version 2)

- La serie se divide cronológicamente 70 % entrenamiento, 15 % calibración/selección y 15 % test final. La calibración se divide internamente en mitad para definir umbral p99 y mitad para evaluar candidatos.
- Prueba 12 configuraciones de Isolation Forest; `last` se sobrescribe con el candidato más reciente y `best` con el mejor por utilidad de validación (sensibilidad ante perturbaciones sintéticas menos penalización por falsos positivos). Se priorizan candidatos con tasa de falsas alarmas de validación no mayor a 5 %.
- El conjunto de test final NO participa en la elección. Los gráficos y el cliente AWS utilizan `best_model.joblib`.
- Al no haber fallos etiquetados, `best` significa mejor en este **proxy sintético**, no mejor detector de averías reales.
- `last` no significa última época: Isolation Forest no entrena por épocas. Para usar el modelo en un sistema nuevo, **siempre conserva el preprocesador y el umbral juntos**.
