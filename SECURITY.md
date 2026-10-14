# セキュリティポリシー / Security Policy

QjiDSP は、個人が開発し、無償で公開している音楽プレーヤーです。
QjiDSP is a music player developed by an individual and released free of charge.

---

## 日本語

### 脆弱性を見つけたら

**公開の Issue には書かないでください。** 悪用される恐れがあります。
このリポジトリの **「Security」タブ →「Report a vulnerability」** から、非公開で報告してください。

報告には、次の内容を書いていただけると助かります。

- 何が起きるか(影響)と、再現の手順
- 環境(OS、QjiDSP のバージョン、起動の仕方)

個人で開発しているため、お返事に数日かかることがあります。できる範囲で、誠実に対応します。

### 対応するバージョン

最新のリリースのみです。古いバージョンをお使いの場合は、更新をお願いします。

### 安全にお使いいただくために

- **ご家庭のネットワークでお使いください。** Now Playing ミラー、AirPlay、UPnP/DLNA は、同じネットワークの機器から接続できます。ゲスト Wi-Fi や、公共のネットワークでは使わないでください。ルーターのポート開放(外部への公開)も、しないでください。
- **Now Playing ミラーの暗証番号:** 起動すると、ターミナルに URL と、数字だけ6けたの暗証番号(PIN)が表示されます。スマートフォンで URL を開き、暗証番号を入力してください(`qrcode` ライブラリが入っていれば、読み取るだけで開ける QR コードも表示されます)。暗証番号は、起動のたびに変わります。他の人に教えないでください。入力を何度も間違えると、しばらくロックされます。暗証番号を使わない従来の動作に戻すことも、LAN の外からの接続を許可することもできますが、おすすめしません。
- **ジャケット選択ページ**は、お使いのパソコンだけから開けます(`127.0.0.1`)。
- **ffmpeg、shairport-sync、gmediarender、CamillaDSP** は、最新のものをお使いください。音源やネットワークからのデータを処理するので、これらの更新が、いちばん効果のある対策です。
- **管理者(root)で起動しないでください。** 管理者権限が必要なのは、インストーラーを実行するときだけです。
- **ダウンロードは、公式のリポジトリから**行ってください。
- **認証情報:** Qobuz などのトークンは、`~/.config/` の設定ファイル(自分だけが読める権限)に保存されます。他の人に見せないでください。漏れた可能性があるときは、パスワードの変更や、トークンの再発行をしてください。

### 免責

無償のソフトウェアです。ライセンスに記載のとおり、動作や安全性を保証するものではありません。

---

## English

### If you find a vulnerability

**Please do not open a public issue** — it could be abused.
Report it privately through this repository's **"Security" tab → "Report a vulnerability"**.

It helps if you include:

- What happens (the impact) and how to reproduce it
- Your environment (OS, QjiDSP version, how you started it)

This is a one-person project, so replies may take a few days. I will respond in good faith, as far as I can.

### Supported versions

Only the latest release. If you are on an older version, please update.

### Using QjiDSP safely

- **Use it on your home network.** The Now Playing mirror, AirPlay and UPnP/DLNA accept connections from devices on the same network. Do not use them on guest Wi-Fi or public networks, and do not forward ports on your router.
- **Now Playing mirror PIN:** when started, the terminal prints a URL and a 6-digit numeric PIN. Open the URL on your phone and enter the PIN (if the `qrcode` library is installed, a QR code you can simply scan is shown as well). The PIN changes every time you start it. Do not share it. After too many wrong attempts it locks for a while. You can switch back to the old no-PIN behaviour or allow connections from outside your LAN, but this is not recommended.
- **The cover-selection page** can be opened only from your own computer (`127.0.0.1`).
- **Keep ffmpeg, shairport-sync, gmediarender and CamillaDSP up to date.** They process data from audio files and the network, so updating them is the most effective protection.
- **Do not run QjiDSP as root.** Administrator rights are needed only when running the installer.
- **Download only from the official repository.**
- **Credentials:** tokens for services such as Qobuz are stored in a config file under `~/.config/` (readable only by you). Do not show it to anyone. If it may have leaked, change your password or reissue the token.

### Disclaimer

This is free software. As stated in the license, there is no warranty of operation or security.
