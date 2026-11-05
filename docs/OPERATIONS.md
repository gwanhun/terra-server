# 운영 가이드 (배포·재배포·로그·재시작)

> Node.js + pm2 익숙한 사람을 위한 Python + uv + systemd 운영 cheatsheet.
> 초기 인프라 셋업은 [DEPLOYMENT.md](DEPLOYMENT.md) 참고. 본 문서는 **일상 운영** 만.

## pm2 ↔ systemd 멘탈 매핑

| 하는 일 | pm2 | systemd (terra-server) |
|---------|-----|------------------------|
| 시작 | `pm2 start app.js` | `sudo systemctl start terra-api` |
| 중지 | `pm2 stop app` | `sudo systemctl stop terra-api` |
| 재시작 | `pm2 restart app` | `sudo systemctl restart terra-api` |
| 상태 | `pm2 list` | `sudo systemctl status terra-api` |
| 로그 (실시간) | `pm2 logs app` | `sudo journalctl -u terra-api -f` |
| 로그 (최근) | `pm2 logs --lines 100` | `sudo journalctl -u terra-api -n 100` |
| 부팅 시 자동 시작 | `pm2 startup` + `pm2 save` | `sudo systemctl enable terra-api` (이미 설정됨) |
| 부팅 시 자동 시작 해제 | `pm2 unstartup` | `sudo systemctl disable terra-api` |
| 죽으면 자동 재시작 | pm2 기본 | systemd `Restart=always` (이미 설정됨) |
| 설정 파일 | `ecosystem.config.js` | `/etc/systemd/system/terra-api.service` |
| 프로세스 매니저 자체 재시작 | `pm2 update` | 없음 (systemd 는 OS 의 일부) |

## 핵심 차이점

| 항목 | pm2 | systemd |
|------|-----|---------|
| 권한 | 사용자 권한 | `sudo` 필수 (시스템 서비스) |
| 의존성 | npm 패키지 | OS 의 init system (Ubuntu 기본) |
| `.env` 로드 | dotenv 라이브러리 | 코드에서 `load_dotenv()` 직접 호출 |
| 클러스터 모드 | `instances: max` | 별도 |
| 무중단 reload | `pm2 reload` | `systemctl reload` (앱 지원해야 함) |

## 서비스 구성 (현재 등록된 것)

| 서비스 | 역할 | 포트 |
|--------|------|------|
| `terra-api.service` | FastAPI (uvicorn) | 8000 |
| `terra-bridge.service` | MQTT 브리지 (paho) | - |
| `caddy.service` | HTTPS reverse proxy | 80/443 → 8000 |
| `mosquitto.service` | MQTT 브로커 | 8883 (TLS) |

설정 파일:
- terra-api: `/etc/systemd/system/terra-api.service`
- terra-bridge: `/etc/systemd/system/terra-bridge.service`
- Caddy: `/etc/caddy/Caddyfile`
- Mosquitto: `/etc/mosquitto/conf.d/terra.conf`

---

## 1. 코드 재배포 (가장 자주)

### 시나리오 A — 코드만 바뀜 (의존성 변경 X)

```bash
ssh ubuntu@<정적IP>
cd ~/terra-server
git pull
sudo systemctl restart terra-api
sudo systemctl restart terra-bridge
```

검증:
```bash
curl https://api.terra-server.uk/health
# {"ok":true,"service":"terra-api"}
sudo systemctl status terra-api    # active (running) 확인
```

### 시나리오 B — 의존성 추가/변경됨 (`pyproject.toml` 수정)

`npm install` 에 해당. **`uv sync` 가 핵심**:

```bash
cd ~/terra-server
git pull
uv sync                              # 의존성 동기화 (lockfile 기준)
sudo systemctl restart terra-api
sudo systemctl restart terra-bridge
```

> pm2 와의 차이: pm2 는 npm install 후 그냥 `pm2 restart` 면 되지만, systemd 는 같은 흐름. 단 `.venv/` 가 변경되니 restart 필수.

### 시나리오 C — `.env` 환경변수 변경

```bash
vim ~/terra-server/.env
sudo systemctl restart terra-api
sudo systemctl restart terra-bridge
```

> dotenv 는 프로세스 시작 시 1회 로드. 재시작 안 하면 새 값 안 먹힘.

### 시나리오 D — DB 마이그레이션

코드와 별개 흐름. **Supabase 대시보드 > SQL Editor 에 SQL 붙여넣고 실행.**

```bash
# 로컬 머신
cat migrations/2026-XX-XX_xxxxx.sql
# → 복사 → Supabase 대시보드 > SQL Editor > New query > 붙여넣기 > Run
```

마이그레이션 끝나면 코드 배포는 위 A/B 그대로.

> 자동화하려면 `psql "$DATABASE_URL" -f migrations/xxx.sql` 도 가능 (필요 시 `.env` 에 `DATABASE_URL` 추가).

---

## 1-1. 클립 mp4 faststart 백필 (2026-10-26, 1회 + 필요 시 재실행)

카메라 0.4.14 이하가 올린 mp4 는 moov(목차)가 파일 끝이라 iOS/Safari 재생 시작이 느리다(10Mbps 11초).
새 업로드는 메타 등록 응답 뒤 서버가 자동으로 옮기고(`backend/clip_faststart.py`), 이미 올라간 파일은
아래로 한 번 옮긴다. 같은 key 덮어쓰기·크기 동일·DB 무변경·멱등(여러 번 돌려도 됨).

```bash
cd ~/terra-server
uv run python scripts/faststart_backfill.py --dry-run     # 옮길 개수 확인
uv run python scripts/faststart_backfill.py               # 전체 (클립당 15~20MB 받고 올림)
uv run python scripts/faststart_backfill.py --camera p4cam-2d854b2f --limit 20
```

결과 로그 `완료 Ns: {'already': A, 'converted': C, ...}`. `error` 가 있으면 다시 돌리면 그것만 재시도된다.

## 1-2. R2 고아 클립 역등록 (2026-11-04, 매일 자동)

카메라가 R2 PUT 까지 마치고 meta POST 가 서버에 닿지 못하면(카메라 쪽 네트워크·TLS 끊김) mp4 만
남고 앱엔 안 보인다. 서버 로그엔 흔적이 없고 펌웨어 기본값은 실패 클립 즉시 폐기라, 서버가 주기적으로
R2 를 스캔해 빠진 행을 채운다. 멱등(이미 등록된 clip_id 스킵)이고, 등록 클립과 같은 시각인 고아는
중복 재업로드로 보고 제외하며, 해제된 카메라는 건너뛴다.

```bash
cd ~/terra-server
uv run python scripts/reconcile_r2_orphans.py --all-linked --days 2            # dry-run
uv run python scripts/reconcile_r2_orphans.py --all-linked --days 2 --apply    # 등록
uv run python scripts/reconcile_r2_orphans.py --camera p4cam-d141abfb --since 2026-10-18 --until 2026-10-26  # 특정 카메라·범위
```

매일 04:10 KST 자동 실행 (`crontab -e`, 서버 시간대가 UTC 면 `10 19 * * *`):

```cron
10 4 * * * cd /home/ubuntu/terra-server && .venv/bin/python scripts/reconcile_r2_orphans.py --all-linked --days 2 --apply >> /home/ubuntu/reconcile_r2.log 2>&1
```

확인은 `tail -50 ~/reconcile_r2.log`. 매일 `orphan 0건` 이 정상이고, 특정 카메라에 계속 생기면 그 보드의
네트워크·TLS 상태를 본다. R2 버킷엔 clips 경로 lifecycle 규칙이 **없다**(2026-10-20 대시보드 확인) —
삭제 API 가 R2 객체를 직접 지우고, 고아는 이 스크립트가 등록으로 흡수한다.

### 역방향 — 행은 있는데 R2 미디어가 없는 클립 (2026-11-05)

미디어 없는 행은 **지우지 않는다.** 라벨링 팀 원장 `motion_clip_system_exclusions`(state=media_deleted) 가
"의도적으로 원본만 지운 클립" 의 기준이고, 앱·라벨링 웹은 이 원장을 보고 숨긴다. 행엔 라벨러 제출·GME 실행·
감사 기록이 FK 로 붙어 있어 CASCADE 로 지우면 같이 사라진다(2026-11-05 라벨링 팀 합의).

- **R2 대시보드에서 카메라 폴더를 직접 지우지 말 것.** 원장 없이 미디어만 사라져 앱에 404 클립이 보인다
  (2026-11-05 확인 3,494행 — 과거 대시보드 삭제). 카메라 영상 삭제는 앱의 카메라 삭제(DELETE /cameras) 로.
- 원장에 없는 미디어 없는 행이 보이면 라벨링 팀에 CSV 를 넘겨 원장 행을 넣게 한다. 아래 모드가 그 CSV 를 만든다.

```bash
cd ~/terra-server
uv run python scripts/reconcile_r2_orphans.py --prune-missing                     # 보고: 원장 유/무 분리 → tmp/prune_missing_<날짜>.csv
uv run python scripts/reconcile_r2_orphans.py --prune-missing --camera p4cam-…    # 한 카메라만
```

`--apply`(원장 없는 행만 DELETE) 는 남겨두지만 기본 경로가 아니다 — 라벨링 팀과 합의된 경우에만.

---

## 2. 로그 확인

### 실시간 (pm2 logs 대체)
```bash
sudo journalctl -u terra-api -f         # API
sudo journalctl -u terra-bridge -f      # MQTT 브리지
sudo tail -f /var/log/mosquitto/mosquitto.log    # 브로커
sudo journalctl -u caddy -f             # HTTPS / 인증서
```

### 최근 N 줄
```bash
sudo journalctl -u terra-api -n 100 --no-pager
```

### 특정 시간 범위
```bash
sudo journalctl -u terra-api --since "1 hour ago"
sudo journalctl -u terra-api --since "2026-05-29 14:00" --until "2026-05-29 15:00"
```

### 에러만 필터
```bash
sudo journalctl -u terra-api -p err --since today
```

### 로그가 너무 커지면
journald 는 자동으로 회전하지만 수동 정리:
```bash
sudo journalctl --disk-usage              # 현재 크기
sudo journalctl --vacuum-time=7d          # 7일 이전 삭제
sudo journalctl --vacuum-size=100M        # 100MB 이하로 유지
```

---

## 3. 상태 확인 / 재시작 / 정지

```bash
# pm2 list 대체
sudo systemctl list-units --type=service --state=running | grep -iE "terra|caddy|mosquitto"

# 단건 상태 (RAM, CPU, PID, 최근 로그)
sudo systemctl status terra-api

# 정지 / 시작 / 재시작
sudo systemctl stop terra-api
sudo systemctl start terra-api
sudo systemctl restart terra-api

# 잠시 끄고 부팅 시 자동 시작도 해제
sudo systemctl stop terra-api
sudo systemctl disable terra-api

# 다시 켜고 자동 시작 등록
sudo systemctl enable terra-api
sudo systemctl start terra-api
```

---

## 4. systemd 서비스 파일 수정

### 예: 메모리 제한 추가, ExecStart 변경

```bash
sudo vim /etc/systemd/system/terra-api.service
# 수정 후
sudo systemctl daemon-reload     # 서비스 정의 다시 읽기 (필수!)
sudo systemctl restart terra-api
```

> `daemon-reload` 안 하면 `restart` 해도 옛날 정의로 뜸. pm2 의 `pm2 update` 와 비슷한 개념.

---

## 5. Mosquitto 자동 등록 셋업 (1회만)

terra-api 가 `POST /devices/pair` / `POST /cameras/pair` 시점에 Mosquitto password 파일 + ACL 을 자동 갱신하도록.

```bash
ssh ubuntu@<정적IP>
cd ~/terra-server

# 1) 헬퍼 스크립트 복사 + 실행권한
sudo cp scripts/terra-mosquitto-register.sh /usr/local/bin/
sudo chmod 755 /usr/local/bin/terra-mosquitto-register.sh

# 2) sudoers 등록 (visudo 로 문법 검증 후 적용)
sudo cp scripts/terra-mosquitto-sudoers /etc/sudoers.d/terra-mosquitto
sudo chmod 440 /etc/sudoers.d/terra-mosquitto
sudo visudo -c -f /etc/sudoers.d/terra-mosquitto   # "/etc/sudoers.d/terra-mosquitto: parsed OK" 떠야 함

# 3) .env 활성화
vim ~/terra-server/.env
# MOSQUITTO_REGISTRY_ENABLED=true 로 변경

# 4) terra-api 재시작
sudo systemctl restart terra-api

# 5) 검증 — 페어링 호출 후 password 파일에 줄 추가됐나
sudo cat /etc/mosquitto/passwd | tail -5
sudo cat /etc/mosquitto/acl | tail -10
sudo journalctl -u terra-api -n 20 | grep -i mosquitto
```

수동 동기화 (실패 복구용):
```bash
# 누락된 디바이스 직접 등록
sudo /usr/local/bin/terra-mosquitto-register.sh register terra-xxxxxxxx <plaintext_token>

# ACL 전체 재생성은 페어링 호출 시 자동. 수동으로 강제하려면 임의 디바이스 페어링 후 삭제.
```

---

## 6. 첫 배포 (참고)

이 단계는 한 번만. 자세한 건 [DEPLOYMENT.md](DEPLOYMENT.md). 요약:

1. Lightsail 인스턴스 + 정적 IP + 도메인 DNS
2. SSH 접속
3. Python 3.12 + uv 설치
4. Mosquitto + Caddy 설치 + 설정
5. `git clone` + `uv sync` + `.env` 작성
6. `terra-api.service` / `terra-bridge.service` 등록 + `enable` + `start`

---

## 6. 트러블슈팅

### 6.1 포트 8000 이미 점유 ("address already in use")

대부분 **systemd 서비스가 이미 떠 있는데 수동으로 또 띄우려는 경우**. 정상이야:

```bash
# 현재 떠 있는 거 확인
sudo systemctl status terra-api

# active (running) 이면 systemd 가 잘 띄움. 수동 uvicorn 띄울 필요 없음.
# inactive 인데 8000 점유면:
sudo lsof -i :8000              # 어떤 PID
sudo fuser -k 8000/tcp          # 그 PID 죽이기
```

### 6.2 코드 수정했는데 반영 안 됨

`restart` 까먹은 경우 90%. systemd 는 코드 자동 reload 안 함:

```bash
sudo systemctl restart terra-api
sudo journalctl -u terra-api -n 20    # 부팅 로그에서 새 동작 확인
```

### 6.3 `uv sync` 실패 — Python 버전 문제

```bash
uv python list                  # 설치된 버전
uv python install 3.12          # 필요 시
uv sync
```

### 6.4 `.env` 변경했는데 새 값 안 먹힘

```bash
sudo systemctl restart terra-api
sudo systemctl restart terra-bridge
```

확인:
```bash
sudo journalctl -u terra-api -n 5  # 부팅 시 `load_dotenv` 동작 로그
```

### 6.5 systemd 서비스 시작 실패

```bash
sudo systemctl status terra-api    # ExitCode / 마지막 에러 줄
sudo journalctl -u terra-api -n 50 # 자세한 traceback
```

가장 흔한 원인:
- `.env` 누락 → `SupabaseNotConfigured` 같은 RuntimeError
- `uv sync` 안 함 → `.venv/bin/uvicorn` 없음
- 권한 문제 → `WorkingDirectory` / `User` 확인

### 6.6 디스크 풀림

```bash
df -h
sudo journalctl --disk-usage
sudo journalctl --vacuum-time=7d
sudo apt clean
```

### 6.7 인증서 갱신 확인

Let's Encrypt 인증서는 90일 만료. Caddy 가 자동 갱신. 강제 확인:

```bash
sudo systemctl status caddy
sudo journalctl -u caddy --since "1 day ago" | grep -i renew
```

만료 임박 (30일 이내) 알림 받으려면 모니터링 별도.

---

## 7. 코드 ↔ 운영 매핑 (전체 흐름)

```
[로컬 맥북]                          [Lightsail Ubuntu]                  [Supabase / R2]
                                                                              
1. 코드 수정                         (자동 가동 중)                            
2. uv run pytest                                                              
3. git push                                                                   
                                     4. git pull                              
                                     5. uv sync (의존성 바뀌었으면)              
                                     6. systemctl restart terra-api          
                                     7. systemctl restart terra-bridge       
                                     8. journalctl -f 로 확인                  
9. curl https://api.terra-server.uk/health
                                     10. Supabase Table Editor             ◄──┘
                                         로 데이터 흐름 검증
```

## 8. 변경 이력

| 날짜 | 변경 |
|------|------|
| 2026-05-29 | 최초 작성 |
| 2026-11-04 | 1-2 R2 고아 클립 역등록 cron 추가 |
