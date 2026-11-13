#include "secrets.h"
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <WiFi.h>

#define AWS_IOT_PUBLISH_TOPIC   "esp32/pub"
#define AWS_IOT_SUBSCRIBE_TOPIC "esp32/sub"

WiFiClientSecure net;
PubSubClient client(net);

// ---------- Traducción de códigos de error ----------
const char* wifiStatusText(int s) {
  switch (s) {
    case WL_IDLE_STATUS:     return "IDLE (intentando)";
    case WL_NO_SSID_AVAIL:   return "NO ENCUENTRA LA RED (nombre mal o red 5 GHz)";
    case WL_CONNECTED:       return "CONECTADO";
    case WL_CONNECT_FAILED:  return "FALLO (probablemente contrasena incorrecta)";
    case WL_CONNECTION_LOST: return "CONEXION PERDIDA";
    case WL_DISCONNECTED:    return "DESCONECTADO (intentando / contrasena mal)";
    default:                 return "DESCONOCIDO";
  }
}

const char* mqttStateText(int s) {
  switch (s) {
    case -4: return "TIMEOUT (el servidor no respondio)";
    case -3: return "CONEXION PERDIDA";
    case -2: return "FALLO TLS/RED (endpoint mal, certificados mal o puerto bloqueado)";
    case -1: return "DESCONECTADO";
    case  0: return "CONECTADO";
    case  1: return "PROTOCOLO MQTT INCORRECTO";
    case  2: return "CLIENT ID RECHAZADO";
    case  3: return "SERVIDOR NO DISPONIBLE";
    case  4: return "CREDENCIALES INCORRECTAS";
    case  5: return "NO AUTORIZADO (revisar policy / certificado activo / Thing adjunto)";
    default: return "DESCONOCIDO";
  }
}

// ---------- Escaneo de redes ----------
void scanNetworks() {
  Serial.println("\n=== Escaneando redes WiFi (solo 2.4 GHz son visibles) ===");
  int n = WiFi.scanNetworks();
  if (n == 0) {
    Serial.println("No se encontro ninguna red.");
  }
  bool encontrada = false;
  for (int i = 0; i < n; i++) {
    Serial.printf("  %2d) %-32s  senal: %4d dBm  canal: %2d\n",
                  i + 1, WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i));
    if (WiFi.SSID(i) == String(WIFI_SSID)) encontrada = true;
  }
  if (encontrada) {
    Serial.printf(">>> Tu red \"%s\" SI aparece.\n", WIFI_SSID);
  } else {
    Serial.printf(">>> Tu red \"%s\" NO aparece: nombre mal escrito o es de 5 GHz.\n", WIFI_SSID);
  }
  WiFi.scanDelete();
}

// ---------- Callback ----------
void messageHandler(char* topic, byte* payload, unsigned int length) {
  Serial.print("Mensaje en ");
  Serial.println(topic);
  StaticJsonDocument<200> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("JSON invalido: ");
    Serial.println(err.c_str());
    return;
  }
  const char* message = doc["message"];
  Serial.println(message ? message : "(sin campo 'message')");
}

// ---------- WiFi ----------
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  scanNetworks();

  Serial.printf("\nConectando a WiFi \"%s\"\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int intentos = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    intentos++;
    int s = WiFi.status();
    Serial.printf("  intento %d -> estado %d: %s\n", intentos, s, wifiStatusText(s));
    if (intentos % 20 == 0) {   // cada 10 s reintenta desde cero
      Serial.println("  Reintentando WiFi.begin()...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  }
  Serial.println("WiFi OK");
  Serial.print("  IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("  Senal: ");
  Serial.print(WiFi.RSSI());
  Serial.println(" dBm");
}

// ---------- AWS IoT ----------
void connectAWS() {
  Serial.println("\n=== Configurando AWS IoT ===");
  Serial.print("  Endpoint: ");
  Serial.println(AWS_IOT_ENDPOINT);
  Serial.print("  Thing name / client ID: ");
  Serial.println(THINGNAME);

  // Revisión rápida de los certificados en secrets.h
  Serial.printf("  CA cert:      %s\n", strstr(AWS_CERT_CA, "BEGIN CERTIFICATE") ? "parece OK" : "MAL FORMADO");
  Serial.printf("  Device cert:  %s\n", strstr(AWS_CERT_CRT, "BEGIN CERTIFICATE") ? "parece OK" : "MAL FORMADO");
  Serial.printf("  Private key:  %s\n", strstr(AWS_CERT_PRIVATE, "PRIVATE KEY") ? "parece OK" : "MAL FORMADO");

  net.setCACert(AWS_CERT_CA);
  net.setCertificate(AWS_CERT_CRT);
  net.setPrivateKey(AWS_CERT_PRIVATE);

  client.setServer(AWS_IOT_ENDPOINT, 8883);
  client.setBufferSize(1024);
  client.setCallback(messageHandler);

  // Prueba de conexión TCP/TLS directa para separar problemas de red vs MQTT
  Serial.println("  Probando conexion TLS al endpoint...");
  if (net.connect(AWS_IOT_ENDPOINT, 8883)) {
    Serial.println("  TLS OK (endpoint y certificados aceptados)");
    net.stop();
  } else {
    char errBuf[128];
    int errCode = net.lastError(errBuf, sizeof(errBuf));
    Serial.printf("  TLS FALLO (codigo %d): %s\n", errCode, errBuf);
  }

  Serial.println("\nConectando a AWS IoT (MQTT)");
  int intentos = 0;
  while (!client.connect(THINGNAME)) {
    intentos++;
    int s = client.state();
    Serial.printf("  intento %d -> estado %d: %s\n", intentos, s, mqttStateText(s));
    delay(2000);
  }
  Serial.println("AWS IoT OK");

  if (client.subscribe(AWS_IOT_SUBSCRIBE_TOPIC)) {
    Serial.printf("  Suscrito a %s\n", AWS_IOT_SUBSCRIBE_TOPIC);
  } else {
    Serial.println("  FALLO al suscribirse (revisar iot:Subscribe en la policy)");
  }
}

// ---------- Publicación ----------
void publishMessage() {
  StaticJsonDocument<200> doc;
  doc["device"] = THINGNAME;
  doc["temperature"] = random(20, 30);
  doc["timestamp"] = millis();
  char buffer[512];
  serializeJson(doc, buffer);

  if (client.publish(AWS_IOT_PUBLISH_TOPIC, buffer)) {
    Serial.print("Publicado: ");
    Serial.println(buffer);
  } else {
    Serial.println("FALLO al publicar (revisar iot:Publish en la policy)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(2000);  // tiempo para que abras el Serial Monitor
  Serial.println("\n\n===== ESP32 + AWS IoT: DIAGNOSTICO =====");
  connectWiFi();
  connectAWS();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi perdido, reconectando...");
    connectWiFi();
  }
  if (!client.connected()) {
    Serial.printf("MQTT desconectado (estado %d: %s), reconectando...\n",
                  client.state(), mqttStateText(client.state()));
    connectAWS();
  }
  client.loop();
  publishMessage();
  delay(5000);
}