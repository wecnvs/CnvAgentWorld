# -*- coding: utf-8 -*-
"""화면 연속 녹화 — ffmpeg gdigrab + 실제 OS 마우스 커서 그리기(draw_mouse 1).

튜토리얼/시연 영상을 만들 때 반드시 이 도구로 녹화한다.
mss 스크린샷(screenshot.py)은 커서를 찍지 않으므로 '영상'에는 절대 쓰지 않는다.
gdigrab의 -draw_mouse 1 로 실제 커서 모양과 그 이동이 그대로 영상에 기록된다.
(click.py 가 pyautogui.moveTo(duration=...) 로 부드럽게 글라이드 → 커서가 미끄러지는 모습까지 보임)

사용법:
  python record.py start <out.mp4> [--fps 30]   -> ffmpeg 시작, <out.mp4>.stop 파일이 생길 때까지 블록
  녹화 종료: <out.mp4>.stop 파일을 생성하면 ffmpeg에 'q'를 보내 깨끗하게 종료한다.
  이 스크립트 자체를 백그라운드로 실행하고, 끝낼 때 stop 파일을 만든다.

예:
  run_tool.bat record.py start "D:/.../TUTORIAL.mp4" --fps 30   (백그라운드)
  ...작업 수행...
  type nul > "D:/.../TUTORIAL.mp4.stop"                          (녹화 종료 신호)
"""
import subprocess
import sys
import os
import time


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "start":
        print("usage: record.py start <out.mp4> [--fps N]")
        sys.exit(2)

    out = sys.argv[2]
    fps = "30"
    if "--fps" in sys.argv:
        try:
            fps = sys.argv[sys.argv.index("--fps") + 1]
        except Exception:
            fps = "30"

    stopfile = out + ".stop"
    if os.path.exists(stopfile):
        os.remove(stopfile)

    cmd = [
        "ffmpeg", "-y",
        "-f", "gdigrab",
        "-draw_mouse", "1",          # ★ 실제 OS 마우스 커서를 영상에 그린다 (절대 0으로 바꾸지 말 것)
        "-framerate", fps,
        "-i", "desktop",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-r", fps,
        out,
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=open(out + ".log", "wb"),
        stderr=subprocess.STDOUT,
    )

    # guard: 최대 60분
    t0 = time.time()
    while not os.path.exists(stopfile):
        if proc.poll() is not None:
            break
        if time.time() - t0 > 3600:
            break
        time.sleep(0.2)

    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except Exception:
        proc.terminate()

    if os.path.exists(stopfile):
        os.remove(stopfile)
    print("record done:", out)


if __name__ == "__main__":
    main()
