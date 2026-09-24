from flask import Flask, render_template, request, jsonify
from tsum_core import forge_sync, forge_sync_guest

app = Flask(__name__, template_folder=".")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/execute', methods=['POST'])
def execute():
    data = request.json or {}
    mode = data.get('mode', 'normal')

    if mode == 'normal':
        res, msg = forge_sync(
            login_id=data.get('login_id'),
            password=data.get('password'),
            coin=data.get('coin'),
            score=data.get('score'),
            exp=data.get('exp'),
            tsum_lv=data.get('tsum_lv'),
            box=data.get('box'),
            gacha_full=data.get('gacha_full'),
            tsumid=data.get('tsumid', 860),
            proxy=data.get('proxy')
        )
        return jsonify({'message': msg})
    else:
        res, msg, info = forge_sync_guest(
            migration_id=data.get('migration_id'),
            password=data.get('password'),
            coin=data.get('coin'),
            score=data.get('score'),
            exp=data.get('exp'),
            tsum_lv=data.get('tsum_lv'),
            box=data.get('box'),
            gacha_full=data.get('gacha_full'),
            tsumid=data.get('tsumid', 860),
            proxy=data.get('proxy')
        )
        return jsonify({'message': msg})

if __name__ == '__main__':
    
    # Renderから割り当てられるPORTを取得（ローカルの場合は5000）
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
