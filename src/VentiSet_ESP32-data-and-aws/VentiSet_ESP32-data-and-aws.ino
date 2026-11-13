#include "secrets.h"
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// =====================================
// CONFIGURACION
// =====================================

#define AWS_IOT_PUBLISH_TOPIC   "esp32/pub"   // lecturas -> IoT Rule -> Lambda
#define AWS_IOT_SUBSCRIBE_TOPIC "esp32/sub"   // comandos {"message":"OFF"|"ON"}

const unsigned long PUBLISH_INTERVAL_MS = 1000;  // 1 lectura/s a AWS
const unsigned long RECONNECT_INTERVAL_MS = 5000;

const int pinRele = 26;
const int pinCorriente = 34;

Adafruit_MPU6050 mpu;
OneWire oneWire(4);
DallasTemperature sonda(&oneWire);

WiFiClientSecure net;
PubSubClient client(net);

bool mpuDetectado = false;
bool ventiladorEncendido = false;
float ceroMv;

unsigned long ultimoPublish = 0;
unsigned long ultimoIntentoWifi = 0;
unsigned long ultimoIntentoMqtt = 0;

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
// MQTT: COMANDOS DESDE AWS
// =====================================

void messageHandler(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<384> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.printf("[MQTT] JSON invalido en %s: %s\n", topic, err.c_str());
    return;
  }

  const char* message = doc["message"];
  Serial.printf("[MQTT] %s -> %s\n", topic, message ? message : "(sin 'message')");
  if (!message) return;

  if (strcasecmp(message, "OFF") == 0) {
    Serial.printf("[MQTT] Apagado remoto (%s, score %.4f)\n",
                  doc["event_type"] | "manual", doc["score"] | 0.0f);
    controlarVentilador(false);
  } else if (strcasecmp(message, "ON") == 0) {
    controlarVentilador(true);
  }
}

// =====================================
// CONEXION (no bloqueante: el control local sigue aunque AWS falle)
// =====================================

void iniciarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[WiFi] Conectando a \"%s\"...\n", WIFI_SSID);
}

void configurarAWS() {
  net.setCACert(AWS_CERT_CA);
  net.setCertificate(AWS_CERT_CRT);
  net.setPrivateKey(AWS_CERT_PRIVATE);
  client.setServer(AWS_IOT_ENDPOINT, 8883);
  client.setBufferSize(1024);
  client.setCallback(messageHandler);
}

void mantenerConexion() {
  unsigned long ahora = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (ahora - ultimoIntentoWifi >= 10000) {
      ultimoIntentoWifi = ahora;
      Serial.println("[WiFi] Sin conexion, reintentando...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
    return;
  }

  if (!client.connected() && ahora - ultimoIntentoMqtt >= RECONNECT_INTERVAL_MS) {
    ultimoIntentoMqtt = ahora;
    Serial.printf("[MQTT] Conectando a AWS IoT como %s...\n", THINGNAME);
    if (client.connect(THINGNAME)) {
      Serial.println("[MQTT] AWS IoT OK");
      if (client.subscribe(AWS_IOT_SUBSCRIBE_TOPIC)) {
        Serial.printf("[MQTT] Suscrito a %s\n", AWS_IOT_SUBSCRIBE_TOPIC);
      } else {
        Serial.println("[MQTT] FALLO al suscribirse (revisar iot:Subscribe en la policy)");
      }
    } else {
      // 5 = no autorizado (policy / certificado), -2 = TLS/red
      Serial.printf("[MQTT] Fallo, estado %d\n", client.state());
    }
  }

  client.loop();
}

// =====================================
// PUBLICACION DE LECTURAS
// =====================================

void publicarLectura(float temperatura, sensors_event_t& acc, float corrienteMv) {
  if (!client.connected()) return;

  StaticJsonDocument<384> doc;
  doc["device"] = THINGNAME;
  doc["temperatura_C"] = temperatura;
  doc["aceleracion_x"] = acc.acceleration.x;
  doc["aceleracion_y"] = acc.acceleration.y;
  doc["aceleracion_z"] = acc.acceleration.z;
  doc["corriente_mV"] = corrienteMv;
  doc["estado_ventilador"] = ventiladorEncendido ? "ENCENDIDO" : "APAGADO";
  doc["tiempo_s"] = millis() / 1000.0;

  char buffer[384];
  size_t n = serializeJson(doc, buffer);
  if (client.publish(AWS_IOT_PUBLISH_TOPIC, (const uint8_t*)buffer, n, false)) {
    Serial.printf("[AWS] Publicado: %s\n", buffer);
  } else {
    Serial.println("[AWS] FALLO al publicar (revisar iot:Publish en la policy)");
  }
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

  iniciarWiFi();
  configurarAWS();

  // Encender automáticamente
  controlarVentilador(true);

  Serial.println("SISTEMA INICIADO");
}

// =====================================
// LOOP
// =====================================

void loop() {
  mantenerConexion();

  // CONTROL SERIAL (funciona aunque no haya AWS)
  while (Serial.available()) {
    char comando = Serial.read();
    if (comando == 'L' || comando == 'l') controlarVentilador(true);
    if (comando == 'H' || comando == 'h') controlarVentilador(false);
    if (comando == 'S' || comando == 's') {
      Serial.print("ESTADO_RELE: ");
      Serial.println(ventiladorEncendido ? "ENCENDIDO" : "APAGADO");
    }
  }

  unsigned long tiempoMs = millis();

  // TEMPERATURA
  sonda.requestTemperatures();
  float temperatura = sonda.getTempCByIndex(0);
  bool tempOk = temperatura != DEVICE_DISCONNECTED_C;

  if (tempOk) Serial.printf("DS18B20: %.2f C", temperatura);
  else        Serial.print("DS18B20: sin lectura");

  // ACELEROMETRO
  sensors_event_t aceleracion, giro, temperaturaInterna;
  if (mpuDetectado) {
    mpu.getEvent(&aceleracion, &giro, &temperaturaInterna);
    Serial.printf(" | MPU6050 X/Y/Z: %.2f / %.2f / %.2f m/s^2",
                  aceleracion.acceleration.x,
                  aceleracion.acceleration.y,
                  aceleracion.acceleration.z);
  } else {
    Serial.print(" | MPU6050: sin lectura");
  }

  // CORRIENTE
  float actualMv = leerCorrienteMv();
  Serial.printf(" | ACS712: %.0f mV (cambio: %+.0f mV)", actualMv, actualMv - ceroMv);

  // TIEMPO
  Serial.printf(" | Tiempo: %.2f s\n", tiempoMs / 1000.0);

  // A AWS solo lecturas completas: el modelo necesita las 5 features
  if (tempOk && mpuDetectado && tiempoMs - ultimoPublish >= PUBLISH_INTERVAL_MS) {
    ultimoPublish = tiempoMs;
    publicarLectura(temperatura, aceleracion, actualMv);
  }

  delay(300);
}
