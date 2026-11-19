#include "secrets.h"
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <algorithm>

// =====================================
// CONFIGURACION
// =====================================

#define AWS_IOT_PUBLISH_TOPIC   "esp32/pub"   // lecturas -> IoT Rule -> Lambda
#define AWS_IOT_SUBSCRIBE_TOPIC "esp32/sub"   // comandos {"message":"OFF"|"ON"}

const unsigned long PUBLISH_INTERVAL_MS = 1000;  // 1 lectura/s a AWS
const unsigned long RECONNECT_INTERVAL_MS = 5000;

// NUEVO: ventana de vibracion
const int VIB_MUESTRAS = 200;          // muestras por ventana
const int VIB_PAUSA_US = 500;          // pausa entre muestras (~1.4 ms/muestra -> ~0.3 s por ventana)

// NUEVO: lectura directa del acelerometro por I2C (sin getEvent)
// getEvent() vuelve a leer el registro de rango en CADA muestra para escalar;
// si esa lectura llega corrupta, la muestra sale x2 / x4. Leyendo directo
// usamos una escala fija y una sola transaccion de 6 bytes.
const uint8_t MPU_ADDR = 0x68;
const uint8_t MPU_REG_ACCEL = 0x3B;    // ACCEL_XOUT_H
const uint8_t MPU_REG_ACCEL_CONFIG = 0x1C;
const uint8_t MPU_REG_CONFIG = 0x1A;
const uint8_t MPU_REG_PWR_MGMT_1 = 0x6B;
const uint8_t MPU_REG_WHO_AM_I = 0x75;
const float G_MS2 = 9.80665;

// NUEVO: configuracion que asegurarConfigMPU() verifica en CADA ciclo.
// Con el bus ruidoso, una escritura puede perderse o corromperse (o el MPU
// reiniciarse si el motor hace caer la alimentacion) y quedar en otro rango:
// todas las lecturas saldrian con la escala equivocada.
const uint8_t ACCEL_CONFIG_8G = 0x10;          // AFS_SEL=2 -> +-8g
const uint8_t DLPF_94HZ = 0x02;                // CONFIG bits 2:0
const uint8_t PWR_BIT_SLEEP = 0x40;
const uint8_t PWR_DESPIERTO_PLL = 0x01;        // despierto, reloj PLL del giroscopio X
const float ACC_LSB_POR_G = 4096.0;            // +-8g (garantizado por asegurarConfigMPU)
int mpuReconfiguraciones = 0;                  // veces que se encontro la config perdida

// NUEVO: filtro de muestras imposibles
// Con el DLPF de 94 Hz un pico real dura varias muestras. Una muestra suelta
// que se aleja mucho de la mediana mientras sus vecinas no, es un error de bus.
const float VIB_ATIPICO_MADS = 6.0;    // distancia a la mediana, en "sigmas robustas"
const float VIB_ATIPICO_MIN = 4.9;     // y ademas al menos 0.5 g (con 1 g se escapaba un pico
                                       // falso de ~0.95 g por ventana: pico fijo en ~5.2)
const float VIB_MAG_MIN = 2.9;         // NUEVO: magnitud < 0.3 g es imposible con el ventilador
                                       // apoyado (seria caida libre): es una lectura corrupta.
                                       // Llegan hasta 2-3 seguidas y el filtro de sueltas no las
                                       // quita (pico falso fijo en ~5.2). Un golpe no baja de ~0.4 g.

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

// NUEVO: temperatura no bloqueante
unsigned long ultimoPedidoTemp = 0;
float temperatura = DEVICE_DISCONNECTED_C;

// NUEVO: resultado de la ventana de vibracion.
// Tiene que ir ANTES de cualquier funcion: el Arduino IDE genera los prototipos
// de las funciones justo antes de la primera y, si el struct esta despues,
// da "'Vibracion' does not name a type".
struct Vibracion {
  float instX, instY, instZ;     // lectura instantanea (lo que ya enviabas)
  float mediaX, mediaY, mediaZ;  // promedio de la ventana (orientacion estable)
  float std;                     // desviacion estandar de la magnitud (intensidad de vibracion)
  float pico;                    // (max - min) / 2 de la magnitud
  int muestras;                  // muestras validas usadas en las estadisticas
  int descartadas;               // NUEVO: muestras atipicas sueltas (picos falsos) descartadas
  int erroresI2c;                // NUEVO: lecturas I2C fallidas
};

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
// NUEVO: ACELEROMETRO -> ventana de muestras + estadisticas
// (el struct Vibracion esta definido arriba, antes de cualquier funcion)
// =====================================

// Lee X/Y/Z en m/s^2 con una sola transaccion I2C. false si el bus fallo.
bool leerAcelDirecto(float& x, float& y, float& z) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(MPU_REG_ACCEL);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)6) != 6) return false;

  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = Wire.read();

  // Bus pegado: todo 0x00 o todo 0xFF no es una lectura real
  bool todo00 = true, todoFF = true;
  for (int i = 0; i < 6; i++) {
    if (b[i] != 0x00) todo00 = false;
    if (b[i] != 0xFF) todoFF = false;
  }
  if (todo00 || todoFF) return false;

  int16_t rx = (int16_t)((b[0] << 8) | b[1]);
  int16_t ry = (int16_t)((b[2] << 8) | b[3]);
  int16_t rz = (int16_t)((b[4] << 8) | b[5]);
  x = rx / ACC_LSB_POR_G * G_MS2;
  y = ry / ACC_LSB_POR_G * G_MS2;
  z = rz / ACC_LSB_POR_G * G_MS2;
  return true;
}

// NUEVO: lee un registro de 1 byte del MPU. false si el bus fallo.
bool leerRegistroMPU(uint8_t reg, uint8_t& valor) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)1) != 1) return false;
  valor = Wire.read();
  return true;
}

// NUEVO: lectura confiable de un registro (2 lecturas iguales seguidas)
bool leerRegistroSeguro(uint8_t reg, uint8_t& valor) {
  for (int i = 0; i < 5; i++) {
    uint8_t a, b;
    if (leerRegistroMPU(reg, a) && leerRegistroMPU(reg, b) && a == b) {
      valor = a;
      return true;
    }
  }
  return false;
}

// NUEVO: escribe un registro y lo relee hasta confirmar que quedo bien
bool escribirVerificado(uint8_t reg, uint8_t valor) {
  for (int i = 0; i < 5; i++) {
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(reg);
    Wire.write(valor);
    Wire.endTransmission();
    uint8_t leido;
    if (leerRegistroSeguro(reg, leido) && leido == valor) return true;
    delay(1);
  }
  return false;
}

// NUEVO: verifica que el MPU sigue despierto, en +-8g y con DLPF 94 Hz.
// Si algo se perdio lo vuelve a escribir. true = tuvo que reconfigurar.
// Si el bus no deja leer los registros, no hace nada (se intenta el proximo ciclo).
bool asegurarConfigMPU() {
  uint8_t pwr, acc, cfg;
  if (!leerRegistroSeguro(MPU_REG_PWR_MGMT_1, pwr) ||
      !leerRegistroSeguro(MPU_REG_ACCEL_CONFIG, acc) ||
      !leerRegistroSeguro(MPU_REG_CONFIG, cfg)) {
    return false;
  }
  if (!(pwr & PWR_BIT_SLEEP) && acc == ACCEL_CONFIG_8G && (cfg & 0x07) == DLPF_94HZ) {
    return false;
  }

  Serial.printf("[MPU6050] Config perdida (PWR=0x%02X ACCEL=0x%02X CONFIG=0x%02X) -> reconfigurando\n",
                pwr, acc, cfg);
  if (pwr & PWR_BIT_SLEEP) {
    escribirVerificado(MPU_REG_PWR_MGMT_1, PWR_DESPIERTO_PLL);
    delay(10);
  }
  escribirVerificado(MPU_REG_ACCEL_CONFIG, ACCEL_CONFIG_8G);
  escribirVerificado(MPU_REG_CONFIG, (cfg & ~0x07) | DLPF_94HZ);
  mpuReconfiguraciones++;
  return true;
}

// NUEVO: al arrancar (motor apagado) informa el rango real del sensor y compara
// la gravedad medida directo vs con Adafruit. En reposo deberian dar ~9.8 m/s^2.
void diagnosticarMPU() {
  uint8_t who = 0, cfg = 0;
  leerRegistroMPU(MPU_REG_WHO_AM_I, who);
  bool ok = leerRegistroSeguro(MPU_REG_ACCEL_CONFIG, cfg);
  int afs = (cfg >> 3) & 0x03;
  Serial.printf("[MPU6050] WHO_AM_I=0x%02X ACCEL_CONFIG=0x%02X -> rango +-%dg%s\n",
                who, cfg, 2 << afs,
                !ok ? "  (LECTURA DUDOSA)" : (cfg == ACCEL_CONFIG_8G ? "" : "  (NO ES +-8g!)"));

  float sumD = 0, sumA = 0;
  int nD = 0, errores = 0;
  for (int i = 0; i < 50; i++) {
    float x, y, z;
    if (leerAcelDirecto(x, y, z)) { sumD += sqrt(x * x + y * y + z * z); nD++; }
    else errores++;
    sensors_event_t a, g, t;
    mpu.getEvent(&a, &g, &t);
    sumA += sqrt(a.acceleration.x * a.acceleration.x +
                 a.acceleration.y * a.acceleration.y +
                 a.acceleration.z * a.acceleration.z);
    delay(2);
  }
  Serial.printf("[MPU6050] |g| en reposo: directo=%.2f  adafruit=%.2f m/s^2 (esperado ~9.8) | errores I2C: %d/50\n",
                nD ? sumD / nD : 0.0, sumA / 50, errores);
}

float medianaDe(float* datos, int n) {   // ordena 'datos' (pasar una copia)
  std::sort(datos, datos + n);
  return (n % 2) ? datos[n / 2] : (datos[n / 2 - 1] + datos[n / 2]) / 2.0;
}

Vibracion leerVibracion() {
  // static: ~3 KB fuera del stack del loop
  static float ax[VIB_MUESTRAS], ay[VIB_MUESTRAS], az[VIB_MUESTRAS], mag[VIB_MUESTRAS];
  static float tmp[VIB_MUESTRAS];
  static bool atipico[VIB_MUESTRAS];

  Vibracion v = {};

  // 1) Captura: solo se guardan las lecturas I2C correctas
  int n = 0;
  for (int i = 0; i < VIB_MUESTRAS; i++) {
    float x, y, z;
    if (leerAcelDirecto(x, y, z)) {
      float m = sqrt(x * x + y * y + z * z);
      if (m < VIB_MAG_MIN) {          // NUEVO: lectura corrupta (casi cero)
        v.descartadas++;
      } else {
        ax[n] = x; ay[n] = y; az[n] = z;
        mag[n] = m;
        n++;
      }
    } else {
      v.erroresI2c++;
    }
    delayMicroseconds(VIB_PAUSA_US);
  }
  if (n == 0) return v;

  // La primera muestra valida es la "instantanea" (igual que antes)
  v.instX = ax[0]; v.instY = ay[0]; v.instZ = az[0];

  // 2) Mediana y MAD de la magnitud (robustas a picos falsos)
  for (int i = 0; i < n; i++) tmp[i] = mag[i];
  float med = medianaDe(tmp, n);
  for (int i = 0; i < n; i++) tmp[i] = fabs(mag[i] - med);
  float sigmaRobusta = 1.4826 * medianaDe(tmp, n);
  float umbral = VIB_ATIPICO_MADS * sigmaRobusta;
  if (umbral < VIB_ATIPICO_MIN) umbral = VIB_ATIPICO_MIN;

  for (int i = 0; i < n; i++) atipico[i] = fabs(mag[i] - med) > umbral;

  // 3) Estadisticas descartando SOLO los atipicos sueltos.
  //    Si sus vecinas tambien son atipicas es un evento real (golpe) y se queda.
  float sX = 0, sY = 0, sZ = 0, sMag = 0, sMag2 = 0;
  float magMin = 1e9, magMax = -1e9;
  int usadas = 0;
  for (int i = 0; i < n; i++) {
    bool vecinaAtipica = (i > 0 && atipico[i - 1]) || (i < n - 1 && atipico[i + 1]);
    if (atipico[i] && !vecinaAtipica) {
      v.descartadas++;
      continue;
    }
    sX += ax[i]; sY += ay[i]; sZ += az[i];
    sMag += mag[i]; sMag2 += mag[i] * mag[i];
    if (mag[i] < magMin) magMin = mag[i];
    if (mag[i] > magMax) magMax = mag[i];
    usadas++;
  }
  if (usadas == 0) return v;

  v.mediaX = sX / usadas;
  v.mediaY = sY / usadas;
  v.mediaZ = sZ / usadas;
  float mediaMag = sMag / usadas;
  float var = sMag2 / usadas - mediaMag * mediaMag;
  v.std = sqrt(var > 0 ? var : 0);
  v.pico = (magMax - magMin) / 2.0;
  v.muestras = usadas;
  return v;
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

void publicarLectura(float temperatura, const Vibracion& v, float corrienteMv) {
  if (!client.connected()) return;

  StaticJsonDocument<640> doc;
  doc["device"] = THINGNAME;
  doc["temperatura_C"] = temperatura;

  // Campos originales (lectura instantanea, igual que antes)
  doc["aceleracion_x"] = v.instX;
  doc["aceleracion_y"] = v.instY;
  doc["aceleracion_z"] = v.instZ;

  // NUEVO: estadisticas de la ventana
  doc["acel_media_x"] = v.mediaX;
  doc["acel_media_y"] = v.mediaY;
  doc["acel_media_z"] = v.mediaZ;
  doc["vibracion_std"] = v.std;
  doc["vibracion_pico"] = v.pico;
  doc["muestras_vib"] = v.muestras;
  doc["muestras_descartadas"] = v.descartadas;   // NUEVO
  doc["errores_i2c"] = v.erroresI2c;             // NUEVO
  doc["mpu_reconfig"] = mpuReconfiguraciones;    // NUEVO: acumulado desde el arranque

  doc["corriente_mV"] = corrienteMv;
  doc["estado_ventilador"] = ventiladorEncendido ? "ENCENDIDO" : "APAGADO";
  doc["tiempo_s"] = millis() / 1000.0;

  char buffer[640];
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
  mpuDetectado = mpu.begin(MPU_ADDR);
  Wire.setClock(100000);                          // NUEVO: 100 kHz, mas tolerante al ruido del motor
  if (mpuDetectado) {
    // NUEVO: +-8g y DLPF 94 Hz, escritos y RELEIDOS hasta confirmar
    escribirVerificado(MPU_REG_ACCEL_CONFIG, ACCEL_CONFIG_8G);
    asegurarConfigMPU();
    mpuReconfiguraciones = 0;                      // lo de setup no cuenta como perdida
    delay(50);
    diagnosticarMPU();                             // NUEVO: informa rango y |g| (motor aun apagado)
  } else {
    Serial.println("[MPU6050] No detectado");
  }

  sonda.begin();
  sonda.setWaitForConversion(false);              // NUEVO: DS18B20 no bloqueante
  sonda.requestTemperatures();

  // Referencia sin corriente del ventilador
  delay(800);                                     // tambien da tiempo a la 1ra conversion del DS18B20
  ceroMv = leerCorrienteMv();
  temperatura = sonda.getTempCByIndex(0);
  sonda.requestTemperatures();
  ultimoPedidoTemp = millis();

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

  // TEMPERATURA (no bloqueante: se actualiza cada ~750 ms)
  if (tiempoMs - ultimoPedidoTemp >= 750) {
    temperatura = sonda.getTempCByIndex(0);
    sonda.requestTemperatures();
    ultimoPedidoTemp = tiempoMs;
  }
  bool tempOk = temperatura != DEVICE_DISCONNECTED_C;

  // NUEVO: antes de medir, confirmar que el MPU sigue en +-8g / 94 Hz
  // (va antes del print de la linea para no partirla con el aviso)
  if (mpuDetectado) asegurarConfigMPU();

  if (tempOk) Serial.printf("DS18B20: %.2f C", temperatura);
  else        Serial.print("DS18B20: sin lectura");

  // ACELEROMETRO (ventana de ~0.3 s)
  Vibracion vib = {};
  if (mpuDetectado) {
    vib = leerVibracion();
    Serial.printf(" | MPU inst X/Y/Z: %.2f / %.2f / %.2f", vib.instX, vib.instY, vib.instZ);
    Serial.printf(" | Media X/Y/Z: %.2f / %.2f / %.2f", vib.mediaX, vib.mediaY, vib.mediaZ);
    Serial.printf(" | Vib std: %.3f pico: %.3f", vib.std, vib.pico);
    Serial.printf(" | Desc: %d I2C: %d Reconf: %d", vib.descartadas, vib.erroresI2c, mpuReconfiguraciones);
  } else {
    Serial.print(" | MPU6050: sin lectura");
  }

  // CORRIENTE
  float actualMv = leerCorrienteMv();
  Serial.printf(" | ACS712: %.0f mV (cambio: %+.0f mV)", actualMv, actualMv - ceroMv);

  // TIEMPO
  Serial.printf(" | Tiempo: %.2f s\n", tiempoMs / 1000.0);

  // A AWS solo lecturas completas
  if (tempOk && mpuDetectado && tiempoMs - ultimoPublish >= PUBLISH_INTERVAL_MS) {
    ultimoPublish = tiempoMs;
    publicarLectura(temperatura, vib, actualMv);
  }

  delay(50);
}
