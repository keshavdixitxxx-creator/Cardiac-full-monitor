/*
 * strap_node.ino — Cardiac Monitor strap node (the "BP-machine cuff" side)
 *
 * Tie the sensor to your arm, plug the laptop wire into this board, then run:
 *     python agent.py
 *
 * Hardware (any ONE of these works):
 *   A) AD8232 ECG front-end  → signal pin to A0, connect the arm straps
 *      (LO- / LO+ go to D10 / D11 — agent treats them as "electrode off")
 *   B) Pulse/PPG sensor (e.g. SEN-11574) → signal to A0, GND, 3V3/5V
 *
 * Protocol (9600 baud, one message per line) — matches agent.py:
 *   W:512   raw waveform sample (ADC 0..1023)
 *   Q:1     electrodes/signal OK   (Q:0 = lead off / bad contact)
 *
 * agent.py detects the R-peaks itself and derives BPM + HRV + the live
 * heart-attack % line chart in the dashboard's "Live sensor agent" tab.
 */

const int SIG_PIN   = A0;   // AD8232 / pulse sensor signal
const int LO_MINUS  = 10;   // AD8232 LO- (lead-off detect) — pulse sensors: tie unused pin LOW
const int LO_PLUS   = 11;   // AD8232 LO+

unsigned long lastQ = 0;

void setup() {
  Serial.begin(9600);
  pinMode(LO_MINUS, INPUT);
  pinMode(LO_PLUS, INPUT);
}

void loop() {
  // Lead-off detection (AD8232): HIGH = not connected properly
  bool leadOff = (digitalRead(LO_MINUS) == HIGH) || (digitalRead(LO_PLUS) == HIGH);

  int raw = analogRead(SIG_PIN);
  Serial.print("W:");
  Serial.println(raw);

  // quality flag ~every 400 ms so agent.py can discount noisy stretches
  if (millis() - lastQ > 400) {
    lastQ = millis();
    Serial.print("Q:");
    Serial.println(leadOff ? 0 : 1);
  }

  delay(8);   // ~125 Hz sampling — plenty for rhythm + HRV
}
