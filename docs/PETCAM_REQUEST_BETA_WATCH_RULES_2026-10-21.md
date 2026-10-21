---
handoff_version: 1
task_id: beta-watch-rules
execution_repo: /Users/baek/petcam-lab
requester: terra-server (owner 2026-10-21 합의)
---

# 요청: 베타 카메라 감시 판정 규칙 보강 (카메라 에러로그·clip_stats 델타)

> **대상 레포:** `/Users/baek/petcam-lab`
> **대상 파일:** `scripts/beta_camera_watch.py`, `tests/test_beta_camera_watch.py`, 예약 작업 `beta2-camera-watch-hourly` 지시문(`~/.claude/scheduled-tasks/beta2-camera-watch-hourly/SKILL.md`)
> **규칙 원문:** 이관훈 2026-10-21 (아래 §2). **결정:** owner 2026-10-21 — §3 표 추천안 그대로.
> **요청 한 줄:** "마지막 클립이 N시간 전"만으로 재부팅 요청을 내지 말고, `clip_stats` 5분 델타·`camera_logs`·평소 시간대 녹화 패턴으로 판정해 카메라별 표를 만든다.

---

## 1. 배경

- 지금 판정(`check()`): 하트비트 30분 없음 → 꺼짐, 마지막 클립 낮 6h·밤(22~06시) 2h → 녹화 멈춤 의심 → 슬랙 "재부팅 요청 필요".
- 야행성·주간성 카메라는 매일 같은 시간대에 공백이 생기는 게 정상이라 **10/20·10/21 오탐 3건**이 났다.
- terra-server 운영 재부팅 API(`POST /ops/cameras/{id}/reboot`, 요청 6)는 배포 완료됐지만, **이번 작업에서는 호출하지 않는다**(§3 결정 1).

## 2. 판정 규칙 (이관훈 원문 요지)

자료: `cameras.clip_stats`(15초 하트비트마다 갱신, 재부팅 시 카운터 0) + `clip_stats_at`, `camera_logs`(`msg`·`uptime_s`·`prev_boot`·`created_at`), `motion_clips.started_at`.

1. **그 시간대에 원래 찍히는 카메라인가** — 최근 7일 `motion_clips` 를 날짜×시(KST)로 집계. 지금 ±2시간 구간에 지난 7일 중 2일 이상 클립이 없으면 → 일과상 공백, 알림 금지. 평소 찍히는 카메라만 다음 단계.
2. **살아 있는가** — `last_seen_at` 2분 이내, `camera_logs` 에 `mem:` 노트(매시간 1줄)가 최근 70분 안 → 살아 있음. 둘 다 없으면 오프라인(재부팅 명령은 도달 못 하므로 "오프라인"으로만 보고).
3. **멈춤 vs 트리거 없음** — `clip_stats` 를 5분 간격 2회 읽어 델타로 판정.
   - **A. 트리거 없음**(정상, 알림 금지): `rec`·`skip_noise`·`skip_lock`·`last_rec_s` 모두 불변, `up_busy_s = -1`, 그 구간 SD/인코더 에러 줄 0건(이 보드들은 녹화할 때마다 SD 에러 줄이 찍히므로 에러 0 = 녹화 시도 0).
   - **B. 노이즈 게이트**(재부팅 무효, 펌웨어 튜닝 건): `skip_noise` 증가, `rec` 불변, `noise_q4 >= noise_gate`.
   - **C. 업로드 정체**(재부팅 후보): `rec` 증가했는데 `motion_clips` 새 행 없음, 또는 `up_busy_s > 600`, 또는 `up_fail` 증가 + `last_err.age_s` 작음.
   - **D. 인코더 사망**(재부팅 후보): `enc_streak` 증가 또는 `enc_kbps = 0` 두 샘플 연속, `rec` 불변.
   - **E. 슬롯 잠김**(재부팅 후보): `skip_lock` 증가.
   - **F. 내부 RAM 고갈**(재부팅 후보): `sys.int_largest < 20480`.
4. **이미 재부팅됐는가** — `sys.uptime_s < 3시간` 이거나 `camera_logs` 에 `boot: reset=` 줄이 최근 3시간 안 → 재부팅 요청 금지. reset 사유(`SW:mqtt_reboot`, `SW:rotate`, `PANIC`, `WDT` …)를 그대로 보고.
5. **참고만, 재부팅 사유로 쓰지 말 것** — "SD 쓰기 fault 래치", `sdmmc_read/write failed (0x108/0x109)`, "재업로드 보류", `R2 PUT failed: ESP_ERR_NOT_FINISHED`: SD 보드 문제(PSRAM 폴백으로 녹화 계속, 재부팅해도 안 고쳐짐). `sd_backlog` 와 함께 "SD 상태" 칸에만 적는다.
6. **보고 형식(카메라마다 한 줄)** —
   `이름 (uuid 앞 8자리 / camera_id) | fw | uptime | reset 사유 | 마지막 mem 노트 시각 | 마지막 클립 | 평소 이 시간대 클립 여부 | clip_stats 델타(rec/skip_noise/up_fail/enc_streak) | 판정 | 재부팅 권고: 예/아니오(사유)`
   판정: 트리거 없음 / 노이즈 게이트 / 업로드 정체 / 인코더 사망 / 슬롯 잠김 / RAM 고갈 / 오프라인.
   **재부팅 권고 "예"는 3-C/D/E/F 중 하나가 두 샘플에서 확인되고 4번에 걸리지 않을 때만.**

## 3. owner 결정 (2026-10-21, 원문과 다른 부분 포함)

| # | 결정 | 내용 |
|---|------|------|
| 1 | 재부팅 권고 "예"의 처리 | **슬랙 보고만.** terra-server 운영 재부팅 API 자동 호출은 며칠 판정 결과를 본 뒤 별도로 켠다(이번 범위 밖) |
| 2 | 오프라인 확인 위치 | **1단계와 무관하게 모든 감시 카메라에** 2단계 기준(신호 2분 + mem 70분, 둘 다 없으면 오프라인)을 적용한다. 원문 순서대로면 일과상 공백 시간대의 꺼짐이 보고되지 않기 때문 |
| 3 | 설치 7일 미만 카메라 | 1단계 집계는 **카메라의 첫 클립 날짜부터 센 날만** 쓴다(최대 7일). 이력이 3일 미만이면 1단계를 건너뛰고 3단계로 간다(3-A 가 오탐을 거른다). 2차 베타는 10/19 시작이라 데이터 없는 날을 "클립 없음"으로 세면 전부 공백 판정이 된다 |
| 4 | 3단계 진입 조건 | 기존 `is_rec_stall()`(낮 6h·밤 2h)을 **후보 선별용으로만** 남긴다. 재부팅 권고는 3단계 델타로만 정한다. 5분 간격 2회 읽기는 후보가 있을 때만 한다(그때만 실행이 5분 늘어남) |
| 5 | 슬랙 게시 | 게시는 **오프라인 · 재부팅 권고 "예" · 노이즈 게이트**만, 같은 카메라·같은 판정은 지금처럼 24시간(`REMIND_H`)에 1회. 트리거 없음·일과상 공백은 게시 안 함. §2-6 형식의 카메라별 전체 표는 출력 JSON 의 새 `report` 필드와 예약 작업의 마지막 요약에 남긴다. 슬랙 문구 "재부팅 요청 필요 → 전원 껐다 켜기 요청" 은 권고 "예"일 때만 |

## 4. terra-server 쪽에서 확인한 사실 (2026-10-21 운영 DB, 읽기만)

- `clip_stats` 키: `rec, skip, skip_noise, skip_lock, skip_old, skip_bitrate, skip_expired, last_rec_s, up_ok, up_fail, up_busy_s, up_left_d, enc_err, enc_streak, enc_kbps, noise_q4, noise_gate, br_gate_kbps, sd_ok, sd_fail, sd_fault, sd_freq, sd_rtry, sd_rderr, sd_rskip, sd_backlog, open_fail, max_age_d, sys{uptime_s, reset, int_largest, int_free, heap, rssi, temp, txpwr_q4, ch, bssid, net{…}}`.
  - **`last_err` 는 일부 카메라에만 있다**(예: `{"err":268,"http":0,"age_s":3581,"stage":4}`). 없으면 3-C 의 `up_fail` 조건은 `up_fail` 증가만으로 보지 말고 그 항을 건너뛴다.
- `camera_logs` 컬럼: `id, camera_id, created_at, uptime_s, prev_boot, count, msg`.
  - `mem:` 노트 예: `N (15021678) mem: if=123611 lg=42996 ps=19697420 t=44.9 sw=1036 | ` — 0.4.5.1 펌웨어도 매시간 찍는다.
  - **`boot: reset=` 줄은 재부팅마다 찍히지 않는다.** 예: `p4cam-ff153960` 이 10/21 04:12 경 `reset=WDT` 로 재부팅(`sys.uptime_s=463`)했는데 그 줄이 없다. → 4번은 **`sys.uptime_s < 10800` 을 주 신호**로, `boot: reset=` 줄은 보조로 쓴다. reset 사유는 `sys.reset` 에서 읽는다.
  - **인코더 에러 문구는 최근 3일 로그에 0건**이다. 3-A 의 "SD/인코더 에러 줄 0건"은 SD 패턴으로 판정한다: `msg` 에 `sdmmc`, `clip_sd`, `terra_mp4`, `SD ` 중 하나 포함 + `E (` 로 시작.
  - 최근 2일 SD 에러 줄 약 7,500건(감시 카메라 전체). 5분 구간만 세면 충분하다(`created_at` 범위 조회).
  - `reboot requested: mqtt_reboot (uptime …s)` 노트가 재부팅 직전에 남는다(참고용).
- 지금 `labeling_group='beta'` 이고 연결된 카메라에는 1차 베타 잔여 4대(오프라인)가 섞여 있다. 감시 대상은 지금처럼 `TESTERS` 기준이라 영향 없음.

## 5. 구현 메모

- `check()` 를 단계별 함수로 나눈다(예: `usual_hour(clips, now) -> bool | None`, `alive(cam, mem_last, now) -> bool`, `classify(s1, s2, sd_err_count, new_clip) -> str`, `already_rebooted(cs, boot_line_at, now) -> str | None`). 이름은 자유, 판정 순서·임계값은 §2·§3 그대로.
- 5분 대기: 후보가 1대 이상일 때만 첫 샘플 → `time.sleep(300)` → 두 번째 샘플. `--dry-run` 에도 대기는 그대로(판정이 같아야 하니까). 테스트는 sleep 을 주입/패치.
- 상태 파일 키는 `camera_id:판정` 으로 바꾸고, 기존 `:offline`·`:rec_stall` 키는 자연 소멸(recovered)로 둔다.
- 출력 JSON 에 `report`(카메라별 §2-6 한 줄 문자열 배열) 추가. 기존 필드(`watched`, `online`, `problems`, `alerts`, `recovered`, `reassign`, `slack_text`)는 유지 — 예약 작업이 읽는다.
- 라벨러 재배정 부분은 건드리지 않는다.
- 예약 작업 `SKILL.md` 4단계 요약에 `report` 를 덧붙이도록 지시문을 한 줄 고친다(스케줄·채널·나머지 규칙은 그대로).

## 6. 검증

1. `uv run pytest tests/test_beta_camera_watch.py -q` — 기존 테스트 + 판정별 테스트(1단계 공백/이력 3일 미만 건너뜀, 2단계 오프라인, 3-A~F 각각, 4번 uptime·boot 줄, 5번 SD 줄은 판정 불변, 권고 "예"는 두 샘플 확인 시에만).
2. 운영 DB `uv run python scripts/beta_camera_watch.py --dry-run` 1회 — 카메라별 `report` 가 말이 되는지, **10/20·10/21 오탐 3건이 "트리거 없음/일과상 공백"으로 나오는지** 확인.
3. 쓰기 모드는 예약 작업 다음 회차에 맡긴다.

## 7. 주의

- petcam-lab 작업 트리에 이 작업과 무관한 미커밋 변경(`.claude/launch.json`, `.gitignore`, `docs/…`, `experiments/…`)이 있다. **감시 스크립트·테스트만 커밋**하고 나머지는 건드리지 않는다.
- DB 쓰기는 기존 재배정 경로 외에 추가하지 않는다. terra-server 운영 재부팅 API 는 부르지 않는다.
- 끝나면 terra-server 담당에게 커밋 sha 와 dry-run `report` 를 공유해 주세요.
