#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <OneWire.h>
#include <DallasTemperature.h>

const int pinRele = 26;
const int pinCorriente = 34;

Adafruit_MPU6050 mpu;
OneWire oneWire(4);
DallasTemperature sonda(&oneWire);

bool mpuDetectado = false;
bool ventiladorEncendido = false;
float ceroMv;

// =====================================
// SENSOR DE CORRIENTE
// =====================================

float leerCorrienteMv() {
  float suma = 0;

  for (int i = 0; i < 64; i++) {
    suma += analogReadMilliVolts(pinCorriente);
    delay(2);
  }

  return suma / 64;
}

// =====================================
// CONTROL DEL VENTILADOR
// =====================================

void controlarVentilador(bool encender) {
  ventiladorEncendido = encender;

  // Relé activo en LOW
  digitalWrite(pinRele, encender ? LOW : HIGH);

  Serial.print("ESTADO_RELE: ");
  Serial.println(encender ? "ENCENDIDO" : "APAGADO");
}

// =====================================
// SETUP
// =====================================

void setup() {
  Serial.begin(115200);

  // Inicializar relé apagado
  digitalWrite(pinRele, HIGH);
  pinMode(pinRele, OUTPUT);

  Wire.begin(21, 22);
  mpuDetectado = mpu.begin(0x68);

  sonda.begin();

  // Referencia sin corriente del ventilador
  delay(500);
  ceroMv = leerCorrienteMv();

  // Encender automáticamente
  controlarVentilador(true);

  Serial.println("SISTEMA INICIADO");
}

// =====================================
// LOOP
// =====================================

void loop() {

  // CONTROL SERIAL
  while (Serial.available()) {
    char comando = Serial.read();

    if (comando == 'L' || comando == 'l') {
      controlarVentilador(true);
    }

    if (comando == 'H' || comando == 'h') {
      controlarVentilador(false);
    }

    // Consultar estado sin cambiar el relé
    if (comando == 'S' || comando == 's') {
      Serial.print("ESTADO_RELE: ");
      Serial.println(
        ventiladorEncendido ? "ENCENDIDO" : "APAGADO"
      );
    }
  }

  unsigned long tiempoMs = millis();

  // TEMPERATURA
  sonda.requestTemperatures();
  float temperatura = sonda.getTempCByIndex(0);

  if (temperatura == DEVICE_DISCONNECTED_C) {
    Serial.print("DS18B20: sin lectura");
  } else {
    Serial.printf("DS18B20: %.2f C", temperatura);
  }

  // ACELEROMETRO
  if (mpuDetectado) {
    sensors_event_t aceleracion, giro, temperaturaInterna;

    mpu.getEvent(
      &aceleracion,
      &giro,
      &temperaturaInterna
    );

    Serial.printf(
      " | MPU6050 X/Y/Z: %.2f / %.2f / %.2f m/s^2",
      aceleracion.acceleration.x,
      aceleracion.acceleration.y,
      aceleracion.acceleration.z
    );
  } else {
    Serial.print(" | MPU6050: sin lectura");
  }

  // CORRIENTE
  float actualMv = leerCorrienteMv();

  Serial.printf(
    " | ACS712: %.0f mV (cambio: %+.0f mV)",
    actualMv,
    actualMv - ceroMv
  );

  // TIEMPO
  Serial.printf(
    " | Tiempo: %.2f s\n",
    tiempoMs / 1000.0
  );

  delay(300);
}
