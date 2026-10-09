import math
import time
from camilladsp import CamillaClient

# ★★★ miniAI(QNG) フェーズ2: V6正式版(音量反応型・控えめ、ヘッドホン用) ★★★
# 元のwobble_v5.pyは cdsp.set_volume("wet_gain", wet) という
# トップレベルの呼び方をしていたが、実機確認の結果 cdsp オブジェクトには
# そのようなメソッドは存在しない(cdsp.volume.set_volume()という
# 名前空間形式のみ存在する)。そのため元のV5のwobbleは、接続のたびに
# エラーになり実質的に機能していなかった可能性が高い。
#
# 今回は、V1/V2で実証済みの正しい呼び方(cdsp.volume.set_volume(1/2, 値))
# を使い、かつV1/V2と同じ発想で、揺れ幅を再生音量(RMS)に連動させる。
# V5のYAML構成はV1/V2と同じwet_gain_L/Rの骨格を持っているため、
# 同じチャンネル番号(1=L, 2=R)がそのまま使える見込み。
#
# V5はもともと「倍音モード」として、ヴァイオリン/尺八系の共鳴EQが主役で、
# wobble自体は控えめな性格(元の揺れ幅は固定0.15とごく小さい)だったため、
# 音量反応の可変域もV1/V2よりさらに控えめに設定している。

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)

    WET_BASE = -40.0
    PERIOD = 60.0  # 元のwobble_v5.pyと同じ、ゆったりした周期
    UPDATE_INTERVAL = 0.20

    # V6はV5(倍音モード)にヘッドホン向けクロスフィードを加えたバリエーション。
# 音の性格はV5と同じにしたいため、同じ可変域をそのまま採用している。
# ★ V5は元が控えめ(固定0.15)だったため、V1/V2より可変域を小さくした
    WET_DEPTH_MIN, WET_DEPTH_MAX = 0.1, 1.0

    QUIET_DB = -35.0
    LOUD_DB  = -15.0
    SMOOTHING_ALPHA = 0.08

    print("揺らぎLFO開始(V6: 音量反応版・ヘッドホン用)...")

    while True:
        try:
            cdsp.connect()
            print("接続成功")

            t0 = time.time()
            smoothed_intensity = 0.3

            while True:
                t = time.time() - t0
                phase = 2.0 * math.pi * t / PERIOD

                try:
                    rms = cdsp.levels.playback_rms()
                    avg_db = sum(rms) / len(rms) if rms else QUIET_DB
                except Exception:
                    avg_db = QUIET_DB

                raw_intensity = (avg_db - QUIET_DB) / (LOUD_DB - QUIET_DB)
                raw_intensity = max(0.0, min(1.0, raw_intensity))
                smoothed_intensity += SMOOTHING_ALPHA * (raw_intensity - smoothed_intensity)

                wet_depth = WET_DEPTH_MIN + smoothed_intensity * (WET_DEPTH_MAX - WET_DEPTH_MIN)

                wl = WET_BASE + wet_depth * math.sin(phase)
                wr = WET_BASE + wet_depth * math.sin(phase + math.pi / 2)
                cdsp.volume.set_volume(1, wl)
                cdsp.volume.set_volume(2, wr)

                time.sleep(UPDATE_INTERVAL)

        except Exception as e:
            print(f"接続エラー: {e}")
            time.sleep(3)


main()
