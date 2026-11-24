
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>
#include "esp_camera.h"

#include "secrets.h"   // copy secrets.example.h to secrets.h

#define TALK_BTN D0
const int   MIC_RATE    = 16000;
const int   MAX_SECONDS = 10;
const int   OUT_RATE    = 24000;
const float GAIN        = 1.0;

I2SClass mic, spk;
uint8_t* rec;
const size_t MAX_BYTES = MIC_RATE * 2 * MAX_SECONDS;
const size_t PHOTO_MAX = 600 * 1024;
const bool   FLIP_PHOTO = true;
bool camOK = false;

void writeWavHeader(uint8_t* h, uint32_t dataBytes) {
  uint32_t v; uint16_t s;
  memcpy(h, "RIFF", 4); v = 36 + dataBytes; memcpy(h + 4, &v, 4);
  memcpy(h + 8, "WAVEfmt ", 8); v = 16; memcpy(h + 16, &v, 4);
  s = 1; memcpy(h + 20, &s, 2);
  s = 1; memcpy(h + 22, &s, 2);
  v = MIC_RATE; memcpy(h + 24, &v, 4);
  v = MIC_RATE * 2; memcpy(h + 28, &v, 4);
  s = 2; memcpy(h + 32, &s, 2);
  s = 16; memcpy(h + 34, &s, 2);
  memcpy(h + 36, "data", 4); memcpy(h + 40, &dataBytes, 4);
}

void playSilence(int ms) {
  int16_t z[256] = {0};
  for (int i = 0; i < ms * OUT_RATE / 1000 / 128; i++) spk.write((uint8_t*)z, sizeof(z));
}

void fail(const char* msg) {
  while (1) { Serial.println(msg); delay(2000); }
}

bool startCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = 15; c.pin_d1 = 17; c.pin_d2 = 18; c.pin_d3 = 16;
  c.pin_d4 = 14; c.pin_d5 = 12; c.pin_d6 = 11; c.pin_d7 = 48;
  c.pin_xclk = 10; c.pin_pclk = 13; c.pin_vsync = 38; c.pin_href = 47;
  c.pin_sccb_sda = 40; c.pin_sccb_scl = 39;
  c.pin_pwdn = -1; c.pin_reset = -1;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAMESIZE_UXGA;
  c.jpeg_quality = 10;
  c.fb_count     = 1;
  c.fb_location  = CAMERA_FB_IN_PSRAM;
  c.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  if (esp_camera_init(&c) != ESP_OK) return false;

  sensor_t* s = esp_camera_sensor_get();
  bool flip = FLIP_PHOTO;
  if (s->id.PID == OV3660_PID) {
    flip = !flip;
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
  }
  s->set_vflip(s, flip);
  if (FLIP_PHOTO) s->set_hmirror(s, 1);
  Serial.printf("Camera OK (sensor 0x%x)\n", s->id.PID);
  return true;
}

camera_fb_t* snap() {
  for (int i = 0; i < 3; i++) {
    camera_fb_t* old = esp_camera_fb_get();
    if (old) esp_camera_fb_return(old);
  }
  return esp_camera_fb_get();
}

size_t record() {
  size_t len = 0;
  while (digitalRead(TALK_BTN) == LOW && len < MAX_BYTES) {
    len += mic.readBytes((char*)rec + 44 + len, min((size_t)1024, MAX_BYTES - len));
  }
  return len;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(TALK_BTN, INPUT_PULLUP);

  rec = (uint8_t*)ps_malloc(44 + MAX_BYTES + PHOTO_MAX);
  if (!rec) fail("PSRAM failed: set Tools > PSRAM > OPI PSRAM, then upload again");

  mic.setPinsPdmRx(42, 41);
  if (!mic.begin(I2S_MODE_PDM_RX, MIC_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    fail("mic failed");
  }
  spk.setPins(D3, D1, D2);
  if (!spk.begin(I2S_MODE_STD, OUT_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO)) {
    fail("speaker failed");
  }

  camOK = startCamera();
  if (!camOK) Serial.println("Camera failed: voice still works, photo questions are off. Check the camera ribbon.");

  WiFi.mode(WIFI_STA);
  while (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WiFi: joining \"%s\"", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(300); Serial.print("."); }
    if (WiFi.status() == WL_CONNECTED) break;

    Serial.printf("\nFailed (status %d). Networks in range:\n", WiFi.status());
    WiFi.disconnect();
    int n = WiFi.scanNetworks();
    bool seen = false;
    for (int i = 0; i < n; i++) {
      Serial.printf("  \"%s\"  ch %d  %d dBm\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i));
      if (WiFi.SSID(i) == WIFI_SSID) seen = true;
    }
    if (n <= 0) Serial.println("  none: check the antenna is clicked on");
    else if (!seen) Serial.println("  -> hotspot not in the list: name must match exactly (capitals, spaces, apostrophes)");
    else Serial.println("  -> hotspot is visible, so the password is wrong");
    WiFi.scanDelete();
  }
  Serial.println(" connected. Hold the button and talk! (tap, then hold = question about what you see)");
}

void loop() {
  if (digitalRead(TALK_BTN) == HIGH) { delay(10); return; }

  Serial.println("Recording...");
  bool withPhoto = false;
  size_t len = record();
  if (len < MIC_RATE) {
    delay(30);
    uint32_t t = millis();
    while (digitalRead(TALK_BTN) == HIGH && millis() - t < 700) delay(5);
    if (digitalRead(TALK_BTN) == HIGH) { Serial.println("Too short, hold longer."); return; }
    withPhoto = camOK;
    Serial.println(camOK ? "Camera question: recording..." : "Camera is off, recording voice only...");
    len = record();
    if (len < MIC_RATE) { Serial.println("Too short, hold longer."); delay(200); return; }
  }
  writeWavHeader(rec, len);
  size_t wavBytes = 44 + len, total = wavBytes;

  if (withPhoto) {
    delay(300);
    camera_fb_t* fb = snap();
    if (fb && fb->len <= PHOTO_MAX) {
      memcpy(rec + total, fb->buf, fb->len);
      total += fb->len;
      Serial.printf("Photo: %u KB\n", (unsigned)(fb->len / 1024));
    } else {
      Serial.println("Photo failed, sending voice only.");
    }
    if (fb) esp_camera_fb_return(fb);
  }

  // send to the server
  Serial.printf("Sending %.1f s of audio...\n", len / (MIC_RATE * 2.0));
  HTTPClient http;
  http.begin(SERVER);
  http.setTimeout(30000);
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Wav-Bytes", String(wavBytes));
  const char* keys[] = {"X-Reply"};
  http.collectHeaders(keys, 1);
  int code = http.POST(rec, total);
  if (code != 200) { Serial.printf("Server error: %d\n", code); http.end(); return; }
  Serial.println("AI: " + http.header("X-Reply"));

  WiFiClient* s = http.getStreamPtr();
  int remaining = http.getSize();
  uint8_t in[512];
  int16_t out[512];
  while (http.connected() && (remaining > 0 || remaining == -1)) {
    size_t avail = s->available();
    if (avail < 2) { delay(1); continue; }
    size_t want = min(avail, sizeof(in)) & ~1;
    size_t n = s->readBytes(in, want);
    int16_t* smp = (int16_t*)in;
    for (size_t i = 0; i < n / 2; i++) {
      int v = smp[i] * GAIN;
      v = constrain(v, -32768, 32767);
      out[2 * i] = out[2 * i + 1] = v;
    }
    spk.write((uint8_t*)out, n * 2);
    if (remaining > 0) remaining -= n;
  }
  playSilence(100);
  http.end();
  Serial.println("Done. Hold the button to ask again.");
}
