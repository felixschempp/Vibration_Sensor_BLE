// ===== XIAO nRF52840 Sense – LSM6DS3 accel -> BLE notifications (batched) =====
// Packet: [0xAA55][seq u16][base_us u32][n u8][n * (ax,ay,az int16)]

#include <Wire.h>
#include <LSM6DS3.h>
#include <bluefruit.h>

// -------- IMU ----------
LSM6DS3 imu(I2C_MODE, 0x6A);   // change to 0x6B if wired that way

// === User settings ===
#define ACCEL_ODR_HZ    833     // 104/208/416/833/1666
#define ACCEL_RANGE_G   8       // ±2/4/8/16
#define BATCH_N         20      // <= 39 so one packet fits MTU-244
#define FLUSH_MS        50      // timeout flush partial batch

const uint16_t SYNC_WORD = 0xAA55;

// -------- BLE UUIDs ----------
const uint8_t SVC_UUID[16]    = {0x7A,0xB2,0x10,0x01,0xA3,0xC1,0x11,0xED,0xB8,0x7C,0x02,0x42,0xAC,0x12,0x00,0x11};
const uint8_t ACCEL_UUID[16]  = {0x7A,0xB2,0x10,0x02,0xA3,0xC1,0x11,0xED,0xB8,0x7C,0x02,0x42,0xAC,0x12,0x00,0x12};
const uint8_t CMD_UUID[16]    = {0x7A,0xB2,0x10,0x03,0xA3,0xC1,0x11,0xED,0xB8,0x7C,0x02,0x42,0xAC,0x12,0x00,0x13};
const uint8_t STAT_UUID[16]   = {0x7A,0xB2,0x10,0x04,0xA3,0xC1,0x11,0xED,0xB8,0x7C,0x02,0x42,0xAC,0x12,0x00,0x14};

BLEService        svc(SVC_UUID);
BLECharacteristic chAccel(ACCEL_UUID);  // notify-only, variable len up to 244
BLECharacteristic chCmd(CMD_UUID);      // small write for commands
BLECharacteristic chStat(STAT_UUID);    // status/ack via notify

// --- ODR helper ---
static inline uint16_t odr_value() {
  if      (ACCEL_ODR_HZ <= 13)   return 13;
  else if (ACCEL_ODR_HZ <= 26)   return 26;
  else if (ACCEL_ODR_HZ <= 52)   return 52;
  else if (ACCEL_ODR_HZ <= 104)  return 104;
  else if (ACCEL_ODR_HZ <= 208)  return 208;
  else if (ACCEL_ODR_HZ <= 416)  return 416;
  else if (ACCEL_ODR_HZ <= 833)  return 833;
  else                           return 1666;
}

// prefer raw int16 if available in your library
static inline int16_t readRawX() { return imu.readRawAccelX(); }
static inline int16_t readRawY() { return imu.readRawAccelY(); }
static inline int16_t readRawZ() { return imu.readRawAccelZ(); }

// ---- TX state (GLOBAL so we can reset on disconnect) ----
volatile bool     streamingEnabled = true;  // gated by CMDs
uint16_t          g_seq     = 0;
int16_t           g_buf[BATCH_N * 3];
uint8_t           g_count   = 0;
uint32_t          g_base_us = 0;
uint32_t          g_last_ms = 0;

// ---- Connection tweaks ----
void onConnect(uint16_t conn) {
#ifdef BLE_GAP_PHY_2MBPS
  ble_gap_phys_t phys = { BLE_GAP_PHY_2MBPS, BLE_GAP_PHY_2MBPS };
  sd_ble_gap_phy_update(conn, &phys);
#endif
}

// Proactively reset & re-advertise on disconnect
void onDisconnect(uint16_t /*conn*/, uint8_t /*reason*/) {
  g_seq = 0; g_count = 0;
  // If you use LED as a connection indicator, turn it off here:
  // pinMode(LED_BUILTIN, OUTPUT); digitalWrite(LED_BUILTIN, LOW);

  Bluefruit.Advertising.restartOnDisconnect(true);
  if (!Bluefruit.Advertising.isRunning()) {
    Bluefruit.Advertising.start(0);
  }
}

// ---- BLE send (single notify) ----
bool sendPacketBLE(uint16_t seq, uint32_t base_us, const int16_t* buf, uint8_t n) {
  if (n == 0 || n > 39) return false;
  uint8_t frame[9 + 39 * 6];
  size_t  o = 0;

  // SYNC
  frame[o++] = (uint8_t)(SYNC_WORD & 0xFF);
  frame[o++] = (uint8_t)(SYNC_WORD >> 8);

  // seq
  frame[o++] = (uint8_t)(seq & 0xFF);
  frame[o++] = (uint8_t)(seq >> 8);

  // base_us
  frame[o++] = (uint8_t)(base_us & 0xFF);
  frame[o++] = (uint8_t)((base_us >> 8) & 0xFF);
  frame[o++] = (uint8_t)((base_us >> 16) & 0xFF);
  frame[o++] = (uint8_t)((base_us >> 24) & 0xFF);

  // n
  frame[o++] = n;

  // payload
  memcpy(frame + o, reinterpret_cast<const uint8_t*>(buf), n * 3 * sizeof(int16_t));
  o += n * 3 * sizeof(int16_t);

  return chAccel.notify(frame, o);
}

// ---- CMD/STAT helpers ----
static inline void sendStatus(const char* s){
  if (Bluefruit.connected()) chStat.notify((const uint8_t*)s, strlen(s));
}

void onCmdWrite(uint16_t conn, BLECharacteristic* chr, uint8_t* data, uint16_t len) {
  (void)chr;

  char cmd[32];
  uint16_t n = (len < sizeof(cmd)-1) ? len : sizeof(cmd)-1;
  for (uint16_t i=0;i<n;i++) cmd[i] = tolower((int)data[i]);
  cmd[n] = 0;

  if (strncmp(cmd, "start", 5) == 0) {
    streamingEnabled = true;
    g_seq = 0; g_count = 0;
    sendStatus("START");
    return;
  }
  if (strncmp(cmd, "stop", 4) == 0) {
    streamingEnabled = false;
    g_count = 0;
    sendStatus("STOP");
    return;
  }
  if (strncmp(cmd, "bye", 3) == 0) {
    streamingEnabled = false;
    g_count = 0;
    sendStatus("BYE");
    Bluefruit.disconnect(conn);
    return;
  }
}

// ===== Setup =====
void setup() {
  // Serial.begin(115200);

  // --- IMU ---
  Wire.begin();
  Wire.setClock(400000);

  imu.settings.gyroEnabled       = 0;
  imu.settings.gyroFifoEnabled   = 0;

  imu.settings.accelEnabled      = 1;
  imu.settings.accelRange        = ACCEL_RANGE_G;
  imu.settings.accelSampleRate   = odr_value();
  imu.settings.accelBandWidth    = (ACCEL_ODR_HZ >= 833) ? 400 : 200;

  imu.settings.accelFifoEnabled    = 0;
  imu.settings.timestampEnabled    = 0;
  imu.settings.timestampFifoEnabled= 0;

  if (imu.begin() != 0) { while (1) {} }

  // --- BLE stack ---
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX); // larger MTU/data length
  Bluefruit.begin();
  Bluefruit.setName("XIAO-ACCEL");
  Bluefruit.setTxPower(8); // dBm

  Bluefruit.Periph.setConnInterval(6, 12);   // ~7.5–15 ms
  Bluefruit.Periph.setConnSlaveLatency(0);
  Bluefruit.Periph.setConnSupervisionTimeout(400); // 4.0 s
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  // --- GATT ---
  svc.begin();

  chAccel.setProperties(CHR_PROPS_NOTIFY);
  chAccel.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  chAccel.setFixedLen(0);     // variable length
  chAccel.setMaxLen(244);     // match MTU-3
  chAccel.begin();

  chCmd.setProperties(CHR_PROPS_WRITE_WO_RESP | CHR_PROPS_WRITE);
  chCmd.setPermission(SECMODE_OPEN, SECMODE_OPEN);
  chCmd.setWriteCallback(onCmdWrite);
  chCmd.setMaxLen(32);
  chCmd.begin();

  chStat.setProperties(CHR_PROPS_NOTIFY);
  chStat.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  chStat.setMaxLen(20);
  chStat.begin();

  // --- Advertising ---
  Bluefruit.Advertising.stop();
  Bluefruit.Advertising.clearData();
  Bluefruit.ScanResponse.clearData();

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(svc);
  Bluefruit.ScanResponse.addName();

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setFastTimeout(0);
  Bluefruit.Advertising.setInterval(64, 244); // 40–152.5 ms
  Bluefruit.Advertising.start(0);
}

// ===== Loop =====
void loop() {
  if (!Bluefruit.connected() || !streamingEnabled) {
    g_count = 0;
    delay(1);
    return;
  }

  // pace reads to ODR using micros()
  const uint32_t period_us =
      (ACCEL_ODR_HZ == 1666) ? 600 : (uint32_t)(1000000.0 / (double)ACCEL_ODR_HZ);
  static uint32_t next_us = 0;
  uint32_t now = micros();
  if ((int32_t)(now - next_us) < 0) return;
  next_us = now + period_us;

  int16_t ax = readRawX();
  int16_t ay = readRawY();
  int16_t az = readRawZ();

  if (g_count == 0) {
    g_base_us = now;
    g_last_ms = millis();
  }

  g_buf[3*g_count + 0] = ax;
  g_buf[3*g_count + 1] = ay;
  g_buf[3*g_count + 2] = az;
  g_count++;

  // Full batch?
  if (g_count >= BATCH_N) {
    (void)sendPacketBLE(g_seq++, g_base_us, g_buf, g_count);
    g_count = 0;
  } else if ((millis() - g_last_ms) >= FLUSH_MS) {
    // timeout flush for partial batch
    (void)sendPacketBLE(g_seq++, g_base_us, g_buf, g_count);
    g_count = 0;
  }
}