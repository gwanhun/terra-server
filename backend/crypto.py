"""
디바이스 MQTT 토큰 단방향 해시 (bcrypt).

petcam-lab 의 Fernet (양방향) 과 달리 여기는 토큰 검증만 하면 되므로 bcrypt.
- 페어링 시: 평문 토큰 생성 → bcrypt 해시 → devices.token_hash 저장
- 디바이스로는 평문 1회 전달 (NVS 저장)
- 검증 시: 디바이스가 보낸 토큰 vs DB 해시 비교

평문은 DB 에 절대 저장하지 않음.
"""

from __future__ import annotations

import secrets

import bcrypt


def generate_token(nbytes: int = 32) -> str:
    """URL-safe base64 토큰 생성 (기본 32바이트 → 43자)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(plaintext: str) -> str:
    """bcrypt 해시 (cost=12 기본). DB 저장용."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_token(plaintext: str, hashed: str) -> bool:
    """디바이스가 보낸 토큰과 DB 해시 비교."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
