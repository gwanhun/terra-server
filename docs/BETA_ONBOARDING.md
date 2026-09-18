# 베타 기기·카메라 온보딩 절차 (nRF/BLE 불필요)

> 대상: 베타 테스터 계정에 사육장 기기(terra-iot)·카메라(firebeetle2-p4)를 붙이는 운영 작업.
> 원칙: nRF Connect(BLE) 안 씀. **웹에서 등록 → 소스/설정에 자격증명 넣고 플래시 → 웹에서 확인.**

## 0. 전체 그림

| 단계 | 하는 일 | 도구 |
|------|---------|------|
| A. 등록 | 계정별로 서버에 기기 신분(device_id/mqtt_token) 발급 | 웹 콘솔 등록 패널 |
| B. 주입 | 그 값을 물리 기기에 넣고 펌웨어 플래시 | 기기=헤더 / 카메라=sdkconfig |
| C. 확인 | 계정별 등록 상태 점검 | 웹 콘솔 [확인] |

**핵심**: A(등록)는 서버에 레코드+MQTT 브로커 계정을 만드는 것, B(주입)는 그 자격증명을 물리 기기에 넣는 것. 둘은 별개다.

## 1. 사전 준비

- **웹 콘솔은 프로덕션에서 열 것**: `https://api.terra-server.uk/`
  - 로컬(`AUTH_MODE=dev`)은 로그인 JWT 를 무시하고 고정 dev 유저로 등록하므로 계정별 등록이 안 된다.
  - 브로커(mosquitto) 계정 등록도 프로덕션 서버에서만 된다.
- 테스트 계정: `<전화번호>@test.com` / 비밀번호 `123456` (전화번호는 숫자만).
- 계정 목록·비밀번호는 `storage/phone_accounts_*.csv` 참고.

## 2. 단계 A — 웹에서 등록

1. `https://api.terra-server.uk/` 접속 → 설정에서 API Base 를 같은 주소로 저장.
2. 아무 계정으로 로그인 (패널은 로그인 후 표시됨).
3. **"🎫 베타 기기·카메라 등록"** 패널 → **목록 적용** (계정 행이 뜬다).
4. 붙일 계정 행에서:
   - **[기기]** → `device_id` + `mqtt_token` 발급 (결과칸 표시).
   - **[카메라]** → `camera_id` + `camera_token` (+ 결과에 UUID) 발급.
   - 발급 값을 안전한 곳에 복사(토큰은 1회만 노출).

> 계정 로그인/로그아웃 없이 백그라운드로 처리되며, 메인 로그인 세션은 바뀌지 않는다.

## 3. 단계 B — 물리 기기에 주입 후 플래시

### 3-1. 사육장 기기 (terra-iot: supermini / nano)

파일 편집 → 빌드 → 플래시. `main/include/provision_creds.h` 를 연다 (없으면 `provision_creds.h.example` 복사):

```c
#define PROVISION_DEVICE_ID   "terra-a1b2c3d4"   // 웹 발급 device_id
#define PROVISION_MQTT_TOKEN  "…"                // 웹 발급 mqtt_token
#define PROVISION_WIFI_SSID   "베타와이파이"      // 비우면 기존 NVS/Kconfig WiFi 사용
#define PROVISION_WIFI_PASS   "와이파이비번"
```

```bash
cd ~/project/esp32/terra-iot-supermini      # 07 등 supermini 보드
export IDF_PYTHON_ENV_PATH=$HOME/.espressif/python_env/idf6.0_py3.11_env
source ~/.espressif/v6.0.1/esp-idf/export.sh
idf.py build flash          # -p /dev/tty.usbmodemXXXX 로 포트 지정 가능
```

- 부팅 시 값이 NVS 에 써지고, WiFi+MQTT 로 그 계정 소유로 붙는다.
- `provision_creds.h` 는 비밀값이라 git 에 안 올라간다(.gitignore). 기기마다 값만 바꿔 다시 빌드/플래시.
- 값 비우면 기존 BLE 페어링 흐름 그대로.
- **펌웨어 업데이트 시**: 한번 등록된 보드는 NVS 에 creds 가 남아 `idf.py flash` 로 앱만 갱신해도 등록이 유지된다. 업데이트할 땐 `provision_creds.h` 를 **비워두는 것**을 권장(안 비우면 그 소스로 다른 보드를 플래시할 때 남의 device_id 가 박힘). 신규 등록할 때만 값을 채운다.

> ⚠️ 07 은 반드시 `terra-iot-supermini` 에서 플래시 (nano 아님). IDF export 는 py3.11 환경 지정 필요. VS Code 모니터가 포트를 잡고 있으면 `Ctrl+]` 로 끊고 플래시.

### 3-2. 카메라 (firebeetle2-p4)

코드 수정 불필요. `idf.py menuconfig` 또는 `sdkconfig` 에서 값 설정:

```
CONFIG_APP_CAMERA_ID    = 웹 발급 camera_id (예: p4cam-xxxxxxxx)
CONFIG_APP_CAMERA_TOKEN = 웹 발급 camera_token
CONFIG_APP_CAMERA_UUID  = 웹 발급 camera uuid
CONFIG_APP_WIFI_SSID    = 베타 와이파이
CONFIG_APP_WIFI_PASSWORD= 와이파이 비번
```

```bash
cd ~/project/esp32/firebeetle2-p4-yr030
idf.py menuconfig     # 또는 sdkconfig 직접 편집
idf.py build flash
```

## 4. 단계 C — 웹에서 확인·삭제

등록 패널의 계정 행에서:
- **[확인]** → 그 계정의 등록 기기·카메라 목록 (`📟 device_id … 🟢/⚪`, `📷 camera_id … 🟢/⚪`, 🟢=온라인). 카메라도 함께 표시된다.
- 각 항목 옆 **[테스트]** → 아래 줄이 펼쳐진다(다시 누르면 닫힘).
  - **기기**: **제어 버튼 전체**(💧펌프·🌀팬·❄️냉각팬·💡LED·🔥히터·⚠래치해제·💦분무 1/2/3초). 그 계정 JWT 로 실제 명령을 보내고 ack 를 폴링해 `✅`(응답 OK)/`⏳`(무응답)/`✖`(거부) 표시. **프로덕션에서만 실제 전달**(브리지).
  - **카메라**: 상태 요약(online, 해상도/fps, stream, rotate180)과 **클립 파이프라인 통계**(rec/skip/up_ok/up_fail/last_rec)를 표시해 녹화·업로드가 도는지 확인. 라이브 영상은 메인 콘솔(그 계정 로그인) 카메라 [라이브]에서.
- 각 항목 옆 **[삭제]** → 하드 삭제(되돌릴 수 없음, mosquitto 계정도 제거). 같은 기기가 여러 번 등록됐을 때 **중복 정리용**. 삭제 후 목록 자동 갱신.
- **[전체 확인]** → 모든 계정 순차 조회 (로그인 rate limit 약 30회/5분 주의).

플래시한 기기가 부팅 후 20~30초 내 🟢(온라인) 로 뜨면 성공.

> 카메라가 [확인]에 안 보이면: (1) 프로덕션에서 열었는지, (2) [카메라] 등록이 실제로 201 로 성공했는지(결과칸에 camera_id 표시), (3) 같은 계정으로 조회 중인지 확인.

## 5. NVS 직접 주입 (대안, 재빌드 없이)

펌웨어 재빌드 없이 자격증명만 바꾸려면 NVS 이미지를 구워 플래시한다:

```bash
uv run python scripts/provision_device.py \
  --device-id terra-a1b2c3d4 --token <mqtt_token> \
  --ssid 베타와이파이 --wifi-pass 비번 --port /dev/tty.usbmodemXXXX
# 여러 대: --csv storage/beta_devices.csv 로 하나씩 꽂아가며
```

(NVS 레이아웃: 기기 ns `terra`(device_id, mqtt_token)/`wifi`(ssid, pass), 오프셋 0x9000. 카메라 ns `terra-cam`/`wifi-prov`.)

## 6. 계정 관리 (서버 전용)

계정 생성·삭제는 `service_role` 키가 필요해 **브라우저에서 불가**(키 노출 금지). 서버에서 스크립트로:

```bash
uv run python scripts/phone_test_accounts.py --recreate   # 전화번호 계정 재생성(기존은 소프트삭제)
uv run python scripts/list_registered.py                  # 계정별 등록 기기·카메라 (CLI 확인용)
```

> 이 프로젝트는 현재 `auth.users` 하드 삭제가 전역으로 막혀 있어(500), 기존 계정 정리는 소프트 삭제로 처리한다. 완전 삭제는 Supabase SQL 편집기에서 `auth.users` 의 DELETE 트리거를 손봐야 한다.

## 7. 문제 해결

| 증상 | 원인 / 조치 |
|------|-------------|
| 등록 시 500 | 로컬(AUTH_MODE=dev)에서 호출함. **프로덕션**에서 열 것 |
| CORS 오류 | API Base 가 페이지 주소와 다른 오리진. 주소창과 동일하게 맞출 것 |
| 기기가 온라인 안 됨 | WiFi 값/토큰 확인, mosquitto 등록(=프로덕션 등록) 여부 확인 |
| 카메라가 개발 계정으로 뜸 | 구 펌웨어. 카메라 BLE/JWT 등록은 2026-09-17 펌웨어부터 |
