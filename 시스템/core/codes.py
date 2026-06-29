# -*- coding: utf-8 -*-
"""고유코드 생성과 토큰(이름_코드) 분해."""
import secrets


def gen_code() -> str:
    return secrets.token_hex(2)  # 4 hex


def split_token(token: str):
    name, code = token.rsplit("_", 1)
    return name, code
