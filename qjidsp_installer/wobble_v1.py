import time, math
from camilladsp import CamillaClient

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    # ★ ごく控えめな「出汁」レベルの揺らぎ
    # wet_gainだけをゆっくり・浅く動かす（気づかない程度）
    wet_period = 34.0   # かなりゆったり
    wet_depth  = 0.8    # ±0.8dBだけ（控えめ）
    wet_base   = -40.0

    t0 = time.time()
    print("揺らぎLFO開始（ミニマル版）...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                t = time.time() - t0

                wl = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period)
                wr = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period + math.pi/2)
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                time.sleep(0.15)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
