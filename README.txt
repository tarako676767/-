■ セットアップ手順

【必要なもの】
- Python 3.10以上
- Tor（別途起動が必要）

【Torのインストール】
Windows: https://www.torproject.org/download/ からダウンロード
Mac:     brew install tor
Linux:   sudo apt install tor

【Torの起動】
Windows: Expert Bundle を解凍して tor.exe を起動
Mac/Linux: tor &

【botの起動】
Windows: start.bat をダブルクリック
Mac/Linux: bash start.sh

【bot_config.jsonの設定】
- token: Discord Botのトークン
- guild_id: サーバーID
- ticket_channel_id: チケットチャンネルID
- public_result_channel_id: 結果通知チャンネルID

【注意】
- bot起動前に必ずTorを起動してください
- bot_config.jsonのtokenは他人に渡さないでください
