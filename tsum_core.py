import sys
import os
import io
import json
import functools
import traceback
import re
import time
import random
import unicodedata
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tsum_login
import line_password_login
import tsum
import tsum_guest
from tsum_forge import Forge

# ==========================================
# main13.py と共通のHTML読み込み・管理処理
# ==========================================
HTML_FILE_PATH = os.path.join(HERE, "index.html")

def load_html_content():
    """main13.py と同様に外部の index.html を読み込む処理"""
    if os.path.exists(HTML_FILE_PATH):
        try:
            with open(HTML_FILE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[tsum] HTML読み込みエラー: {e}")
            return "<h1>HTMLの読み込みに失敗しました</h1>"
    return "<h1>index.html が見つかりません</h1>"

# 設定ファイルのロード
_cfg_name = os.environ.get("TSUM_BOT_CONFIG", "bot_config.json")
CONFIG_FILE = _cfg_name if os.path.isabs(_cfg_name) else os.path.join(HERE, _cfg_name)
print(f"[tsum] 設定ファイル: {os.path.basename(CONFIG_FILE)}")
CREDS_FILE = os.path.join(HERE, "line_credentials.json")
TOKENS_FILE = os.path.join(HERE, "line_tokens_latest.json")
SESSION_FILE = os.path.join(HERE, "tsum_session_headless.json")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        return json.load(open(CONFIG_FILE, encoding="utf-8"))
    except Exception:
        return {}


CONFIG = load_config()
try:
    tsum_login.resolve_appver()
except Exception:
    pass
print(f"[tsum] appver: {tsum_login.APPVER}")

SALES_FILE = os.path.join(HERE, CONFIG.get("sales_file", "tsum_sales.json"))
FREE_USED_FILE = os.path.join(HERE, CONFIG.get("free_used_file", "tsum_free_used.json"))


def save_config():
    json.dump(CONFIG, open(CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def _dbg(msg):
    try:
        with open(os.path.join(HERE, "tsum_debug.log"), "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def record_sale(user_id, action, amount):
    data = _jload(SALES_FILE, [])
    data.append({"ts": int(time.time()), "user_id": int(user_id), "action": action, "amount": int(amount)})
    try:
        json.dump(data, open(SALES_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def sales_summary():
    import datetime
    data = _jload(SALES_FILE, [])
    JST = datetime.timezone(datetime.timedelta(hours=9))
    today0 = datetime.datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    now = time.time()

    def agg(since):
        rows = [d for d in data if d.get("ts", 0) >= since]
        return sum(int(d.get("amount", 0)) for d in rows), len(rows)

    return {
        "today": agg(today0),
        "week": agg(now - 7 * 86400),
        "month": agg(now - 30 * 86400),
        "total": (sum(int(d.get("amount", 0)) for d in data), len(data)),
    }


def reset_sales():
    try:
        json.dump([], open(SALES_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def _free_key_account(login_id):
    s = unicodedata.normalize("NFKC", str(login_id or "")).strip().lower()
    s = re.sub(r"[\s\-()]", "", s)
    return "a:" + s if s else ""


def free_unlimited_enabled():
    return bool(CONFIG.get("free_unlimited_enabled", True))


def free_is_unlimited(user_id):
    if user_id is None or not free_unlimited_enabled():
        return False
    try:
        return int(user_id) in [int(x) for x in (CONFIG.get("free_unlimited_ids") or [])]
    except Exception:
        return False


def free_can_use(user_id=None, login_id=None, game_userid=None, days=30):
    data = _jload(FREE_USED_FILE, {})
    now = time.time()
    keys = []
    if user_id is not None and not free_is_unlimited(user_id):
        keys += [f"u:{user_id}", str(user_id)]
    if login_id:
        keys.append(_free_key_account(login_id))
    if game_userid:
        keys.append(f"g:{game_userid}")
    for k in keys:
        if not k:
            continue
        last = data.get(k)
        if last and (now - last) <= days * 86400:
            return False
    return True


def _free_write(mutate):
    data = _jload(FREE_USED_FILE, {})
    mutate(data)
    try:
        json.dump(data, open(FREE_USED_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def free_mark_used(user_id=None, login_id=None, game_userid=None):
    now = time.time()

    def _m(data):
        if user_id is not None and not free_is_unlimited(user_id):
            data[f"u:{user_id}"] = now
        if login_id:
            k = _free_key_account(login_id)
            if k:
                data[k] = now
        if game_userid:
            data[f"g:{game_userid}"] = now

    _free_write(_m)


def free_unmark_used(user_id=None, login_id=None, game_userid=None):
    def _m(data):
        if user_id is not None:
            data.pop(f"u:{user_id}", None)
            data.pop(str(user_id), None)
        if login_id:
            k = _free_key_account(login_id)
            if k:
                data.pop(k, None)
        if game_userid:
            data.pop(f"g:{game_userid}", None)

    _free_write(_m)


def is_guest_account(login_id):
    s = unicodedata.normalize("NFKC", (login_id or "")).strip()
    if "@" in s:
        return False
    digits = re.sub(r"\D+", "", s)
    if digits.startswith("0081"):
        digits = "0" + digits[4:]
    elif digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    has_alpha = any(c.isalpha() for c in s)
    if not has_alpha and digits.startswith("0") and 10 <= len(digits) <= 11:
        return False
    return True


def clear_tsum_login_files(preserve_proxy=True):
    removed = []
    for path in (CREDS_FILE, TOKENS_FILE, SESSION_FILE):
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except Exception as e:
                print(f"[login] cleanup failed {path}: {e}")
    return removed


def save_input_account(login_id, password):
    clear_tsum_login_files(preserve_proxy=False)
    cred = {"id": login_id, "password": password}
    json.dump(cred, open(CREDS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return []


def improve_captcha_image(image_bytes):
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except Exception:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes or b"")).convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        white.alpha_composite(img)
        img = white.convert("L")
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(1.8)
        img = ImageEnhance.Sharpness(img).enhance(1.4)
        img = img.resize((img.width * 3, img.height * 3), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        print(f"[captcha] improve failed: {e}")
        return image_bytes


def _failure_hint_from_response(resp):
    if not isinstance(resp, dict):
        return "原因を特定できませんでした"
    retcode = resp.get("retcode")
    retsubcode = resp.get("retsubcode")
    msg = " ".join(str(resp.get(k, "")) for k in ("retmsg", "message", "errmsg", "error"))
    low = msg.lower()
    if retcode == 8 and retsubcode == 2:
        return "ゲーム開始時の入力パラメータが不正です"
    if "heart" in low or "ハート" in msg:
        return "ハート不足、またはプレイ開始条件が足りていません"
    if any(k in low for k in ("session", "token", "checkval", "hash", "auth", "login")):
        return "ログイン状態が途中で無効になりました"
    if any(k in low for k in ("version", "appver", "resver", "update")) or "更新" in msg:
        return "アプリのバージョン情報が合っていません"
    if any(k in low for k in ("maintenance", "maint")) or "メンテ" in msg:
        return "ゲーム側がメンテナンス中の可能性があります"
    if any(k in low for k in ("ban", "block", "restrict", "limit", "forbidden")) or any(
        k in msg for k in ("制限", "拒否", "禁止")
    ):
        return "通信またはアカウントが制限されています"
    if "tsum" in low or "ツム" in msg:
        return "ツム設定が合っていない可能性があります"
    if retcode not in (None, 0, "0"):
        return "ゲーム側が処理を拒否しました"
    return "必要なデータが返ってきませんでした"


def describe_tsum_api_failure(stage, resp=None, forge=None, missing_data=False):
    status = getattr(forge, "last_http_status", None)
    error = getattr(forge, "last_error", None)

    if error == "proxy_error":
        return f"{stage}に失敗しました（VPN/プロキシ接続エラー）。VPN設定と接続先を確認して、もう一度お試しください。"
    if error == "timeout":
        return f"{stage}に失敗しました（通信タイムアウト）。VPNまたは回線が不安定な可能性があります。"
    if error == "network_error":
        return f"{stage}に失敗しました（通信エラー）。VPN、回線、接続先IPを確認してください。"
    if status in (401, 403):
        return f"{stage}に失敗しました（通信が拒否されました）。VPNのIP、アカウント制限、ログイン状態のどれかが原因の可能性があります。"
    if status == 404:
        return f"{stage}に失敗しました（接続先が見つかりません）。アプリのバージョン情報が合っていない可能性があります。"
    if status and status >= 500:
        return f"{stage}に失敗しました（ゲーム側の応答エラー）。少し待ってからもう一度お試しください。"
    if error == "decrypt_failed":
        return f"{stage}に失敗しました（ゲーム側の応答を読み取れませんでした）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"
    if isinstance(resp, str):
        return f"{stage}に失敗しました（想定外の応答）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"

    hint = _failure_hint_from_response(resp)
    if missing_data and hint == "必要なデータが返ってきませんでした":
        return f"{stage}に失敗しました（開始用データが返ってきませんでした）。ログイン状態、VPN/IP、アカウント状態を確認してください。"
    return f"{stage}に失敗しました（{hint}）。もう一度お試しください。"


def describe_login_failure(reason):
    reason = str(reason or "").strip()
    low = reason.lower()
    if "PIN" in reason.upper() or "pincode" in low or "端末認証" in reason:
        return "LINEの本人確認（PIN）が完了しませんでした。LINEアプリで番号を入力してから、もう一度お試しください"
    if "captcha" in low or "画像認証" in reason:
        return (
            "画像認証が繰り返し出てログインできませんでした。LINE側で一時的に制限がかかっている可能性が高いので、"
            "20〜30分ほど時間を置いてからお試しください（続けて試すと制限が延びることがあります）。"
        )
    if "通信エラー" in reason or "接続が不安定" in reason or "プロキシ" in reason:
        return "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください"
    if "アカウント情報が違う" in reason or "errorcode=445" in low:
        return "メールアドレスかパスワードが違います"
    if (
        "中断されました" in reason
        or "追加認証" in reason
        or "一時制限" in reason
        or "連続試行" in reason
        or "errorcode=446" in low
        or "errorcode=401" in low
    ):
        return "LINE側で一時的にログイン制限中です。時間をおいて再度お試しください"
    return "メールアドレスかパスワードが違います"


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_owned_tsum(row):
    if not isinstance(row, dict):
        return False
    if row.get("setflg") in (1, "1", True):
        return True
    for key in ("getflg", "ownflg", "haveflg", "possessionflg"):
        if row.get(key) in (1, "1", True):
            return True
    return _to_int(row.get("lv") or row.get("level")) > 0


def select_play_tsumids(forge, requested_tsumid=None):
    requested = _to_int(requested_tsumid)
    info = forge.get_info()
    if not isinstance(info, dict):
        return None, None, describe_tsum_api_failure("ツム情報取得", info, forge)

    tsum_rows = [t for t in (info.get("tsuminfo") or []) if isinstance(t, dict)]
    owned = [t for t in tsum_rows if _is_owned_tsum(t) and _to_int(t.get("tsumid"))]
    candidates = []

    def add_candidate(tsumid):
        tsumid = _to_int(tsumid)
        if tsumid and tsumid not in candidates:
            candidates.append(tsumid)

    if requested:
        for t in owned:
            if _to_int(t.get("tsumid")) == requested:
                add_candidate(requested)

    for t in owned:
        if t.get("setflg") in (1, "1", True):
            add_candidate(t.get("tsumid"))
    if owned:
        for t in owned:
            add_candidate(t.get("tsumid"))
    if candidates:
        return candidates[:20], info, None
    return None, info, "使用できるツムが見つかりませんでした。ゲームを開いてマイツムをセットしてから、もう一度お試しください。"


def heart_count_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    return _to_int(ui.get("bheart")) + _to_int(ui.get("pheart"))


def heart_state_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    bheart = _to_int(ui.get("bheart"))
    pheart = _to_int(ui.get("pheart"))
    return bheart, pheart, bheart + pheart


def hearttype_candidates_from_info(info):
    bheart, pheart, total = heart_state_from_info(info)
    if total < 1:
        return []
    candidates = []
    if bheart > 0:
        candidates.append(0)
    if pheart > 0:
        candidates.append(1)
    for value in (0, 1):
        if value not in candidates:
            candidates.append(value)
    return candidates


_EVENT_CACHE = {"ts": 0.0, "cands": None}


def _cached_event_candidates(f, ttl=900):
    now = time.time()
    if _EVENT_CACHE["cands"] and (now - _EVENT_CACHE["ts"] < ttl):
        return _EVENT_CACHE["cands"]
    cands = f.event_candidates()
    if cands:
        _EVENT_CACHE["cands"] = cands
        _EVENT_CACHE["ts"] = now
    return cands


_GACHA_CACHE = {"ts": 0.0, "ids": None}


def _cached_gacha_ids(f, ttl=600):
    now = time.time()
    if _GACHA_CACHE["ids"] and (now - _GACHA_CACHE["ts"] < ttl):
        return _GACHA_CACHE["ids"]
    m = f.get_mast()
    if not isinstance(m, dict) or m.get("retcode") not in (0, None):
        return None
    rows = m.get("gachamst")
    if not isinstance(rows, list) or not rows:
        return None
    by_type = {}
    for g in rows:
        if not isinstance(g, dict):
            continue
        try:
            t = int(g.get("type"))
            gid = int(g.get("gachaid"))
        except Exception:
            continue
        by_type.setdefault(t, gid)
    box_id = by_type.get(41)
    if not box_id and CONFIG.get("box_fallback_premium_box"):
        box_id = by_type.get(64)
    ids = {"premium": by_type.get(11), "box": box_id, "has_select_box": bool(by_type.get(41))}
    if ids["premium"] or ids["box"]:
        _GACHA_CACHE["ids"] = ids
        _GACHA_CACHE["ts"] = now
    return ids


def resolve_gacha_id(f, kind, fallback):
    try:
        ids = _cached_gacha_ids(f)
        if ids is not None:
            gid = ids.get(kind)
            if gid:
                if int(gid) != int(fallback):
                    _dbg(f"[gacha] {kind}: config={fallback} → 現行={gid} に自動切替")
                return int(gid), "auto"
            _dbg(f"[gacha] {kind}: 現在開催されていません(config={fallback}は古い可能性)")
            return None, "not_running"
    except Exception as e:
        print(f"[gacha] 自動取得に失敗→configの値を使用: {type(e).__name__}", flush=True)
    return int(fallback), "fallback"


def forge_sync(
    score=None,
    coin=None,
    exp=None,
    medal=5,
    tsumid=860,
    tsum_lv=None,
    box=None,
    login_id=None,
    password=None,
    captcha_solver=None,
    proxy=None,
    sess=None,
    f=None,
    pin_notice=None,
    gacha_full=None,
    on_login=None,
):
    if sess is None or f is None:
        if not login_id or not password:
            return None, "アカウント情報を入力してください。"
        sess = None
        login_error = {}
        pin_state = {"shown": False}

        def _pin_relay(pincode):
            pin_state["shown"] = True
            if pin_notice:
                try:
                    pin_notice(pincode)
                except Exception:
                    pass

        captcha_state = {"shown": False}

        def _captcha_relay(img_bytes):
            captcha_state["shown"] = True
            return captcha_solver(img_bytes) if captcha_solver else ""

        for _try in range(3):
            login_error = {}
            try:
                sess = tsum_login.headless_session_direct(
                    login_id,
                    password,
                    proxy=proxy,
                    captcha_solver=_captcha_relay,
                    save=False,
                    verbose=False,
                    error_box=login_error,
                    pin_notice=_pin_relay,
                )
            except (requests.exceptions.RequestException, ValueError) as e:
                sess = None
                _dbg(f"[login例外] id={str(login_id)[:3]}*** try={_try+1} {type(e).__name__}: {str(e)[:150]}")
                if _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                    time.sleep(0.6)
                    continue
                return None, "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください。"
            if sess:
                if on_login:
                    try:
                        on_login(str(sess.get("userid") or ""))
                    except Exception:
                        pass
                break
            _raw = login_error.get("error") or ""
            _dbg(f"[login失敗] id={str(login_id)[:3]}*** try={_try+1} raw={_raw!r}")
            low = _raw.lower()
            ip_blocked = (
                "errorcode=446" in low
                or "errorcode=401" in low
                or "中断されました" in _raw
                or "プロキシ接続エラー" in _raw
                or "通信エラー" in _raw
            )
            if ip_blocked and _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                continue
            return None, describe_login_failure(_raw)
        if not sess:
            return None, describe_login_failure(login_error.get("error"))
        f = Forge(userid=sess["userid"], hashv=sess["hash"], checkval=sess["checkval"], proxy=proxy)
        try:
            f.get_public_key()
        except Exception as e:
            _dbg(f"[getPublicKey例外] {type(e).__name__}: {str(e)[:150]}")
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"

    if tsum_lv is not None:
        tid = int(tsum_lv)
        if tid == 0:
            info = f.get_info()
            if not isinstance(info, dict):
                return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
            for t in info.get("tsuminfo") or []:
                if t.get("setflg") in (1, "1"):
                    tid = t.get("tsumid")
                    break
            if not tid:
                return None, "セット中のマイツムが見つかりませんでした。ゲームでマイツムをセットしてから、もう一度お試しください。"
        res = f.max_tsum_level(tid, target=50, min_heart=10, sleep_s=0.3, log=None)
        st = res.get("status")
        if st == "done":
            return res, "セット中のツムをレベルMAXにしました\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
        if st == "insufficient_heart":
            return (
                None,
                f"ハートが足りないため中断しました（所持 {res['have']} / 必要 {res['need']}）。ハートを貯めてからもう一度お試しください。",
            )
        if st == "heart_ran_out":
            return None, "ハートが切れたため途中で終了しました。ハートを補充すると続きから実行できます。"
        if st == "stuck":
            return None, "途中で中断しました。もう一度お試しください。"
        return None, "ツムのレベルMAXに失敗しました。もう一度お試しください。"

    if box is not None:
        need = 30_000_000
        box, _st = resolve_gacha_id(f, "box", box)
        if _st == "not_running":
            return None, "現在セレクトBOXが開催されていないため実行できません。"
        res = f.complete_box(int(box), min_coin=need, sleep_s=0, log=None)
        st = res.get("status")
        if st in ("complete", "already_complete"):
            return res, f"セレクトBOXを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return (
                None,
                "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。",
            )
        if st == "insufficient_coin":
            return (
                None,
                f"コインが足りないため中断しました（所持 {res['have']:,} / 必要 {res['need']:,}）。先にコインを増やしてからお試しください。",
            )
        if st == "coin_ran_out":
            return None, "コインが切れたため途中で終了しました。もう一度お試しください。"
        return None, "セレクトBOXの完売に失敗しました。もう一度お試しください。"

    if gacha_full is not None:
        gacha_full, _st = resolve_gacha_id(f, "premium", gacha_full)
        if _st == "not_running":
            return None, "現在プレミアムガチャが開催されていないため実行できません。"
        res = f.complete_box(int(gacha_full), min_coin=300_000, sleep_s=0, log=None)
        st = res.get("status")
        if st in ("complete", "already_complete"):
            return (
                res,
                f"プレミアムガチャを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。",
            )
        if st == "network_error":
            return (
                None,
                "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。",
            )
        if st == "insufficient_coin":
            return None, f"コインが足りません（所持 {res['have']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, f"コインが足りず途中で終了しました（{res.get('draws','-')}連）。コインを増やしてからお試しください。"
        return None, "プレミアムガチャの完売に失敗しました。もう一度お試しください。"

    candidates, _info, _err = select_play_tsumids(f, tsumid)
    tsum_candidates = list(candidates or [])[:3]
    if isinstance(_info, dict):
        for t in _info.get("tsuminfo") or []:
            tid = _to_int(t.get("tsumid")) if isinstance(t, dict) else 0
            if tid and tid not in tsum_candidates and len(tsum_candidates) < 5:
                tsum_candidates.append(tid)
    if tsumid and tsumid not in tsum_candidates:
        tsum_candidates.append(tsumid)
    if not tsum_candidates:
        tsum_candidates = [tsumid]

    ht_candidates = hearttype_candidates_from_info(_info) or [0, 1]
    bheart, pheart, total_heart = heart_state_from_info(_info)
    _set_tsum = (
        next(
            (
                t.get("tsumid")
                for t in (_info.get("tsuminfo") or [])
                if isinstance(t, dict) and t.get("setflg") in (1, "1", True)
            ),
            None,
        )
        if isinstance(_info, dict)
        else None
    )
    uid = str(sess.get("userid") or "")
    try:
        ev_order = _cached_event_candidates(f)
    except Exception as _e:
        ev_order = ["9999", "0"]
        print(f"[gs] event_candidates失敗→fallback: {_e}", flush=True)
    if not ev_order:
        ev_order = ["9999", "0"]
    _dbg(
        f"[gs v7] userid={uid[:3]}… set_tsum={_set_tsum} bheart={bheart} pheart={pheart} "
        f"ht={ht_candidates} tsum候補={tsum_candidates[:4]} eventid候補={ev_order}"
    )

    r1 = None
    playcode = None
    won_tid = None
    won_ht = None
    won_ev = None
    attempts = []

    def _attempt(tid, ht, ev):
        nonlocal r1, playcode, won_tid, won_ht, won_ev
        r1 = f.game_start(tsumid=tid, hearttype=ht, probmstver="2", eventid=ev)
        rc = (r1.get("retcode"), r1.get("retsubcode")) if isinstance(r1, dict) else None
        attempts.append((tid, ht, f"ev{ev}", rc))
        playcode = (
            (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode") if isinstance(r1, dict) else None
        )
        if playcode:
            won_tid = tid
            won_ht = ht
            won_ev = ev
        return playcode

    play_tids = []
    for t in [_set_tsum] + list(tsum_candidates) + [tsumid]:
        if t and t not in play_tids:
            play_tids.append(t)
    for tid in play_tids[:4]:
        for ht in ht_candidates:
            for ev in ev_order:
                if _attempt(tid, ht, ev):
                    break
                time.sleep(0.15)
            if playcode:
                break
        if playcode:
            break
    before_coin = (r1.get("userinfo") or {}).get("bcoin") if isinstance(r1, dict) else None
    if not playcode:
        _EVENT_CACHE["cands"] = None
        try:
            ui = (_info.get("userinfo") or {}) if isinstance(_info, dict) else {}
            set_tsum = (
                next(
                    (
                        t.get("tsumid")
                        for t in (_info.get("tsuminfo") or [])
                        if isinstance(t, dict) and t.get("setflg") in (1, "1", True)
                    ),
                    None,
                )
                if isinstance(_info, dict)
                else None
            )
            dump = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "userid": sess.get("userid"),
                "hashlen": len(str(sess.get("hash") or "")),
                "checkval_present": bool(sess.get("checkval")),
                "getInfo_retcode": (
                    _info.get("retcode") if isinstance(_info, dict) else f"non-dict({type(_info).__name__})"
                ),
                "getInfo_keys": sorted(_info.keys()) if isinstance(_info, dict) else None,
                "userinfo": ui,
                "n_tsum": (
                    len([t for t in (_info.get("tsuminfo") or []) if isinstance(t, dict)])
                    if isinstance(_info, dict)
                    else 0
                ),
                "set_tsum": set_tsum,
                "sample_tsums": (
                    [t for t in ((_info.get("tsuminfo") or [])[:5]) if isinstance(t, dict)]
                    if isinstance(_info, dict)
                    else []
                ),
                "attempts": [list(a) for a in attempts],
                "gameStart_response": r1 if isinstance(r1, dict) else repr(r1),
            }
            with open(os.path.join(HERE, "tsum_gs_fulldump.json"), "w", encoding="utf-8") as df:
                json.dump(dump, df, ensure_ascii=False, indent=2)
            with open(os.path.join(HERE, "tsum_gs_diag.log"), "a", encoding="utf-8") as lf:
                lf.write(
                    f"{dump['ts']} GS_FAIL userid={dump['userid']} getInfo_rc={dump['getInfo_retcode']} "
                    f"n_tsum={dump['n_tsum']} set_tsum={set_tsum} bheart={bheart} pheart={pheart} "
                    f"attempts={attempts}  (full -> tsum_gs_fulldump.json)\n"
                )
        except Exception as _e:
            print(f"[gs_diag] dump failed: {_e}")
        if isinstance(_info, dict) and total_heart < 1:
            return None, "ハートが足りないため開始できませんでした。ハートが回復してから、もう一度お試しください。"
        return None, describe_tsum_api_failure("ゲーム開始", r1, f, missing_data=True)
    vanishcnt = f"{won_tid},0|9,0|12,0|309,0|4,0|" if won_tid else None

    credited = 0
    games = 0
    chunk_field = "exp" if exp is not None else None
    target = int(exp) if exp is not None else 0
    if chunk_field:
        chunk = int(CONFIG.get(f"{chunk_field}_per_game", 40_000_000))
        r2 = None
        pc = playcode
        while credited < target and games < 30:
            send = min(chunk, target - credited)
            if pc is None:
                rs = f.game_start(tsumid=won_tid, hearttype=won_ht, probmstver="2", eventid=won_ev)
                pc = (
                    (rs.get("userinfo") or {}).get("playcode") or rs.get("playcode")
                    if isinstance(rs, dict)
                    else None
                )
                if not pc:
                    break
            ge = dict(
                score=score if score is not None else 95000,
                coin=61,
                medal=medal,
                exp=120,
                vanishcnt=vanishcnt,
            )
            ge[chunk_field] = send
            rr = f.game_end(pc, **ge)
            pc = None
            if isinstance(rr, dict) and rr.get("retcode") == 0:
                r2 = rr
                credited += send
                games += 1
            elif chunk > 1_000_000:
                chunk //= 2
            else:
                break
            time.sleep(0.2)
        _dbg(f"[{chunk_field}] target={target} credited={credited} games={games} last_chunk={chunk}")
        if credited <= 0 or r2 is None:
            return None, "送信に失敗しました。もう一度試してください。"
    else:
        r2 = f.game_end(
            playcode,
            score=score if score is not None else 95000,
            coin=coin if coin is not None else 61,
            medal=medal,
            exp=120,
            vanishcnt=vanishcnt,
        )
        if not isinstance(r2, dict) or r2.get("retcode") != 0:
            return None, "送信に失敗しました。もう一度試してください。"
    ui = r2.get("userinfo") or {}
    after_coin = ui.get("bcoin")
    lines = []
    if coin is not None:
        if before_coin is not None and after_coin is not None:
            lines.append(f"コイン: {before_coin:,} → {after_coin:,} ( +{after_coin - before_coin:,} )")
        else:
            lines.append(f"コイン: +{coin:,}")
    if score is not None:
        lines.append(f"スコア: {score:,}")
    if exp is not None:
        lines.append("プレイヤーレベルMAX" if credited >= target else f"経験値を付与しました（{credited:,}）")
    lines.append("完了しました。")
    lines.append("またのご利用をお待ちしております。")
    return r2, "\n".join(lines)


def _proxies_dict(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def _save_orphan_guest(sess, migration_id):
    try:
        path = os.path.join(HERE, "tsum_guest_orphans.json")
        data = []
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8")) or []
            except Exception:
                data = []
        data.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "orig_migration_id": migration_id,
                "uuid": sess.get("uuid"),
                "userToken": sess.get("userToken"),
                "userid": sess.get("userid"),
                "hash": sess.get("hash"),
                "checkval": sess.get("checkval"),
                "migrated_userKey": sess.get("migrated_userKey"),
            }
        )
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(
            f"[guest] orphan session saved -> {path} (uuid={sess.get('uuid')} userid={sess.get('userid')})"
        )
    except Exception:
        traceback.print_exc()


def forge_sync_guest(
    migration_id,
    password,
    score=None,
    coin=None,
    exp=None,
    medal=5,
    tsumid=860,
    tsum_lv=None,
    box=None,
    proxy=None,
    gacha_full=None,
    **_ignore,
):
    if not migration_id or not password:
        return None, "引き継ぎ番号とパスワードを入力してください。", {"moved": False}
    proxies = _proxies_dict(proxy)
    try:
        sess, err = tsum_guest.guest_takeover(migration_id, password, proxies)
    except Exception:
        traceback.print_exc()
        return None, "引き継ぎ処理でエラーが発生しました。番号とパスワードを確認して、もう一度お試しください。", {"moved": False}
    if not sess:
        return None, (err or "引き継ぎに失敗しました（番号かパスワードが違う/期限切れ）。"), {"moved": False}

    info = {"moved": True, "new_id": None, "new_pw": None}
    result_obj, text = None, ""
    try:
        f = Forge(
            userid=sess["userid"],
            hashv=sess.get("hash", ""),
            checkval=sess.get("checkval", ""),
            proxy=proxy,
        )
        f.get_public_key()
        result_obj, text = forge_sync(
            score=score,
            coin=coin,
            exp=exp,
            medal=medal,
            tsumid=tsumid,
            tsum_lv=tsum_lv,
            box=box,
            proxy=proxy,
            sess=sess,
            f=f,
            gacha_full=gacha_full,
        )
    except Exception:
        traceback.print_exc()
        try:
            with open(os.path.join(HERE, "guest_forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write(
                    "\n==== guest盛りエラー coin=%s score=%s exp=%s box=%s gacha_full=%s tsum_lv=%s ====\n"
                    % (coin, score, exp, box, gacha_full, tsum_lv)
                )
                _lf.write(traceback.format_exc())
        except Exception:
            pass
        result_obj, text = None, "盛り処理でエラーが発生しました。"

    new_id = new_pw = None
    for _ in range(3):
        try:
            nid, npw = tsum_guest.guest_issue_transfer(sess, proxies)
        except Exception:
            traceback.print_exc()
            nid, npw = None, None
        if nid:
            new_id, new_pw = nid, npw
            break
        time.sleep(0.4)
    info["new_id"], info["new_pw"] = new_id, new_pw

    if new_id:
        text += (
            "\n\n──────────\n**新しい引き継ぎ情報**\n"
            f"引き継ぎ番号: `{new_id}`\nパスワード: `{new_pw}`\n"
            "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。必ず控えてください。"
        )
    else:
        _save_orphan_guest(sess, migration_id)
        text += (
            "\n\n──────────\n⚠️新しい引き継ぎ情報の発行に失敗しました。"
            "この画面をスクリーンショットして管理者にご連絡ください（管理者側で復旧できます）。"
        )
    return result_obj, (text or "処理に失敗しました。"), info


def status_text():
    label, cid = tsum.current_label()
    ip = "?"
    region = ""
    try:
        import requests

        ip = requests.get("https://api.ipify.org", timeout=25).text.strip()
        try:
            j = requests.get(
                f"http://ip-api.com/json/{ip}?fields=country,regionName&lang=ja", timeout=10
            ).json()
            region = " ".join(x for x in (j.get("country"), j.get("regionName")) if x)
        except Exception:
            pass
    except Exception:
        ip = "?"
    st, info = tsum.tsum_status_check_token()
    try:
        cap, recap = tsum.check_captcha()
    except Exception:
        cap, recap = "?", "?"
    tok = "有効" if st == "ok" else info
    capj = "reCAPTCHA)" if recap == "true" else ("画像CAPTCHA" if cap == "true" else "無し")
    ip_line = f"接続元IP: `{ip}`" + (f"（{region}）" if region else "")
    return (
        f"**状態**\n{ip_line}\nアカウント: {label} (`{cid}`)\n"
        f"トークン: {tok}\nログイン障壁: {capj}"
    )


def tsum_login_debug_text():
    try:
        sess = tsum_login.headless_session(save=False, verbose=False)
    except Exception as exc:
        return f"ログイン処理で例外が発生しました: `{type(exc).__name__}`"

    if not isinstance(sess, dict):
        reason = getattr(tsum_login, "LAST_ERROR", "") or "セッションを取得できませんでした"
        return f"ツムツムログイン失敗: {reason[:180]}"

    userid = str(sess.get("userid") or "")
    safe_userid = ("*" * max(0, len(userid) - 4) + userid[-4:]) if userid else "(不明)"

    try:
        forge = Forge(
            userid=sess.get("userid"),
            hashv=sess.get("hash"),
            checkval=sess.get("checkval"),
        )
        info = forge.get_info()
    except Exception as exc:
        return (
            "**ツムツムログイン確認**\n"
            "ログイン: 成功\n"
            f"ユーザーID: `{safe_userid}`\n"
            f"ゲーム情報取得: 失敗 (`{type(exc).__name__}`)"
        )

    if not isinstance(info, dict):
        return (
            "**ツムツムログイン確認**\n"
            "ログイン: 成功\n"
            f"ユーザーID: `{safe_userid}`\n"
            "ゲーム情報取得: 応答なし"
        )

    resources = forge.resources(info)
    userinfo = info.get("userinfo") or {}
    resource_line = " / ".join(
        f"{key}={resources.get(key, 0)}"
        for key in ("bcoin", "pcoin", "bheart", "pheart", "bruby", "pruby")
    )
    tsum_count = len(info.get("tsuminfo") or [])
    return (
        "**ツムツムログイン確認**\n"
        "ログイン: 成功\n"
        f"ユーザーID: `{safe_userid}`\n"
        f"ゲーム情報: 取得成功（キー数 {len(info)} / ツム情報 {tsum_count}件）\n"
        f"リソース: `{resource_line}`\n"
        f"userinfo: `{'取得済み' if userinfo else 'なし'}`\n"
        "認証トークン: 非表示"
    )
