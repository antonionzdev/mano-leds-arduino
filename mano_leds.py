"""
mano_leds.py  ·  Controla 5 LEDs y un servo con la mano, usando la webcam

Qué hace:
  1. Lee la webcam del portátil.
  2. MediaPipe (de Google) encuentra tu mano y devuelve 21 puntos
     (nudillos, articulaciones, puntas de los dedos...).
  3. Calculamos qué dedos están estirados y cuánto está abierto el "pellizco"
     (distancia entre la punta del pulgar y la del índice).
  4. Mandamos por USB una línea de texto a la placa Arduino, tipo "10110,87":
       - los 5 primeros caracteres = un dedo cada uno (1 = estirado, 0 = doblado)
         en orden: pulgar, índice, corazón, anular, meñique
       - el número tras la coma = ángulo del servo (0-180), controlado por el pellizco

Uso:
  python mano_leds.py --puerto COM3            (Windows)
  python mano_leds.py --puerto /dev/ttyACM0    (Linux)
  python mano_leds.py --sin-placa              (probar solo la cámara, sin Arduino)

Pulsa la tecla q con la ventana de la cámara seleccionada para salir.

Numeración de los 21 puntos de MediaPipe:
   0 = muñeca
   1-4   = pulgar   (4 = punta)
   5-8   = índice   (5 = base, 6 = articulación intermedia, 8 = punta)
   9-12  = corazón  (9 = base, 10 = intermedia, 12 = punta)
   13-16 = anular   (13 base, 14 intermedia, 16 punta)
   17-20 = meñique  (17 base, 18 intermedia, 20 punta)
"""

import argparse
import math
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

import cv2
import mediapipe as mp
import serial
import serial.tools.list_ports
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

URL_MODELO = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)
RUTA_MODELO = Path(__file__).with_name("hand_landmarker.task")

BAUDIOS = 115200

# --- Ajustes de los gestos: si algo no responde bien, toca estos números ---
UMBRAL_PULGAR = 0.55      # el pulgar cuenta como "estirado" si su ratio supera esto
PELLIZCO_CERRADO = 0.15   # ratio del pellizco con los dedos juntos  -> servo a 0 grados
PELLIZCO_ABIERTO = 1.00   # ratio del pellizco con los dedos separados -> servo a 180 grados
SUAVIZADO = 0.3           # 0 = servo congelado, 1 = sin suavizar (más temblor)

# Líneas que unen los puntos para dibujar el esqueleto de la mano
CONEXIONES = [
    (0, 1), (1, 2), (2, 3), (3, 4),          # pulgar
    (0, 5), (5, 6), (6, 7), (7, 8),          # índice
    (5, 9), (9, 10), (10, 11), (11, 12),     # corazón
    (9, 13), (13, 14), (14, 15), (15, 16),   # anular
    (13, 17), (17, 18), (18, 19), (19, 20),  # meñique
    (0, 17),                                  # borde de la palma
]


# ----------------------------------------------------------------------------
# Matemáticas de los gestos (no tocan ni la cámara ni la placa)
# ----------------------------------------------------------------------------
def dist(a, b):
    """Distancia en píxeles entre dos puntos (x, y)."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def tam_mano(p):
    """Distancia muñeca -> base del dedo corazón. Hace de 'regla' para que los
    gestos funcionen igual aunque acerques o alejes la mano de la cámara."""
    return max(dist(p[0], p[9]), 1e-6)


def ratio_pulgar(p):
    """Lo lejos que está la punta del pulgar de la base del índice, medido en
    'tamaños de mano'. Pulgar pegado a la palma = número bajo."""
    return dist(p[4], p[5]) / tam_mano(p)


def ratio_pellizco(p):
    """Distancia punta del pulgar <-> punta del índice, en 'tamaños de mano'."""
    return dist(p[4], p[8]) / tam_mano(p)


def dedos_estirados(p):
    """Devuelve [pulgar, índice, corazón, anular, meñique] con True/False."""
    pulgar = ratio_pulgar(p) > UMBRAL_PULGAR
    # Resto de dedos: estirado si la punta está más lejos de la muñeca que la
    # articulación intermedia. Con el dedo doblado, la punta vuelve hacia la palma.
    otros = [
        dist(p[0], p[punta]) > dist(p[0], p[intermedia])
        for punta, intermedia in ((8, 6), (12, 10), (16, 14), (20, 18))
    ]
    return [pulgar] + otros


def ratio_a_angulo(ratio):
    """Convierte el ratio del pellizco en un ángulo de servo entre 0 y 180."""
    t = (ratio - PELLIZCO_CERRADO) / (PELLIZCO_ABIERTO - PELLIZCO_CERRADO)
    return round(max(0.0, min(1.0, t)) * 180)


def construir_mensaje(estirados, angulo):
    """Ejemplo: [True, False, True, True, False], 87  ->  '10110,87'"""
    bits = "".join("1" if e else "0" for e in estirados)
    return f"{bits},{angulo}"


# ----------------------------------------------------------------------------
# Cámara, modelo y placa
# ----------------------------------------------------------------------------
def asegurar_modelo():
    """Descarga el modelo de manos de Google la primera vez (unos pocos MB)."""
    if not RUTA_MODELO.exists():
        print("Descargando el modelo de manos (solo pasa la primera vez)...")
        urllib.request.urlretrieve(URL_MODELO, RUTA_MODELO)


def abrir_placa(puerto):
    try:
        placa = serial.Serial(puerto, BAUDIOS, timeout=1)
    except serial.SerialException as error:
        print(f"No he podido abrir {puerto}: {error}")
        puertos = [x.device for x in serial.tools.list_ports.comports()]
        print("Puertos que veo ahora mismo:", puertos or "ninguno (¿está el cable USB conectado?)")
        print("Cierra el Monitor Serie del Arduino IDE: solo un programa puede usar el puerto a la vez.")
        sys.exit(1)
    time.sleep(2)  # la placa puede reiniciarse al abrir el puerto; le damos tiempo
    return placa


def dibujar(frame, p, estirados):
    for a, b in CONEXIONES:
        cv2.line(frame, (int(p[a][0]), int(p[a][1])), (int(p[b][0]), int(p[b][1])), (200, 200, 200), 2)
    # Puntas de los dedos: verde = estirado, rojo = doblado
    for i, punta in enumerate((4, 8, 12, 16, 20)):
        color = (0, 255, 0) if estirados[i] else (0, 0, 255)
        cv2.circle(frame, (int(p[punta][0]), int(p[punta][1])), 9, color, -1)


def texto(frame, linea, y):
    cv2.putText(frame, linea, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


# ----------------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Controla LEDs y un servo con la mano")
    parser.add_argument("--puerto", default="COM3", help="puerto de la placa (COM3, /dev/ttyACM0...)")
    parser.add_argument("--camara", type=int, default=0, help="número de la webcam (0 = la primera)")
    parser.add_argument("--sin-placa", action="store_true", help="probar solo la cámara, sin Arduino")
    args = parser.parse_args()

    asegurar_modelo()
    placa = None if args.sin_placa else abrir_placa(args.puerto)

    cap = cv2.VideoCapture(args.camara)
    if not cap.isOpened():
        print(f"No puedo abrir la cámara {args.camara}. Prueba con --camara 1.")
        sys.exit(1)

    opciones = vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(RUTA_MODELO)),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.5,
    )

    historial = deque(maxlen=3)   # últimos 3 resultados, para que los LEDs no parpadeen
    angulo_suave = 90.0
    ultimo_mensaje = None
    ultimo_ts = 0

    try:
        with vision.HandLandmarker.create_from_options(opciones) as detector:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("La cámara ha dejado de enviar imagen.")
                    break

                frame = cv2.flip(frame, 1)  # efecto espejo, más natural
                alto, ancho = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                imagen = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

                # En modo VIDEO los tiempos (ms) tienen que ir siempre creciendo
                ts = max(int(time.monotonic() * 1000), ultimo_ts + 1)
                ultimo_ts = ts
                resultado = detector.detect_for_video(imagen, ts)

                if resultado.hand_landmarks:
                    # MediaPipe da coordenadas entre 0 y 1; las pasamos a píxeles
                    p = [(lm.x * ancho, lm.y * alto) for lm in resultado.hand_landmarks[0]]

                    historial.append(dedos_estirados(p))
                    # Voto por mayoría entre los últimos frames para cada dedo
                    estirados = [sum(h[i] for h in historial) * 2 > len(historial) for i in range(5)]

                    r_pellizco = ratio_pellizco(p)
                    objetivo = ratio_a_angulo(r_pellizco)
                    angulo_suave += SUAVIZADO * (objetivo - angulo_suave)

                    dibujar(frame, p, estirados)
                    texto(frame, f"Pulgar {ratio_pulgar(p):.2f} (umbral {UMBRAL_PULGAR})", 30)
                    texto(frame, f"Pellizco {r_pellizco:.2f} -> servo {round(angulo_suave)}", 55)
                else:
                    historial.clear()
                    estirados = [False] * 5
                    texto(frame, "Sin mano", 30)

                mensaje = construir_mensaje(estirados, round(angulo_suave))
                texto(frame, f"Enviado: {mensaje}", alto - 15)

                if mensaje != ultimo_mensaje:  # solo mandamos cuando algo cambia
                    if placa is not None:
                        placa.write((mensaje + "\n").encode())
                    ultimo_mensaje = mensaje

                cv2.imshow("Mano -> LEDs (q para salir)", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    finally:
        if placa is not None:
            placa.write(b"00000,90\n")  # apagar LEDs y servo al centro
            placa.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
