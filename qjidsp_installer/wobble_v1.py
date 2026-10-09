import time, math
from camilladsp import CamillaClient

# ★★★ miniAI(QNG) フェーズ2: V1正式版(音量反応型) ★★★
# 既存wobble_v1.pyの固定サイン波LFOを、再生中の音量(RMS)に応じて
# 揺らぎの"深さ"が変化するようにしたもの。
# 制御方式(cdsp.volume.set_volume)自体は元のwobble_v1.pyと同じ。
# 実地テスト(ヘッドホン/カーオーディオ/ホームオーディオ)で良好な結果が得られたため正式採用。

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    wet_period = 34.0   # 揺らぎの周期(秒) - 元のwobble_v1.pyと同じ
    wet_base   = -40.0  # 揺らぎの中心値(dB) - 元のwobble_v1.pyと同じ

    # ★ RMSレベル(dB)を 0〜1 の"盛り上がり度"に変換するための範囲。
    #   実測(2026-10-10、Captain環境、通常再生時): 概ね-22〜-33dB程度だった。
    #   これを踏まえた初期値。実際に聴きながら調整してほしい。
    QUIET_DB = -35.0   # これ以下は"静か"とみなす
    LOUD_DB  = -15.0   # これ以上は"大きい"とみなす
    DEPTH_MIN = 0.2    # 静かな時でも完全に静止させない(控えめな呼吸感を残す)
    DEPTH_MAX = 2.5    # 盛り上がった時の最大揺らぎ幅(dB) ※元は固定0.8だった

    SMOOTHING_ALPHA = 0.08  # 0〜1。大きいほど音量変化への追従が速くなる

    t0 = time.time()
    smoothed_intensity = 0.3  # 起動直後の初期値(無難な中間)
    print("揺らぎLFO開始(V1: 音量反応版)...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                t = time.time() - t0

                # ★ 現在の再生音量(RMS)を取得
                try:
                    rms = cdsp.levels.playback_rms()
                    avg_db = sum(rms) / len(rms) if rms else QUIET_DB
                except Exception:
                    avg_db = QUIET_DB  # 取得失敗時は"静か"として安全側に倒す

                raw_intensity = (avg_db - QUIET_DB) / (LOUD_DB - QUIET_DB)
                raw_intensity = max(0.0, min(1.0, raw_intensity))
                # ★ 平滑化: 急激な音量変化にすぐ反応せず、じわっと追従させる
                smoothed_intensity += SMOOTHING_ALPHA * (raw_intensity - smoothed_intensity)

                wet_depth = DEPTH_MIN + smoothed_intensity * (DEPTH_MAX - DEPTH_MIN)

                wl = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period)
                wr = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period + math.pi/2)
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                time.sleep(0.15)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
