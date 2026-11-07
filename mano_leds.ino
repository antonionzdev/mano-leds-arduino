/*
  mano_leds.ino  ·  Arduino UNO R4 WiFi

  Recibe por USB líneas de texto del script de Python con este formato:
      10110,87
  - 5 primeros caracteres: LEDs de pulgar, índice, corazón, anular, meñique (1 = encendido)
  - número tras la coma: ángulo del servo (0-180)

  Cableado:
    LED 1..5   -> pines D2, D3, D4, D5, D6 (pata larga del LED, con resistencias en serie)
                  y la pata corta a GND
    Servo 9g   -> señal a D9, rojo a 5V, marrón/negro a GND
*/

#include <Servo.h>

const int PINES_LED[5] = {2, 3, 4, 5, 6};
const int PIN_SERVO = 9;

Servo servo;
String linea = "";

void setup() {
  Serial.begin(115200);

  for (int i = 0; i < 5; i++) {
    pinMode(PINES_LED[i], OUTPUT);
  }

  // Test de arranque: enciende los LEDs de uno en uno para comprobar el cableado
  for (int i = 0; i < 5; i++) {
    digitalWrite(PINES_LED[i], HIGH);
    delay(150);
    digitalWrite(PINES_LED[i], LOW);
  }

  servo.attach(PIN_SERVO);
  servo.write(90);  // servo al centro
}

void loop() {
  // Vamos leyendo caracteres hasta que llega el salto de línea
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      procesar(linea);
      linea = "";
    } else if (c != '\r') {
      linea += c;
      if (linea.length() > 20) {
        linea = "";  // basura: empezamos de nuevo
      }
    }
  }
}

void procesar(String mensaje) {
  // Formato correcto: 5 dígitos, una coma y el ángulo -> la coma está en la posición 5
  if (mensaje.indexOf(',') != 5) {
    return;
  }

  for (int i = 0; i < 5; i++) {
    digitalWrite(PINES_LED[i], mensaje.charAt(i) == '1' ? HIGH : LOW);
  }

  int angulo = constrain(mensaje.substring(6).toInt(), 0, 180);
  servo.write(angulo);
}
