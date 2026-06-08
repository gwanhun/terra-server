#!/bin/bash
# terra-mosquitto-register.sh — Mosquitto password/ACL 관리 헬퍼.
#
# terra-api (uvicorn, ubuntu user) 가 sudo -n 으로 호출. NOPASSWD 등록 필수.
# /etc/sudoers.d/terra-mosquitto 참조.
#
# 사용:
#   sudo terra-mosquitto-register.sh register <device_id> <plaintext_token>
#   sudo terra-mosquitto-register.sh unregister <device_id>
#   sudo terra-mosquitto-register.sh regen-acl < acl_content_via_stdin
#   sudo terra-mosquitto-register.sh reload
#
# 종료 코드:
#   0 — 성공
#   1 — 잘못된 인자
#   2 — mosquitto_passwd / 파일 권한 실패

set -euo pipefail

PASSWD_FILE="${TERRA_MOSQUITTO_PASSWD:-/etc/mosquitto/passwd}"
ACL_FILE="${TERRA_MOSQUITTO_ACL:-/etc/mosquitto/acl}"

usage() {
    echo "usage: $0 {register <id> <token> | unregister <id> | regen-acl | reload}" >&2
    exit 1
}

[ $# -lt 1 ] && usage

ACTION="$1"
shift

case "$ACTION" in
    register)
        [ $# -ne 2 ] && usage
        DEVICE_ID="$1"
        PASSWORD="$2"
        # -b: batch (비대화형), -c 안 씀 (기존 파일 유지)
        mosquitto_passwd -b "$PASSWD_FILE" "$DEVICE_ID" "$PASSWORD"
        ;;

    unregister)
        [ $# -ne 1 ] && usage
        DEVICE_ID="$1"
        # 디바이스가 password 파일에 없어도 OK (idempotent)
        mosquitto_passwd -D "$PASSWD_FILE" "$DEVICE_ID" 2>/dev/null || true
        ;;

    regen-acl)
        # STDIN 으로 전체 ACL 내용 받음 → atomic write (tmp → rename)
        TMP="${ACL_FILE}.tmp.$$"
        cat > "$TMP"
        # 권한 mosquitto:mosquitto 644
        chown mosquitto:mosquitto "$TMP" 2>/dev/null || true
        chmod 644 "$TMP"
        mv "$TMP" "$ACL_FILE"
        ;;

    reload)
        # SIGHUP — password/ACL 파일 다시 읽기. mosquitto 재시작 X (연결 끊김 없음)
        systemctl reload mosquitto
        ;;

    *)
        usage
        ;;
esac
