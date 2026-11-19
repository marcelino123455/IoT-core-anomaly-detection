"""VentiSet: intercepta las lecturas del ESP32 por el puerto serial y las guarda en CSV.

Lee lo mismo que muestra el Monitor Serial del Arduino IDE (cierralo antes, el
puerto solo admite un programa a la vez) y guarda cada lectura con una ETIQUETA
que tu cambias en vivo, para luego comparar "apagado" vs "normal" vs "movimiento".

Uso:
    pip install pyserial
    python interceptar_lecturas.py --port COM9
    python interceptar_lecturas.py                 # autodetecta el puerto

Teclas (una sola tecla, SIN Enter):
    a  -> etiqueta "apagado"
    n  -> etiqueta "normal"      (encendido y quieto)
    m  -> etiqueta "movimiento"  (lo mueves / inclinas)
    g  -> etiqueta "golpe"       (golpecitos suaves)
    e  -> etiqueta libre: escribes el nombre y Enter
    h  -> apagar ventilador   (envia 'H' al ESP32)
    l  -> encender ventilador (envia 'L' al ESP32)
    s  -> pedir estado del rele
    ?  -> resumen por etiqueta hasta ahora
    q  -> terminar (o Ctrl+C)

Salida:
    capturas/captura_<fecha>.csv  lecturas (una fila por lectura)
    capturas/captura_<fecha>.log  todo lo que llego por el serial, sin procesar
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import queue
import re
import statistics
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("Falta pyserial:  pip install pyserial")

# Mismo orden que el CSV con el que se entreno el modelo (main.py -> EXPECTED),
# y despues las columnas nuevas.
COLUMNAS = [
    "fecha_hora", "tiempo_s",
    "temperatura_C", "aceleracion_x", "aceleracion_y", "aceleracion_z", "corriente_mV",
    "cambio_mV", "estado_ventilador",
    "acel_media_x", "acel_media_y", "acel_media_z",
    "vibracion_std", "vibracion_pico", "muestras_vib",
    "muestras_descartadas", "errores_i2c", "mpu_reconfig",
    "etiqueta", "fuente",
]

TECLAS_ETIQUETA = {"a": "apagado", "n": "normal", "m": "movimiento", "g": "golpe"}
TECLAS_ESP32 = {"h": "H", "l": "L", "s": "S"}
AYUDA = ("Teclas: [a]pagado [n]ormal [m]ovimiento [g]olpe [e]tiqueta libre | "
         "[h] apagar [l] encender [s] estado | [?] resumen | [q] salir")

NUM = r"([-+]?\d+(?:\.\d+)?)"
RE_TEMP = re.compile(rf"DS18B20:\s*{NUM}\s*C")
RE_INST = re.compile(rf"(?:MPU inst|MPU6050) X/Y/Z:\s*{NUM}\s*/\s*{NUM}\s*/\s*{NUM}")
RE_MEDIA = re.compile(rf"Media X/Y/Z:\s*{NUM}\s*/\s*{NUM}\s*/\s*{NUM}")
RE_VIB = re.compile(rf"Vib std:\s*{NUM}\s*pico:\s*{NUM}")
RE_DESC = re.compile(r"Desc:\s*(\d+)\s*I2C:\s*(\d+)(?:\s*Reconf:\s*(\d+))?")
RE_CORR = re.compile(rf"ACS712:\s*{NUM}\s*mV\s*\(cambio:\s*{NUM}\s*mV\)")
RE_TIEMPO = re.compile(rf"Tiempo:\s*{NUM}\s*s")
RE_RELE = re.compile(r"ESTADO_RELE:\s*(\w+)")
PREFIJO_AWS = "[AWS] Publicado:"


# =====================================================================
# Teclado: una tecla a la vez, sin Enter (Windows: msvcrt, Linux: termios)
# =====================================================================

class Teclado:
    def __init__(self, cola: queue.Queue):
        self.cola = cola
        self._restaurar = None

    def __enter__(self):
        if os.name == "nt":
            import msvcrt

            def leer():
                while True:
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):   # flechas / teclas especiales
                        msvcrt.getwch()
                        continue
                    self.cola.put(ch)
        elif sys.stdin.isatty():
            import termios
            import tty
            fd = sys.stdin.fileno()
            viejo = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            self._restaurar = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, viejo)

            def leer():
                while True:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                    self.cola.put(ch)
        else:
            def leer():
                for linea in sys.stdin:
                    for ch in linea:
                        self.cola.put(ch)

        threading.Thread(target=leer, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._restaurar:
            self._restaurar()


# =====================================================================
# Utilidades
# =====================================================================

def elegir_puerto(port: str | None) -> str:
    if port:
        return port
    puertos = list(list_ports.comports())
    candidatos = [p for p in puertos
                  if any(k in f"{p.description} {p.hwid}".upper()
                         for k in ("CP210", "CH340", "CH910", "USB", "UART", "SLAB"))]
    elegidos = candidatos or puertos
    if len(elegidos) == 1:
        print(f"Puerto detectado: {elegidos[0].device} ({elegidos[0].description})")
        return elegidos[0].device
    lista = "\n".join(f"  {p.device}  {p.description}" for p in puertos) or "  (ninguno)"
    sys.exit(f"No pude elegir el puerto automaticamente. Usa --port. Puertos:\n{lista}")


def parsear_linea_serial(linea: str) -> dict | None:
    """Linea 'DS18B20: ... | Tiempo: ...' -> dict. None si no es una linea de lectura."""
    if "DS18B20" not in linea or "Tiempo:" not in linea:
        return None
    r: dict = {}
    if m := RE_TEMP.search(linea):
        r["temperatura_C"] = float(m[1])
    if m := RE_INST.search(linea):
        r["aceleracion_x"], r["aceleracion_y"], r["aceleracion_z"] = map(float, m.groups())
    if m := RE_MEDIA.search(linea):
        r["acel_media_x"], r["acel_media_y"], r["acel_media_z"] = map(float, m.groups())
    if m := RE_VIB.search(linea):
        r["vibracion_std"], r["vibracion_pico"] = float(m[1]), float(m[2])
    if m := RE_DESC.search(linea):
        r["muestras_descartadas"], r["errores_i2c"] = int(m[1]), int(m[2])
        if m[3] is not None:
            r["mpu_reconfig"] = int(m[3])
    if m := RE_CORR.search(linea):
        r["corriente_mV"], r["cambio_mV"] = float(m[1]), float(m[2])
    if m := RE_TIEMPO.search(linea):
        r["tiempo_s"] = float(m[1])
    return r


class LectorLineas:
    """Junta bytes del serial hasta '\\n'. pyserial.readline() corta la linea si
    el ESP32 hace una pausa a la mitad (la ventana del MPU tarda ~0.3 s)."""

    def __init__(self, ser: serial.Serial):
        self.ser = ser
        self.buf = bytearray()

    def lineas(self) -> list[str]:
        datos = self.ser.read(self.ser.in_waiting or 1)   # espera hasta 'timeout'
        if not datos:
            return []
        self.buf += datos
        *completas, self.buf = self.buf.split(b"\n")
        self.buf = bytearray(self.buf)
        return [c.decode("utf-8", errors="replace").strip() for c in completas]


def construir_protocolo(duracion: int, ciclos: int, tipo: str = "golpes") -> list[tuple[str, int, str]]:
    """Pasos (etiqueta, segundos, instruccion) del modo --protocolo."""
    pasos = [("inicio", 15, "ESTABILIZANDO: no toques nada")]
    if tipo == "carga":
        return pasos + [("sin_carga", 60, "SIN cuaderno, no toques nada"),
                        ("con_carga", 120, "PON EL CUADERNO encima y no lo toques"),
                        ("sin_carga2", 60, "QUITA EL CUADERNO y no toques nada")]
    for _ in range(ciclos):
        pasos += [("normal", duracion, "GIRANDO SIN TOCAR nada"),
                  ("golpe", duracion, "GOLPECITOS suaves en la base, cada 1-2 s, sin parar")]
    return pasos


def pitido(veces: int = 1, freq: int = 1000) -> None:
    try:
        import winsound
        for _ in range(veces):
            winsound.Beep(freq, 150)
    except Exception:
        print("\a" * veces, end="", flush=True)


def banner(texto: str) -> None:
    linea = "#" * (len(texto) + 8)
    print(f"\n{linea}\n#   {texto}   #\n{linea}\n", flush=True)


def fmt(x, nd=3) -> str:
    return "  -  " if x is None else f"{x:.{nd}f}"


def imprimir_resumen(filas_por_etiqueta: dict[str, list[dict]]) -> None:
    if not filas_por_etiqueta:
        print("\n(sin lecturas todavia)")
        return
    campos = ["vibracion_std", "vibracion_pico", "muestras_descartadas", "errores_i2c",
              "acel_media_x", "acel_media_y",
              "acel_media_z", "aceleracion_x", "corriente_mV", "temperatura_C"]
    print("\n================ RESUMEN POR ETIQUETA (media ± desv / min..max) ================")
    for etiqueta, filas in filas_por_etiqueta.items():
        print(f"\n[{etiqueta}]  {len(filas)} lecturas")
        for c in campos:
            vals = [f[c] for f in filas if isinstance(f.get(c), (int, float))]
            if not vals:
                continue
            desv = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            print(f"  {c:<16} {statistics.fmean(vals):9.3f} ± {desv:7.3f}   "
                  f"{min(vals):9.3f} .. {max(vals):9.3f}")
    print("================================================================================\n")


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="ej. COM9 (Windows) o /dev/ttyUSB0 (Linux). Sin esto, autodetecta.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", type=Path, help="ruta del CSV (default: capturas/captura_<fecha>.csv)")
    ap.add_argument("--etiqueta", default="normal", help="etiqueta inicial (default: normal)")
    ap.add_argument("--fuente", choices=["auto", "aws", "serial"], default="auto",
                    help="aws: solo lo que se publico a AWS (1/s). serial: todas las lineas "
                         "del monitor (~2/s). auto: aws, y si AWS no publica en 5 s, serial.")
    ap.add_argument("--protocolo", action="store_true",
                    help="modo guiado: inicio 15 s -> normal -> golpe (cambia etiquetas solo, "
                         "avisa 15 s antes de cada cambio y termina solo)")
    ap.add_argument("--tipo", choices=["golpes", "carga"], default="golpes",
                    help="golpes: normal/golpe. carga: 1 min sin cuaderno, 2 con, 1 sin")
    ap.add_argument("--duracion", type=int, default=60, help="segundos de normal y de golpe (default 60)")
    ap.add_argument("--ciclos", type=int, default=1, help="repeticiones de normal+golpe (default 1)")
    ap.add_argument("--sin-teclado", action="store_true",
                    help="no leer el teclado (para lanzarlo desde otro programa, sin consola)")
    args = ap.parse_args()

    puerto = elegir_puerto(args.port)
    salida = args.out or Path("capturas") / f"captura_{datetime.now():%Y%m%d_%H%M%S}.csv"
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida_log = salida.with_suffix(".log")

    try:
        ser = serial.Serial(puerto, args.baud, timeout=0.1)
    except serial.SerialException as e:
        sys.exit(f"No pude abrir {puerto}: {e}\n(¿El Monitor Serial del Arduino IDE sigue abierto?)")
    lector = LectorLineas(ser)

    cola: queue.Queue = queue.Queue()

    etiqueta = args.etiqueta
    estado_rele = ""
    ultimo_cambio_mv = None
    ultimo_aws = 0.0
    aviso_fallback = False
    escribiendo: str | None = None          # texto de la etiqueta libre mientras se escribe
    filas_por_etiqueta: dict[str, list[dict]] = defaultdict(list)
    total = 0

    protocolo = construir_protocolo(args.duracion, args.ciclos, args.tipo) if args.protocolo else None
    if protocolo:
        etiqueta = protocolo[0][0]          # la 1ra lectura ya cae en "inicio"
    paso_idx = -1
    t_paso = 0.0
    aviso15 = False
    ultimo_conteo = None

    print(f"Leyendo {puerto} @ {args.baud}  ->  {salida}")
    print(AYUDA)
    if protocolo:
        total_s = sum(p[1] for p in protocolo)
        print("PROTOCOLO: " + " -> ".join(f"{p[0]} {p[1]}s" for p in protocolo) + f"  (~{total_s} s)")
        print("Arranca solo cuando llegue la primera lectura del ESP32.")
    print(f"Etiqueta actual: [{etiqueta}]\n")

    with open(salida, "w", newline="", encoding="utf-8") as f_csv, \
         open(salida_log, "w", encoding="utf-8") as f_log, \
         (contextlib.nullcontext() if args.sin_teclado else Teclado(cola)):
        writer = csv.DictWriter(f_csv, fieldnames=COLUMNAS, extrasaction="ignore")
        writer.writeheader()

        def avisar(msg: str) -> None:
            if escribiendo is None:
                print(msg)

        def cambiar_etiqueta(nueva: str) -> None:
            nonlocal etiqueta
            etiqueta = nueva
            marca = f"{datetime.now():%H:%M:%S}"
            print(f"\n===== {marca}  etiqueta -> [{etiqueta}] =====\n")
            f_log.write(f"# {marca} ETIQUETA -> {etiqueta}\n")

        def guardar(fila: dict, fuente: str) -> None:
            nonlocal total
            fila.setdefault("cambio_mV", ultimo_cambio_mv)
            fila.setdefault("estado_ventilador", estado_rele or None)
            fila["fecha_hora"] = datetime.now().isoformat(sep=" ", timespec="milliseconds")
            fila["etiqueta"] = etiqueta
            fila["fuente"] = fuente
            writer.writerow(fila)
            f_csv.flush()
            filas_por_etiqueta[etiqueta].append(fila)
            total += 1
            avisar(f"[{etiqueta:>10}] #{total:<5} t={fmt(fila.get('tiempo_s'), 1):>7}  "
                   f"vib_std={fmt(fila.get('vibracion_std'))}  pico={fmt(fila.get('vibracion_pico'))}  "
                   f"media=({fmt(fila.get('acel_media_x'), 2)}, {fmt(fila.get('acel_media_y'), 2)}, "
                   f"{fmt(fila.get('acel_media_z'), 2)})  "
                   f"desc={fila.get('muestras_descartadas', '-')} i2c={fila.get('errores_i2c', '-')} "
                   f"reconf={fila.get('mpu_reconfig', '-')}  "
                   f"I={fmt(fila.get('corriente_mV'), 0)}mV  T={fmt(fila.get('temperatura_C'), 2)}  "
                   f"{fila.get('estado_ventilador') or ''}")

        try:
            while True:
                # --- protocolo guiado ---
                if protocolo and total > 0:
                    ahora = time.monotonic()
                    if paso_idx < 0 or ahora - t_paso >= protocolo[paso_idx][1]:
                        paso_idx += 1
                        if paso_idx >= len(protocolo):
                            banner("PROTOCOLO TERMINADO - ya puedes dejar de tocar")
                            pitido(3, 800)
                            raise KeyboardInterrupt
                        et, dur, instr = protocolo[paso_idx]
                        cambiar_etiqueta(et)
                        banner(f"AHORA: {instr}  ({dur} s)")
                        pitido(2, 1500) if et == "golpe" else pitido(1, 1000)
                        t_paso, aviso15, ultimo_conteo = ahora, False, None

                    restante = protocolo[paso_idx][1] - (ahora - t_paso)
                    sig = protocolo[paso_idx + 1] if paso_idx + 1 < len(protocolo) else None
                    if not aviso15 and restante <= 15:
                        aviso15 = True
                        banner(f"FALTAN 15 s -> luego: {sig[2]}" if sig else "FALTAN 15 s para terminar")
                        pitido(1, 700)
                    seg = int(restante) + 1
                    if 0 < seg <= 5 and seg != ultimo_conteo:
                        ultimo_conteo = seg
                        print(f"   >>> {seg} ...", flush=True)

                # --- teclado ---
                while not cola.empty():
                    ch = cola.get()
                    if ch == "\x03":                          # Ctrl+C en Windows
                        raise KeyboardInterrupt

                    if escribiendo is not None:               # modo etiqueta libre
                        if ch in ("\r", "\n"):
                            texto, escribiendo = escribiendo.strip(), None
                            print()
                            if texto:
                                cambiar_etiqueta(texto)
                            else:
                                print("(cancelado)")
                        elif ch == "\x1b":                    # Esc
                            escribiendo = None
                            print("\n(cancelado)")
                        elif ch in ("\b", "\x7f"):
                            if escribiendo:
                                escribiendo = escribiendo[:-1]
                                print("\b \b", end="", flush=True)
                        elif ch.isprintable():
                            escribiendo += ch
                            print(ch, end="", flush=True)
                        continue

                    k = ch.lower()
                    if k in TECLAS_ETIQUETA:
                        cambiar_etiqueta(TECLAS_ETIQUETA[k])
                    elif k in TECLAS_ESP32:
                        ser.write(TECLAS_ESP32[k].encode())
                        print(f">> enviado al ESP32: {TECLAS_ESP32[k]}")
                    elif k == "e":
                        escribiendo = ""
                        print("\nNueva etiqueta (Enter = ok, Esc = cancelar): ", end="", flush=True)
                    elif k == "?":
                        imprimir_resumen(filas_por_etiqueta)
                    elif k == "q":
                        raise KeyboardInterrupt
                    elif k in ("\r", "\n", " "):
                        print(f"Etiqueta actual: [{etiqueta}]  ({total} lecturas)")
                    else:
                        print(AYUDA)

                # --- serial ---
                for linea in lector.lineas():
                    if not linea:
                        continue
                    f_log.write(linea + "\n")

                    if m := RE_RELE.search(linea):
                        estado_rele = m[1]
                        avisar(f"   (ESP32) {linea}")
                        continue

                    if linea.startswith(PREFIJO_AWS):
                        ultimo_aws = time.monotonic()
                        if args.fuente == "serial":
                            continue
                        try:
                            dato = json.loads(linea[len(PREFIJO_AWS):].strip())
                        except json.JSONDecodeError:
                            avisar(f"   (JSON cortado, se ignora) {linea[:80]}")
                            continue
                        if dato.get("estado_ventilador"):
                            estado_rele = dato["estado_ventilador"]
                        guardar(dato, "aws")
                        continue

                    lectura = parsear_linea_serial(linea)
                    if lectura is not None:
                        if "cambio_mV" in lectura:
                            ultimo_cambio_mv = lectura["cambio_mV"]
                        sin_aws = time.monotonic() - ultimo_aws > 5
                        if args.fuente == "serial" or (args.fuente == "auto" and sin_aws):
                            if args.fuente == "auto" and not aviso_fallback:
                                avisar("   (AWS no esta publicando: guardo las lineas del monitor serial)")
                                aviso_fallback = True
                            guardar(lectura, "serial")
                        elif aviso_fallback and not sin_aws:
                            aviso_fallback = False
                        continue

                    # Otros mensajes del ESP32 (WiFi, MQTT, apagado remoto, errores)
                    avisar(f"   (ESP32) {linea}")

        except KeyboardInterrupt:
            pass
        finally:
            ser.close()

    imprimir_resumen(filas_por_etiqueta)
    print(f"Guardado: {salida}  ({total} lecturas)\n          {salida_log}")


if __name__ == "__main__":
    main()
