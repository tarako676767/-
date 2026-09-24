import uuid as _uuid, secrets, json, time
import requests, msgpack
import tsum_login as L


def _auth_guest(uuid_str, proxies=None):
    p = msgpack.packb({"uuid": uuid_str, "termsResult": L.TERMS_RESULT, "country": "JP"}, use_bin_type=False)
    try:
        r = requests.post(L.AUTH_BASE + "/auth/v3.8/authentication/GUEST", data=p,
                          headers=L.auth_headers(p), proxies=proxies, timeout=20)
        return r.json()
    except Exception:
        return {}


def _common(userid="", hsh="", checkval=""):
    return [("userid", userid), ("appver", L.APPVER), ("resver", L.RESVER), ("mstver", L.MSTVER),
            ("hash", hsh), ("os", "2"), ("lang", "ja"), ("checkval", checkval),
            ("countrycode", "JP"), ("growthyinfo", L.growthyinfo())]


def _game_post(path, pairs, proxies=None):
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "tsumtsum/%s" % L.APPVER})
        if proxies:
            s.proxies.update(proxies)
        key = secrets.token_bytes(32)
        j = s.get(L.GAME_BASE + "api/getPublicKey.nhn?appver=" + L.APPVER, timeout=15).json()
        pub = j.get("public_key") or j.get("publicKey")
        kid = str(j.get("kid", ""))
        enc = L.aes_gcm_encrypt(L.build_form(pairs), key)
        hdr = {"Content-Type": "application/tsum", "X-Encrypted-Session-Key": L.rsa_oaep(key, pub), "X-Key-Id": kid}
        r = s.post(L.GAME_BASE + path, data=enc, headers=hdr, timeout=20)
        return json.loads(L.aes_gcm_decrypt(r.content, key))
    except Exception:
        return {}


def _login(ut, proxies=None):
    lg = _game_post("login.nhn", [("rankdt", 0), ("accesstoken", ut), ("trigger", 0)] + _common(), proxies)
    ui = lg.get("userinfo") or {}
    if lg.get("retcode") != 0 or not ui.get("checkval"):
        return None
    return {"userToken": ut, "userid": ui.get("userid"), "hash": lg.get("hash"), "checkval": ui.get("checkval")}


def guest_create(proxies=None):
    u = str(_uuid.uuid4())
    g = _auth_guest(u, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "ゲスト認証に失敗しました"
    sess = _login(ut, proxies)
    if not sess:
        return None, "ゲスト登録(login)に失敗しました"
    sess["uuid"] = u
    return sess, None


def guest_takeover(migration_id, password, proxies=None):
    u = str(_uuid.uuid4())
    g = _auth_guest(u, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "ゲスト認証に失敗しました"
    mig = _game_post("migration.nhn",
                     [("accesstoken", ut), ("id", str(migration_id)), ("password", str(password))] + _common(), proxies)
    if not (isinstance(mig, dict) and mig.get("session_id")):
        rm = mig.get("retmsg") if isinstance(mig, dict) else ""
        return None, "引き継ぎに失敗しました(IDかパスワードが違う/期限切れ)" + (("\n" + rm) if rm else "")
    sess = None
    for _try in range(3):
        g2 = _auth_guest(u, proxies)
        ut2 = g2.get("userToken") or ut
        sess = _login(ut2, proxies)
        if sess:
            ut = ut2
            break
        time.sleep(0.5)
    if not sess:
        sess = {"userToken": ut, "userid": str(mig.get("userid") or ""), "hash": "", "checkval": ""}
    sess["uuid"] = u
    sess["migrated_userKey"] = mig.get("userKey")
    sess["migrated"] = True
    return sess, None


def guest_recover(uuid_str, proxies=None):
    g = _auth_guest(uuid_str, proxies)
    ut = g.get("userToken")
    if not ut:
        return None, "再認証に失敗しました"
    sess = _login(ut, proxies)
    if not sess:
        return None, "loginに失敗しました"
    sess["uuid"] = uuid_str
    mid, pw = guest_issue_transfer(sess, proxies)
    if mid:
        return (mid, pw), None
    return None, "引き継ぎ発行に失敗しました: " + str(pw)


def guest_issue_transfer(sess, proxies=None, migration_id=None):
    mid = migration_id or "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
    r = _game_post("migrationId.nhn",
                   [("accesstoken", sess["userToken"]), ("id", mid)]
                   + _common(sess["userid"], sess.get("hash", ""), sess["checkval"]), proxies)
    if isinstance(r, dict) and r.get("password"):
        return mid, r.get("password")
    return None, (r.get("retmsg") if isinstance(r, dict) else str(r))[:160]
