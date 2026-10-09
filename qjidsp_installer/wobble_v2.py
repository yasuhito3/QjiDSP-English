import time, math
from camilladsp import CamillaClient

# ★★★ miniAI(QNG) フェーズ2: V2正式版(音量反応型・wet/pan) ★★★
# 当初delay_R/airフィルターもconfig.patch()で音量反応させる計画だったが、
# 実機確認の結果 config.patch() の正確な引数形式が不明瞭だった。
# 代替手段(config.active()取得→値書き換え→set_active()で書き戻し)は
# 技術的には可能だが、0.15秒ごとに設定全体を送り直すことになり、
# V1で実証済みのvolume.set_volume()(軽量・安全)とは性質が異なるため、
# 音切れ等のリスクが未検証。
#
# そのため今回のプロトタイプは、V1と同じ安全な方式(volume.set_volume)
# だけを使い、残響(wet)とパンニングの2要素を音量反応にする。
# delay/airは元のwobble_v2.pyの固定揺らぎのまま(変更なし)。
# 実地テストで良好な結果が得られたため正式採用(V1よりさらに広がりが出たとのこと)。

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    wet_period = 34.0
    wet_base   = -40.0
    pan_period = 40.0

    # --- delay/airは元のwobble_v2.pyと同じ固定揺らぎ(今回は変調しない) ---
    delay_period = 27.0
    delay_base   = 35.0
    delay_depth  = 5.0
    air_period   = 45.0
    air_base     = 1.2
    air_depth    = 0.4

    # --- wet/panの揺れ幅だけを音量反応で動的に変える(V1と同じ考え方) ---
    WET_DEPTH_MIN, WET_DEPTH_MAX = 0.2, 2.5
    PAN_DEPTH_MIN, PAN_DEPTH_MAX = 0.15, 2.0

    QUIET_DB = -35.0
    LOUD_DB  = -15.0
    SMOOTHING_ALPHA = 0.08

    t0 = time.time()
    smoothed_intensity = 0.3
    print("揺らぎLFO開始(V2: 音量反応版・wet/pan)...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                t = time.time() - t0

                try:
                    rms = cdsp.levels.playback_rms()
                    avg_db = sum(rms) / len(rms) if rms else QUIET_DB
                except Exception:
                    avg_db = QUIET_DB

                raw_intensity = (avg_db - QUIET_DB) / (LOUD_DB - QUIET_DB)
                raw_intensity = max(0.0, min(1.0, raw_intensity))
                smoothed_intensity += SMOOTHING_ALPHA * (raw_intensity - smoothed_intensity)

                wet_depth = WET_DEPTH_MIN + smoothed_intensity * (WET_DEPTH_MAX - WET_DEPTH_MIN)
                pan_depth = PAN_DEPTH_MIN + smoothed_intensity * (PAN_DEPTH_MAX - PAN_DEPTH_MIN)

                pan = math.sin(2*math.pi*t/pan_period)
                wl = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period) + pan * pan_depth
                wr = wet_base + wet_depth * math.sin(2*math.pi*t/wet_period + math.pi/2) - pan * pan_depth
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                # --- delay/airは固定揺らぎのまま(元のwobble_v2.pyと同じ) ---
                # ★ ここはconfig.active()/set_active()で書き戻す必要があり、
                #   今回は安全のため見送っている。

                time.sleep(0.15)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
