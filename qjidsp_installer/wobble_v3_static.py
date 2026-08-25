import time
from camilladsp import CamillaClient

def main():
    cdsp = CamillaClient("127.0.0.1", 1234)
    print("静的モード（揺らぎなし）...")
    while True:
        try:
            cdsp.connect()
            print("接続成功")
            while True:
                time.sleep(5)
        except Exception as e:
            print(f"接続エラー、3秒後に再試行: {e}")
            time.sleep(3.0)

main()
