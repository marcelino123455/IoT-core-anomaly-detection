# VentiSet – inferencia en AWS (SAM)

```
ESP32 --MQTT esp32/pub--> IoT Rule --> Lambda (contenedor, best_model.joblib)
                                           |-> DynamoDB (predicciones + contador por device)
                                           |-> MQTT ventiset/inference/<device>  (resultado)
                                           '-> MQTT esp32/sub {"message":"OFF"}  (solo si EnableRemoteShutdown=true)
```

## Requisitos
- AWS CLI configurado, SAM CLI y **Docker Desktop corriendo** (la Lambda es imagen: sklearn+scipy no caben en zip).
- `inference/model/best_model.joblib` es una copia de `rodri/VentiSet_ML/VentiSet_ML/models/best_model.joblib`.
  Si reentrenas, vuelve a copiarlo. scikit-learn está fijado en **1.9.1** (la versión que lo serializó).

## Deploy
```powershell
cd infra
aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text
sam build
sam deploy --guided   # IotDataEndpoint = salida del comando anterior; resto por defecto
```

## Probar
```powershell
sam local invoke InferenceFunction -e events/reading.json   # (fallará en DynamoDB si no hay tabla; útil para ver carga del modelo)
aws iot-data publish --topic esp32/pub --cli-binary-format raw-in-base64-out --payload file://events/reading.json
```
Ver resultados: suscribirse a `ventiset/inference/#` en el MQTT test client, o `sam logs -n InferenceFunction --tail`.

## Payload que debe publicar el ESP32
Una lectura por mensaje (el modelo no usa ventanas):
```json
{"device":"esp32-26","temperatura_C":26.56,"aceleracion_x":-8.74,"aceleracion_y":-2.84,
 "aceleracion_z":-1.38,"corriente_mV":1304.0,"estado_ventilador":"ENCENDIDO"}
```
También acepta lote: `{"device":"esp32-26","samples":[{...},{...}]}`.
Si `estado_ventilador` no es `ENCENDIDO` la lectura se omite y el contador vuelve a 0.

> Ojo: el `main/main.ino` actual solo publica `temperature` y `timestamp`; hay que publicar las 5 features con esos nombres.

## Apagado remoto
Con `ConsecutiveAnomalies` (3) anomalías seguidas se *prepara* `{"message":"OFF",...}`. Solo se publica en `esp32/sub`
si `EnableRemoteShutdown=true`. Mantenerlo en `false` hasta validar en banco; el ESP32 debe poder apagarse localmente aunque AWS falle.
