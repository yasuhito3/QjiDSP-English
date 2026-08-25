import time, math
from camilladsp import CamillaClient

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    # ★ 和食系パンニング版：シンプルyaml（real_hall/wet_gain/delay_Rのみ）に対応
    # v1_minimalの「出汁」に、ごく控えめな左右の漂いをプラスする

    # ウェット（残響）の揺らぎ
    wet_period = 34.0
    wet_depth  = 0.8
    wet_base   = -40.0

    # 左右のパンニング（残響側だけをそっと動かす）
    pan_period = 40.0   # かなりゆったり
    pan_depth  = 1.0    # 控えめ

    # delay揺らぎ（奥行きの微妙な変動）
    delay_period = 27.0
    delay_base   = 35.0
    delay_depth  = 6.0  # 控えめ（±6ms）

    t0 = time.time()
    print("揺らぎLFO v3（和食系）開始...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                t = time.time() - t0

                # ウェット揺らぎ＋パンニングを合成
                pan = math.sin(2*math.pi*t/pan_period)
                wl = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period) + pan * 0.6
                wr = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period + math.pi/2) - pan * 0.6
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                # delay揺らぎ（奥行きがそっと動く）
                delay_val = round(max(5, delay_base + delay_depth * math.sin(2*math.pi*t/delay_period)), 1)
                patch = [
                    {"op": "replace", "path": "/filters/delay_R/parameters/delay", "value": delay_val}
                ]
                cdsp.config.patch(patch)

                time.sleep(0.15)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
