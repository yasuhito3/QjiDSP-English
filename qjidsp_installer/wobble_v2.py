import time, math
from camilladsp import CamillaClient

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    # ウェット（残響）の揺らぎ（従来のv1と同じ、控えめ）
    wet_period = 34.0
    wet_depth  = 0.8
    wet_base   = -40.0

    # 左右のパンニング（残響側に混ぜ込む、控えめ）
    pan_period = 40.0
    pan_depth  = 0.7

    # delay揺らぎ（奥行きの微妙な変動）
    delay_period = 27.0
    delay_base   = 35.0
    delay_depth  = 5.0

    # airフィルタのゲイン変動（高さ・空気感の揺らぎ）
    air_period = 45.0   # かなりゆったり
    air_depth  = 0.4    # ごく控えめ
    air_base   = 1.2    # v1のair既定値

    t0 = time.time()
    print("揺らぎLFO v1（空気の流れ版）開始...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                t = time.time() - t0

                pan = math.sin(2*math.pi*t/pan_period)
                wl = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period) + pan * 0.5
                wr = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period + math.pi/2) - pan * 0.5
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                delay_val = round(max(5, delay_base + delay_depth * math.sin(2*math.pi*t/delay_period)), 1)
                air_val = round(air_base + air_depth * math.sin(2*math.pi*t/air_period), 3)

                patch = [
                    {"op": "replace", "path": "/filters/delay_R/parameters/delay", "value": delay_val},
                    {"op": "replace", "path": "/filters/air/parameters/gain", "value": air_val}
                ]
                cdsp.config.patch(patch)

                time.sleep(0.15)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
