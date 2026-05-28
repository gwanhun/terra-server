# 배포 가이드 (AWS Lightsail)

## 인스턴스 생성

1. Lightsail 콘솔 > **인스턴스** > 인스턴스 생성
2. **위치**: 서울 (ap-northeast-2)
3. **플랫폼**: Linux/Unix
4. **블루프린트**: OS 전용 > **Ubuntu 24.04 LTS** (Python 3.12 기본 포함, deadsnakes PPA 불필요)
5. **플랜**: **$7/월** (1GB / 2 vCPUs / 40GB SSD / 2TB transfer) — 디바이스 100대까지 무리 없음
   - 최저가는 $5/월 (512MB)이지만 Mosquitto + bridge + API + Caddy 동시 가동엔 빡빡 → swap 1GB 필수
6. **인스턴스 이름**: `terra-server`
7. **생성**

> **요금 변경 노트**: Lightsail 가격이 인상되어 기존 $3.50/$5/$10 플랜은 각각 $5/$7/$12로 조정됨 (2025년 중반 기준).

## 정적 IP 할당 (필수)

1. Lightsail 콘솔 > 네트워킹 > 정적 IP 생성
2. 위 인스턴스에 연결
3. → 재부팅해도 IP 유지됨

## 도메인 연결 (Let's Encrypt 위해 필요)

1. 도메인 DNS A 레코드 → 위 정적 IP
2. 예: `mqtt.example.com`, `api.example.com`

## SSH 접속

```bash
# Lightsail 콘솔 > 계정 > SSH 키 > 다운로드 (region 별로)
chmod 600 ~/Downloads/LightsailDefaultKey-ap-northeast-2.pem
ssh -i ~/Downloads/LightsailDefaultKey-ap-northeast-2.pem ubuntu@<정적IP>
```

## 초기 셋업 (SSH 내부)

```bash
# 시스템 업데이트
sudo apt update && sudo apt upgrade -y

# 기본 패키지 (Ubuntu 24.04 는 Python 3.12 기본)
sudo apt install -y \
  python3.12 python3.12-venv python3-pip \
  mosquitto mosquitto-clients \
  certbot \
  git curl ufw

# uv 설치
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Swap 1GB (1GB RAM 플랜에서도 안전 마진용)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
sudo sysctl vm.swappiness=10

# 방화벽 (Lightsail 콘솔에서도 동일 규칙 추가 필요 — IPv4/IPv6 양쪽)
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # certbot HTTP-01 + Caddy
sudo ufw allow 443/tcp   # API HTTPS
sudo ufw allow 8883/tcp  # MQTT TLS
sudo ufw --force enable
```

## Mosquitto TLS 셋업

```bash
# Let's Encrypt 인증서 발급 (mqtt.example.com 으로 DNS A 레코드 미리)
sudo certbot certonly --standalone -d mqtt.example.com

# Mosquitto 설정
sudo tee /etc/mosquitto/conf.d/terra.conf > /dev/null <<'EOF'
listener 8883
cafile /etc/letsencrypt/live/mqtt.example.com/chain.pem
certfile /etc/letsencrypt/live/mqtt.example.com/cert.pem
keyfile /etc/letsencrypt/live/mqtt.example.com/privkey.pem

allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl

persistence true
persistence_location /var/lib/mosquitto/
log_dest file /var/log/mosquitto/mosquitto.log
EOF

# 브리지 계정 생성
sudo mosquitto_passwd -c /etc/mosquitto/passwd terra-bridge
# 비밀번호 입력 (terra-server .env 의 MQTT_BRIDGE_PASSWORD 와 동일)

# ACL (브리지는 전부, 디바이스는 본인 토픽만)
sudo tee /etc/mosquitto/acl > /dev/null <<'EOF'
user terra-bridge
topic readwrite esp32/#
EOF
# 디바이스별 ACL 은 페어링 시 자동 추가 스크립트로

# 인증서 권한 (mosquitto 가 읽을 수 있게)
sudo chmod 644 /etc/letsencrypt/live/mqtt.example.com/cert.pem
sudo chmod 644 /etc/letsencrypt/live/mqtt.example.com/chain.pem
sudo chmod 600 /etc/letsencrypt/live/mqtt.example.com/privkey.pem
sudo chown mosquitto:mosquitto /etc/letsencrypt/live/mqtt.example.com/privkey.pem

sudo systemctl restart mosquitto
sudo systemctl enable mosquitto
```

## terra-server 배포

```bash
# 코드 클론 (또는 rsync)
cd ~
git clone <repo-url> terra-server
cd terra-server

# 의존성
uv sync

# 환경변수
cp .env.example .env
vim .env   # 모든 값 입력

chmod 600 .env

# Supabase 마이그레이션 실행 (대시보드 SQL Editor 에 붙여넣기)
# migrations/2026-05-26_initial_schema.sql
```

## systemd 서비스 등록

### API 서버

```bash
sudo tee /etc/systemd/system/terra-api.service > /dev/null <<'EOF'
[Unit]
Description=terra-server FastAPI
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/terra-server
ExecStart=/home/ubuntu/terra-server/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable terra-api
sudo systemctl start terra-api
sudo systemctl status terra-api
```

### MQTT 브리지

```bash
sudo tee /etc/systemd/system/terra-bridge.service > /dev/null <<'EOF'
[Unit]
Description=terra-server MQTT bridge
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/terra-server
ExecStart=/home/ubuntu/terra-server/.venv/bin/terra-bridge
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable terra-bridge
sudo systemctl start terra-bridge
sudo systemctl status terra-bridge
```

## API 서버 HTTPS (리버스 프록시)

두 가지 옵션 중 선택. **Caddy를 권장** — Let's Encrypt 자동 발급/갱신을 Caddy가 직접 처리하므로 `api` 도메인 인증서는 certbot이 필요 없다 (Mosquitto용 `mqtt` 인증서만 certbot 사용).

### 옵션 A: Caddy (권장) ⭐

```bash
# Caddy 공식 저장소 추가 + 설치
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# Caddyfile (Caddy가 인증서 자동 발급/갱신)
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
api.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8000
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
    }
}
EOF

sudo systemctl reload caddy
sudo systemctl enable caddy

# 동작 확인
curl -I https://api.example.com/health
```

> Caddy 인증서 저장 위치: `/var/lib/caddy/.local/share/caddy/certificates/`. 자동 갱신은 Caddy 내부 스케줄러가 처리하므로 systemd timer 별도 설정 불필요.

### 옵션 B: Nginx + certbot (전통적 방식)

```bash
sudo apt install -y nginx python3-certbot-nginx
sudo certbot certonly --standalone -d api.example.com

sudo tee /etc/nginx/sites-available/terra-api > /dev/null <<'EOF'
server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name api.example.com;
    return 301 https://$server_name$request_uri;
}
EOF

sudo ln -s /etc/nginx/sites-available/terra-api /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 인증서 자동 갱신 (Mosquitto용 — Caddy/Nginx 공통)

```bash
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer

# 갱신 후 Mosquitto 재시작 훅 (Caddy 선택 시 Caddy 부분 생략)
sudo tee /etc/letsencrypt/renewal-hooks/post/restart-services.sh > /dev/null <<'EOF'
#!/bin/bash
systemctl restart mosquitto
# Nginx 옵션 사용 시 ↓ 한 줄 추가
# systemctl reload nginx
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/post/restart-services.sh

# 갱신 시뮬레이션
sudo certbot renew --dry-run
```

## 로그 / 모니터링

```bash
# API
sudo journalctl -u terra-api -f

# 브리지
sudo journalctl -u terra-bridge -f

# Mosquitto
sudo tail -f /var/log/mosquitto/mosquitto.log
```

## 백업

- Lightsail 콘솔 > 스냅샷 > **자동 스냅샷 활성화** (매일 1회, 7일 보관, ~$0.10/월)
- Supabase 는 자체 자동 백업 (Pro 플랜부터 PITR)
