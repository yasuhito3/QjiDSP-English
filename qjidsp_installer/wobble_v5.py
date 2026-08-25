import math
import time
from camilladsp import CamillaClient

UPDATE_INTERVAL = 0.20  # 秒

# ごく弱い揺らぎ
WET_BASE  = -40.0
WET_DEPTH = 0.15        # ±0.15 dB

PAN_BASE  = 0.0
PAN_DEPTH = 0.15        # ±0.15 %

PERIOD = 60.0           # 60秒で1周期


def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    print("Micro Wobble v1")

    while True:
        try:
            cdsp.connect()
            print("接続成功")

            t0 = time.time()

            while True:
                t = time.time() - t0
                phase = 2.0 * math.pi * t / PERIOD

                wet = WET_BASE + WET_DEPTH * math.sin(phase)
                pan = PAN_BASE + PAN_DEPTH * math.sin(phase + math.pi / 2)

                cdsp.set_volume("wet_gain", wet)
                cdsp.set_volume("pan", pan)

                time.sleep(UPDATE_INTERVAL)

        except Exception as e:
            print(f"接続エラー: {e}")
            time.sleep(3)


main()
