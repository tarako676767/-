import sys, os, io, json, asyncio, functools, traceback, re, time, random
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import discord
from discord.ext import commands
import unicodedata
import requests

import tsum_login
import line_password_login
import tsum
import tsum_guest
from tsum_forge import Forge

_cfg_name = os.environ.get("TSUM_BOT_CONFIG", "bot_config.json")
CONFIG_FILE = _cfg_name if os.path.isabs(_cfg_name) else os.path.join(HERE, _cfg_name)
print(f"[bot] 設定ファイル: {os.path.basename(CONFIG_FILE)}")
CREDS_FILE = os.path.join(HERE, "line_credentials.json")
TOKENS_FILE = os.path.join(HERE, "line_tokens_latest.json")
SESSION_FILE = os.path.join(HERE, "tsum_session_headless.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[bot] {CONFIG_FILE} がありません。下記の内容で作成してください:")
        print('{\n  "token": "あなたのBOTトークン",\n  "prefix": "!"\n}')
        sys.exit(1)
    return json.load(open(CONFIG_FILE, encoding="utf-8"))

CONFIG = load_config()
try:
    tsum_login.resolve_appver()
except Exception:
    pass
print(f"[bot] appver: {tsum_login.APPVER}")
GUILD_ID = CONFIG.get("guild_id")
PAYPAY_SHARED_FILE = CONFIG.get("paypay_token_file") or "paypay_token.json"
if not os.path.isabs(PAYPAY_SHARED_FILE):
    PAYPAY_SHARED_FILE = os.path.join(HERE, PAYPAY_SHARED_FILE)
PYTHON_REALIP = CONFIG.get("python_realip") or sys.executable
PAYPAY_HELPER = os.path.join(HERE, "paypay_helper.py")
PP_RELOGIN_START = os.path.join(HERE, "_pp_relogin_start.py")
PP_RELOGIN_OTP = os.path.join(HERE, "_pp_relogin_otp.py")
PAYPAY_PROXY_URL = str(CONFIG.get("paypay_proxy_url") or "").strip()
if PAYPAY_PROXY_URL:
    # The helper runs in a child process and reads this value when importing
    # paypayu.  Do not print it because it may contain proxy credentials.
    os.environ["PAYPAY_PROXY_URL"] = PAYPAY_PROXY_URL

from discord import app_commands

def save_config():
    json.dump(CONFIG, open(CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


SALES_FILE = os.path.join(HERE, CONFIG.get("sales_file", "tsum_sales.json"))
FREE_USED_FILE = os.path.join(HERE, CONFIG.get("free_used_file", "tsum_free_used.json"))
ACTIVE_ORDERS = {}


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
    return {"today": agg(today0), "week": agg(now - 7 * 86400),
            "month": agg(now - 30 * 86400),
            "total": (sum(int(d.get("amount", 0)) for d in data), len(data))}


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


intents = discord.Intents.default()
intents.message_content = True

class TsumBot(commands.Bot):
    async def setup_hook(self):
        try:
            self.add_view(MenuView())
        except Exception:
            pass
        try:
            self.add_view(TicketView())
        except Exception:
            pass
        try:
            self.add_view(FreeDaikouView())
        except Exception:
            pass
        try:
            self.add_dynamic_items(RetryLoginButton)
        except Exception as _e:
            print(f"[setup] add_dynamic_items(RetryLoginButton) failed: {_e}")
        try:
            self.add_dynamic_items(GuestRetryButton)
        except Exception as _e:
            print(f"[setup] add_dynamic_items(GuestRetryButton) failed: {_e}")
        try:
            _admin_default = discord.Permissions(manage_guild=True)
            for _c in self.tree.walk_commands():
                try:
                    _c.guild_only = True
                    _c.default_permissions = _admin_default
                except Exception:
                    pass
            if GUILD_ID:
                g = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
            else:
                await self.tree.sync()
        except Exception as e:
            print(f"[bot] tree sync 失敗: {e}")

bot = TsumBot(command_prefix="!", intents=intents, help_command=None)

paypay_lock = asyncio.Lock()
account_locks = {}
ticket_locks = {}


def account_lock_key(login_id):
    value = unicodedata.normalize("NFKC", login_id or "")
    value = "".join(value.split()).lower()
    if "@" in value:
        return f"mail:{value}"

    digits = re.sub(r"\D+", "", value)
    if digits.startswith("0081"):
        digits = "0" + digits[4:]
    elif digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    if len(digits) >= 8:
        return f"phone:{digits}"
    return value


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


def get_account_lock(login_id):
    key = account_lock_key(login_id)
    if not key:
        key = "__empty__"
    lock = account_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        account_locks[key] = lock
    return key, lock


def get_ticket_lock(channel_id):
    key = str(channel_id or "__unknown__")
    lock = ticket_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ticket_locks[key] = lock
    return key, lock


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
    cred = {}
    cred["id"] = login_id
    cred["password"] = password
    json.dump(cred, open(CREDS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return []

class CaptchaInputModal(discord.ui.Modal):
    def __init__(self, future):
        super().__init__(title="画像の文字を入力")
        self.future = future
        self.text = discord.ui.TextInput(
            label="画像の文字",
            placeholder="画像に表示されている文字",
            max_length=32,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        import unicodedata
        value = unicodedata.normalize("NFKC", self.text.value)
        value = "".join(value.split())
        if not value:
            await interaction.response.send_message(embed=notice_embed("文字を入力してください。"), ephemeral=True)
            return
        if not self.future.done():
            self.future.set_result(value)
        await interaction.response.send_message("入力を受け付けました。", ephemeral=True)

def can_operate_ticket(interaction: discord.Interaction, owner_id=None) -> bool:
    if owner_id and interaction.user.id == owner_id:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_channels", False)
        or getattr(perms, "manage_threads", False)
    )

def is_bot_admin(interaction: discord.Interaction) -> bool:
    try:
        ids = [int(x) for x in (CONFIG.get("allowed_user_ids") or [])]
        if ids and interaction.user.id in ids:
            return True
    except Exception:
        pass
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms:
        return False
    return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

async def _tree_admin_only(interaction: discord.Interaction) -> bool:
    if is_bot_admin(interaction):
        return True
    try:
        await interaction.response.send_message(
            embed=notice_embed("このコマンドは管理者のみ使用できます。"), ephemeral=True)
    except Exception:
        pass
    return False

bot.tree.interaction_check = _tree_admin_only

class CaptchaView(discord.ui.View):
    def __init__(self, user_id, future):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.future = future

    async def interaction_check(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.user_id):
            await interaction.response.send_message(embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="画像の文字を入力", style=discord.ButtonStyle.success)
    async def input_captcha(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CaptchaInputModal(self.future))

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.future.done():
            self.future.set_result("")
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result("")

def make_solver(channel, user, loop):
    def solver(image_bytes):
        fut = asyncio.run_coroutine_threadsafe(_ask_captcha(channel, user, image_bytes), loop)
        try:
            return fut.result(timeout=200)
        except Exception:
            return ""
    return solver


def make_pin_notice(channel, loop):
    def notice(pincode):
        emb = discord.Embed(
            description=f"LINEアプリで本人確認の番号を入力してください。\nPIN: **{pincode}**",
            color=0xf1c40f,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(channel.send(embed=emb), loop)
            fut.result(timeout=30)
        except Exception:
            print("[pin] notice send failed")
    return notice

def notice_embed(text, color=0xe74c3c):
    return discord.Embed(description=text, color=color)

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

async def _ask_captcha(channel, user, image_bytes):
    try:
        image_bytes = improve_captcha_image(image_bytes)
        f = discord.File(io.BytesIO(image_bytes or b""), filename="captcha.png")
        await channel.send(file=f)
    except Exception as e:
        print(f"[captcha] send failed: {e}")
        await channel.send(embed=notice_embed("CAPTCHA画像を送信できませんでした。もう一度試してください。"))
        return ""

    fut = asyncio.get_running_loop().create_future()
    view = CaptchaView(user.id, fut)
    emb = discord.Embed(
        description="下のボタンから画像の文字を入力してください。",
        color=0xf1c40f,
    )
    prompt = await channel.send(embed=emb, view=view)
    text = await fut
    for child in view.children:
        child.disabled = True
    try:
        await prompt.edit(view=view)
    except Exception:
        pass
    if not text:
        await channel.send(embed=notice_embed("CAPTCHA入力がキャンセルまたはタイムアウトしました。"))
        return ""
    return text.strip()

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
    if any(k in low for k in ("ban", "block", "restrict", "limit", "forbidden")) or any(k in msg for k in ("制限", "拒否", "禁止")):
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
        return ("画像認証が繰り返し出てログインできませんでした。LINE側で一時的に制限がかかっている可能性が高いので、"
                "20〜30分ほど時間を置いてからお試しください（続けて試すと制限が延びることがあります）。")
    if "通信エラー" in reason or "接続が不安定" in reason or "プロキシ" in reason:
        return "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください"
    if "アカウント情報が違う" in reason or "errorcode=445" in low:
        return "メールアドレスかパスワードが違います"
    if ("中断されました" in reason or "追加認証" in reason or "一時制限" in reason
            or "連続試行" in reason or "errorcode=446" in low or "errorcode=401" in low):
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


def forge_sync(score=None, coin=None, exp=None, medal=5, tsumid=860, tsum_lv=None, box=None,
               login_id=None, password=None, captcha_solver=None, proxy=None, sess=None, f=None,
               pin_notice=None, gacha_full=None, on_login=None):
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
                    login_id, password, proxy=proxy,
                    captcha_solver=_captcha_relay, save=False, verbose=False, error_box=login_error,
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
            ip_blocked = ("errorcode=446" in low or "errorcode=401" in low or "中断されました" in _raw
                          or "プロキシ接続エラー" in _raw or "通信エラー" in _raw)
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
            for t in (info.get("tsuminfo") or []):
                if t.get("setflg") in (1, "1"):
                    tid = t.get("tsumid"); break
            if not tid:
                return None, "セット中のマイツムが見つかりませんでした。ゲームでマイツムをセットしてから、もう一度お試しください。"
        res = f.max_tsum_level(tid, target=50, min_heart=10, sleep_s=0.3, log=None)
        st = res.get("status")
        if st == "done":
            return res, "セット中のツムをレベルMAXにしました\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
        if st == "insufficient_heart":
            return None, f"ハートが足りないため中断しました（所持 {res['have']} / 必要 {res['need']}）。ハートを貯めてからもう一度お試しください。"
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
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "insufficient_coin":
            return None, f"コインが足りないため中断しました（所持 {res['have']:,} / 必要 {res['need']:,}）。先にコインを増やしてからお試しください。"
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
            return res, f"プレミアムガチャを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "insufficient_coin":
            return None, f"コインが足りません（所持 {res['have']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, f"コインが足りず途中で終了しました（{res.get('draws','-')}連）。コインを増やしてからお試しください。"
        return None, "プレミアムガチャの完売に失敗しました。もう一度お試しください。"

    candidates, _info, _err = select_play_tsumids(f, tsumid)
    tsum_candidates = list(candidates or [])[:3]
    if isinstance(_info, dict):
        for t in (_info.get("tsuminfo") or []):
            tid = _to_int(t.get("tsumid")) if isinstance(t, dict) else 0
            if tid and tid not in tsum_candidates and len(tsum_candidates) < 5:
                tsum_candidates.append(tid)
    if tsumid and tsumid not in tsum_candidates:
        tsum_candidates.append(tsumid)
    if not tsum_candidates:
        tsum_candidates = [tsumid]

    ht_candidates = hearttype_candidates_from_info(_info) or [0, 1]
    bheart, pheart, total_heart = heart_state_from_info(_info)
    _set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                      if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
    uid = str(sess.get("userid") or "")
    try:
        ev_order = _cached_event_candidates(f)
    except Exception as _e:
        ev_order = ["9999", "0"]
        print(f"[gs] event_candidates失敗→fallback: {_e}", flush=True)
    if not ev_order:
        ev_order = ["9999", "0"]
    _dbg(f"[gs v7] userid={uid[:3]}… set_tsum={_set_tsum} bheart={bheart} pheart={pheart} "
         f"ht={ht_candidates} tsum候補={tsum_candidates[:4]} eventid候補={ev_order}")

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
        playcode = (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode") if isinstance(r1, dict) else None
        if playcode:
            won_tid = tid; won_ht = ht; won_ev = ev
        return playcode

    play_tids = []
    for t in ([_set_tsum] + list(tsum_candidates) + [tsumid]):
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
            set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                             if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
            dump = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "userid": sess.get("userid"),
                "hashlen": len(str(sess.get("hash") or "")),
                "checkval_present": bool(sess.get("checkval")),
                "getInfo_retcode": _info.get("retcode") if isinstance(_info, dict) else f"non-dict({type(_info).__name__})",
                "getInfo_keys": sorted(_info.keys()) if isinstance(_info, dict) else None,
                "userinfo": ui,
                "n_tsum": len([t for t in (_info.get("tsuminfo") or []) if isinstance(t, dict)]) if isinstance(_info, dict) else 0,
                "set_tsum": set_tsum,
                "sample_tsums": [t for t in ((_info.get("tsuminfo") or [])[:5]) if isinstance(t, dict)] if isinstance(_info, dict) else [],
                "attempts": [list(a) for a in attempts],
                "gameStart_response": r1 if isinstance(r1, dict) else repr(r1),
            }
            with open(os.path.join(HERE, "tsum_gs_fulldump.json"), "w", encoding="utf-8") as df:
                json.dump(dump, df, ensure_ascii=False, indent=2)
            with open(os.path.join(HERE, "tsum_gs_diag.log"), "a", encoding="utf-8") as lf:
                lf.write(f"{dump['ts']} GS_FAIL userid={dump['userid']} getInfo_rc={dump['getInfo_retcode']} "
                         f"n_tsum={dump['n_tsum']} set_tsum={set_tsum} bheart={bheart} pheart={pheart} "
                         f"attempts={attempts}  (full -> tsum_gs_fulldump.json)\n")
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
                pc = (rs.get("userinfo") or {}).get("playcode") or rs.get("playcode") if isinstance(rs, dict) else None
                if not pc:
                    break
            ge = dict(score=score if score is not None else 95000, coin=61,
                      medal=medal, exp=120, vanishcnt=vanishcnt)
            ge[chunk_field] = send
            rr = f.game_end(pc, **ge)
            pc = None
            if isinstance(rr, dict) and rr.get("retcode") == 0:
                r2 = rr; credited += send; games += 1
            elif chunk > 1_000_000:
                chunk //= 2
            else:
                break
            time.sleep(0.2)
        _dbg(f"[{chunk_field}] target={target} credited={credited} games={games} last_chunk={chunk}")
        if credited <= 0 or r2 is None:
            return None, "送信に失敗しました。もう一度試してください。"
    else:
        r2 = f.game_end(playcode,
                        score=score if score is not None else 95000,
                        coin=coin if coin is not None else 61,
                        medal=medal, exp=120,
                        vanishcnt=vanishcnt)
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

async def _do_forge(channel, user, login_id, password, **kw):
    lock_key, account_lock = get_account_lock(login_id)
    if account_lock.locked():
        await channel.send(embed=discord.Embed(
            description="同じアカウントの依頼を処理中です。順番に実行します。",
            color=0xf1c40f,
        ))

    try:
        await asyncio.wait_for(account_lock.acquire(), timeout=900)
    except asyncio.TimeoutError:
        await channel.send(embed=notice_embed("同じアカウントの処理待ちが長すぎるため中止しました。もう一度試してください。"))
        return False, "同じアカウントの処理待ちが長すぎるため中止しました。"

    await channel.send(embed=discord.Embed(description="ログインしています...", color=0x3498db))
    loop = asyncio.get_running_loop()
    captcha_solver = make_solver(channel, user, loop)
    pin_notice = make_pin_notice(channel, loop)
    proxy = None
    result_obj = None
    text = ""
    try:
        result_obj, text = await loop.run_in_executor(
            None,
            functools.partial(
                forge_sync,
                login_id=login_id,
                password=password,
                captcha_solver=captcha_solver,
                proxy=proxy,
                pin_notice=pin_notice,
                **kw,
            ),
        )
    except Exception:
        print("[forge] error")
        traceback.print_exc()
        try:
            with open(os.path.join(HERE, "forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write("\n==== forgeエラー id=%s*** kw=%s ====\n" % (str(login_id)[:3], kw))
                _lf.write(traceback.format_exc())
        except Exception:
            pass
        text = "エラーが発生しました。しばらく待ってからもう一度お試しください。"
    finally:
        clear_tsum_login_files()
        if account_lock.locked():
            account_lock.release()
        if not account_lock.locked():
            if account_locks.get(lock_key) is account_lock:
                account_locks.pop(lock_key, None)
    success = result_obj is not None
    color = 0x2ecc71 if success else 0xe74c3c
    try:
        await channel.send(embed=discord.Embed(description=text, color=color))
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] result send skipped: channel unavailable")
    return success, text

async def run_forge(ctx, **kw):
    await _do_forge(ctx.channel, ctx.author, None, None, **kw)


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
        data.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "orig_migration_id": migration_id,
            "uuid": sess.get("uuid"),
            "userToken": sess.get("userToken"),
            "userid": sess.get("userid"),
            "hash": sess.get("hash"),
            "checkval": sess.get("checkval"),
            "migrated_userKey": sess.get("migrated_userKey"),
        })
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[guest] orphan session saved -> {path} (uuid={sess.get('uuid')} userid={sess.get('userid')})")
    except Exception:
        traceback.print_exc()


def forge_sync_guest(migration_id, password, score=None, coin=None, exp=None, medal=5,
                     tsumid=860, tsum_lv=None, box=None, proxy=None, gacha_full=None, **_ignore):
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
        f = Forge(userid=sess["userid"], hashv=sess.get("hash", ""), checkval=sess.get("checkval", ""), proxy=proxy)
        f.get_public_key()
        result_obj, text = forge_sync(score=score, coin=coin, exp=exp, medal=medal, tsumid=tsumid,
                                      tsum_lv=tsum_lv, box=box, proxy=proxy, sess=sess, f=f,
                                      gacha_full=gacha_full)
    except Exception:
        traceback.print_exc()
        try:
            with open(os.path.join(HERE, "guest_forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write("\n==== guest盛りエラー coin=%s score=%s exp=%s box=%s gacha_full=%s tsum_lv=%s ====\n"
                          % (coin, score, exp, box, gacha_full, tsum_lv))
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
        text += ("\n\n──────────\n**新しい引き継ぎ情報**\n"
                 f"引き継ぎ番号: `{new_id}`\nパスワード: `{new_pw}`\n"
                 "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。必ず控えてください。")
    else:
        _save_orphan_guest(sess, migration_id)
        text += ("\n\n──────────\n⚠️新しい引き継ぎ情報の発行に失敗しました。"
                 "この画面をスクリーンショットして管理者にご連絡ください（管理者側で復旧できます）。")
    return result_obj, (text or "処理に失敗しました。"), info


async def _do_forge_guest(channel, user, migration_id, password, **kw):
    lock_key, account_lock = get_account_lock("guest:" + str(migration_id))
    if account_lock.locked():
        await channel.send(embed=discord.Embed(
            description="同じ引き継ぎ番号の依頼を処理中です。順番に実行します。", color=0xf1c40f))
    try:
        await asyncio.wait_for(account_lock.acquire(), timeout=900)
    except asyncio.TimeoutError:
        await channel.send(embed=notice_embed("処理待ちが長すぎるため中止しました。もう一度試してください。"))
        return False, "処理待ちが長すぎるため中止しました。", {"moved": False}

    await channel.send(embed=discord.Embed(description="引き継ぎ中です...", color=0x3498db))
    loop = asyncio.get_running_loop()
    proxy = None
    result_obj = None
    text = ""
    info = {"moved": False}
    try:
        result_obj, text, info = await loop.run_in_executor(
            None,
            functools.partial(forge_sync_guest, migration_id, password, proxy=proxy, **kw),
        )
    except Exception:
        print("[forge_guest] error")
        traceback.print_exc()
        info = {"moved": True}
        text = "エラーが発生しました。お手数ですが管理者にご連絡ください。"
    finally:
        if account_lock.locked():
            account_lock.release()
        if not account_lock.locked():
            if account_locks.get(lock_key) is account_lock:
                account_locks.pop(lock_key, None)
    success = result_obj is not None
    color = 0x2ecc71 if success else 0xe74c3c
    try:
        await channel.send(embed=discord.Embed(description=text, color=color))
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] guest result send skipped: channel unavailable")
    return success, text, info

def status_text():
    label, cid = tsum.current_label()
    ip = "?"
    region = ""
    try:
        import requests
        ip = requests.get("https://api.ipify.org", timeout=25).text.strip()
        try:
            j = requests.get(f"http://ip-api.com/json/{ip}?fields=country,regionName&lang=ja", timeout=10).json()
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
    return (f"**状態**\n{ip_line}\nアカウント: {label} (`{cid}`)\n"
            f"トークン: {tok}\nログイン障壁: {capj}")


def tsum_login_debug_text():
    """Run a read-only game login and return sanitized diagnostics.

    Authentication material such as userToken, hash and checkval is
    intentionally never included in the returned text.
    """
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


MAX_PRESETS = {
    "score_max": ("score", 2_147_483_647),
    "level_max": ("exp", 100_000_000),
    "coin_max":  ("coin", 200_000_000),
}
DEFAULT_SELECT_BOX_ID = int(CONFIG.get("select_box_id", 12006012))
DEFAULT_PREMIUM_GACHA_ID = int(CONFIG.get("premium_gacha_id", 12006007))
PREMIUM_FILL_COINS = int(CONFIG.get("premium_fill_coins", 160_000_000))
FREE_PAYMENT_USER_ID = int(CONFIG.get("free_payment_user_id", 0) or 0)
DEFAULT_MENU_PRICES = {
    "score_max": 100,
    "level_max": 100,
    "coin_max": 100,
    "tsum_lv": 100,
    "box": 200,
    "premium": 200,
    "coin": 100,
    "score": 100,
}
CONFIG_PRICES = CONFIG.get("menu_prices", {})
MENU_PRICES = {
    key: int(CONFIG_PRICES.get(key, price))
    for key, price in DEFAULT_MENU_PRICES.items()
}
MENU_ACTIONS = [
    ("coin",    "コイン指定"),
    ("coin_max",  "コインMAX"),
    ("level_max", "プレイヤーレベルMAX"),
    ("score",   "スコア指定"),
    ("score_max", "スコアMAX"),
    ("tsum_lv",   "ツムレベルMAX"),
    ("box",       "セレクトBOX完売"),
    ("premium",   "プレミアム完売"),
]

PRICE_MENU_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in MENU_ACTIONS
]

def menu_description():
    return "\n".join([
        "ご希望のメニューを選択してください。",
        "",
        f"**コイン指定 - ¥{menu_price('coin'):,}**",
        "0〜2億枚まで指定可能",
        f"**コインMAX - ¥{menu_price('coin_max'):,}**",
        "コインを2億枚にします",
        f"**プレイヤーレベルMAX - ¥{menu_price('level_max'):,}**",
        "レベルMAXにします",
        f"**スコア指定 - ¥{menu_price('score'):,}**",
        "好きなスコアを指定可能",
        f"**スコアMAX - ¥{menu_price('score_max'):,}**",
        "スコアMAXにします",
        f"**ツムレベルMAX - ¥{menu_price('tsum_lv'):,}**",
        "セット中のツムをレベル50",
        f"**セレクトBOX完売 - ¥{menu_price('box'):,}**",
        "完売まで",
        f"**プレミアム完売 - ¥{menu_price('premium'):,}**",
        "プレミアムガチャを完売まで",
    ])

def is_free_payment_user(user):
    return user.id == FREE_PAYMENT_USER_ID

def menu_price(menu_key):
    return MENU_PRICES.get(menu_key, DEFAULT_MENU_PRICES.get(menu_key, 0))

def load_paypay():
    if not os.path.exists(PAYPAY_SHARED_FILE):
        return {}
    try:
        return json.load(open(PAYPAY_SHARED_FILE, encoding="utf-8"))
    except Exception:
        return {}

def save_paypay(data):
    folder = os.path.dirname(PAYPAY_SHARED_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    json.dump(data, open(PAYPAY_SHARED_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def normalize_paypay_link(raw):
    m = re.search(r"https?://(?:pay\.paypay\.ne\.jp|paypay\.ne\.jp)/[^\s]+", raw or "")
    if not m:
        return ""
    return m.group(0).strip("<>、。,.")

def payment_from_inputs(user, menu_key, paypay_raw):
    if is_free_payment_user(user):
        return 0, "", None

    amount = menu_price(menu_key)
    if amount <= 0:
        return 0, "", None

    link = normalize_paypay_link(paypay_raw)
    if not link:
        return amount, "", "PayPay送金リンクを入力してください（¥%s ちょうど）。" % format(amount, ",")
    return amount, link, None

async def _run_paypay_helper_dict(*args, timeout=180):
    try:
        proc = await asyncio.create_subprocess_exec(
            PYTHON_REALIP, PAYPAY_HELPER, *[str(a) for a in args],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception as e:
        print(f"[paypay] helper spawn failed: {type(e).__name__}")
        return {"ok": False, "msg": "PayPay処理に失敗しました。"}
    text = (out or b"").decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    print(f"[paypay] helper bad output err={(err or b'')[:200]!r}")
    return {"ok": False, "msg": "PayPay処理に失敗しました。"}


async def _run_paypay_helper(*args, timeout=180):
    d = await _run_paypay_helper_dict(*args, timeout=timeout)
    return bool(d.get("ok")), d.get("msg", "")


async def _run_pp_script(script, *args, timeout=180):
    try:
        proc = await asyncio.create_subprocess_exec(
            PYTHON_REALIP, script, *[str(a) for a in args],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (out or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"[paypay] login script spawn failed: {type(e).__name__}")
        return f"SPAWN_FAIL: {type(e).__name__}"


async def verify_paypay_link(paypay_link, amount):
    return await _run_paypay_helper("check", paypay_link, str(int(amount)))

def _paypay_receive_sync(paypay_link, phone, password, client_uuid, access_token=None):
    from PayPaython import PayPay
    from PayPaython.main import PayPayError, PayPayLoginError

    last_err = None
    if access_token:
        try:
            pp = PayPay(phone=phone, password=password, client_uuid=client_uuid, access_token=access_token)
            pp.link_receive(paypay_link)
            return True, access_token, None
        except PayPayLoginError:
            pass
        except PayPayError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("already", "expired", "invalid", "not_found", "claimed", "受取済", "受け取り済")):
                return False, access_token, "link_invalid"
            last_err = e
        except Exception as e:
            last_err = e

    try:
        pp = PayPay(phone=phone, password=password, client_uuid=client_uuid)
        new_token = getattr(pp, "access_token", None)
    except Exception:
        return False, access_token, "login_failed"

    try:
        pp.link_receive(paypay_link)
        return True, new_token, None
    except PayPayLoginError:
        return False, new_token, "login_failed"
    except PayPayError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("already", "expired", "invalid", "not_found", "claimed", "受取済", "受け取り済")):
            return False, new_token, "link_invalid"
        last_err = e
    except Exception as e:
        last_err = e

    return False, new_token, f"receive_failed:{type(last_err).__name__ if last_err else 'unknown'}"

async def receive_paypay_link(paypay_link):
    creds = load_paypay()
    client_uuid = (creds.get("uuid") or creds.get("client_uuid")) if creds else None
    if not creds or not creds.get("phone") or not creds.get("password") or not client_uuid:
        return False, "PayPay受取アカウントが未設定です。"

    async with paypay_lock:
        return await _run_paypay_helper("receive", paypay_link, PAYPAY_SHARED_FILE)

_CONSUMED_LINKS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(PAYPAY_SHARED_FILE)), "consumed_paypay_links.json")


def _link_key(pay_link):
    s = (pay_link or "").strip()
    m = re.search(r"https?://(?:pay\.paypay\.ne\.jp|paypay\.ne\.jp)/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    return s


def _is_link_consumed(pay_link):
    k = _link_key(pay_link)
    if not k:
        return False
    try:
        with open(_CONSUMED_LINKS_FILE, encoding="utf-8") as f:
            return k in set(json.load(f))
    except Exception:
        return False


def _mark_link_consumed(pay_link):
    k = _link_key(pay_link)
    if not k:
        return
    try:
        cur = []
        if os.path.exists(_CONSUMED_LINKS_FILE):
            with open(_CONSUMED_LINKS_FILE, encoding="utf-8") as f:
                cur = json.load(f)
        if k not in cur:
            cur.append(k)
            with open(_CONSUMED_LINKS_FILE, "w", encoding="utf-8") as f:
                json.dump(cur, f)
    except Exception as e:
        print(f"[tsum] consumed link save error: {e}")


def _hold_embed():
    return discord.Embed(
        title="受け取り一時保留になりました",
        description=("PayPayのアプリ側から取引を承認してください。\n"
                     "承認した場合は下のボタンを押してください。\n"
                     "承認が確認できたら代行を開始します。\n\n"
                     "ボタンの有効期限: 1時間"),
        color=0xf1c40f,
    )


class TsumHoldApproveView(discord.ui.View):

    def __init__(self, func, kwargs):
        super().__init__(timeout=3600)
        self.func = func
        self.kwargs = kwargs
        self._last = 0.0

    @discord.ui.button(label="承認しました", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.monotonic()
        if now - self._last < 10:
            await interaction.response.send_message(
                "少し待ってからもう一度押してください。", ephemeral=True)
            return
        self._last = now
        try:
            await self.func(interaction, **self.kwargs)
        except Exception as e:
            print(f"[tsum] hold retry failed: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    embed=notice_embed("処理中に問題が発生しました。オーナーにご連絡ください。"), ephemeral=True)
            except Exception:
                pass


def ticket_channel_name(name, user_id):
    base = "".join(c.lower() if c.isascii() and c.isalnum() else "-" for c in name)
    base = "-".join(part for part in base.split("-") if part)
    if not base:
        base = "ticket"
    return f"ticket-{user_id}-{base}"[:90]

async def get_ticket_category(guild):
    category_id = CONFIG.get("ticket_category_id")
    if not guild or not category_id:
        return None
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return None

    category = guild.get_channel(category_id)
    if category is None:
        try:
            category = await guild.fetch_channel(category_id)
        except Exception:
            return None
    if isinstance(category, discord.CategoryChannel):
        return category
    return None

async def open_ticket(interaction, name):
    category = await get_ticket_category(interaction.guild)
    if category is not None:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
        }
        if interaction.guild.me:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                manage_channels=True,
            )
        async def _create_ticket_channel():
            return await interaction.guild.create_text_channel(
                ticket_channel_name(name, interaction.user.id),
                category=category,
                overwrites=overwrites,
                topic=str(interaction.user.id),
                reason=f"ticket for {interaction.user}",
            )
        try:
            return await _create_ticket_channel()
        except discord.HTTPException as e:
            full = (getattr(e, "code", None) == 50035) or ("Maximum number of channels" in str(e))
            if full:
                try:
                    tickets = [c for c in getattr(category, "channels", [])
                               if isinstance(c, discord.TextChannel)
                               and str(getattr(c, "topic", "") or "").isdigit()]
                    for old in sorted(tickets, key=lambda c: c.created_at)[:5]:
                        try:
                            await old.delete(reason="ticket category full: prune oldest")
                        except Exception:
                            pass
                    return await _create_ticket_channel()
                except Exception as e2:
                    print(f"[ticket] category full cleanup+retry failed: {e2}")
            else:
                print(f"[ticket] category channel create failed: {e}")
        except Exception as e:
            print(f"[ticket] category channel create failed: {e}")

    ch = interaction.channel
    try:
        th = await ch.create_thread(name=name, type=discord.ChannelType.private_thread, invitable=False)
        try: await th.add_user(interaction.user)
        except Exception: pass
        return th
    except Exception:
        try:
            th = await ch.create_thread(name=name, type=discord.ChannelType.public_thread)
            return th
        except Exception:
            return ch

def ticket_link(ch):
    return getattr(ch, "mention", None) or "このチャット"

def ticket_request_text(action, val):
    if action == "tsum_lv":
        return "ツムlvMAX\nセット中のツム"
    if action == "box":
        return "セレクトBOX完売"
    if action == "gacha_full":
        return "プレミアムガチャ完売"
    labels = {
        "coin": "コイン指定",
        "score": "スコア指定",
        "exp": "プレイヤーレベルMAX",
        "tsum_lv": "ツムlvMAX",
        "box": "セレクトBOX",
    }
    label = labels.get(action, action)
    return f"{label}\n{val:,}"

def ticket_embed(user, action, val, amount, paypay_link):
    emb = discord.Embed(title="ツムツム代行", color=0x2ecc71)
    pay_text = f"支払い済み（¥{amount:,}）" if amount > 0 else "無料"
    emb.description = (
        f"**依頼内容**\n{ticket_request_text(action, val)}\n"
        f"**金額**\n¥{amount:,}\n"
        f"**支払い状況**\n{pay_text}\n"
        f"**実行ユーザー**\n{user.mention} ({user.id})"
    )
    return emb

class TicketView(discord.ui.View):
    def __init__(self, owner_id=None):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.custom_id = "tsum_ticket_delete"

    @discord.ui.button(label="チケット削除", style=discord.ButtonStyle.danger)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
            await interaction.response.send_message(embed=notice_embed("この場所ではチケット削除を実行できません。"), ephemeral=True)
            return
        await interaction.response.send_message("チケットを削除します。", ephemeral=True)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    def resolve_owner_id(self, interaction: discord.Interaction):
        if self.owner_id:
            return self.owner_id
        channel = interaction.channel
        topic = getattr(channel, "topic", None)
        if topic and str(topic).isdigit():
            return int(topic)
        name = getattr(channel, "name", "") or ""
        match = re.search(r"ticket-(\d{15,25})-", name)
        if match:
            return int(match.group(1))
        message = getattr(interaction, "message", None)
        for embed in getattr(message, "embeds", []) or []:
            text = " ".join(filter(None, [
                getattr(embed, "description", None),
                getattr(embed, "title", None),
            ]))
            match = re.search(r"\((\d{15,25})\)", text) or re.search(r"<@!?(\d{15,25})>", text)
            if match:
                return int(match.group(1))
        return None

    async def interaction_check(self, interaction: discord.Interaction):
        owner_id = self.resolve_owner_id(interaction)
        if owner_id and not can_operate_ticket(interaction, owner_id):
            await interaction.response.send_message(embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return False
        return True

async def send_ticket_header(channel, user, action, val, amount, paypay_link):
    await channel.send(embed=ticket_embed(user, action, val, amount, paypay_link), view=TicketView(user.id))

def ticket_thread_name(action, val):
    if action == "box":
        return "セレクトBOX完売"
    if action == "gacha_full":
        return "プレミアム完売"
    if action == "tsum_lv":
        return "ツムlvMAX"
    labels = {
        "score": "スコア",
        "coin": "コイン",
        "exp": "レベルMAX",
    }
    label = labels.get(action, action)
    return f"{label}-{val}"

async def reset_menu_message(message):
    if message is None:
        return
    try:
        await message.edit(view=MenuView())
    except Exception as e:
        print(f"[menu] reset failed: {e}")

async def post_public_result(guild, user, action, val, amount):
    channel_id = CONFIG.get("public_result_channel_id")
    if not guild or not channel_id:
        return
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return

    emb = discord.Embed(color=0x2ecc71)
    emb.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    emb.description = (
        f"**注文内容**\n{ticket_request_text(action, val)}\n"
        f"**金額**\n¥{amount:,}"
    )
    try:
        await channel.send(embed=emb)
    except Exception as e:
        print(f"[public_result] send failed: {e}")

def ticket_owner_user(guild, owner_id, fallback):
    try:
        owner_id = int(owner_id)
    except (TypeError, ValueError):
        return fallback
    if guild:
        member = guild.get_member(owner_id)
        if member:
            return member
    user = bot.get_user(owner_id)
    return user or fallback

class RetryLoginModal(discord.ui.Modal):
    def __init__(self, owner_id, action, val, amount, prompt_message=None):
        super().__init__(title="アカウント情報を再入力")
        self.owner_id = owner_id
        self.action = action
        self.val = val
        self.amount = amount
        self.prompt_message = prompt_message
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
            style=discord.TextStyle.short,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if self.prompt_message:
                await self.prompt_message.edit(view=None)
        except Exception:
            pass

        channel = interaction.channel
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            await interaction.followup.send("この場所では再実行できません。", ephemeral=True)
            return

        lock_key, ticket_lock = get_ticket_lock(channel.id)
        if ticket_lock.locked():
            await interaction.followup.send("このチケットは処理中です。完了までお待ちください。", ephemeral=True)
            return

        is_free = (self.amount == 0 and self.action == "coin" and int(self.val) == FREE_DAIKOU_COIN)
        if is_free:
            if not free_can_use(self.owner_id, login_id=login_id):
                _m = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=login_id)
                      else FREE_DAIKOU_LIMIT_MSG)
                await interaction.followup.send(embed=notice_embed(_m), ephemeral=True)
                return
            free_mark_used(self.owner_id, login_id=login_id)

        await interaction.followup.send("再実行します。", ephemeral=True)
        ok = False
        _game_state = {"userid": "", "blocked": False}
        def _on_login_free(game_userid):
            _game_state["userid"] = game_userid or ""
            if game_userid and not free_can_use(game_userid=game_userid):
                _game_state["blocked"] = True
                raise RuntimeError("free_daikou_already_used_for_this_game_account")
            if game_userid:
                free_mark_used(game_userid=game_userid)
        try:
            async with ticket_lock:
                _kw = {"on_login": _on_login_free} if is_free else {}
                ok, _ = await _do_forge(channel, interaction.user, login_id, password,
                                        **{self.action: self.val}, **_kw)
        finally:
            if is_free and not ok:
                if _game_state["blocked"]:
                    free_mark_used(self.owner_id, login_id=login_id, game_userid=_game_state["userid"])
                    try:
                        await interaction.followup.send(
                            embed=notice_embed(FREE_DAIKOU_ACCOUNT_LIMIT_MSG), ephemeral=True)
                    except Exception:
                        pass
                else:
                    free_unmark_used(self.owner_id, login_id=login_id, game_userid=_game_state["userid"])
        if not ticket_lock.locked():
            ticket_locks.pop(lock_key, None)

        owner = ticket_owner_user(interaction.guild, self.owner_id, interaction.user)
        if ok:
            await post_public_result(interaction.guild, owner, self.action, self.val, self.amount)
        else:
            await send_retry_prompt(channel, self.owner_id, self.action, self.val, self.amount)

class RetryLoginButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r'tsum_retry_login:(?P<owner>\d+):(?P<action>[A-Za-z_]+):(?P<val>-?\d+)(?::(?P<amount>-?\d+))?'):
    def __init__(self, owner_id, action, val, amount):
        self.owner_id = int(owner_id)
        self.action = str(action)
        self.val = int(val)
        self.amount = int(amount)
        super().__init__(
            discord.ui.Button(
                label="アカウント情報を再入力",
                style=discord.ButtonStyle.primary,
                custom_id=f"tsum_retry_login:{self.owner_id}:{self.action}:{self.val}:{self.amount}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        amt = match.group("amount")
        return cls(int(match["owner"]), match["action"], int(match["val"]), int(amt) if amt else 0)

    async def callback(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.owner_id):
            await interaction.response.send_message(
                embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return
        await interaction.response.send_modal(
            RetryLoginModal(self.owner_id, self.action, self.val, self.amount, interaction.message)
        )

async def send_retry_prompt(channel, owner_id, action, val, amount):
    emb = discord.Embed(
        description="成功しませんでした。アカウント情報を入力し直して、同じチケットで再実行できます。",
        color=0xf1c40f,
    )
    view = discord.ui.View(timeout=None)
    try:
        view.add_item(RetryLoginButton(owner_id, action, val, amount))
    except Exception as _e:
        print(f"[ticket] retry button build failed: {_e}")
        return
    try:
        await channel.send(embed=emb, view=view)
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] retry prompt skipped: channel unavailable")

async def create_ticket_and_forge(interaction: discord.Interaction, action, val, amount, paypay_link, login_id, password, menu_message=None, pp_id="", on_login=None):
    if interaction.guild is None:
        try:
            await interaction.response.send_message(
                embed=notice_embed("ご注文はサーバー内のチャンネルからお願いします。DMでは受け付けていません。"))
        except Exception:
            pass
        return False
    if is_guest_account(login_id):
        return await create_ticket_and_forge_guest(
            interaction, action, val, amount, paypay_link, login_id, password, menu_message)
    await interaction.response.defer(ephemeral=True)
    if amount > 0:
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。届いていない場合はオーナーにご連絡ください。"), ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict("verify_receive", paypay_link, str(int(amount)), PAYPAY_SHARED_FILE)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(create_ticket_and_forge, dict(
                    action=action, val=val, amount=amount, paypay_link=paypay_link,
                    login_id=login_id, password=password, menu_message=menu_message)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            clear_tsum_login_files()
            await reset_menu_message(menu_message)
            return False

    th = await open_ticket(interaction, ticket_thread_name(action, val))
    await interaction.followup.send(f"チケット作成: {ticket_link(th)}", ephemeral=True)
    await send_ticket_header(th, interaction.user, action, val, amount, paypay_link)
    await reset_menu_message(menu_message)
    okey = f"{interaction.id}"
    ACTIVE_ORDERS[okey] = {"user": str(interaction.user), "action": action, "val": val, "ts": int(time.time())}
    try:
        _extra = {"on_login": on_login} if on_login else {}
        ok, _ = await _do_forge(th, interaction.user, login_id, password, **{action: val}, **_extra)
    finally:
        ACTIVE_ORDERS.pop(okey, None)
    print(f"[注文] {interaction.user} {action} {val} ¥{amount} → {'成功' if ok else '失敗'}", flush=True)
    if ok:
        if amount > 0:
            record_sale(interaction.user.id, action, amount)
        await post_public_result(interaction.guild, interaction.user, action, val, amount)
    else:
        await send_retry_prompt(th, interaction.user.id, action, val, amount)
    return ok

VALUE_LABELS = {
    "score": ("スコア", "数値（半角）", "例: 3000000"),
    "coin":  ("コイン", "数値（半角）", "例: 100000000"),
}

class ValueModal(discord.ui.Modal):
    def __init__(self, action, menu_message=None, menu_key=None):
        title_name, in_label, in_ph = VALUE_LABELS.get(action, ("値", "数値（半角）", ""))
        super().__init__(title=title_name + " とアカウント情報")
        self.action = action
        self.menu_message = menu_message
        self.menu_key = menu_key or action
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.tin = discord.ui.TextInput(label=in_label, placeholder=in_ph)
        self.add_item(self.login_id)
        self.add_item(self.password)
        self.add_item(self.tin)
        self.paypay = None
        if menu_price(self.menu_key) > 0:
            self.paypay = discord.ui.TextInput(
                label="PayPay送金リンク",
                placeholder="https://pay.paypay.ne.jp/xxxx（料金ちょうどを送金）",
                required=True,
                max_length=200,
            )
            self.add_item(self.paypay)
    async def on_submit(self, interaction: discord.Interaction):
        raw = self.tin.value.strip().replace(",", "").replace("，", "")
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        paypay_raw = self.paypay.value.strip() if self.paypay is not None else ""
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        if not raw.isdigit():
            await interaction.response.send_message(embed=notice_embed("数値で入力してください。"), ephemeral=True); return
        val = int(raw)
        amount, paypay_link, error = payment_from_inputs(interaction.user, self.menu_key, paypay_raw)
        if error:
            await interaction.response.send_message(embed=notice_embed(error), ephemeral=True)
            return
        await create_ticket_and_forge(interaction, self.action, val, amount, paypay_link, login_id, password, self.menu_message)

class AccountBeforeTicketModal(discord.ui.Modal):
    def __init__(self, action, val, menu_message=None, menu_key=None):
        super().__init__(title="アカウント情報を入力")
        self.action = action
        self.val = val
        self.menu_message = menu_message
        self.menu_key = menu_key or action
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)
        self.paypay = None
        if menu_price(self.menu_key) > 0:
            self.paypay = discord.ui.TextInput(
                label="PayPay送金リンク",
                placeholder="https://pay.paypay.ne.jp/xxxx（料金ちょうどを送金）",
                required=True,
                max_length=200,
            )
            self.add_item(self.paypay)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        paypay_raw = self.paypay.value.strip() if self.paypay is not None else ""
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        amount, paypay_link, error = payment_from_inputs(interaction.user, self.menu_key, paypay_raw)
        if error:
            await interaction.response.send_message(embed=notice_embed(error), ephemeral=True)
            return
        await create_ticket_and_forge(interaction, self.action, self.val, amount, paypay_link, login_id, password, self.menu_message)

class AccountInputModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="アカウント情報を入力")
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号",
            placeholder="メール・電話番号・引き継ぎ番号のいずれか",
            max_length=200,
        )
        self.password = discord.ui.TextInput(
            label="パスワード",
            placeholder="パスワード",
            max_length=200,
        )
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not login_id or not password:
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        clear_tsum_login_files()
        await interaction.response.send_message(
            "アカウント情報は保存しません。メニューを選んだ後の入力画面で、その都度入力してください。",
            ephemeral=True,
        )

class MenuSelect(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=label, value=key)
                for key, label in MENU_ACTIONS]
        super().__init__(placeholder="ご希望のメニューを選択", options=opts, custom_id="tsum_menu_select")
    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        menu_msg = interaction.message
        if key in MAX_PRESETS:
            action, val = MAX_PRESETS[key]
            factory = (lambda a=action, v=val, k=key, m=menu_msg:
                       AccountBeforeTicketModal(a, v, m, k))
        elif key == "tsum_lv":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("tsum_lv", 0, m, "tsum_lv")
        elif key == "box":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("box", DEFAULT_SELECT_BOX_ID, m, "box")
        elif key == "premium":
            factory = lambda m=menu_msg: AccountBeforeTicketModal("gacha_full", DEFAULT_PREMIUM_GACHA_ID, m, "premium")
        elif key in ("score", "coin", "exp"):
            factory = lambda k=key, m=menu_msg: ValueModal(k, m, k)
        elif key == "account":
            await interaction.response.send_modal(AccountInputModal())
            return
        elif key == "status":
            await interaction.response.defer(ephemeral=True)
            loop = asyncio.get_running_loop()
            txt = await loop.run_in_executor(None, status_text)
            await interaction.followup.send(txt, ephemeral=True)
            return
        else:
            return
        await interaction.response.send_modal(factory())

class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(MenuSelect())


FREE_DAIKOU_COIN = 1000000
FREE_DAIKOU_LIMIT_MSG = "無料代行は1ヶ月に1回までです。また来月ご利用ください。"
FREE_DAIKOU_ACCOUNT_LIMIT_MSG = "このツムツムのアカウントは今月すでに無料代行を利用しています。"


class FreeDaikouModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="無料代行（100万コイン）")
        self.login_id = discord.ui.TextInput(
            label="メール/電話/引き継ぎ番号", placeholder="メール・電話番号・引き継ぎ番号のいずれか", max_length=200)
        self.password = discord.ui.TextInput(label="パスワード", placeholder="パスワード", max_length=200)
        self.add_item(self.login_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        login_id = self.login_id.value.strip()
        password = self.password.value.strip()
        if not free_can_use(uid, login_id=login_id):
            _msg = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=login_id)
                    else FREE_DAIKOU_LIMIT_MSG)
            await interaction.response.send_message(embed=notice_embed(_msg), ephemeral=True)
            return
        free_mark_used(uid, login_id=login_id)
        if not login_id or not password:
            free_unmark_used(uid, login_id=login_id)
            await interaction.response.send_message(embed=notice_embed("アカウント情報を入力してください。"), ephemeral=True)
            return
        game_state = {"userid": "", "blocked": False}
        def _on_login(game_userid):
            game_state["userid"] = game_userid or ""
            if game_userid and not free_can_use(game_userid=game_userid):
                game_state["blocked"] = True
                raise RuntimeError("free_daikou_already_used_for_this_game_account")
            if game_userid:
                free_mark_used(game_userid=game_userid)
        ok = False
        try:
            ok = await create_ticket_and_forge(interaction, "coin", FREE_DAIKOU_COIN, 0, "", login_id, password,
                                               None, on_login=_on_login)
        finally:
            if not ok:
                if game_state["blocked"]:
                    free_mark_used(uid, login_id=login_id, game_userid=game_state["userid"])
                    try:
                        await interaction.followup.send(
                            embed=notice_embed(FREE_DAIKOU_ACCOUNT_LIMIT_MSG), ephemeral=True)
                    except Exception:
                        pass
                else:
                    free_unmark_used(uid, login_id=login_id, game_userid=game_state["userid"])


class FreeDaikouView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="無料代行をうける", style=discord.ButtonStyle.success, custom_id="tsum_free_daikou")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not free_can_use(user_id=interaction.user.id):
            await interaction.response.send_message(embed=notice_embed(FREE_DAIKOU_LIMIT_MSG), ephemeral=True)
            return
        await interaction.response.send_modal(FreeDaikouModal())


def free_daikou_embed():
    return discord.Embed(
        title="ツムツム無料代行",
        description=("1人1ヶ月に1回まで、**100万コイン**を無料で代行します。\n"
                     "下のボタンから、アカウント情報を入力してください。"),
        color=0x2ecc71)


async def create_ticket_and_forge_guest(interaction: discord.Interaction, action, val, amount,
                                         paypay_link, migration_id, password, menu_message=None):
    if interaction.guild is None:
        try:
            await interaction.response.send_message(
                embed=notice_embed("ご注文はサーバー内のチャンネルからお願いします。DMでは受け付けていません。"))
        except Exception:
            pass
        return False
    await interaction.response.defer(ephemeral=True)
    if amount > 0:
        async with paypay_lock:
            if _is_link_consumed(paypay_link):
                await interaction.followup.send(
                    embed=notice_embed("この支払いは既に処理済みです。届いていない場合はオーナーにご連絡ください。"), ephemeral=True)
                return False
            pres = await _run_paypay_helper_dict("verify_receive", paypay_link, str(int(amount)), PAYPAY_SHARED_FILE)
            if pres.get("ok"):
                _mark_link_consumed(paypay_link)
        if pres.get("held"):
            await interaction.followup.send(
                embed=_hold_embed(),
                view=TsumHoldApproveView(create_ticket_and_forge_guest, dict(
                    action=action, val=val, amount=amount, paypay_link=paypay_link,
                    migration_id=migration_id, password=password, menu_message=menu_message)),
                ephemeral=True)
            return False
        if not pres.get("ok"):
            await interaction.followup.send(
                embed=notice_embed(pres.get("msg") or "PayPayの受け取りに失敗しました。"), ephemeral=True)
            await reset_menu_message(menu_message)
            return False

    th = await open_ticket(interaction, ticket_thread_name(action, val))
    await interaction.followup.send(f"チケット作成: {ticket_link(th)}", ephemeral=True)
    await send_ticket_header(th, interaction.user, action, val, amount, paypay_link)
    await reset_menu_message(menu_message)
    okey = f"{interaction.id}"
    ACTIVE_ORDERS[okey] = {"user": str(interaction.user), "action": action, "val": val, "ts": int(time.time())}
    info = {"moved": False}
    try:
        ok, _text, info = await _do_forge_guest(th, interaction.user, migration_id, password, **{action: val})
    finally:
        ACTIVE_ORDERS.pop(okey, None)
    print(f"[注文/guest] {interaction.user} {action} {val} ¥{amount} → "
          f"{'成功' if ok else '失敗'} moved={info.get('moved')}", flush=True)
    if info.get("new_id"):
        try:
            await interaction.user.send(embed=discord.Embed(
                title="ツムツム代行 — 新しい引き継ぎ情報",
                description=(f"引き継ぎ番号: `{info['new_id']}`\nパスワード: `{info['new_pw']}`\n"
                             "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。"),
                color=0x2ecc71))
        except Exception:
            pass
    if ok:
        if amount > 0:
            record_sale(interaction.user.id, action, amount)
        await post_public_result(interaction.guild, interaction.user, action, val, amount)
    else:
        if not info.get("moved"):
            await send_guest_retry_prompt(th, interaction.user.id, action, val, amount)
    return ok


class GuestRetryModal(discord.ui.Modal):
    def __init__(self, owner_id, action, val, amount, prompt_message=None):
        super().__init__(title="引き継ぎ情報を再入力")
        self.owner_id = owner_id
        self.action = action
        self.val = val
        self.amount = amount
        self.prompt_message = prompt_message
        self.migration_id = discord.ui.TextInput(
            label="引き継ぎ番号", placeholder="ゲームで発行した引き継ぎ番号", max_length=100)
        self.password = discord.ui.TextInput(
            label="引き継ぎパスワード", placeholder="引き継ぎパスワード", max_length=100)
        self.add_item(self.migration_id)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        migration_id = self.migration_id.value.strip()
        password = self.password.value.strip()
        if not migration_id or not password:
            await interaction.response.send_message(
                embed=notice_embed("引き継ぎ番号とパスワードを入力してください。"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if self.prompt_message:
                await self.prompt_message.edit(view=None)
        except Exception:
            pass

        channel = interaction.channel
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            await interaction.followup.send("この場所では再実行できません。", ephemeral=True)
            return

        lock_key, ticket_lock = get_ticket_lock(channel.id)
        if ticket_lock.locked():
            await interaction.followup.send("このチケットは処理中です。完了までお待ちください。", ephemeral=True)
            return

        is_free = (self.amount == 0 and self.action == "coin" and int(self.val) == FREE_DAIKOU_COIN)
        if is_free:
            if not free_can_use(self.owner_id, login_id=migration_id):
                _m = (FREE_DAIKOU_ACCOUNT_LIMIT_MSG if not free_can_use(login_id=migration_id)
                      else FREE_DAIKOU_LIMIT_MSG)
                await interaction.followup.send(embed=notice_embed(_m), ephemeral=True)
                return
            free_mark_used(self.owner_id, login_id=migration_id)

        await interaction.followup.send("再実行します。", ephemeral=True)
        ok = False
        info = {"moved": False}
        try:
            async with ticket_lock:
                ok, _t, info = await _do_forge_guest(
                    channel, interaction.user, migration_id, password, **{self.action: self.val})
        finally:
            if is_free and not ok:
                free_unmark_used(self.owner_id, login_id=migration_id)
        if not ticket_lock.locked():
            ticket_locks.pop(lock_key, None)

        owner = ticket_owner_user(interaction.guild, self.owner_id, interaction.user)
        if ok:
            await post_public_result(interaction.guild, owner, self.action, self.val, self.amount)
        elif not info.get("moved"):
            await send_guest_retry_prompt(channel, self.owner_id, self.action, self.val, self.amount)


class GuestRetryButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r'tsum_guest_retry:(?P<owner>\d+):(?P<action>[A-Za-z_]+):(?P<val>-?\d+)(?::(?P<amount>-?\d+))?'):
    def __init__(self, owner_id, action, val, amount):
        self.owner_id = int(owner_id)
        self.action = str(action)
        self.val = int(val)
        self.amount = int(amount)
        super().__init__(
            discord.ui.Button(
                label="引き継ぎ情報を再入力",
                style=discord.ButtonStyle.primary,
                custom_id=f"tsum_guest_retry:{self.owner_id}:{self.action}:{self.val}:{self.amount}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        amt = match.group("amount")
        return cls(int(match["owner"]), match["action"], int(match["val"]), int(amt) if amt else 0)

    async def callback(self, interaction: discord.Interaction):
        if not can_operate_ticket(interaction, self.owner_id):
            await interaction.response.send_message(
                embed=notice_embed("このチケットの実行ユーザー、または管理者だけ操作できます。"), ephemeral=True)
            return
        await interaction.response.send_modal(
            GuestRetryModal(self.owner_id, self.action, self.val, self.amount, interaction.message))


async def send_guest_retry_prompt(channel, owner_id, action, val, amount):
    emb = discord.Embed(
        description="引き継ぎに失敗しました。引き継ぎ番号とパスワードを入力し直して、同じチケットで再実行できます。",
        color=0xf1c40f)
    view = discord.ui.View(timeout=None)
    try:
        view.add_item(GuestRetryButton(owner_id, action, val, amount))
    except Exception as _e:
        print(f"[ticket] guest retry button build failed: {_e}")
        return
    try:
        await channel.send(embed=emb, view=view)
    except (discord.NotFound, discord.Forbidden):
        print("[ticket] guest retry prompt skipped: channel unavailable")


@bot.event
async def on_ready():
    print(f"[bot] ログイン: {bot.user} (id={bot.user.id})")
    _ids = CONFIG.get("allowed_user_ids") or []
    print(f"[bot] /コマンド: manage_guild必須(Discord側で一般ユーザーには非表示) "
          f"＋実行時ゲート(allowed_user_ids={_ids or '未設定'} / 管理者権限)  "
          f"同期: {'guild即時' if GUILD_ID else '全体(最大1h)'}")

async def _ticket_forge(interaction: discord.Interaction, **kw):
    action, val = next(iter(kw.items()))
    await interaction.response.send_modal(AccountBeforeTicketModal(action, val))

def _menu_panel_embed():
    return discord.Embed(title="ツムツム代行料金表", description=menu_description(), color=0x2fd4ff)


@bot.tree.command(name="パネル設置", description="メニュー(注文パネル)を設置します")
async def slash_menu(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_menu_panel_embed(), view=MenuView())


@bot.tree.command(name="パネル再設置", description="メニューパネルを再設置します(特に意味なし)")
async def slash_menu_redisplay(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_menu_panel_embed(), view=MenuView())


@bot.tree.command(name="料金読込", description="bot_config.jsonの料金変更を読み込みます")
async def slash_menu_reload(interaction: discord.Interaction):
    global CONFIG
    try:
        CONFIG = load_config()
        MENU_PRICES.update(CONFIG.get("menu_prices", {}))
    except Exception as e:
        await interaction.response.send_message(embed=notice_embed(f"再読込に失敗しました: {e}"), ephemeral=True)
        return
    await interaction.response.send_message("料金を再読込しました。", ephemeral=True)


@bot.tree.command(name="料金設定", description="メニューの値段を変更します")
@app_commands.rename(menu="メニュー", price="料金")
@app_commands.describe(menu="料金を変更するメニュー", price="変更後の料金")
@app_commands.choices(menu=PRICE_MENU_CHOICES)
async def slash_set_price(interaction: discord.Interaction, menu: app_commands.Choice[str], price: int):
    if price < 0:
        await interaction.response.send_message(embed=notice_embed("料金は0円以上で入力してください。"), ephemeral=True)
        return
    key = menu.value
    MENU_PRICES[key] = price
    CONFIG.setdefault("menu_prices", {})[key] = price
    save_config()
    await interaction.response.send_message(f"{menu.name} を ¥{price:,} に変更しました。", ephemeral=True)


@bot.tree.command(name="無料代行", description="無料代行パネル(100万コイン・月1回)を設置します")
async def slash_free_daikou(interaction: discord.Interaction):
    await interaction.response.send_message(embed=free_daikou_embed(), view=FreeDaikouView())


@bot.tree.command(name="チケットカテゴリー", description="チケットを作成するカテゴリを設定します")
@app_commands.rename(category="カテゴリー")
@app_commands.describe(category="チケットを作るカテゴリー")
async def slash_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    CONFIG["ticket_category_id"] = str(category.id)
    save_config()
    await interaction.response.send_message(f"チケット作成カテゴリーを {category.mention} に設定しました。", ephemeral=True)


@bot.tree.command(name="チケットチャンネル設定", description="(予備)チケット用チャンネルを設定します")
@app_commands.rename(channel="チャンネル")
async def slash_ticket_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    CONFIG["ticket_channel_id"] = str(channel.id)
    save_config()
    await interaction.response.send_message(f"チケットチャンネルを {channel.mention} に設定しました。", ephemeral=True)


@bot.tree.command(name="実績チャンネル設定", description="代行完了の実績を投稿するチャンネルを設定します")
@app_commands.rename(channel="チャンネル")
@app_commands.describe(channel="実績を出すチャンネル。空欄で解除")
async def slash_public_result(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if channel is None:
        CONFIG.pop("public_result_channel_id", None)
        save_config()
        await interaction.response.send_message("実績の自動投稿を解除しました。", ephemeral=True)
        return
    CONFIG["public_result_channel_id"] = str(channel.id)
    save_config()
    await interaction.response.send_message(f"代行完了の実績を {channel.mention} に自動投稿します。", ephemeral=True)


@bot.tree.command(name="注文状況", description="現在処理中の注文を表示します")
async def slash_orders(interaction: discord.Interaction):
    if not ACTIVE_ORDERS:
        await interaction.response.send_message("現在処理中の注文はありません。", ephemeral=True)
        return
    lines = []
    for o in list(ACTIVE_ORDERS.values()):
        ago = int(time.time() - o.get("ts", 0))
        lines.append(f"・{o.get('user')} … {o.get('action')} {o.get('val')}（{ago}秒経過）")
    await interaction.response.send_message("**処理中の注文**\n" + "\n".join(lines), ephemeral=True)


_APPCMD_BACKUP = os.path.join(HERE, "appcmd_perm_backup.json")

@bot.tree.command(name="コマンド禁止",
                  description="全ロール/チャンネルから『アプリコマンドを使う』を剥奪します(他のbotのスラッシュも止まる)")
async def slash_appcmd_deny(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    if g is None:
        return await interaction.followup.send(embed=notice_embed("サーバー内で実行してください。"), ephemeral=True)
    me = g.me
    if not me.guild_permissions.manage_roles:
        return await interaction.followup.send(
            embed=notice_embed("botに『ロールの管理』権限がありません。付与してから実行してください。"), ephemeral=True)
    backup = {"roles": [], "channels": []}
    done_roles, done_ch, skipped = [], [], []
    for role in g.roles:
        try:
            if role.managed or role.permissions.administrator:
                continue
            if not role.permissions.use_application_commands:
                continue
            if role != g.default_role and role >= me.top_role:
                skipped.append(f"{role.name}(botより上位で編集不可)")
                continue
            p = discord.Permissions(role.permissions.value)
            p.update(use_application_commands=False)
            await role.edit(permissions=p, reason="コマンド禁止: アプリコマンドを剥奪")
            backup["roles"].append(role.id)
            done_roles.append(role.name)
        except Exception as e:
            skipped.append(f"{role.name}({type(e).__name__})")
    for ch in g.channels:
        try:
            for target, ow in list(ch.overwrites.items()):
                if ow.use_application_commands is True:
                    ow.update(use_application_commands=None)
                    await ch.set_permissions(target, overwrite=ow, reason="コマンド禁止: allow上書きを解除")
                    backup["channels"].append({"ch": ch.id, "target": target.id})
                    done_ch.append(f"#{ch.name}/{getattr(target, 'name', target.id)}")
        except Exception as e:
            skipped.append(f"#{getattr(ch, 'name', '?')}({type(e).__name__})")
    try:
        with open(_APPCMD_BACKUP, "w", encoding="utf-8") as _f:
            json.dump(backup, _f)
    except Exception:
        pass
    msg = ["**アプリコマンドを剥奪しました**（管理者は元々バイパスするので影響なし）",
           f"・ロール {len(done_roles)}件: {', '.join(done_roles) or 'なし'}",
           f"・チャンネルのallow上書き解除 {len(done_ch)}件: {', '.join(done_ch[:8]) or 'なし'}"]
    if skipped:
        msg.append(f"⚠️触れなかった: {', '.join(skipped[:8])}")
    msg.append("※元に戻すには `/コマンド許可`")
    await interaction.followup.send("\n".join(msg)[:1900], ephemeral=True)


@bot.tree.command(name="コマンド許可", description="/コマンド禁止 で剥奪した『アプリコマンドを使う』を元に戻します")
async def slash_appcmd_allow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    if g is None:
        return await interaction.followup.send(embed=notice_embed("サーバー内で実行してください。"), ephemeral=True)
    try:
        backup = json.load(open(_APPCMD_BACKUP, encoding="utf-8"))
    except Exception:
        return await interaction.followup.send(
            embed=notice_embed("復元データ(appcmd_perm_backup.json)がありません。"), ephemeral=True)
    done = []
    for rid in backup.get("roles", []):
        role = g.get_role(int(rid))
        if not role:
            continue
        try:
            p = discord.Permissions(role.permissions.value)
            p.update(use_application_commands=True)
            await role.edit(permissions=p, reason="コマンド許可: 復元")
            done.append(role.name)
        except Exception:
            pass
    for c in backup.get("channels", []):
        try:
            ch = g.get_channel(int(c["ch"]))
            target = g.get_role(int(c["target"])) or g.get_member(int(c["target"]))
            if ch and target:
                ow = ch.overwrites_for(target)
                ow.update(use_application_commands=True)
                await ch.set_permissions(target, overwrite=ow, reason="コマンド許可: 復元")
        except Exception:
            pass
    await interaction.followup.send(f"復元しました（ロール{len(done)}件: {', '.join(done) or 'なし'}）"[:1900],
                                    ephemeral=True)


@bot.tree.command(name="無料無制限", description="指定した人の『無料代行は月1回』制限を解除/解除取消/一覧します")
@app_commands.describe(操作="追加=制限解除する / 解除=元に戻す / 一覧=今の登録者を見る",
                       ユーザー="対象のユーザー(一覧のときは不要)")
@app_commands.choices(操作=[
    app_commands.Choice(name="追加(制限を解除する)", value="add"),
    app_commands.Choice(name="解除(月1回に戻す)", value="remove"),
    app_commands.Choice(name="一覧", value="list"),
])
async def slash_free_unlimited(interaction: discord.Interaction,
                               操作: app_commands.Choice[str],
                               ユーザー: discord.User = None):
    global CONFIG
    if not free_unlimited_enabled():
        return await interaction.response.send_message(
            embed=notice_embed("このbotでは無制限ユーザー機能は使用できません。"), ephemeral=True)
    ids = [int(x) for x in (CONFIG.get("free_unlimited_ids") or [])]
    op = 操作.value
    if op == "list":
        if not ids:
            msg = "無制限に設定されている人はいません。"
        else:
            msg = "**無料代行が無制限の人**\n" + "\n".join(f"・<@{i}>（{i}）" for i in ids)
        return await interaction.response.send_message(msg, ephemeral=True)
    if ユーザー is None:
        return await interaction.response.send_message(
            embed=notice_embed("対象のユーザーを指定してください。"), ephemeral=True)
    uid = int(ユーザー.id)
    if op == "add":
        if uid in ids:
            return await interaction.response.send_message(
                f"<@{uid}> はすでに無制限です。", ephemeral=True)
        ids.append(uid)
        CONFIG["free_unlimited_ids"] = ids
        save_config()
        return await interaction.response.send_message(
            f"<@{uid}> の無料代行の月1回制限を解除しました（何回でも利用できます）。", ephemeral=True)
    if uid not in ids:
        return await interaction.response.send_message(
            f"<@{uid}> は無制限に登録されていません。", ephemeral=True)
    ids = [i for i in ids if i != uid]
    CONFIG["free_unlimited_ids"] = ids
    save_config()
    await interaction.response.send_message(
        f"<@{uid}> を通常（月1回まで）に戻しました。", ephemeral=True)


@bot.tree.command(name="paypayログイン", description="このbotの受取PayPay口座にログインします(SMSにOTPが届きます)")
@app_commands.describe(phone="PayPayの電話番号(例 07012345678)", password="PayPayのパスワード")
async def slash_paypay_login(interaction: discord.Interaction, phone: str, password: str):
    await interaction.response.defer(ephemeral=True)
    out = await _run_pp_script(PP_RELOGIN_START, PAYPAY_SHARED_FILE, phone, password)
    if "DIRECT_OK" in out:
        msg = ("ログイン完了しました（OTP不要でトークン取得）。\n"
               f"このbotの受取口座: {phone}\nこのまま受け取りに使えます。")
    elif "OTP_SENT" in out:
        prefix = ""
        for line in out.splitlines():
            if "otp_prefix" in line:
                prefix = line.split(":", 1)[-1].strip()
        msg = ("SMSにOTPコードを送信しました。\n"
               + (f"OTPの接頭辞: `{prefix}`\n" if prefix else "")
               + "届いた数字を `/paypayotp` で入力してください。")
    elif "LOGIN_ERROR" in out:
        msg = "電話番号かパスワードが違う可能性があります。確認してもう一度お試しください。"
    else:
        msg = f"ログイン開始に失敗しました。\n```{out[:600]}```"
    await interaction.followup.send(embed=notice_embed(msg, color=0x3498db), ephemeral=True)


@bot.tree.command(name="paypayotp", description="paypayログインの後、SMSで届いたOTPコードを入力して完了します")
@app_commands.describe(code="SMSで届いたOTPコード(数字)")
async def slash_paypay_otp(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    out = await _run_pp_script(PP_RELOGIN_OTP, code.strip(), PAYPAY_SHARED_FILE)
    if "RELOGIN_OK" in out:
        msg = ("PayPayログインが完了しました。\n"
               f"保存先: `{os.path.basename(PAYPAY_SHARED_FILE)}`\n"
               "以降の注文の受け取りは、この口座で行われます。")
    elif "OTP_FAIL" in out:
        msg = "OTPコードが違うか期限切れです。`/paypayログイン` からやり直してください。"
    else:
        msg = f"OTP確定に失敗しました。\n```{out[:600]}```"
    await interaction.followup.send(embed=notice_embed(msg, color=0x2ecc71), ephemeral=True)


@bot.tree.command(name="paypay状態", description="PayPay受取アカウントの安全な状態を確認します")
async def slash_paypay_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not os.path.exists(PAYPAY_SHARED_FILE):
        return await interaction.followup.send(
            embed=notice_embed("PayPayアカウントは未設定です。", color=0xe67e22),
            ephemeral=True,
        )
    try:
        with open(PAYPAY_SHARED_FILE, encoding="utf-8") as fp:
            data = json.load(fp) or {}
    except Exception:
        return await interaction.followup.send(
            embed=notice_embed("PayPay設定ファイルを読み込めませんでした。", color=0xe74c3c),
            ephemeral=True,
        )

    phone = str(data.get("phone") or "")
    masked_phone = ("*" * max(0, len(phone) - 4) + phone[-4:]) if phone else "(未設定)"
    has_access = bool(data.get("access_token"))
    has_refresh = bool(data.get("refresh_token"))
    await interaction.followup.send(
        embed=notice_embed(
            "**PayPay状態**\n"
            f"アカウント: `{masked_phone}`\n"
            f"アクセストークン: {'保存済み' if has_access else 'なし'}\n"
            f"更新トークン: {'保存済み' if has_refresh else 'なし'}\n"
            f"設定ファイル: `{os.path.basename(PAYPAY_SHARED_FILE)}`\n"
            "※トークン本体は表示していません。",
            color=0x3498db,
        ),
        ephemeral=True,
    )


class PayPayLogoutView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id

    @discord.ui.button(label="ログアウトして削除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "この確認ボタンを操作できるのはコマンド実行者だけです。",
                ephemeral=True,
            )
        try:
            if os.path.exists(PAYPAY_SHARED_FILE):
                os.remove(PAYPAY_SHARED_FILE)
                msg = "PayPayの保存済み認証情報を削除しました。"
            else:
                msg = "PayPayの保存済み認証情報はありませんでした。"
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                embed=notice_embed(msg, color=0x2ecc71), view=self
            )
        except OSError:
            await interaction.response.send_message(
                "PayPay設定ファイルを削除できませんでした。",
                ephemeral=True,
            )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "この確認ボタンを操作できるのはコマンド実行者だけです。",
                ephemeral=True,
            )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=notice_embed("PayPayログアウトをキャンセルしました。", color=0x95a5a6),
            view=self,
        )


@bot.tree.command(name="paypayログアウト", description="保存済みPayPay認証情報を削除します")
async def slash_paypay_logout(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=notice_embed(
            "保存済みのPayPay認証情報を削除します。\n"
            "削除後は、受け取り処理の前に再ログインが必要です。\n"
            "実行する場合は下のボタンを押してください。",
            color=0xe74c3c,
        ),
        view=PayPayLogoutView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="収益", description="今日/今週/今月の収益を確認します")
async def slash_sales(interaction: discord.Interaction):
    s = sales_summary()
    def line(label, t):
        amt, n = t
        return f"{label}: ¥{amt:,}（{n}件）"
    msg = ("**収益**\n" + line("今日", s["today"]) + "\n" + line("今週(7日)", s["week"]) + "\n"
           + line("今月(30日)", s["month"]) + "\n" + line("累計", s["total"]))
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="収益リセット", description="収益記録をリセットします")
async def slash_sales_reset(interaction: discord.Interaction):
    reset_sales()
    await interaction.response.send_message("収益記録をリセットしました。", ephemeral=True)


@bot.tree.command(name="状態", description="状態(IP/アカウント/トークン/CAPTCHA)を確認します")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    txt = await loop.run_in_executor(None, status_text)
    await interaction.followup.send(txt, ephemeral=True)


@bot.tree.command(name="ツムログイン確認", description="ツムツムへログインして安全なデバッグ情報を確認します")
async def slash_tsum_login_debug(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    txt = await loop.run_in_executor(None, tsum_login_debug_text)
    try:
        await interaction.user.send(txt)
        await interaction.followup.send(
            "確認結果をあなたのDMへ送信しました。", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "DMを送信できませんでした。サーバーのメンバー一覧からBotとのDMを許可してから、もう一度実行してください。",
            ephemeral=True,
        )


if __name__ == "__main__":
    bot.run(CONFIG["token"])
