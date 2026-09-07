#include <Arduino.h>

// put function declarations here:
void blink_led(int LED, int delaytime);

#define LED_BUILTIN 2

void setup() {
  // put your setup code here, to run once:
  // int result = myFunction(2, 3);
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  // Blink the LED at half-second interval six times
  for (int i = 0; i < 6; i++) {
	blink_led(LED_BUILTIN, 500);
  }

  // Blink the LED at two-second interval three times
  for (int i = 0; i < 3; i++) {
	blink_led(LED_BUILTIN, 2000);
  }
}

// put function definitions here:
void blink_led(int LED, int delaytime) {

  // Set output HIGH for specified time and print on serial monitor
  Serial.println("LED ON");
  digitalWrite(LED, HIGH);
  delay(delaytime);

  // Set output LOW for specified time and print on serial monitor
  Serial.println("LED LOW");
  digitalWrite(LED, LOW);
  delay(delaytime);
}