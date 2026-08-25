import subprocess, time, os

HOME = os.path.expanduser("~")
CDSP_YML = f"{HOME}/camilladsp_test/spatial_final.yml"
CDSP_PORT = 1234
CHECK_INTERVAL = 5

def is_camilladsp_running():
    result = subprocess.run(['pgrep', '-f', 'camilladsp'], capture_output=True)
    return result.returncode == 0

def restart_camilladsp():
    print("💀 CamillaDSP停止検出 → 再起動します...")
    subprocess.run(['pkill', '-9', '-f', 'camilladsp'], capture_output=True)
    time.sleep(2.0)
    log = open('/tmp/camilladsp.log', 'a')
    subprocess.Popen(
        ['camilladsp', CDSP_YML, '--port', str(CDSP_PORT)],
        stdout=log, stderr=log
    )
    time.sleep(3.0)
    print("✅ CamillaDSP再起動完了")

print("🐕 CamillaDSP ウォッチドッグ開始...")
while True:
    if not is_camilladsp_running():
        restart_camilladsp()
    time.sleep(CHECK_INTERVAL)
