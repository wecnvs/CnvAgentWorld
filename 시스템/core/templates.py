# -*- coding: utf-8 -*-
"""템플릿 로딩/치환. 에이전트가 읽는 프롬프트는 코드가 아니라 여기(파일)에 있다."""
from .paths import TPL


def load(name: str) -> str:
    return (TPL / name).read_text(encoding="utf-8")


def fill(text: str, **kw) -> str:
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", v)
    return text
